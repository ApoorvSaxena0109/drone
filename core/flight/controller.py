"""MAVLink flight controller interface.

Handles connection to ArduPilot/PX4 via pymavlink. Provides high-level
commands (arm, takeoff, goto, land, RTL) and continuous telemetry updates.

This module talks to the flight controller only — all intelligence
(mission logic, AI decisions) lives in the apps layer above.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Optional

from pymavlink import mavutil

from core.flight.telemetry import TelemetryState, TelemetryStore

logger = logging.getLogger(__name__)

# ArduPilot mode mappings (copter)
COPTER_MODES = {
    "STABILIZE": 0, "ACRO": 1, "ALT_HOLD": 2, "AUTO": 3,
    "GUIDED": 4, "LOITER": 5, "RTL": 6, "CIRCLE": 7,
    "LAND": 9, "DRIFT": 11, "SPORT": 13, "BRAKE": 17,
    "SMART_RTL": 21, "GUIDED_NOGPS": 20,
}


class FlightController:
    """Interface to ArduPilot/PX4 flight controller via MAVLink."""

    def __init__(
        self,
        connection_string: str = "udp:127.0.0.1:14550",
        baud_rate: int = 57600,
        heartbeat_timeout: float = 5.0,
    ):
        self._conn_str = connection_string
        self._baud = baud_rate
        self._heartbeat_timeout = heartbeat_timeout
        self._mav: Optional[mavutil.mavlink_connection] = None
        self._telemetry = TelemetryStore()
        self._running = False
        self._connected = False

    @property
    def telemetry(self) -> TelemetryState:
        return self._telemetry.state

    @property
    def location(self) -> tuple[float, float, float]:
        return self._telemetry.location

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Establish MAVLink connection to flight controller."""
        logger.info("Connecting to flight controller: %s", self._conn_str)
        try:
            self._mav = mavutil.mavlink_connection(
                self._conn_str,
                baud=self._baud,
                autoreconnect=True,
            )
            # Wait for first heartbeat
            msg = self._mav.wait_heartbeat(timeout=self._heartbeat_timeout)
            if msg is None:
                logger.error("No heartbeat received within %.1fs", self._heartbeat_timeout)
                return False

            self._connected = True
            self._telemetry.update(connected=True)
            logger.info(
                "Connected. System %d, Component %d",
                self._mav.target_system,
                self._mav.target_component,
            )

            # Request data streams
            self._request_data_streams()
            return True
        except Exception as e:
            logger.error("Connection failed: %s", e)
            return False

    def _request_data_streams(self) -> None:
        """Request telemetry streams from flight controller."""
        if not self._mav:
            return
        # Request all streams at 4Hz (good balance of data vs bandwidth)
        self._mav.mav.request_data_stream_send(
            self._mav.target_system,
            self._mav.target_component,
            mavutil.mavlink.MAV_DATA_STREAM_ALL,
            4,  # Hz
            1,  # start
        )

    def update_telemetry(self) -> None:
        """Read and process pending MAVLink messages.

        Call this in a loop or on a timer. Non-blocking — processes
        whatever messages are available right now.
        """
        if not self._mav:
            return

        while True:
            msg = self._mav.recv_match(blocking=False)
            if msg is None:
                break

            msg_type = msg.get_type()

            if msg_type == "HEARTBEAT":
                mode_num = msg.custom_mode
                mode_name = ""
                for name, num in COPTER_MODES.items():
                    if num == mode_num:
                        mode_name = name
                        break
                self._telemetry.update(
                    armed=bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED),
                    mode=mode_name,
                    connected=True,
                    last_heartbeat=time.strftime("%H:%M:%S"),
                )

            elif msg_type == "GLOBAL_POSITION_INT":
                self._telemetry.update(
                    lat=msg.lat / 1e7,
                    lon=msg.lon / 1e7,
                    alt_msl=msg.alt / 1000.0,
                    alt_rel=msg.relative_alt / 1000.0,
                    vx=msg.vx / 100.0,
                    vy=msg.vy / 100.0,
                    vz=msg.vz / 100.0,
                    yaw=msg.hdg / 100.0,
                )

            elif msg_type == "GPS_RAW_INT":
                self._telemetry.update(
                    gps_fix=msg.fix_type,
                    gps_satellites=msg.satellites_visible,
                )

            elif msg_type == "SYS_STATUS":
                battery_pct = msg.battery_remaining if msg.battery_remaining >= 0 else -1
                self._telemetry.update(
                    battery_pct=battery_pct,
                    battery_voltage=msg.voltage_battery / 1000.0,
                )

            elif msg_type == "ATTITUDE":
                self._telemetry.update(
                    roll=math.degrees(msg.roll),
                    pitch=math.degrees(msg.pitch),
                    yaw=math.degrees(msg.yaw),
                )

            elif msg_type == "VFR_HUD":
                self._telemetry.update(groundspeed=msg.groundspeed)

    # ── Flight Commands ───────────────────────────────────────

    def arm(self) -> bool:
        """Arm the drone motors."""
        if not self._mav:
            return False
        self._mav.mav.command_long_send(
            self._mav.target_system,
            self._mav.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0, 1, 0, 0, 0, 0, 0, 0,
        )
        return self._wait_for_ack(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM)

    def disarm(self) -> bool:
        """Disarm the drone motors."""
        if not self._mav:
            return False
        self._mav.mav.command_long_send(
            self._mav.target_system,
            self._mav.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0, 0, 0, 0, 0, 0, 0, 0,
        )
        return self._wait_for_ack(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM)

    def set_mode(self, mode: str) -> bool:
        """Set flight mode by name (e.g., 'GUIDED', 'RTL', 'LAND')."""
        if not self._mav:
            return False
        mode_upper = mode.upper()
        mode_id = COPTER_MODES.get(mode_upper)
        if mode_id is None:
            logger.error("Unknown mode: %s", mode)
            return False
        self._mav.set_mode(mode_id)
        # Verify mode change
        for _ in range(10):
            self.update_telemetry()
            if self._telemetry.state.mode == mode_upper:
                return True
            time.sleep(0.2)
        logger.warning("Mode change to %s may not have completed", mode_upper)
        return False

    def takeoff(self, altitude_m: float = 10.0) -> bool:
        """Takeoff to specified altitude. Drone must be armed and in GUIDED mode."""
        if not self._mav:
            return False
        self._mav.mav.command_long_send(
            self._mav.target_system,
            self._mav.target_component,
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            0, 0, 0, 0, 0, 0, 0, altitude_m,
        )
        logger.info("Takeoff command sent: %.1fm", altitude_m)
        return self._wait_for_ack(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF)

    def goto(self, lat: float, lon: float, alt: float) -> None:
        """Fly to a GPS coordinate (GUIDED mode).

        Args:
            lat: Latitude in degrees.
            lon: Longitude in degrees.
            alt: Altitude in meters (relative to home).
        """
        if not self._mav:
            return
        self._mav.mav.set_position_target_global_int_send(
            0,  # time_boot_ms (not used)
            self._mav.target_system,
            self._mav.target_component,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            0b0000111111111000,  # type_mask: only position
            int(lat * 1e7),
            int(lon * 1e7),
            alt,
            0, 0, 0,  # velocity (ignored)
            0, 0, 0,  # acceleration (ignored)
            0, 0,     # yaw, yaw_rate (ignored)
        )
        logger.debug("Goto: %.7f, %.7f, %.1fm", lat, lon, alt)

    def set_speed(self, speed_ms: float) -> bool:
        """Set the target groundspeed."""
        if not self._mav:
            return False
        self._mav.mav.command_long_send(
            self._mav.target_system,
            self._mav.target_component,
            mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
            0,
            0,          # speed type: groundspeed
            speed_ms,
            -1,         # throttle (no change)
            0, 0, 0, 0,
        )
        return True

    def land(self) -> bool:
        """Switch to LAND mode."""
        return self.set_mode("LAND")

    def rtl(self) -> bool:
        """Return to launch."""
        return self.set_mode("RTL")

    def reached_waypoint(self, lat: float, lon: float, tolerance_m: float = 2.0) -> bool:
        """Check if drone is within tolerance of a waypoint."""
        current = self._telemetry.state
        dist = self._haversine(current.lat, current.lon, lat, lon)
        return dist <= tolerance_m

    # ── Internal ──────────────────────────────────────────────

    def _wait_for_ack(self, command_id: int, timeout: float = 5.0) -> bool:
        """Wait for COMMAND_ACK for a specific command."""
        if not self._mav:
            return False
        start = time.time()
        while time.time() - start < timeout:
            msg = self._mav.recv_match(type="COMMAND_ACK", blocking=True, timeout=1)
            if msg and msg.command == command_id:
                if msg.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    return True
                else:
                    logger.warning("Command %d rejected: result=%d", command_id, msg.result)
                    return False
        logger.warning("Timeout waiting for ACK on command %d", command_id)
        return False

    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Distance between two GPS points in meters."""
        R = 6371000  # Earth radius in meters
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        )
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def disconnect(self) -> None:
        """Close MAVLink connection."""
        if self._mav:
            self._mav.close()
            self._connected = False
            self._telemetry.update(connected=False)
            logger.info("Disconnected from flight controller")

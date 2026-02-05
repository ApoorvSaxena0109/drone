"""Autonomous patrol mission for surveillance.

Flies a series of waypoints while running the detection pipeline.
On detection, can loiter (hover in place) for closer inspection.

This is the main mission loop that ties together:
- Flight control (navigate waypoints)
- Vision (capture and detect)
- Alerts (sign and publish findings)
- Audit (log all actions)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from core.comms.mqtt_client import MQTTClient
from core.data.models import Mission, MissionStatus
from core.data.store import DataStore
from core.flight.controller import FlightController
from core.security.audit import AuditLogger
from core.security.crypto import CryptoEngine
from core.vision.camera import Camera
from core.vision.detector import Detector

from apps.surveillance.alerts import AlertManager

logger = logging.getLogger(__name__)


class PatrolMission:
    """Executes an autonomous patrol with detection.

    Lifecycle:
        1. preflight_check() — verify systems ready
        2. start() — arm, takeoff, begin patrol loop
        3. (runs autonomously until complete or abort)
        4. complete() / abort() — land and finalize
    """

    def __init__(
        self,
        mission: Mission,
        flight: FlightController,
        camera: Camera,
        detector: Detector,
        store: DataStore,
        crypto: CryptoEngine,
        audit: AuditLogger,
        mqtt_client: Optional[MQTTClient] = None,
        config: Optional[dict] = None,
    ):
        self._mission = mission
        self._flight = flight
        self._camera = camera
        self._detector = detector
        self._store = store
        self._crypto = crypto
        self._audit = audit
        self._mqtt = mqtt_client
        self._config = config or {}

        self._alert_mgr = AlertManager(
            store=store,
            crypto=crypto,
            audit=audit,
            mqtt_client=mqtt_client,
            mission_id=mission.id,
            detections_dir=self._config.get("detections_dir", "/var/drone/detections"),
            cooldown_s=self._config.get("alert_cooldown_s", 30.0),
        )

        self._current_wp_index = 0
        self._running = False
        self._paused = False
        self._total_findings = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_waypoint_index(self) -> int:
        return self._current_wp_index

    @property
    def total_findings(self) -> int:
        return self._total_findings

    def preflight_check(self) -> tuple[bool, list[str]]:
        """Verify all systems are ready for patrol.

        Returns (ready, issues). If not ready, issues list explains why.
        """
        issues = []

        # Flight controller connected?
        if not self._flight.is_connected:
            issues.append("Flight controller not connected")

        # Camera working?
        if not self._camera.is_open:
            if not self._camera.open():
                issues.append("Camera failed to open")

        # Detector loaded?
        if self._detector.backend == "none":
            issues.append("Detection model not loaded")

        # Waypoints defined?
        if not self._mission.waypoints:
            issues.append("No waypoints defined in mission")

        # Battery check
        telem = self._flight.telemetry
        if telem.battery_pct >= 0 and telem.battery_pct < 30:
            issues.append(f"Battery low: {telem.battery_pct}%")

        # GPS check
        if telem.gps_fix < 3:
            issues.append(f"GPS fix insufficient: {telem.gps_fix} (need 3D)")

        ready = len(issues) == 0
        return ready, issues

    def start(self) -> bool:
        """Start the patrol mission.

        Arms the drone, takes off, and begins the patrol loop.
        Returns False if preflight fails.
        """
        ready, issues = self.preflight_check()
        if not ready:
            for issue in issues:
                logger.error("Preflight: %s", issue)
            return False

        self._audit.log("mission_start", {
            "mission_id": self._mission.id,
            "waypoints": len(self._mission.waypoints),
        })

        # Update mission status
        self._mission.status = MissionStatus.ACTIVE
        self._store.save_mission(self._mission)

        altitude = self._mission.parameters.get("altitude_m", 30.0)
        speed = self._mission.parameters.get("speed_ms", 5.0)

        # Set GUIDED mode, arm, takeoff
        logger.info("Setting GUIDED mode...")
        if not self._flight.set_mode("GUIDED"):
            logger.error("Failed to set GUIDED mode")
            return False

        logger.info("Arming...")
        if not self._flight.arm():
            logger.error("Failed to arm")
            return False

        logger.info("Taking off to %.1fm...", altitude)
        if not self._flight.takeoff(altitude):
            logger.error("Takeoff command failed")
            return False

        # Wait for takeoff
        logger.info("Waiting for altitude...")
        self._wait_for_altitude(altitude * 0.9, timeout=30)

        # Set cruise speed
        self._flight.set_speed(speed)

        # Start camera capture
        self._camera.start()

        # Begin patrol
        self._running = True
        logger.info("Patrol started with %d waypoints", len(self._mission.waypoints))

        self._run_patrol_loop()
        return True

    def _run_patrol_loop(self) -> None:
        """Main patrol loop: navigate waypoints, detect, alert."""
        waypoints = self._mission.waypoints
        loop_patrol = self._mission.parameters.get("loop", True)
        hover_time = self._config.get("waypoint_hover_s", 5)
        loiter_time = self._config.get("detection_loiter_s", 10)
        altitude = self._mission.parameters.get("altitude_m", 30.0)
        rtl_battery = self._config.get("rtl_battery_pct", 25)

        while self._running:
            for i, wp in enumerate(waypoints):
                if not self._running:
                    break

                self._current_wp_index = i
                lat, lon = wp["lat"], wp["lon"]
                wp_alt = wp.get("alt", altitude)

                logger.info("Navigating to waypoint %d: %.6f, %.6f", i, lat, lon)
                self._audit.log("waypoint_navigate", {
                    "waypoint_index": i,
                    "target": [lat, lon, wp_alt],
                })

                self._flight.goto(lat, lon, wp_alt)

                # Fly to waypoint while running detection
                while self._running and not self._flight.reached_waypoint(lat, lon):
                    self._flight.update_telemetry()
                    self._process_frame()
                    self._check_battery(rtl_battery)

                    if self._paused:
                        self._handle_pause()

                    time.sleep(0.1)

                if not self._running:
                    break

                # Hover at waypoint
                logger.debug("Reached waypoint %d, hovering %.1fs", i, hover_time)
                hover_end = time.time() + hover_time
                while self._running and time.time() < hover_end:
                    self._flight.update_telemetry()
                    detections_found = self._process_frame()

                    # If we detect something, loiter longer
                    if detections_found:
                        logger.info("Detection at waypoint %d, loitering %.1fs", i, loiter_time)
                        loiter_end = time.time() + loiter_time
                        while self._running and time.time() < loiter_end:
                            self._flight.update_telemetry()
                            self._process_frame()
                            time.sleep(0.1)

                    time.sleep(0.1)

            # Completed one loop
            if not loop_patrol:
                logger.info("Patrol complete (single pass)")
                self._running = False
                break
            else:
                logger.info("Patrol loop complete, restarting...")
                self._audit.log("patrol_loop_complete", {
                    "findings_total": self._total_findings,
                })

        self.complete()

    def _process_frame(self) -> bool:
        """Capture frame, run detection, process alerts.

        Returns True if any detections triggered alerts.
        """
        ok, frame, frame_id = self._camera.read()
        if not ok or frame is None:
            return False

        detections = self._detector.detect(frame)
        if not detections:
            return False

        lat, lon, alt = self._flight.location
        findings = self._alert_mgr.process_detections(
            detections, frame, lat, lon, alt
        )
        self._total_findings += len(findings)
        return len(findings) > 0

    def _check_battery(self, rtl_threshold: int) -> None:
        """Trigger RTL if battery is critically low."""
        telem = self._flight.telemetry
        if 0 <= telem.battery_pct < rtl_threshold:
            logger.warning(
                "Battery critical: %d%% < %d%%, initiating RTL",
                telem.battery_pct, rtl_threshold,
            )
            self._audit.log("battery_rtl", {
                "battery_pct": telem.battery_pct,
                "threshold": rtl_threshold,
            })
            self._running = False
            self._flight.rtl()

    def _handle_pause(self) -> None:
        """Handle mission pause — loiter in place."""
        self._flight.set_mode("LOITER")
        self._audit.log("mission_paused", {})
        while self._paused and self._running:
            self._flight.update_telemetry()
            time.sleep(0.5)
        if self._running:
            self._flight.set_mode("GUIDED")
            self._audit.log("mission_resumed", {})

    def _wait_for_altitude(self, target_alt: float, timeout: float = 30) -> bool:
        """Wait until drone reaches target altitude."""
        start = time.time()
        while time.time() - start < timeout:
            self._flight.update_telemetry()
            telem = self._flight.telemetry
            if telem.alt_rel >= target_alt:
                return True
            time.sleep(0.5)
        logger.warning("Altitude timeout: wanted %.1fm, at %.1fm", target_alt, telem.alt_rel)
        return False

    def pause(self) -> None:
        """Pause the patrol (loiter in place)."""
        self._paused = True
        self._mission.status = MissionStatus.PAUSED
        self._store.save_mission(self._mission)

    def resume(self) -> None:
        """Resume a paused patrol."""
        self._paused = False
        self._mission.status = MissionStatus.ACTIVE
        self._store.save_mission(self._mission)

    def abort(self) -> None:
        """Abort the mission and return to launch."""
        logger.warning("Mission ABORTED")
        self._running = False
        self._audit.log("mission_abort", {
            "findings_total": self._total_findings,
            "last_waypoint": self._current_wp_index,
        })
        self._mission.status = MissionStatus.ABORTED
        self._store.save_mission(self._mission)
        self._flight.rtl()
        self._camera.stop()

    def complete(self) -> None:
        """Complete the mission — land and finalize."""
        logger.info(
            "Mission complete. Total findings: %d", self._total_findings
        )
        self._running = False
        self._audit.log("mission_complete", {
            "mission_id": self._mission.id,
            "findings_total": self._total_findings,
        })
        self._mission.status = MissionStatus.COMPLETED
        self._store.save_mission(self._mission)
        self._flight.land()
        self._camera.stop()

        # Publish completion status
        if self._mqtt and self._mqtt.is_connected:
            self._mqtt.publish_status({
                "mission_id": self._mission.id,
                "status": "completed",
                "findings_total": self._total_findings,
            })

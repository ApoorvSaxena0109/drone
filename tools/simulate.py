"""Simulation helper — run the platform with ArduPilot SITL.

Sets up a simulated environment for testing without real hardware:
- Uses SITL (Software In The Loop) for the flight controller
- Uses a video file or generated frames for the camera
- Uses a local MQTT broker (or skips if unavailable)

Usage:
    python tools/simulate.py --video test_video.mp4
    python tools/simulate.py --generated   # Use generated test frames
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.models import Mission
from core.data.store import DataStore
from core.flight.controller import FlightController
from core.security.identity import DroneIdentity
from core.security.crypto import CryptoEngine
from core.security.audit import AuditLogger
from core.vision.camera import Camera
from core.vision.detector import Detector
from core.comms.mqtt_client import MQTTClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulate")


# Default SITL waypoints (around SITL default home: -35.363261, 149.165230)
DEFAULT_WAYPOINTS = [
    {"lat": -35.36326, "lon": 149.16523, "alt": 30},
    {"lat": -35.36280, "lon": 149.16580, "alt": 30},
    {"lat": -35.36240, "lon": 149.16523, "alt": 30},
    {"lat": -35.36280, "lon": 149.16470, "alt": 30},
]


def setup_temp_identity() -> DroneIdentity:
    """Create a temporary identity for simulation."""
    tmp_dir = tempfile.mkdtemp(prefix="drone_sim_identity_")
    identity = DroneIdentity(identity_dir=tmp_dir)
    if not identity.is_provisioned:
        result = identity.provision(org_id="simulation")
        logger.info("Simulated drone ID: %s", result["drone_id"])
    return identity


def run_simulation(
    connection: str = "udp:127.0.0.1:14550",
    video_source=0,
    waypoints: list[dict] = None,
    mqtt_broker: str = None,
):
    """Run a simulated patrol mission.

    Prerequisites:
        - ArduPilot SITL running (sim_vehicle.py -v ArduCopter)
        - Optionally: MQTT broker running (mosquitto)
    """
    if waypoints is None:
        waypoints = DEFAULT_WAYPOINTS

    logger.info("=== DRONE PLATFORM SIMULATOR ===")
    logger.info("Flight controller: %s", connection)
    logger.info("Camera source: %s", video_source)
    logger.info("Waypoints: %d", len(waypoints))

    # Setup identity and security
    identity = setup_temp_identity()
    tmp_db = tempfile.mktemp(suffix=".db", prefix="drone_sim_")
    store = DataStore(db_path=tmp_db)
    crypto = CryptoEngine(identity)
    audit = AuditLogger(store, crypto, identity.drone_id)

    # Connect to flight controller
    logger.info("Connecting to SITL...")
    fc = FlightController(connection_string=connection, heartbeat_timeout=10)
    if not fc.connect():
        logger.error("Cannot connect to SITL at %s", connection)
        logger.error("Start SITL first: sim_vehicle.py -v ArduCopter --console")
        return

    logger.info("Connected to SITL")

    # Update telemetry a few times
    for _ in range(20):
        fc.update_telemetry()
        time.sleep(0.1)

    telem = fc.telemetry
    logger.info("Position: %.7f, %.7f", telem.lat, telem.lon)
    logger.info("Battery: %d%% (%.1fV)", telem.battery_pct, telem.battery_voltage)
    logger.info("GPS: %dD fix, %d sats", telem.gps_fix, telem.gps_satellites)
    logger.info("Mode: %s | Armed: %s", telem.mode, telem.armed)

    # Setup camera
    camera = Camera(source=video_source, width=640, height=480, fps=15)

    # Setup detector
    detector = Detector(
        model_name="yolov8n",
        confidence_threshold=0.5,
        target_classes=["person", "car", "truck"],
    )
    logger.info("Loading detection model...")
    if detector.load():
        logger.info("Detector ready (backend: %s)", detector.backend)
    else:
        logger.warning("Detector not loaded — running flight-only simulation")

    # Setup MQTT
    mqtt_client = None
    if mqtt_broker:
        mqtt_client = MQTTClient(
            broker=mqtt_broker,
            drone_id=identity.drone_id,
            topic_prefix="drone_sim",
        )
        if mqtt_client.connect():
            logger.info("MQTT connected to %s", mqtt_broker)
        else:
            logger.warning("MQTT broker not available — skipping")
            mqtt_client = None

    # Create mission
    mission = Mission(
        created_by=identity.drone_id,
        waypoints=waypoints,
        parameters={
            "altitude_m": 30.0,
            "speed_ms": 5.0,
            "loop": False,
            "detection_classes": ["person", "car", "truck"],
        },
    )
    store.save_mission(mission)
    audit.log("simulation_start", {"mission_id": mission.id})

    # Run simplified patrol (without full PatrolMission, for easier debugging)
    logger.info("")
    logger.info("=== STARTING SIMULATED PATROL ===")
    logger.info("Mission ID: %s", mission.id)

    try:
        # Set GUIDED mode
        logger.info("Setting GUIDED mode...")
        fc.set_mode("GUIDED")
        time.sleep(1)

        # Arm
        logger.info("Arming...")
        fc.arm()
        time.sleep(2)

        # Takeoff
        altitude = mission.parameters["altitude_m"]
        logger.info("Taking off to %.0fm...", altitude)
        fc.takeoff(altitude)

        # Wait for altitude
        for _ in range(60):
            fc.update_telemetry()
            t = fc.telemetry
            logger.info("  Alt: %.1fm / %.0fm", t.alt_rel, altitude)
            if t.alt_rel >= altitude * 0.9:
                break
            time.sleep(1)

        fc.set_speed(mission.parameters["speed_ms"])

        # Navigate waypoints
        for i, wp in enumerate(waypoints):
            logger.info("Waypoint %d: %.6f, %.6f", i, wp["lat"], wp["lon"])
            fc.goto(wp["lat"], wp["lon"], wp.get("alt", altitude))
            audit.log("waypoint_navigate", {"index": i, "target": wp})

            # Fly to waypoint
            for _ in range(120):  # max 2 min per waypoint
                fc.update_telemetry()
                t = fc.telemetry

                if fc.reached_waypoint(wp["lat"], wp["lon"], tolerance_m=3.0):
                    logger.info("  Reached waypoint %d", i)
                    break

                # Log position periodically
                logger.debug(
                    "  Pos: %.6f, %.6f | Alt: %.1f | Spd: %.1f",
                    t.lat, t.lon, t.alt_rel, t.groundspeed,
                )
                time.sleep(1)

            # Hover briefly at waypoint
            logger.info("  Hovering at waypoint %d...", i)
            time.sleep(3)

        # Land
        logger.info("Patrol complete, landing...")
        fc.land()
        audit.log("simulation_complete", {"mission_id": mission.id})

        # Wait for landing
        for _ in range(60):
            fc.update_telemetry()
            if fc.telemetry.alt_rel < 0.5:
                break
            time.sleep(1)

        logger.info("Landed.")

    except KeyboardInterrupt:
        logger.info("Simulation interrupted — RTL")
        fc.rtl()
        audit.log("simulation_interrupted", {})
    finally:
        fc.disconnect()
        if mqtt_client:
            mqtt_client.disconnect()

    # Print summary
    logger.info("")
    logger.info("=== SIMULATION SUMMARY ===")
    valid, count = store.verify_audit_chain()
    logger.info("Audit entries: %d (chain valid: %s)", count, valid)

    entries = store.get_audit_log(limit=100)
    for entry in reversed(entries):
        logger.info("  [%s] %s", entry.action, json.dumps(entry.details))

    store.close()
    logger.info("Temp DB: %s", tmp_db)


def main():
    parser = argparse.ArgumentParser(description="Run drone platform in simulation")
    parser.add_argument(
        "--connection", "-c",
        default="udp:127.0.0.1:14550",
        help="MAVLink connection string (default: udp:127.0.0.1:14550)",
    )
    parser.add_argument(
        "--video", "-V",
        default=None,
        help="Video file for camera source (default: device 0)",
    )
    parser.add_argument(
        "--waypoints", "-w",
        default=None,
        help="Waypoints JSON file (default: built-in SITL waypoints)",
    )
    parser.add_argument(
        "--mqtt", "-m",
        default=None,
        help="MQTT broker address (default: none)",
    )
    args = parser.parse_args()

    video_source = args.video if args.video else 0

    waypoints = None
    if args.waypoints:
        with open(args.waypoints) as f:
            waypoints = json.load(f)

    run_simulation(
        connection=args.connection,
        video_source=video_source,
        waypoints=waypoints,
        mqtt_broker=args.mqtt,
    )


if __name__ == "__main__":
    main()

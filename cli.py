"""Drone Platform CLI — Entry point for all drone operations.

Usage:
    drone-cli provision          Provision a new drone identity
    drone-cli preflight          Run preflight checks
    drone-cli patrol             Start a surveillance patrol mission
    drone-cli status             Show drone status
    drone-cli audit              Show audit log
    drone-cli verify-audit       Verify audit chain integrity
    drone-cli simulate           Start with ArduPilot SITL
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import yaml

from core.data.models import Mission, MissionStatus
from core.data.store import DataStore
from core.security.identity import DroneIdentity
from core.security.crypto import CryptoEngine
from core.security.audit import AuditLogger
from core.flight.controller import FlightController
from core.vision.camera import Camera
from core.vision.detector import Detector
from core.comms.mqtt_client import MQTTClient
from apps.surveillance.patrol import PatrolMission


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file."""
    paths = [
        config_path,
        "/etc/drone/config.yaml",
        str(Path(__file__).parent / "config" / "default.yaml"),
    ]
    for p in paths:
        if p and Path(p).exists():
            with open(p) as f:
                return yaml.safe_load(f)
    return {}


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def main(ctx, config_path, verbose):
    """Drone Platform — Security-first drone software for Jetson."""
    setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)
    ctx.obj["verbose"] = verbose


@main.command()
@click.option("--org-id", default="zypher-prototype", help="Organization ID")
@click.option("--identity-dir", default="/etc/drone/identity", help="Identity directory")
@click.pass_context
def provision(ctx, org_id, identity_dir):
    """Provision a new drone identity (run once per device)."""
    identity = DroneIdentity(identity_dir=identity_dir)

    if identity.is_provisioned:
        click.echo(f"Drone already provisioned: {identity.drone_id}")
        click.echo("To re-provision, delete the identity directory first.")
        return

    click.echo("Provisioning new drone identity...")
    result = identity.provision(org_id=org_id)

    click.echo("")
    click.echo("=== DRONE PROVISIONED ===")
    click.echo(f"  Drone ID:    {result['drone_id']}")
    click.echo(f"  Org ID:      {result['org_id']}")
    click.echo(f"  HW Finger:   {result['hardware_fingerprint'][:16]}...")
    click.echo(f"  Operator ID: {result['operator_id']}")
    click.echo(f"  API Key:     {result['operator_api_key']}")
    click.echo("")
    click.echo("SAVE THE API KEY — it will not be shown again.")
    click.echo(f"Public key stored at: {identity_dir}/drone_key_pub.pem")


@main.command()
@click.pass_context
def status(ctx):
    """Show current drone status."""
    config = ctx.obj["config"]
    fc_config = config.get("flight", {})

    identity = DroneIdentity(
        identity_dir=config.get("drone", {}).get("identity_dir", "/etc/drone/identity")
    )
    if not identity.is_provisioned:
        click.echo("Drone not provisioned. Run 'drone-cli provision' first.")
        return

    click.echo(f"Drone ID: {identity.drone_id}")

    fc = FlightController(
        connection_string=fc_config.get("connection", "udp:127.0.0.1:14550"),
        heartbeat_timeout=fc_config.get("heartbeat_timeout_s", 5),
    )
    click.echo(f"Connecting to {fc_config.get('connection', 'udp:127.0.0.1:14550')}...")

    if fc.connect():
        fc.update_telemetry()
        t = fc.telemetry
        click.echo("")
        click.echo("=== TELEMETRY ===")
        click.echo(f"  Connected:   {t.connected}")
        click.echo(f"  Armed:       {t.armed}")
        click.echo(f"  Mode:        {t.mode}")
        click.echo(f"  Position:    {t.lat:.7f}, {t.lon:.7f}")
        click.echo(f"  Altitude:    {t.alt_rel:.1f}m (rel) / {t.alt_msl:.1f}m (MSL)")
        click.echo(f"  Speed:       {t.groundspeed:.1f} m/s")
        click.echo(f"  Battery:     {t.battery_pct}% ({t.battery_voltage:.1f}V)")
        click.echo(f"  GPS:         {t.gps_fix}D fix, {t.gps_satellites} sats")
        fc.disconnect()
    else:
        click.echo("Could not connect to flight controller.")


@main.command()
@click.option("--waypoints", "-w", required=True, help="Waypoints JSON file")
@click.option("--altitude", "-a", default=30.0, help="Patrol altitude (meters)")
@click.option("--speed", "-s", default=5.0, help="Patrol speed (m/s)")
@click.option("--loop/--no-loop", default=True, help="Loop patrol route")
@click.pass_context
def patrol(ctx, waypoints, altitude, speed, loop):
    """Start a surveillance patrol mission."""
    config = ctx.obj["config"]
    drone_cfg = config.get("drone", {})
    flight_cfg = config.get("flight", {})
    vision_cfg = config.get("vision", {})
    comms_cfg = config.get("comms", {}).get("mqtt", {})
    surv_cfg = config.get("surveillance", {})

    # Load identity
    identity = DroneIdentity(
        identity_dir=drone_cfg.get("identity_dir", "/etc/drone/identity")
    )
    if not identity.is_provisioned:
        click.echo("Drone not provisioned. Run 'drone-cli provision' first.")
        return

    # Load waypoints
    wp_path = Path(waypoints)
    if not wp_path.exists():
        click.echo(f"Waypoints file not found: {waypoints}")
        return
    with open(wp_path) as f:
        wp_data = json.load(f)

    click.echo(f"Drone: {identity.drone_id}")
    click.echo(f"Waypoints: {len(wp_data)} points from {waypoints}")
    click.echo(f"Altitude: {altitude}m | Speed: {speed} m/s | Loop: {loop}")
    click.echo("")

    # Initialize all systems
    store = DataStore(db_path=config.get("data", {}).get("db_path", "/var/drone/missions.db"))
    crypto = CryptoEngine(identity)
    audit = AuditLogger(store, crypto, identity.drone_id)

    fc = FlightController(
        connection_string=flight_cfg.get("connection", "udp:127.0.0.1:14550"),
        heartbeat_timeout=flight_cfg.get("heartbeat_timeout_s", 5),
    )

    camera = Camera(
        source=vision_cfg.get("camera_source", 0),
        width=vision_cfg.get("frame_width", 1280),
        height=vision_cfg.get("frame_height", 720),
        fps=vision_cfg.get("fps", 30),
    )

    detector = Detector(
        model_name=vision_cfg.get("model", "yolov8n"),
        confidence_threshold=vision_cfg.get("confidence_threshold", 0.5),
        target_classes=vision_cfg.get("target_classes", ["person", "car"]),
    )

    # MQTT (optional — patrol works without it)
    mqtt_client = None
    if comms_cfg.get("broker"):
        mqtt_client = MQTTClient(
            broker=comms_cfg.get("broker", "localhost"),
            port=comms_cfg.get("port", 1883),
            drone_id=identity.drone_id,
            topic_prefix=comms_cfg.get("topic_prefix", "drone"),
            use_tls=comms_cfg.get("use_tls", False),
            qos=comms_cfg.get("qos", 1),
        )

    # Connect to flight controller
    click.echo("Connecting to flight controller...")
    if not fc.connect():
        click.echo("FAILED: Cannot connect to flight controller.")
        return

    # Load detection model
    click.echo("Loading detection model...")
    if not detector.load():
        click.echo("WARNING: Detection model not loaded. Running without AI.")

    # Connect MQTT
    if mqtt_client:
        click.echo("Connecting to MQTT broker...")
        if not mqtt_client.connect():
            click.echo("WARNING: MQTT not connected. Alerts will be local only.")

    # Create mission
    mission = Mission(
        created_by=identity.drone_id,
        waypoints=wp_data,
        parameters={
            "altitude_m": altitude,
            "speed_ms": speed,
            "loop": loop,
            "detection_classes": vision_cfg.get("target_classes", ["person", "car"]),
        },
    )
    store.save_mission(mission)
    click.echo(f"Mission created: {mission.id}")

    # Launch patrol
    click.echo("")
    click.echo("=== STARTING PATROL ===")
    patrol_mission = PatrolMission(
        mission=mission,
        flight=fc,
        camera=camera,
        detector=detector,
        store=store,
        crypto=crypto,
        audit=audit,
        mqtt_client=mqtt_client,
        config=surv_cfg,
    )

    try:
        if not patrol_mission.start():
            click.echo("FAILED: Preflight checks did not pass.")
            return
    except KeyboardInterrupt:
        click.echo("\nAborted by operator.")
        patrol_mission.abort()
    finally:
        fc.disconnect()
        if mqtt_client:
            mqtt_client.disconnect()
        store.close()

    click.echo(f"Findings: {patrol_mission.total_findings}")
    click.echo("Mission data stored locally.")


@main.command()
@click.option("--limit", "-n", default=20, help="Number of entries to show")
@click.pass_context
def audit(ctx, limit):
    """Show recent audit log entries."""
    config = ctx.obj["config"]
    store = DataStore(db_path=config.get("data", {}).get("db_path", "/var/drone/missions.db"))
    entries = store.get_audit_log(limit=limit)
    store.close()

    if not entries:
        click.echo("No audit entries found.")
        return

    click.echo(f"=== AUDIT LOG (last {len(entries)} entries) ===")
    click.echo("")
    for entry in reversed(entries):
        click.echo(f"  [{entry.timestamp}] {entry.actor[:8]}.. | {entry.action}")
        if entry.details:
            for k, v in entry.details.items():
                click.echo(f"    {k}: {v}")
    click.echo("")


@main.command("verify-audit")
@click.pass_context
def verify_audit(ctx):
    """Verify the audit log hash chain integrity."""
    config = ctx.obj["config"]
    store = DataStore(db_path=config.get("data", {}).get("db_path", "/var/drone/missions.db"))
    is_valid, count = store.verify_audit_chain()
    store.close()

    if count == 0:
        click.echo("No audit entries to verify.")
    elif is_valid:
        click.echo(f"VALID: Audit chain intact ({count} entries verified)")
    else:
        click.echo(f"TAMPERED: Chain broken at entry {count}")
        click.echo("The audit log may have been modified.")


@main.command()
@click.pass_context
def missions(ctx):
    """List all missions."""
    config = ctx.obj["config"]
    store = DataStore(db_path=config.get("data", {}).get("db_path", "/var/drone/missions.db"))
    all_missions = store.list_missions()
    store.close()

    if not all_missions:
        click.echo("No missions found.")
        return

    click.echo(f"=== MISSIONS ({len(all_missions)}) ===")
    click.echo("")
    for m in all_missions:
        findings = store.get_finding_count(m.id)
        click.echo(
            f"  {m.id[:8]}.. | {m.status.value:10s} | {m.type:12s} | "
            f"{len(m.waypoints)} wps | {findings} findings | {m.created_at}"
        )
    click.echo("")


if __name__ == "__main__":
    main()

"""Drone Platform CLI — Entry point for all drone operations.

Usage:
    drone-cli provision          Provision a new drone identity
    drone-cli preflight          Run preflight checks
    drone-cli patrol             Start a surveillance patrol mission
    drone-cli status             Show drone status
    drone-cli audit              Show audit log
    drone-cli verify-audit       Verify audit chain integrity
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

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

console = Console()

BANNER = """[bold cyan]
  ____                        ____  _       _    __
 |  _ \\ _ __ ___  _ __   ___|  _ \\| | __ _| |_ / _| ___  _ __ _ __ ___
 | | | | '__/ _ \\| '_ \\ / _ \\ |_) | |/ _` | __| |_ / _ \\| '__| '_ ` _ \\
 | |_| | | | (_) | | | |  __/  __/| | (_| | |_|  _| (_) | |  | | | | | |
 |____/|_|  \\___/|_| |_|\\___|_|   |_|\\__,_|\\__|_|  \\___/|_|  |_| |_| |_|
[/bold cyan]
[dim]Security-First Autonomous Drone Software for NVIDIA Jetson[/dim]
[dim]v0.1.0 | Zypher Synergy[/dim]"""


def expand_paths(obj):
    """Recursively expand ~ in all string values that look like paths."""
    if isinstance(obj, dict):
        return {k: expand_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_paths(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("~"):
        return str(Path(obj).expanduser())
    return obj


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file."""
    paths = [
        config_path,
        str(Path("~/.drone/config.yaml").expanduser()),
        "/etc/drone/config.yaml",
        str(Path(__file__).parent / "config" / "default.yaml"),
    ]
    for p in paths:
        if p and Path(p).exists():
            with open(p) as f:
                cfg = yaml.safe_load(f)
                return expand_paths(cfg) if cfg else {}
    return {}


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def step(msg: str, status: str = "info") -> None:
    """Print a styled step message."""
    icons = {
        "info": "[bold blue]\u25b6[/bold blue]",
        "ok": "[bold green]\u2713[/bold green]",
        "warn": "[bold yellow]\u26a0[/bold yellow]",
        "fail": "[bold red]\u2717[/bold red]",
        "wait": "[bold cyan]\u25cb[/bold cyan]",
    }
    icon = icons.get(status, icons["info"])
    console.print(f"  {icon} {msg}")


@click.group()
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def main(ctx, config_path, verbose):
    """Drone Platform \u2014 Security-first drone software for Jetson."""
    setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)
    ctx.obj["verbose"] = verbose


# ── PROVISION ─────────────────────────────────────────────────────────────

@main.command()
@click.option("--org-id", default="zypher-prototype", help="Organization ID")
@click.option("--identity-dir", default="~/.drone/identity", help="Identity directory")
@click.pass_context
def provision(ctx, org_id, identity_dir):
    """Provision a new drone identity (run once per device)."""
    console.print(BANNER)
    console.print()

    identity_dir = str(Path(identity_dir).expanduser())
    identity = DroneIdentity(identity_dir=identity_dir)

    if identity.is_provisioned:
        console.print(Panel(
            f"[yellow]Drone already provisioned[/yellow]\n\n"
            f"  Drone ID:  [bold]{identity.drone_id}[/bold]\n"
            f"  Directory: {identity_dir}\n\n"
            f"[dim]To re-provision, delete the identity directory first.[/dim]",
            title="[yellow]Already Provisioned[/yellow]",
            border_style="yellow",
        ))
        return

    step("Generating Ed25519 keypair...", "wait")
    result = identity.provision(org_id=org_id)
    step("Keypair generated", "ok")
    step("Hardware fingerprint computed", "ok")
    step("Operator credentials created", "ok")
    console.print()

    # Identity card
    id_table = Table(
        show_header=False,
        box=box.SIMPLE,
        padding=(0, 2),
        show_edge=False,
    )
    id_table.add_column("Key", style="dim", width=18)
    id_table.add_column("Value", style="bold")
    id_table.add_row("Drone ID", result["drone_id"])
    id_table.add_row("Organization", result["org_id"])
    id_table.add_row("HW Fingerprint", result["hardware_fingerprint"][:32] + "...")
    id_table.add_row("Public Key", f"{identity_dir}/drone_key_pub.pem")
    id_table.add_row("", "")
    id_table.add_row("Operator ID", result["operator_id"])
    id_table.add_row("API Key", f"[bold red]{result['operator_api_key']}[/bold red]")

    console.print(Panel(
        id_table,
        title="[bold green]Drone Provisioned Successfully[/bold green]",
        subtitle="[bold red]Save the API key \u2014 it will not be shown again[/bold red]",
        border_style="green",
        padding=(1, 2),
    ))


# ── STATUS ────────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def status(ctx):
    """Show current drone status and telemetry."""
    console.print(BANNER)
    console.print()

    config = ctx.obj["config"]
    fc_config = config.get("flight", {})

    identity = DroneIdentity(
        identity_dir=config.get("drone", {}).get("identity_dir", "/etc/drone/identity")
    )
    if not identity.is_provisioned:
        console.print(Panel(
            "[red]Drone not provisioned.[/red]\n\n"
            "Run [bold]drone-cli provision[/bold] first.",
            title="[red]Error[/red]",
            border_style="red",
        ))
        return

    step(f"Drone ID: [bold]{identity.drone_id}[/bold]", "info")

    conn_str = fc_config.get("connection", "udp:127.0.0.1:14550")
    step(f"Connecting to [cyan]{conn_str}[/cyan]...", "wait")

    fc = FlightController(
        connection_string=conn_str,
        heartbeat_timeout=fc_config.get("heartbeat_timeout_s", 5),
    )

    if not fc.connect():
        step("Cannot connect to flight controller", "fail")
        return

    step("Connected", "ok")

    # Poll telemetry a few times for accurate data
    for _ in range(10):
        fc.update_telemetry()
        time.sleep(0.1)
    t = fc.telemetry

    console.print()

    # Build status table
    tbl = Table(
        title="Drone Telemetry",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        padding=(0, 2),
    )
    tbl.add_column("Parameter", style="dim", width=16)
    tbl.add_column("Value", width=30)
    tbl.add_column("Parameter", style="dim", width=16)
    tbl.add_column("Value", width=30)

    conn_style = "bold green" if t.connected else "bold red"
    armed_style = "bold red" if t.armed else "bold green"

    tbl.add_row(
        "Connection", f"[{conn_style}]{'Connected' if t.connected else 'Disconnected'}[/{conn_style}]",
        "Armed", f"[{armed_style}]{t.armed}[/{armed_style}]",
    )
    tbl.add_row(
        "Flight Mode", f"[bold]{t.mode or 'Unknown'}[/bold]",
        "GPS Fix", f"{t.gps_fix}D ({t.gps_satellites} sats)",
    )
    tbl.add_row(
        "Latitude", f"{t.lat:.7f}",
        "Longitude", f"{t.lon:.7f}",
    )
    tbl.add_row(
        "Altitude (rel)", f"{t.alt_rel:.1f} m",
        "Altitude (MSL)", f"{t.alt_msl:.1f} m",
    )
    tbl.add_row(
        "Groundspeed", f"{t.groundspeed:.1f} m/s",
        "Heading", f"{t.yaw:.0f}\u00b0",
    )

    if t.battery_pct >= 0:
        if t.battery_pct > 50:
            bat_style = "bold green"
        elif t.battery_pct > 25:
            bat_style = "bold yellow"
        else:
            bat_style = "bold red"
        bat_str = f"[{bat_style}]{t.battery_pct}%[/{bat_style}] ({t.battery_voltage:.1f}V)"
    else:
        bat_str = "[dim]Unknown[/dim]"
    tbl.add_row(
        "Battery", bat_str,
        "Last Heartbeat", t.last_heartbeat or "[dim]None[/dim]",
    )

    console.print(tbl)
    console.print()
    fc.disconnect()


# ── PATROL ────────────────────────────────────────────────────────────────

@main.command()
@click.option("--waypoints", "-w", required=True, help="Waypoints JSON file")
@click.option("--altitude", "-a", default=30.0, help="Patrol altitude (meters)")
@click.option("--speed", "-s", default=5.0, help="Patrol speed (m/s)")
@click.option("--loop/--no-loop", default=True, help="Loop patrol route")
@click.pass_context
def patrol(ctx, waypoints, altitude, speed, loop):
    """Start a surveillance patrol mission."""
    console.print(BANNER)
    console.print()

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
        console.print(Panel(
            "[red]Drone not provisioned.[/red]\n\n"
            "Run [bold]drone-cli provision[/bold] first.",
            title="[red]Error[/red]",
            border_style="red",
        ))
        return

    # Load waypoints
    wp_path = Path(waypoints)
    if not wp_path.exists():
        step(f"Waypoints file not found: [bold]{waypoints}[/bold]", "fail")
        return
    with open(wp_path) as f:
        wp_data = json.load(f)

    # Mission briefing panel
    briefing = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
    briefing.add_column("Key", style="dim", width=16)
    briefing.add_column("Value")
    briefing.add_row("Drone ID", f"[bold]{identity.drone_id}[/bold]")
    briefing.add_row("Waypoints", f"{len(wp_data)} points from [cyan]{waypoints}[/cyan]")
    briefing.add_row("Altitude", f"{altitude} m")
    briefing.add_row("Speed", f"{speed} m/s")
    briefing.add_row("Loop", f"{'Yes' if loop else 'No'}")
    briefing.add_row("Detection", ", ".join(vision_cfg.get("target_classes", ["person", "car"])))

    console.print(Panel(
        briefing,
        title="[bold]Mission Briefing[/bold]",
        border_style="cyan",
        padding=(1, 1),
    ))
    console.print()

    # Initialize systems
    step("Initializing security layer...", "wait")
    store = DataStore(db_path=config.get("data", {}).get("db_path", "/var/drone/missions.db"))
    crypto = CryptoEngine(identity)
    audit = AuditLogger(store, crypto, identity.drone_id)
    step("Security layer initialized", "ok")

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

    # Connect flight controller
    conn_str = flight_cfg.get("connection", "udp:127.0.0.1:14550")
    step(f"Connecting to flight controller [cyan]{conn_str}[/cyan]...", "wait")
    if not fc.connect():
        step("Cannot connect to flight controller", "fail")
        return
    step("Flight controller connected", "ok")

    # Load detection model
    step("Loading detection model...", "wait")
    if detector.load():
        step(f"Model loaded (backend: {detector.backend})", "ok")
    else:
        step("Detection model not available \u2014 running flight only", "warn")

    # MQTT
    if mqtt_client:
        step("Connecting to MQTT broker...", "wait")
        if mqtt_client.connect():
            step("MQTT connected", "ok")
        else:
            step("MQTT not available \u2014 alerts will be local only", "warn")

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
    step(f"Mission created: [bold]{mission.id[:12]}...[/bold]", "ok")
    console.print()

    console.print(Panel(
        "[bold green]All systems go \u2014 launching patrol[/bold green]",
        border_style="green",
    ))
    console.print()

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
            step("Preflight checks did not pass", "fail")
            return
    except KeyboardInterrupt:
        console.print()
        step("Aborted by operator", "warn")
        patrol_mission.abort()
    finally:
        fc.disconnect()
        if mqtt_client:
            mqtt_client.disconnect()
        store.close()

    console.print()
    console.print(Panel(
        f"[bold]Total Findings:[/bold] {patrol_mission.total_findings}\n"
        f"[bold]Mission ID:[/bold]     {mission.id}\n\n"
        f"[dim]Mission data stored locally. Run 'drone-cli audit' to review.[/dim]",
        title="[bold]Mission Complete[/bold]",
        border_style="green",
    ))


# ── AUDIT ─────────────────────────────────────────────────────────────────

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
        step("No audit entries found.", "info")
        return

    tbl = Table(
        title=f"Audit Log (last {len(entries)} entries)",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
    )
    tbl.add_column("Timestamp", style="dim", width=22)
    tbl.add_column("Actor", width=14)
    tbl.add_column("Action", style="bold", width=22)
    tbl.add_column("Details", width=46)

    for entry in reversed(entries):
        details_str = ""
        if entry.details:
            details_str = ", ".join(f"{k}={v}" for k, v in entry.details.items())

        action_colors = {
            "start": "green", "complete": "bold green", "boot": "blue",
            "abort": "red", "error": "red", "detection": "yellow",
            "navigate": "cyan", "battery": "bold red",
        }
        action_style = "white"
        for keyword, color in action_colors.items():
            if keyword in entry.action:
                action_style = color
                break

        tbl.add_row(
            entry.timestamp[:22],
            entry.actor[:14],
            f"[{action_style}]{entry.action}[/{action_style}]",
            details_str[:46] + ("\u2026" if len(details_str) > 46 else ""),
        )

    console.print()
    console.print(tbl)
    console.print()


# ── VERIFY AUDIT ──────────────────────────────────────────────────────────

@main.command("verify-audit")
@click.pass_context
def verify_audit(ctx):
    """Verify the audit log hash chain integrity."""
    config = ctx.obj["config"]
    store = DataStore(db_path=config.get("data", {}).get("db_path", "/var/drone/missions.db"))
    is_valid, count = store.verify_audit_chain()
    store.close()

    console.print()
    if count == 0:
        step("No audit entries to verify.", "info")
    elif is_valid:
        console.print(Panel(
            f"[bold green]CHAIN INTACT[/bold green]\n\n"
            f"  Entries verified:  [bold]{count}[/bold]\n"
            f"  Hash algorithm:   SHA-256\n"
            f"  Signature:        Ed25519\n\n"
            f"[dim]No tampering detected. All entries are cryptographically linked.[/dim]",
            title="[bold green]Audit Verification Passed[/bold green]",
            border_style="green",
            padding=(1, 2),
        ))
    else:
        console.print(Panel(
            f"[bold red]CHAIN BROKEN[/bold red]\n\n"
            f"  Break detected at entry:  [bold]{count}[/bold]\n\n"
            f"[bold]The audit log has been tampered with.[/bold]\n"
            f"[dim]Entries after position {count} cannot be trusted.[/dim]",
            title="[bold red]Audit Verification FAILED[/bold red]",
            border_style="red",
            padding=(1, 2),
        ))


# ── MISSIONS ──────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def missions(ctx):
    """List all missions."""
    config = ctx.obj["config"]
    store = DataStore(db_path=config.get("data", {}).get("db_path", "/var/drone/missions.db"))
    all_missions = store.list_missions()

    if not all_missions:
        step("No missions found.", "info")
        store.close()
        return

    tbl = Table(
        title=f"Missions ({len(all_missions)})",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
    )
    tbl.add_column("ID", width=14)
    tbl.add_column("Status", width=12)
    tbl.add_column("Type", width=14)
    tbl.add_column("Waypoints", justify="center", width=10)
    tbl.add_column("Findings", justify="center", width=10)
    tbl.add_column("Created", width=22)

    for m in all_missions:
        findings = store.get_finding_count(m.id)

        status_colors = {
            "draft": "dim",
            "active": "bold cyan",
            "paused": "yellow",
            "completed": "bold green",
            "aborted": "bold red",
        }
        style = status_colors.get(m.status.value, "white")
        findings_style = "bold yellow" if findings > 0 else "dim"

        tbl.add_row(
            m.id[:13] + "\u2026",
            f"[{style}]{m.status.value.upper()}[/{style}]",
            m.type,
            str(len(m.waypoints)),
            f"[{findings_style}]{findings}[/{findings_style}]",
            m.created_at[:22],
        )

    store.close()
    console.print()
    console.print(tbl)
    console.print()


# ── PREFLIGHT ─────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def preflight(ctx):
    """Run preflight system checks."""
    console.print(BANNER)
    console.print()

    config = ctx.obj["config"]
    drone_cfg = config.get("drone", {})
    flight_cfg = config.get("flight", {})
    vision_cfg = config.get("vision", {})
    checks = []

    # Identity
    identity = DroneIdentity(
        identity_dir=drone_cfg.get("identity_dir", "/etc/drone/identity")
    )
    if identity.is_provisioned:
        checks.append(("Identity", True, f"Provisioned ({identity.drone_id[:12]}...)"))
    else:
        checks.append(("Identity", False, "Not provisioned"))

    # Flight controller
    conn_str = flight_cfg.get("connection", "udp:127.0.0.1:14550")
    fc = FlightController(connection_string=conn_str, heartbeat_timeout=3)
    if fc.connect():
        for _ in range(10):
            fc.update_telemetry()
            time.sleep(0.1)
        t = fc.telemetry
        checks.append(("Flight Controller", True, f"Connected ({t.mode})"))
        checks.append(("GPS", t.gps_fix >= 3, f"{t.gps_fix}D fix, {t.gps_satellites} sats"))
        if t.battery_pct >= 0:
            checks.append(("Battery", t.battery_pct > 25, f"{t.battery_pct}% ({t.battery_voltage:.1f}V)"))
        else:
            checks.append(("Battery", False, "Unknown"))
        fc.disconnect()
    else:
        checks.append(("Flight Controller", False, f"Cannot connect to {conn_str}"))
        checks.append(("GPS", False, "Unavailable"))
        checks.append(("Battery", False, "Unavailable"))

    # Camera
    cam = Camera(source=vision_cfg.get("camera_source", 0))
    if cam.open():
        checks.append(("Camera", True, "Available"))
        cam.stop()
    else:
        checks.append(("Camera", False, "Cannot open camera source"))

    # Detector
    det = Detector(model_name=vision_cfg.get("model", "yolov8n"))
    if det.load():
        checks.append(("AI Model", True, f"{vision_cfg.get('model', 'yolov8n')} ({det.backend})"))
    else:
        checks.append(("AI Model", False, "Model not available"))

    # Results
    tbl = Table(
        title="Preflight Checks",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
    )
    tbl.add_column("System", width=20)
    tbl.add_column("Status", width=8, justify="center")
    tbl.add_column("Details", width=50)

    all_passed = True
    for name, passed, detail in checks:
        if passed:
            status_str = "[bold green]PASS[/bold green]"
        else:
            status_str = "[bold red]FAIL[/bold red]"
            all_passed = False
        tbl.add_row(name, status_str, detail)

    console.print(tbl)
    console.print()

    if all_passed:
        console.print(Panel(
            "[bold green]All preflight checks passed. Ready for mission.[/bold green]",
            border_style="green",
        ))
    else:
        console.print(Panel(
            "[bold yellow]Some checks failed. Resolve issues before flight.[/bold yellow]",
            border_style="yellow",
        ))


if __name__ == "__main__":
    main()

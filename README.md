# Drone Platform — Security-First Drone Software for Jetson

A prototype drone software platform designed with security as a first-class concern. Runs on NVIDIA Jetson devices as a companion computer alongside ArduPilot/PX4 flight controllers.

## Architecture

```
┌─────────────────────────────────────────────┐
│              JETSON (Companion)              │
│                                             │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
│  │ Camera   │→ │ YOLOv8   │→ │ Alert Mgr │─│─→ MQTT (signed alerts)
│  │ Capture  │  │ Detector │  │ (sign+log)│ │
│  └──────────┘  └──────────┘  └───────────┘ │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ Mission Engine (patrol / SAR / inspect)│  │
│  └──────────────┬──────────────────────┘    │
│                 │ MAVLink                    │
│  ┌──────────────┴──────────────────────┐    │
│  │ Security: Ed25519 identity + audit  │    │
│  │ Data: SQLite + signed findings      │    │
│  └─────────────────────────────────────┘    │
└─────────────────┬───────────────────────────┘
                  │ Serial/USB
         ┌────────┴────────┐
         │ Flight Controller│
         │ (ArduPilot/PX4) │
         └─────────────────┘
```

## Security Model

- **Drone Identity**: Ed25519 keypair generated at provisioning, bound to hardware fingerprint
- **Signed Findings**: Every detection is signed at capture time — tamper-evident chain of evidence
- **Audit Log**: Hash-chained log entries (blockchain-lite) — deletion/modification breaks the chain
- **Operator Auth**: API key verification with HMAC-signed commands and replay protection
- **Data at Rest**: AES-256-GCM encryption available for sensitive data
- **No Cloud Dependency**: All processing runs on-device, data stored locally

## Project Structure

```
drone/
├── core/
│   ├── flight/          MAVLink interface to ArduPilot/PX4
│   │   ├── controller.py    Arm, takeoff, goto, land, RTL
│   │   └── telemetry.py     Thread-safe telemetry state
│   ├── vision/          Camera + AI detection
│   │   ├── camera.py        CSI/USB/file capture with threading
│   │   └── detector.py      YOLOv8 with TensorRT/ONNX fallback
│   ├── security/        Identity + cryptography
│   │   ├── identity.py      Drone provisioning + Ed25519 keypair
│   │   ├── crypto.py        Signing, encryption, command verification
│   │   └── audit.py         Tamper-evident audit logging
│   ├── comms/           Communication
│   │   └── mqtt_client.py   Signed alert delivery + command reception
│   └── data/            Storage
│       ├── models.py        Mission, Finding, AuditEntry (UUID v7)
│       └── store.py         SQLite store with chain verification
├── apps/
│   └── surveillance/    Autonomous patrol + intrusion detection
│       ├── patrol.py        Waypoint navigation + detection loop
│       └── alerts.py        Detection-to-signed-alert pipeline
├── tools/
│   ├── provision.py     Provision new drone identity
│   └── simulate.py      Run with ArduPilot SITL
├── config/
│   ├── default.yaml     Default configuration
│   └── sample_waypoints.json
├── tests/               21 tests covering models, security, storage
└── cli.py               Command-line interface
```

## Quick Start

### 1. Install

```bash
pip install -e .
```

For Jetson with GPU acceleration:
```bash
pip install -e ".[jetson]"
```

### 2. Provision Drone Identity

Run once per device:
```bash
drone-cli provision --org-id my-org
```

Outputs a drone ID, operator ID, and API key. Save the API key — it won't be shown again.

### 3. Run Surveillance Patrol

```bash
drone-cli patrol \
  --waypoints config/sample_waypoints.json \
  --altitude 30 \
  --speed 5 \
  --loop
```

### 4. Check Status

```bash
drone-cli status          # Drone telemetry
drone-cli missions        # List missions
drone-cli audit           # View audit log
drone-cli verify-audit    # Verify audit chain integrity
```

## Simulation (No Hardware)

### With ArduPilot SITL

1. Start SITL:
```bash
sim_vehicle.py -v ArduCopter --console --map
```

2. Run simulation:
```bash
python tools/simulate.py --connection udp:127.0.0.1:14550
```

## Hardware Requirements (Production)

| Component | Recommended |
|-----------|------------|
| Companion Computer | Jetson Nano 4GB / Orin Nano |
| Flight Controller | Pixhawk 4/6 with ArduPilot |
| Camera | IMX219/IMX477 (CSI) or USB webcam |
| FC Connection | USB or UART serial |
| Ground Link | WiFi (prototype) / radio (production) |

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Configuration

Copy and modify `config/default.yaml`:

```bash
cp config/default.yaml /etc/drone/config.yaml
drone-cli -c /etc/drone/config.yaml patrol -w waypoints.json
```

Key settings:
- `flight.connection` — MAVLink endpoint (`/dev/ttyTHS1` for Jetson UART, `udp:...` for SITL)
- `vision.camera_source` — Camera device (0 for USB, `csi:` for Jetson CSI)
- `vision.model` — YOLOv8 variant (`yolov8n` for Nano, `yolov8s` for Orin)
- `vision.target_classes` — What to detect (`person`, `car`, `truck`, etc.)
- `comms.mqtt.broker` — MQTT broker address for alerts

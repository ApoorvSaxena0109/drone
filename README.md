<p align="center">
  <h1 align="center">Drone Platform</h1>
  <p align="center">
    <strong>Security-First Autonomous Drone Software for NVIDIA Jetson</strong>
  </p>
  <p align="center">
    <a href="#quick-start">Quick Start</a> &middot;
    <a href="#architecture">Architecture</a> &middot;
    <a href="#production-use-cases">Use Cases</a> &middot;
    <a href="#security-model">Security</a> &middot;
    <a href="#deployment">Deployment</a>
  </p>
</p>

---

## Overview

Drone Platform is an edge-native autonomous drone software stack built for NVIDIA Jetson companion computers. It provides real-time AI-powered detection, cryptographically signed evidence chains, and autonomous mission execution — all running on-device with zero cloud dependency.

Designed to operate as a companion computer layer alongside ArduPilot/PX4 flight controllers over the MAVLink protocol. Our code never touches GPL flight controller firmware — clean IP separation by design.

### Key Capabilities

| Capability | Description |
|:-----------|:------------|
| **On-Device AI** | YOLOv8 object detection accelerated via TensorRT on Jetson GPU |
| **Autonomous Missions** | Waypoint patrol, loiter-on-detection, auto-RTL on low battery |
| **Tamper-Evident Logging** | Hash-chained audit trail — any modification breaks the chain |
| **Signed Evidence** | Every detection is Ed25519-signed at capture with image hash |
| **Hardware-Bound Identity** | Drone keypair tied to CPU serial + MAC fingerprint |
| **Offline-First** | Full autonomy with no network required; sync when available |

---

## Production Use Cases

### Perimeter Security & Surveillance

Autonomous patrol of facilities such as warehouses, solar farms, data centers, and construction sites. The drone flies predefined routes, detects intrusions using edge AI, and delivers cryptographically signed alerts in real time via MQTT.

**Value**: Replaces manned patrols with 24/7 autonomous coverage. Every detection includes a signed evidence chain admissible for incident review.

### Critical Infrastructure Inspection

Scheduled inspection of power lines, pipelines, bridges, and telecom towers. The vision pipeline flags anomalies (corrosion, structural damage, vegetation encroachment) and produces tamper-proof inspection reports with location-stamped, signed imagery.

**Value**: Reduces inspection cost while creating audit-grade documentation. Data never leaves the organization's infrastructure.

### Search & Rescue Operations

Grid-pattern search over disaster or wilderness areas using thermal and RGB cameras. Detections of human subjects are GPS-tagged, signed, and relayed over mesh radio when cellular infrastructure is unavailable.

**Value**: Accelerates response time in post-disaster environments where communication infrastructure is degraded or destroyed.

### Agriculture & Land Management

Crop health monitoring, livestock tracking, and boundary surveillance for large-scale agricultural operations. Detection data stays on-premise — no third-party cloud exposure of proprietary agricultural intelligence.

**Value**: Protects high-value crop data and operational patterns from supply chain exposure while enabling precision agriculture workflows.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│                 NVIDIA JETSON                     │
│                                                  │
│   ┌──────────┐   ┌──────────┐   ┌────────────┐  │
│   │ Camera   │──▶│ YOLOv8   │──▶│  Alert Mgr │──│──▶ MQTT
│   │ Pipeline │   │ Detector │   │  Sign + Log │  │   (Signed Alerts)
│   └──────────┘   └──────────┘   └────────────┘  │
│                                                  │
│   ┌──────────────────────────────────────────┐   │
│   │          Mission Engine                  │   │
│   │   Patrol · Inspect · Search & Rescue     │   │
│   └─────────────────┬────────────────────────┘   │
│                     │ MAVLink                     │
│   ┌─────────────────┴────────────────────────┐   │
│   │  Security Layer                          │   │
│   │  Ed25519 Identity · AES-256-GCM · Audit  │   │
│   └──────────────────────────────────────────┘   │
│                                                  │
│   ┌──────────────────────────────────────────┐   │
│   │  Local Data Store (SQLite + WAL)         │   │
│   │  Missions · Findings · Audit Chain       │   │
│   └──────────────────────────────────────────┘   │
└──────────────────────┬───────────────────────────┘
                       │ Serial / USB
              ┌────────┴─────────┐
              │ Flight Controller │
              │  ArduPilot / PX4  │
              └──────────────────┘
```

### IP Separation

| Layer | Codebase | License |
|:------|:---------|:--------|
| Companion Computer (Jetson) | Drone Platform (proprietary) | Proprietary |
| Communication Protocol | MAVLink | MIT |
| Flight Controller | ArduPilot / PX4 (unmodified) | GPLv3 |

Separate processes, separate hardware, standard protocol. No GPL contamination.

---

## Security Model

### Identity & Authentication

Each drone is provisioned with a unique **Ed25519 keypair** bound to a hardware fingerprint derived from the device's CPU serial number and primary MAC address. The private key is generated on-device and never leaves the hardware.

Operators authenticate using **API keys** stored as SHA-256 hashes. Commands are verified via **HMAC-SHA256** with a 30-second replay window to prevent replay attacks.

### Evidence Integrity

Every detection finding includes:
- GPS-stamped location at time of capture
- SHA-256 hash of the source image
- Ed25519 signature over the complete evidence payload

Tampering with any field — location, classification, confidence, or image — invalidates the signature.

### Tamper-Evident Audit Log

All system events (mission start, waypoint navigation, detections, commands received) are recorded in a **hash-chained audit log**. Each entry contains the SHA-256 hash of the previous entry, forming a cryptographic chain. Deleting or modifying any entry breaks the chain, and the integrity can be verified at any time:

```bash
drone-cli verify-audit
# Output: VALID — Audit chain intact (347 entries verified)
```

### Encryption

Sensitive data at rest is protected with **AES-256-GCM** authenticated encryption. Nonces are randomly generated per encryption operation.

---

## Project Structure

```
drone/
├── core/
│   ├── flight/                 # MAVLink flight controller interface
│   │   ├── controller.py       #   Arm, takeoff, goto, land, RTL, speed control
│   │   └── telemetry.py        #   Thread-safe telemetry state container
│   ├── vision/                 # Computer vision pipeline
│   │   ├── camera.py           #   Jetson CSI / USB / file capture
│   │   └── detector.py         #   YOLOv8 (TensorRT, ONNX Runtime, OpenCV DNN)
│   ├── security/               # Cryptographic identity & audit
│   │   ├── identity.py         #   Ed25519 provisioning, hardware binding
│   │   ├── crypto.py           #   Signing, encryption, command verification
│   │   └── audit.py            #   Hash-chained tamper-evident logging
│   ├── comms/                  # Communications
│   │   └── mqtt_client.py      #   Signed alert pub/sub, command reception
│   └── data/                   # Persistence
│       ├── models.py           #   Mission, Finding, AuditEntry (UUID v7)
│       └── store.py            #   SQLite with WAL, foreign keys, chain verify
├── apps/
│   └── surveillance/           # Autonomous patrol application
│       ├── patrol.py           #   Mission lifecycle, waypoint navigation
│       └── alerts.py           #   Detection-to-alert pipeline with cooldown
├── tools/
│   ├── provision.py            #   Standalone device provisioning
│   └── simulate.py             #   ArduPilot SITL simulation harness
├── config/
│   ├── default.yaml            #   Reference configuration
│   └── sample_waypoints.json   #   Example patrol route
├── tests/                      #   21 unit tests (models, security, storage)
├── cli.py                      #   Command-line interface
└── pyproject.toml              #   Project metadata and dependencies
```

---

## Quick Start

### Prerequisites

- Python 3.10+
- NVIDIA Jetson device (Nano, Orin Nano, Xavier NX) or any Linux system for simulation
- ArduPilot-compatible flight controller (Pixhawk 4/6, CubeOrange, etc.)

### Installation

```bash
# Standard installation
pip install -e .

# Jetson with GPU-accelerated inference
pip install -e ".[jetson]"

# Development (includes test dependencies)
pip install -e ".[dev]"
```

### Step 1 — Provision Device Identity

Run once per physical device. Generates the Ed25519 keypair and binds it to the hardware.

```bash
drone-cli provision --org-id your-organization
```

```
=== DRONE PROVISIONED ===
  Drone ID:    019c2d06-6d56-7000-8a3f-...
  Org ID:      your-organization
  HW Finger:   a4f8c2e1b9d37a02...
  Operator ID: 019c2d06-6d57-7000-92c1-...
  API Key:     e8a4f2c1d9b37a02e5f8...

SAVE THE API KEY — it will not be shown again.
```

### Step 2 — Configure

```bash
cp config/default.yaml /etc/drone/config.yaml
```

Edit key parameters for your hardware:

```yaml
flight:
  connection: "/dev/ttyTHS1"      # Jetson UART to Pixhawk
  # connection: "udp:127.0.0.1:14550"  # SITL simulation

vision:
  camera_source: "csi:"           # Jetson CSI camera
  model: "yolov8n"                # yolov8n (Nano) or yolov8s (Orin)
  confidence_threshold: 0.5
  target_classes:
    - "person"
    - "car"
    - "truck"

comms:
  mqtt:
    broker: "192.168.1.100"       # Ground station MQTT broker
```

### Step 3 — Run Mission

```bash
drone-cli patrol \
  --waypoints config/sample_waypoints.json \
  --altitude 30 \
  --speed 5 \
  --loop
```

### Operations Commands

```bash
drone-cli status              # Live drone telemetry
drone-cli missions            # List all missions with finding counts
drone-cli audit --limit 50    # View recent audit log entries
drone-cli verify-audit        # Cryptographic chain integrity check
```

---

## Simulation

Test the full platform without hardware using ArduPilot SITL (Software In The Loop).

### 1. Start ArduPilot SITL

```bash
sim_vehicle.py -v ArduCopter --console --map
```

### 2. Run Simulated Mission

```bash
python tools/simulate.py \
  --connection udp:127.0.0.1:14550 \
  --video test_footage.mp4 \
  --mqtt localhost
```

The simulation runs the complete mission lifecycle — provisioning, takeoff, waypoint navigation, detection pipeline, audit logging, and landing — against the simulated flight controller.

---

## Deployment

### Hardware Bill of Materials

| Component | Development | Production |
|:----------|:------------|:-----------|
| Companion Computer | Jetson Nano 4GB | Jetson Orin Nano 8GB |
| Flight Controller | Pixhawk 4 Mini | Pixhawk 6C / CubeOrange |
| Camera | USB Webcam | IMX477 (CSI) / Thermal dual |
| FC Connection | USB Cable | UART (`/dev/ttyTHS1`) |
| Ground Link | WiFi | SiK Radio / RFD900x |
| Storage | MicroSD 32GB | NVMe SSD 128GB+ |
| MQTT Broker | Mosquitto (local) | EMQX / HiveMQ (on-prem) |

### Jetson Setup

```bash
# Install JetPack SDK (includes CUDA, TensorRT, cuDNN)
# https://developer.nvidia.com/embedded/jetpack

# Clone and install
git clone <repository-url> && cd drone
pip install -e ".[jetson]"

# Provision identity
sudo drone-cli provision --org-id production-org \
  --identity-dir /etc/drone/identity

# Deploy configuration
sudo cp config/default.yaml /etc/drone/config.yaml
# Edit /etc/drone/config.yaml for your hardware
```

### Running as a System Service

```ini
# /etc/systemd/system/drone-patrol.service
[Unit]
Description=Drone Patrol Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/drone-cli -c /etc/drone/config.yaml patrol \
  --waypoints /etc/drone/waypoints.json --loop
Restart=on-failure
RestartSec=10
User=drone
Group=drone

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable drone-patrol
sudo systemctl start drone-patrol
```

---

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

```
tests/test_models.py       8 passed    UUID v7, serialization, hash chains
tests/test_security.py     7 passed    Provisioning, signing, encryption
tests/test_store.py        6 passed    SQLite CRUD, audit chain verification
```

---

## Configuration Reference

All configuration is managed through a single YAML file. See [`config/default.yaml`](config/default.yaml) for the complete reference with inline documentation.

| Section | Key | Description |
|:--------|:----|:------------|
| `flight.connection` | string | MAVLink endpoint. Serial: `/dev/ttyTHS1`. SITL: `udp:127.0.0.1:14550` |
| `flight.max_altitude_m` | float | Safety ceiling in meters |
| `flight.rtl_battery_pct` | int | Battery percentage that triggers automatic return-to-launch |
| `vision.camera_source` | string/int | `0` for USB, `csi:` for Jetson CSI, or video file path |
| `vision.model` | string | YOLOv8 variant: `yolov8n`, `yolov8s`, or path to custom `.pt`/`.onnx` |
| `vision.confidence_threshold` | float | Minimum detection confidence (0.0–1.0) |
| `vision.target_classes` | list | COCO class names to detect |
| `security.command_max_age_s` | int | Replay protection window in seconds |
| `comms.mqtt.broker` | string | MQTT broker hostname or IP |
| `comms.mqtt.use_tls` | bool | Enable TLS for MQTT connections |
| `surveillance.alert_cooldown_s` | float | Minimum seconds between alerts for same class |
| `surveillance.detection_loiter_s` | float | Hover duration on detection for closer inspection |

---

## License

Proprietary. All rights reserved by Zypher Synergy.

Flight controller firmware (ArduPilot/PX4) is used unmodified under its original GPLv3 license. Communication occurs exclusively through the MAVLink protocol (MIT). No proprietary code is linked to or derived from GPL-licensed components.

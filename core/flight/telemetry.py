"""Telemetry state container.

Holds the latest telemetry from the flight controller.
Updated continuously by the FlightController background loop.
Thread-safe reads via dataclass copy.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TelemetryState:
    """Current drone state from the flight controller."""

    # Position
    lat: float = 0.0
    lon: float = 0.0
    alt_msl: float = 0.0       # meters above sea level
    alt_rel: float = 0.0       # meters above home/takeoff

    # Attitude
    roll: float = 0.0          # degrees
    pitch: float = 0.0         # degrees
    yaw: float = 0.0           # degrees (heading)

    # Velocity
    vx: float = 0.0            # m/s north
    vy: float = 0.0            # m/s east
    vz: float = 0.0            # m/s down (positive = descending)
    groundspeed: float = 0.0   # m/s

    # System
    battery_pct: int = -1      # 0-100, -1 if unknown
    battery_voltage: float = 0.0
    armed: bool = False
    mode: str = ""
    gps_fix: int = 0           # 0=no fix, 2=2D, 3=3D
    gps_satellites: int = 0

    # Heartbeat
    connected: bool = False
    last_heartbeat: str = ""

    # Timestamp of this snapshot
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class TelemetryStore:
    """Thread-safe telemetry state with lock-protected updates."""

    def __init__(self):
        self._state = TelemetryState()
        self._lock = threading.Lock()

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
            self._state.updated_at = datetime.now(timezone.utc).isoformat()

    @property
    def state(self) -> TelemetryState:
        """Return a snapshot of current telemetry."""
        with self._lock:
            # Shallow copy is fine — all fields are primitives
            return TelemetryState(**{
                k: getattr(self._state, k)
                for k in self._state.__dataclass_fields__
            })

    @property
    def location(self) -> tuple[float, float, float]:
        with self._lock:
            return self._state.lat, self._state.lon, self._state.alt_rel

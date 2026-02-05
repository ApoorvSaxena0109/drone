"""Data models for the drone platform.

All entities use UUID v7 (time-sortable) for identifiers.
Findings are cryptographically signed at creation time.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


_uuid7_counter = 0
_uuid7_last_ms = 0


def uuid7() -> str:
    """Generate a UUID v7 (time-ordered, random).

    UUID v7 encodes a Unix timestamp in the high bits, making IDs
    naturally sortable by creation time — useful for audit trails.

    Uses a monotonic counter within the same millisecond to guarantee
    sort order even when called in rapid succession.
    """
    global _uuid7_counter, _uuid7_last_ms

    timestamp_ms = int(time.time() * 1000)
    if timestamp_ms == _uuid7_last_ms:
        _uuid7_counter += 1
    else:
        _uuid7_counter = 0
        _uuid7_last_ms = timestamp_ms

    # 48 bits of timestamp
    uuid_int = (timestamp_ms & 0xFFFFFFFFFFFF) << 80
    # Version 7
    uuid_int |= 0x7000 << 64
    # Variant 10
    uuid_int |= 0x8000000000000000
    # 12 bits of counter (bits 63-52 of lower half) + 50 bits random
    counter_bits = (_uuid7_counter & 0xFFF) << 50
    random_bits = uuid.uuid4().int & 0x3FFFFFFFFFFFF  # 50 bits
    uuid_int |= counter_bits | random_bits
    return str(uuid.UUID(int=uuid_int))


class MissionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass
class Mission:
    """A patrol/inspection/SAR mission definition."""

    id: str = field(default_factory=uuid7)
    type: str = "surveillance"
    status: MissionStatus = MissionStatus.DRAFT
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    created_by: str = ""
    waypoints: list[dict] = field(default_factory=list)
    parameters: dict = field(default_factory=lambda: {
        "altitude_m": 30.0,
        "speed_ms": 5.0,
        "camera_angle": -90.0,
        "loop": True,
        "detection_classes": ["person", "vehicle"],
    })

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Mission:
        data = data.copy()
        data["status"] = MissionStatus(data["status"])
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class Finding:
    """A detection event — signed at creation for tamper evidence."""

    id: str = field(default_factory=uuid7)
    mission_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0
    detection_class: str = ""
    confidence: float = 0.0
    image_path: str = ""
    image_hash: str = ""
    signature: str = ""  # Ed25519 signature, set by security layer

    def signable_payload(self) -> bytes:
        """The canonical byte string that gets signed.

        Includes all fields that matter for evidence integrity.
        Signature and id are excluded (signature is the output,
        id is assigned before signing).
        """
        parts = [
            self.mission_id,
            self.timestamp,
            f"{self.lat:.8f}",
            f"{self.lon:.8f}",
            f"{self.alt:.2f}",
            self.detection_class,
            f"{self.confidence:.4f}",
            self.image_hash,
        ]
        return "|".join(parts).encode("utf-8")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Finding:
        return cls(**data)

    @staticmethod
    def hash_image(image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes).hexdigest()


@dataclass
class AuditEntry:
    """Tamper-evident audit log entry.

    Each entry includes the hash of the previous entry, forming a
    hash chain. Deleting or modifying any entry breaks the chain.
    """

    id: str = field(default_factory=uuid7)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    actor: str = ""          # drone_id or operator_id
    action: str = ""         # e.g., "mission_start", "detection", "command_received"
    details: dict = field(default_factory=dict)
    prev_hash: str = ""      # SHA-256 of previous entry
    signature: str = ""      # Ed25519 signature

    def signable_payload(self) -> bytes:
        parts = [
            self.timestamp,
            self.actor,
            self.action,
            json.dumps(self.details, sort_keys=True),
            self.prev_hash,
        ]
        return "|".join(parts).encode("utf-8")

    def content_hash(self) -> str:
        """Hash of this entry's content, used as prev_hash for next entry."""
        payload = self.signable_payload() + self.signature.encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["details"] = json.dumps(self.details, sort_keys=True)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> AuditEntry:
        data = data.copy()
        if isinstance(data.get("details"), str):
            data["details"] = json.loads(data["details"])
        return cls(**data)

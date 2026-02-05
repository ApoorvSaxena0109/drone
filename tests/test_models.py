"""Tests for data models — UUID, serialization, signing payloads."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.models import uuid7, Mission, MissionStatus, Finding, AuditEntry


def test_uuid7_format():
    uid = uuid7()
    assert len(uid) == 36  # standard UUID format
    assert uid[14] == "7"  # version 7


def test_uuid7_sortable():
    """UUID v7 should be time-sortable."""
    ids = [uuid7() for _ in range(10)]
    assert ids == sorted(ids)


def test_mission_serialization():
    mission = Mission(
        created_by="test-operator",
        waypoints=[{"lat": 25.0, "lon": 121.5, "alt": 30}],
    )
    d = mission.to_dict()
    assert d["status"] == "draft"
    assert d["type"] == "surveillance"
    assert len(d["waypoints"]) == 1

    restored = Mission.from_dict(d)
    assert restored.id == mission.id
    assert restored.status == MissionStatus.DRAFT
    assert restored.waypoints == mission.waypoints


def test_mission_json_roundtrip():
    mission = Mission(created_by="test")
    json_str = mission.to_json()
    parsed = json.loads(json_str)
    restored = Mission.from_dict(parsed)
    assert restored.id == mission.id


def test_finding_signable_payload():
    f = Finding(
        mission_id="test-mission",
        lat=25.033964,
        lon=121.564468,
        alt=30.0,
        detection_class="person",
        confidence=0.85,
        image_hash="abc123",
    )
    payload = f.signable_payload()
    assert isinstance(payload, bytes)
    assert b"person" in payload
    assert b"abc123" in payload
    # Same finding should produce same payload
    assert payload == f.signable_payload()


def test_finding_hash_image():
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    h = Finding.hash_image(image_bytes)
    assert len(h) == 64  # SHA-256 hex
    # Same bytes, same hash
    assert h == Finding.hash_image(image_bytes)


def test_audit_entry_hash_chain():
    e1 = AuditEntry(
        actor="drone-1",
        action="test_action",
        details={"key": "value"},
        prev_hash="",
        signature="sig1",
    )
    h1 = e1.content_hash()
    assert len(h1) == 64

    e2 = AuditEntry(
        actor="drone-1",
        action="test_action_2",
        details={},
        prev_hash=h1,
        signature="sig2",
    )
    h2 = e2.content_hash()
    assert h2 != h1  # different entries have different hashes


def test_audit_entry_tamper_detection():
    """Changing any field should change the hash."""
    e = AuditEntry(
        actor="drone-1",
        action="test",
        details={"x": 1},
        prev_hash="abc",
        signature="sig",
    )
    original_hash = e.content_hash()

    # Tamper with details
    e.details = {"x": 2}
    assert e.content_hash() != original_hash

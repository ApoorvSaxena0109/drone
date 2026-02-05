"""Tests for SQLite data store."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.models import Mission, MissionStatus, Finding, AuditEntry
from core.data.store import DataStore


def _temp_store() -> DataStore:
    tmp_db = tempfile.mktemp(suffix=".db", prefix="test_store_")
    return DataStore(db_path=tmp_db)


def test_save_and_get_mission():
    store = _temp_store()
    mission = Mission(
        created_by="test-op",
        waypoints=[{"lat": 25.0, "lon": 121.5}],
    )
    store.save_mission(mission)

    loaded = store.get_mission(mission.id)
    assert loaded is not None
    assert loaded.id == mission.id
    assert loaded.status == MissionStatus.DRAFT
    assert loaded.created_by == "test-op"
    assert len(loaded.waypoints) == 1
    store.close()


def test_update_mission_status():
    store = _temp_store()
    mission = Mission(created_by="test")
    store.save_mission(mission)

    store.update_mission_status(mission.id, MissionStatus.ACTIVE)
    loaded = store.get_mission(mission.id)
    assert loaded.status == MissionStatus.ACTIVE

    store.update_mission_status(mission.id, MissionStatus.COMPLETED)
    loaded = store.get_mission(mission.id)
    assert loaded.status == MissionStatus.COMPLETED
    store.close()


def test_list_missions():
    store = _temp_store()
    for i in range(5):
        m = Mission(created_by=f"op-{i}")
        if i >= 3:
            m.status = MissionStatus.COMPLETED
        store.save_mission(m)

    all_missions = store.list_missions()
    assert len(all_missions) == 5

    draft_missions = store.list_missions(status=MissionStatus.DRAFT)
    assert len(draft_missions) == 3

    completed_missions = store.list_missions(status=MissionStatus.COMPLETED)
    assert len(completed_missions) == 2
    store.close()


def test_save_and_get_findings():
    store = _temp_store()
    mission = Mission(created_by="test")
    store.save_mission(mission)

    for i in range(3):
        f = Finding(
            mission_id=mission.id,
            lat=25.0 + i * 0.001,
            lon=121.5,
            alt=30.0,
            detection_class="person",
            confidence=0.9 - i * 0.1,
            image_hash="hash" + str(i),
        )
        store.save_finding(f)

    findings = store.get_findings(mission.id)
    assert len(findings) == 3
    assert store.get_finding_count(mission.id) == 3
    store.close()


def test_audit_log_chain():
    store = _temp_store()

    # First entry — prev_hash is empty (genesis)
    e1 = AuditEntry(
        actor="drone-1",
        action="boot",
        details={"version": "0.1.0"},
        prev_hash="",
        signature="sig1",
    )
    store.append_audit(e1)

    # Second entry — chain to first
    prev_hash = store.get_last_audit_hash()
    assert prev_hash != ""

    e2 = AuditEntry(
        actor="drone-1",
        action="mission_start",
        details={"mission": "abc"},
        prev_hash=prev_hash,
        signature="sig2",
    )
    store.append_audit(e2)

    # Verify chain
    valid, count = store.verify_audit_chain()
    assert valid
    assert count == 2

    # Get log
    entries = store.get_audit_log(limit=10)
    assert len(entries) == 2
    store.close()


def test_audit_chain_detects_tampering():
    store = _temp_store()

    e1 = AuditEntry(
        actor="drone-1", action="boot", prev_hash="", signature="sig1"
    )
    store.append_audit(e1)

    prev_hash = store.get_last_audit_hash()
    e2 = AuditEntry(
        actor="drone-1", action="test", prev_hash=prev_hash, signature="sig2"
    )
    store.append_audit(e2)

    # Tamper with first entry's action
    store._conn.execute(
        "UPDATE audit_log SET action = 'tampered' WHERE id = ?", (e1.id,)
    )
    store._conn.commit()

    # Chain should be broken at entry 1 (second entry's prev_hash won't match)
    valid, count = store.verify_audit_chain()
    assert not valid
    assert count == 1  # breaks at the second entry
    store.close()

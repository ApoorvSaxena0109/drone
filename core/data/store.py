"""SQLite-backed local data store.

All mission data, findings, and audit logs are stored locally on the drone.
No cloud dependency. Data is synced opportunistically when connectivity exists.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from core.data.models import Mission, MissionStatus, Finding, AuditEntry


class DataStore:
    """Local SQLite store for missions, findings, and audit trail."""

    def __init__(self, db_path: str = "/var/drone/missions.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                waypoints TEXT NOT NULL DEFAULT '[]',
                parameters TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS findings (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                alt REAL NOT NULL,
                detection_class TEXT NOT NULL,
                confidence REAL NOT NULL,
                image_path TEXT,
                image_hash TEXT,
                signature TEXT,
                FOREIGN KEY (mission_id) REFERENCES missions(id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                prev_hash TEXT NOT NULL,
                signature TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_findings_mission
                ON findings(mission_id);
            CREATE INDEX IF NOT EXISTS idx_findings_timestamp
                ON findings(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_log(timestamp);
        """)
        self._conn.commit()

    # ── Missions ──────────────────────────────────────────────

    def save_mission(self, mission: Mission) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO missions
               (id, type, status, created_at, created_by, waypoints, parameters)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mission.id,
                mission.type,
                mission.status.value,
                mission.created_at,
                mission.created_by,
                json.dumps(mission.waypoints),
                json.dumps(mission.parameters),
            ),
        )
        self._conn.commit()

    def get_mission(self, mission_id: str) -> Optional[Mission]:
        row = self._conn.execute(
            "SELECT * FROM missions WHERE id = ?", (mission_id,)
        ).fetchone()
        if not row:
            return None
        return Mission(
            id=row["id"],
            type=row["type"],
            status=MissionStatus(row["status"]),
            created_at=row["created_at"],
            created_by=row["created_by"],
            waypoints=json.loads(row["waypoints"]),
            parameters=json.loads(row["parameters"]),
        )

    def update_mission_status(self, mission_id: str, status: MissionStatus) -> None:
        self._conn.execute(
            "UPDATE missions SET status = ? WHERE id = ?",
            (status.value, mission_id),
        )
        self._conn.commit()

    def list_missions(self, status: Optional[MissionStatus] = None) -> list[Mission]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM missions WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM missions ORDER BY created_at DESC"
            ).fetchall()
        return [
            Mission(
                id=r["id"],
                type=r["type"],
                status=MissionStatus(r["status"]),
                created_at=r["created_at"],
                created_by=r["created_by"],
                waypoints=json.loads(r["waypoints"]),
                parameters=json.loads(r["parameters"]),
            )
            for r in rows
        ]

    # ── Findings ──────────────────────────────────────────────

    def save_finding(self, finding: Finding) -> None:
        self._conn.execute(
            """INSERT INTO findings
               (id, mission_id, timestamp, lat, lon, alt,
                detection_class, confidence, image_path, image_hash, signature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                finding.id,
                finding.mission_id,
                finding.timestamp,
                finding.lat,
                finding.lon,
                finding.alt,
                finding.detection_class,
                finding.confidence,
                finding.image_path,
                finding.image_hash,
                finding.signature,
            ),
        )
        self._conn.commit()

    def get_findings(self, mission_id: str) -> list[Finding]:
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE mission_id = ? ORDER BY timestamp",
            (mission_id,),
        ).fetchall()
        return [Finding(**dict(r)) for r in rows]

    def get_finding_count(self, mission_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM findings WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        return row["cnt"]

    # ── Audit Log ─────────────────────────────────────────────

    def append_audit(self, entry: AuditEntry) -> None:
        self._conn.execute(
            """INSERT INTO audit_log
               (id, timestamp, actor, action, details, prev_hash, signature)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id,
                entry.timestamp,
                entry.actor,
                entry.action,
                json.dumps(entry.details, sort_keys=True),
                entry.prev_hash,
                entry.signature,
            ),
        )
        self._conn.commit()

    def get_last_audit_hash(self) -> str:
        """Get the content hash of the most recent audit entry.

        Returns empty string if no entries exist (genesis).
        """
        row = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not row:
            return ""
        entry = AuditEntry.from_dict(dict(row))
        return entry.content_hash()

    def get_audit_log(self, limit: int = 100) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [AuditEntry.from_dict(dict(r)) for r in rows]

    def verify_audit_chain(self) -> tuple[bool, int]:
        """Verify the entire audit hash chain.

        Returns (is_valid, entries_checked). If invalid, entries_checked
        indicates where the chain broke.
        """
        rows = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp ASC"
        ).fetchall()
        if not rows:
            return True, 0

        prev_hash = ""
        for i, row in enumerate(rows):
            entry = AuditEntry.from_dict(dict(row))
            if entry.prev_hash != prev_hash:
                return False, i
            prev_hash = entry.content_hash()
        return True, len(rows)

    def close(self) -> None:
        self._conn.close()

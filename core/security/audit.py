"""Tamper-evident audit logging.

Every significant action (command received, mission state change,
detection event) gets logged with a cryptographic signature and
hash chain. Deleting or modifying entries breaks the chain.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.models import AuditEntry
from core.data.store import DataStore
from core.security.crypto import CryptoEngine

logger = logging.getLogger(__name__)


class AuditLogger:
    """Append-only, tamper-evident audit log backed by SQLite."""

    def __init__(self, store: DataStore, crypto: CryptoEngine, actor_id: str):
        self._store = store
        self._crypto = crypto
        self._actor_id = actor_id

    def log(self, action: str, details: Optional[dict] = None) -> AuditEntry:
        """Create a signed, hash-chained audit entry.

        Args:
            action: Action identifier (e.g., "mission_start", "detection").
            details: Arbitrary JSON-serializable details.

        Returns:
            The created audit entry.
        """
        prev_hash = self._store.get_last_audit_hash()

        entry = AuditEntry(
            actor=self._actor_id,
            action=action,
            details=details or {},
            prev_hash=prev_hash,
        )

        # Sign the entry
        signature = self._crypto.sign_data(entry.signable_payload())
        entry.signature = signature

        self._store.append_audit(entry)
        logger.debug("Audit: %s | %s | %s", entry.action, entry.actor, entry.id)
        return entry

    def verify_chain(self) -> tuple[bool, int]:
        """Verify the entire audit log hash chain.

        Returns (is_valid, entries_verified).
        """
        return self._store.verify_audit_chain()

    def get_recent(self, limit: int = 50) -> list[AuditEntry]:
        """Get recent audit entries."""
        return self._store.get_audit_log(limit=limit)

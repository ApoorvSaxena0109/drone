"""Cryptographic utilities for the drone platform.

Provides signing, verification, encryption, and hashing.
Uses Ed25519 for signatures, AES-256-GCM for encryption.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.security.identity import DroneIdentity


class CryptoEngine:
    """Cryptographic operations bound to a drone identity."""

    def __init__(self, identity: DroneIdentity):
        self._identity = identity

    def sign_data(self, data: bytes) -> str:
        """Sign data and return base64-encoded signature."""
        raw_sig = self._identity.sign(data)
        return base64.b64encode(raw_sig).decode("ascii")

    def verify_signature(self, data: bytes, signature_b64: str) -> bool:
        """Verify a base64-encoded signature."""
        try:
            raw_sig = base64.b64decode(signature_b64)
            return self._identity.verify(data, raw_sig)
        except Exception:
            return False

    def verify_command(
        self,
        payload: dict,
        operator_id: str,
        api_key: str,
        provided_hmac: str,
        max_age_s: int = 30,
    ) -> tuple[bool, str]:
        """Verify an operator command.

        Checks:
        1. Operator API key is valid
        2. HMAC of payload matches
        3. Timestamp is within max_age_s (replay protection)

        Returns (is_valid, reason).
        """
        # Verify operator identity
        if not self._identity.verify_operator(operator_id, api_key):
            return False, "invalid_operator"

        # Verify timestamp freshness
        import json
        timestamp = payload.get("timestamp", "")
        try:
            from datetime import datetime, timezone
            cmd_time = datetime.fromisoformat(timestamp)
            now = datetime.now(timezone.utc)
            age = abs((now - cmd_time).total_seconds())
            if age > max_age_s:
                return False, f"command_expired (age={age:.1f}s)"
        except (ValueError, TypeError):
            return False, "invalid_timestamp"

        # Verify HMAC
        payload_bytes = json.dumps(payload, sort_keys=True).encode()
        expected = hmac.new(
            api_key.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, provided_hmac):
            return False, "invalid_hmac"

        return True, "ok"

    @staticmethod
    def encrypt_data(plaintext: bytes, key: Optional[bytes] = None) -> tuple[bytes, bytes]:
        """Encrypt data with AES-256-GCM.

        If no key is provided, generates a random one.
        Returns (ciphertext_with_nonce, key).
        The first 12 bytes of ciphertext_with_nonce are the nonce.
        """
        if key is None:
            key = AESGCM.generate_key(bit_length=256)
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ciphertext, key

    @staticmethod
    def decrypt_data(ciphertext_with_nonce: bytes, key: bytes) -> bytes:
        """Decrypt AES-256-GCM encrypted data."""
        nonce = ciphertext_with_nonce[:12]
        ciphertext = ciphertext_with_nonce[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)

    @staticmethod
    def hash_file(file_path: str) -> str:
        """SHA-256 hash of a file."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

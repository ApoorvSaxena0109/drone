"""Drone identity management.

Each drone has a unique Ed25519 keypair generated at provisioning time.
The private key never leaves the device. The public key is shared with
ground stations for verification.

Identity is bound to hardware via a fingerprint derived from CPU serial
and MAC address, making it non-transferable between devices.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

from core.data.models import uuid7


class DroneIdentity:
    """Manages drone identity: keypair, hardware binding, operator keys."""

    def __init__(self, identity_dir: str = "/etc/drone/identity"):
        self._dir = Path(identity_dir)
        self._drone_id: Optional[str] = None
        self._private_key: Optional[Ed25519PrivateKey] = None
        self._public_key: Optional[Ed25519PublicKey] = None
        self._hardware_fingerprint: Optional[str] = None
        self._operator_keys: dict[str, str] = {}  # user_id -> api_key_hash

        if self._dir.exists() and (self._dir / "drone_id").exists():
            self._load()

    @property
    def drone_id(self) -> str:
        if not self._drone_id:
            raise RuntimeError("Drone not provisioned. Run 'drone-cli provision' first.")
        return self._drone_id

    @property
    def is_provisioned(self) -> bool:
        return self._drone_id is not None

    def provision(self, org_id: str = "zypher-prototype") -> dict:
        """Provision a new drone identity.

        Generates keypair, computes hardware fingerprint, stores everything.
        Returns public identity info for registering with ground station.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        # Generate identity
        self._drone_id = uuid7()
        self._private_key = Ed25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()
        self._hardware_fingerprint = self._compute_hardware_fingerprint()

        # Save drone ID
        (self._dir / "drone_id").write_text(self._drone_id)

        # Save private key (owner-only permissions)
        key_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_path = self._dir / "drone_key.pem"
        key_path.write_bytes(key_pem)
        os.chmod(key_path, 0o600)

        # Save public key
        pub_pem = self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        (self._dir / "drone_key_pub.pem").write_bytes(pub_pem)

        # Save hardware fingerprint
        (self._dir / "hardware_fingerprint").write_text(self._hardware_fingerprint)

        # Save org binding
        (self._dir / "org_id").write_text(org_id)

        # Generate initial operator API key
        operator_id = uuid7()
        api_key = os.urandom(32).hex()
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        self._operator_keys[operator_id] = api_key_hash
        self._save_operator_keys()

        return {
            "drone_id": self._drone_id,
            "org_id": org_id,
            "public_key_pem": pub_pem.decode(),
            "hardware_fingerprint": self._hardware_fingerprint,
            "operator_id": operator_id,
            "operator_api_key": api_key,  # display once, never stored in plaintext
        }

    def _load(self) -> None:
        """Load existing identity from disk."""
        self._drone_id = (self._dir / "drone_id").read_text().strip()

        key_pem = (self._dir / "drone_key.pem").read_bytes()
        self._private_key = serialization.load_pem_private_key(key_pem, password=None)
        self._public_key = self._private_key.public_key()

        fp_path = self._dir / "hardware_fingerprint"
        if fp_path.exists():
            self._hardware_fingerprint = fp_path.read_text().strip()

        ops_path = self._dir / "operators.json"
        if ops_path.exists():
            self._operator_keys = json.loads(ops_path.read_text())

    def sign(self, data: bytes) -> bytes:
        """Sign data with the drone's private key."""
        if not self._private_key:
            raise RuntimeError("Drone not provisioned.")
        return self._private_key.sign(data)

    def verify(self, data: bytes, signature: bytes) -> bool:
        """Verify a signature using the drone's public key."""
        if not self._public_key:
            raise RuntimeError("Drone not provisioned.")
        try:
            self._public_key.verify(signature, data)
            return True
        except Exception:
            return False

    def verify_operator(self, operator_id: str, api_key: str) -> bool:
        """Verify an operator's API key."""
        expected_hash = self._operator_keys.get(operator_id)
        if not expected_hash:
            return False
        provided_hash = hashlib.sha256(api_key.encode()).hexdigest()
        return provided_hash == expected_hash

    def add_operator(self, operator_id: str, api_key: str) -> None:
        """Register a new operator API key."""
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        self._operator_keys[operator_id] = api_key_hash
        self._save_operator_keys()

    def _save_operator_keys(self) -> None:
        ops_path = self._dir / "operators.json"
        ops_path.write_text(json.dumps(self._operator_keys, indent=2))
        os.chmod(ops_path, 0o600)

    @staticmethod
    def _compute_hardware_fingerprint() -> str:
        """Derive a hardware fingerprint from CPU serial + MAC address.

        On Jetson, reads /proc/device-tree/serial-number.
        Falls back to a UUID-based fingerprint for dev/testing.
        """
        parts = []

        # Try Jetson CPU serial
        serial_path = Path("/proc/device-tree/serial-number")
        if serial_path.exists():
            parts.append(serial_path.read_text().strip().strip("\x00"))
        else:
            # Fallback: /etc/machine-id (Linux) or generate one
            machine_id_path = Path("/etc/machine-id")
            if machine_id_path.exists():
                parts.append(machine_id_path.read_text().strip())
            else:
                parts.append(str(uuid.uuid4()))

        # Primary network interface MAC
        net_path = Path("/sys/class/net")
        if net_path.exists():
            for iface_dir in sorted(net_path.iterdir()):
                if iface_dir.name == "lo":
                    continue
                mac_path = iface_dir / "address"
                if mac_path.exists():
                    mac = mac_path.read_text().strip()
                    if mac != "00:00:00:00:00:00":
                        parts.append(mac)
                        break

        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()

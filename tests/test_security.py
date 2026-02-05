"""Tests for security layer — identity, signing, verification."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.security.identity import DroneIdentity
from core.security.crypto import CryptoEngine
from core.data.models import Finding


def test_provision_creates_identity():
    tmp_dir = tempfile.mkdtemp(prefix="test_identity_")
    identity = DroneIdentity(identity_dir=tmp_dir)
    assert not identity.is_provisioned

    result = identity.provision(org_id="test-org")
    assert identity.is_provisioned
    assert "drone_id" in result
    assert "operator_api_key" in result
    assert "public_key_pem" in result

    # Files created
    assert (Path(tmp_dir) / "drone_id").exists()
    assert (Path(tmp_dir) / "drone_key.pem").exists()
    assert (Path(tmp_dir) / "drone_key_pub.pem").exists()

    # Private key permissions
    key_stat = os.stat(Path(tmp_dir) / "drone_key.pem")
    assert oct(key_stat.st_mode)[-3:] == "600"


def test_identity_reload():
    """Identity should persist across restarts."""
    tmp_dir = tempfile.mkdtemp(prefix="test_identity_")
    identity1 = DroneIdentity(identity_dir=tmp_dir)
    result = identity1.provision(org_id="test-org")
    drone_id = result["drone_id"]

    # Simulate restart — load from disk
    identity2 = DroneIdentity(identity_dir=tmp_dir)
    assert identity2.is_provisioned
    assert identity2.drone_id == drone_id


def test_sign_and_verify():
    tmp_dir = tempfile.mkdtemp(prefix="test_identity_")
    identity = DroneIdentity(identity_dir=tmp_dir)
    identity.provision()

    data = b"test message for signing"
    signature = identity.sign(data)
    assert identity.verify(data, signature)

    # Tampered data should fail
    assert not identity.verify(b"tampered message", signature)


def test_crypto_engine_sign_verify():
    tmp_dir = tempfile.mkdtemp(prefix="test_identity_")
    identity = DroneIdentity(identity_dir=tmp_dir)
    identity.provision()
    crypto = CryptoEngine(identity)

    data = b"important finding data"
    sig_b64 = crypto.sign_data(data)
    assert isinstance(sig_b64, str)
    assert crypto.verify_signature(data, sig_b64)
    assert not crypto.verify_signature(b"wrong data", sig_b64)


def test_operator_verification():
    tmp_dir = tempfile.mkdtemp(prefix="test_identity_")
    identity = DroneIdentity(identity_dir=tmp_dir)
    result = identity.provision()

    operator_id = result["operator_id"]
    api_key = result["operator_api_key"]

    assert identity.verify_operator(operator_id, api_key)
    assert not identity.verify_operator(operator_id, "wrong-key")
    assert not identity.verify_operator("wrong-id", api_key)


def test_encrypt_decrypt():
    plaintext = b"sensitive mission data that must be encrypted"
    ciphertext, key = CryptoEngine.encrypt_data(plaintext)

    assert ciphertext != plaintext
    decrypted = CryptoEngine.decrypt_data(ciphertext, key)
    assert decrypted == plaintext


def test_finding_signature_flow():
    """End-to-end: create finding, sign it, verify it."""
    tmp_dir = tempfile.mkdtemp(prefix="test_identity_")
    identity = DroneIdentity(identity_dir=tmp_dir)
    identity.provision()
    crypto = CryptoEngine(identity)

    finding = Finding(
        mission_id="mission-123",
        lat=25.033964,
        lon=121.564468,
        alt=30.0,
        detection_class="person",
        confidence=0.92,
        image_hash="a" * 64,
    )

    # Sign
    sig = crypto.sign_data(finding.signable_payload())
    finding.signature = sig

    # Verify
    assert crypto.verify_signature(finding.signable_payload(), finding.signature)

    # Tamper with finding
    finding.confidence = 0.50
    assert not crypto.verify_signature(finding.signable_payload(), sig)

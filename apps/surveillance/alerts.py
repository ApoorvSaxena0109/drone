"""Alert management for surveillance missions.

Handles detection-to-alert pipeline:
1. Receives detections from the vision layer
2. Applies cooldown logic (avoid spam for same area)
3. Signs and stores findings
4. Publishes alerts via MQTT

Every alert is cryptographically signed with the drone's identity,
creating a tamper-evident chain from detection to delivery.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from core.comms.mqtt_client import MQTTClient
from core.data.models import Finding
from core.data.store import DataStore
from core.security.audit import AuditLogger
from core.security.crypto import CryptoEngine
from core.vision.detector import Detection

logger = logging.getLogger(__name__)


class AlertManager:
    """Manages detection alerts with deduplication and signing."""

    def __init__(
        self,
        store: DataStore,
        crypto: CryptoEngine,
        audit: AuditLogger,
        mqtt_client: Optional[MQTTClient],
        mission_id: str,
        detections_dir: str = "/var/drone/detections",
        cooldown_s: float = 30.0,
    ):
        self._store = store
        self._crypto = crypto
        self._audit = audit
        self._mqtt = mqtt_client
        self._mission_id = mission_id
        self._detections_dir = Path(detections_dir)
        self._cooldown_s = cooldown_s
        self._last_alert_time: dict[str, float] = {}  # class_name -> timestamp

        self._detections_dir.mkdir(parents=True, exist_ok=True)

    def process_detections(
        self,
        detections: list[Detection],
        frame: np.ndarray,
        lat: float,
        lon: float,
        alt: float,
    ) -> list[Finding]:
        """Process raw detections into signed findings and alerts.

        Args:
            detections: List of detections from the vision layer.
            frame: The original camera frame.
            lat, lon, alt: Current drone GPS position.

        Returns:
            List of new findings that passed the cooldown filter.
        """
        findings = []
        now = time.time()

        for det in detections:
            # Cooldown check — avoid alerting on the same thing repeatedly
            last_time = self._last_alert_time.get(det.class_name, 0)
            if now - last_time < self._cooldown_s:
                continue
            self._last_alert_time[det.class_name] = now

            # Save detection frame
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            image_filename = f"{det.class_name}_{timestamp_str}.jpg"
            image_path = self._detections_dir / image_filename

            # Crop and save detection region with some padding
            h, w = frame.shape[:2]
            pad = 50
            crop = frame[
                max(0, det.y1 - pad):min(h, det.y2 + pad),
                max(0, det.x1 - pad):min(w, det.x2 + pad),
            ]
            cv2.imwrite(str(image_path), crop)

            # Hash the full frame for evidence integrity
            _, frame_bytes = cv2.imencode(".jpg", frame)
            image_hash = Finding.hash_image(frame_bytes.tobytes())

            # Create finding
            finding = Finding(
                mission_id=self._mission_id,
                lat=lat,
                lon=lon,
                alt=alt,
                detection_class=det.class_name,
                confidence=det.confidence,
                image_path=str(image_path),
                image_hash=image_hash,
            )

            # Sign the finding
            signature = self._crypto.sign_data(finding.signable_payload())
            finding.signature = signature

            # Store locally
            self._store.save_finding(finding)

            # Audit log
            self._audit.log("detection", {
                "finding_id": finding.id,
                "class": det.class_name,
                "confidence": round(det.confidence, 3),
                "location": [lat, lon, alt],
            })

            # Publish MQTT alert
            if self._mqtt and self._mqtt.is_connected:
                alert_payload = {
                    "finding_id": finding.id,
                    "mission_id": self._mission_id,
                    "timestamp": finding.timestamp,
                    "detection_class": det.class_name,
                    "confidence": round(det.confidence, 3),
                    "location": {"lat": lat, "lon": lon, "alt": alt},
                    "image_hash": image_hash,
                    "signature": signature,
                }
                self._mqtt.publish_alert(alert_payload)

            findings.append(finding)
            logger.info(
                "ALERT: %s (%.1f%%) at %.6f, %.6f",
                det.class_name, det.confidence * 100, lat, lon,
            )

        return findings

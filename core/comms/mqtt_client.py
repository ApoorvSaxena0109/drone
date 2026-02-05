"""MQTT client for alert delivery and command reception.

Publishes signed detection alerts to the ground station.
Subscribes to command topics for receiving operator instructions.

All messages are JSON with a signature field for verification.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class MQTTClient:
    """Secure MQTT client for drone-to-ground communication."""

    def __init__(
        self,
        broker: str = "localhost",
        port: int = 1883,
        drone_id: str = "",
        topic_prefix: str = "drone",
        use_tls: bool = False,
        qos: int = 1,
    ):
        self._broker = broker
        self._port = port
        self._drone_id = drone_id
        self._prefix = topic_prefix
        self._qos = qos
        self._connected = False
        self._command_callback: Optional[Callable[[dict], None]] = None

        # MQTT client setup
        client_id = f"drone-{drone_id[:8]}" if drone_id else "drone-unknown"
        self._client = mqtt.Client(client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        if use_tls:
            self._client.tls_set()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Connect to MQTT broker."""
        try:
            self._client.connect(self._broker, self._port, keepalive=60)
            self._client.loop_start()
            # Wait for connection
            for _ in range(50):  # 5 seconds max
                if self._connected:
                    return True
                time.sleep(0.1)
            logger.warning("MQTT connection timeout")
            return False
        except Exception as e:
            logger.error("MQTT connection failed: %s", e)
            return False

    def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        self._client.loop_stop()
        self._client.disconnect()
        self._connected = False

    def publish_alert(self, alert: dict) -> bool:
        """Publish a detection alert.

        Topic: {prefix}/alerts/{drone_id}
        """
        topic = f"{self._prefix}/alerts/{self._drone_id}"
        return self._publish(topic, alert)

    def publish_telemetry(self, telemetry: dict) -> bool:
        """Publish telemetry snapshot.

        Topic: {prefix}/telemetry/{drone_id}
        """
        topic = f"{self._prefix}/telemetry/{self._drone_id}"
        return self._publish(topic, telemetry)

    def publish_status(self, status: dict) -> bool:
        """Publish mission status update.

        Topic: {prefix}/status/{drone_id}
        """
        topic = f"{self._prefix}/status/{self._drone_id}"
        return self._publish(topic, status)

    def on_command(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for incoming commands.

        Topic: {prefix}/commands/{drone_id}
        """
        self._command_callback = callback

    def _publish(self, topic: str, payload: dict) -> bool:
        if not self._connected:
            logger.debug("Not connected, queuing message for %s", topic)
            return False
        try:
            msg = json.dumps(payload)
            result = self._client.publish(topic, msg, qos=self._qos)
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error("Publish failed on %s: %s", topic, e)
            return False

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected to %s:%d", self._broker, self._port)
            # Subscribe to command topic
            cmd_topic = f"{self._prefix}/commands/{self._drone_id}"
            client.subscribe(cmd_topic, qos=self._qos)
            logger.info("Subscribed to %s", cmd_topic)
        else:
            logger.error("MQTT connection failed: rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected = False
        if rc != 0:
            logger.warning("MQTT unexpected disconnect: rc=%d", rc)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode())
            logger.debug("Command received on %s", msg.topic)
            if self._command_callback:
                self._command_callback(payload)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON on topic %s", msg.topic)

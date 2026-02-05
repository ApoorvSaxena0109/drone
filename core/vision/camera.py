"""Camera capture for Jetson and standard USB/file sources.

Supports:
- Jetson CSI cameras via GStreamer pipeline (hardware-accelerated)
- USB webcams via device index
- Video files for testing

Frames are captured in a background thread to avoid blocking
the main processing loop.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Union

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# GStreamer pipeline for Jetson CSI camera (IMX219/IMX477)
JETSON_CSI_PIPELINE = (
    "nvarguscamerasrc ! "
    "video/x-raw(memory:NVMM),width={width},height={height},"
    "framerate={fps}/1 ! "
    "nvvidconv ! video/x-raw,format=BGRx ! "
    "videoconvert ! video/x-raw,format=BGR ! appsink"
)


class Camera:
    """Thread-safe camera capture with frame buffering."""

    def __init__(
        self,
        source: Union[int, str] = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
    ):
        """Initialize camera.

        Args:
            source: Camera device index (int), GStreamer pipeline (str),
                    or video file path (str).
            width: Frame width.
            height: Frame height.
            fps: Target framerate.
        """
        self._source = source
        self._width = width
        self._height = height
        self._fps = fps
        self._cap: Optional[cv2.VideoCapture] = None
        self._frame: Optional[np.ndarray] = None
        self._frame_id: int = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @property
    def frame_id(self) -> int:
        return self._frame_id

    def open(self) -> bool:
        """Open the camera source."""
        source = self._source

        # Build GStreamer pipeline for Jetson CSI
        if isinstance(source, str) and "nvarguscamerasrc" in source:
            pipeline = source
        elif isinstance(source, str) and source.startswith("csi:"):
            pipeline = JETSON_CSI_PIPELINE.format(
                width=self._width, height=self._height, fps=self._fps
            )
            source = pipeline
        else:
            pipeline = None

        if pipeline:
            self._cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        elif isinstance(source, int):
            self._cap = cv2.VideoCapture(source)
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            self._cap.set(cv2.CAP_PROP_FPS, self._fps)
        else:
            # Video file or other string source
            self._cap = cv2.VideoCapture(source)

        if not self._cap.isOpened():
            logger.error("Failed to open camera source: %s", self._source)
            return False

        logger.info(
            "Camera opened: %s (%.0fx%.0f @ %.0ffps)",
            self._source,
            self._cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            self._cap.get(cv2.CAP_PROP_FPS),
        )
        return True

    def start(self) -> None:
        """Start background capture thread."""
        if self._running:
            return
        if not self.is_open and not self.open():
            raise RuntimeError("Cannot start capture — camera not open")
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info("Camera capture started")

    def stop(self) -> None:
        """Stop background capture."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._cap:
            self._cap.release()
            self._cap = None
        logger.info("Camera capture stopped")

    def read(self) -> tuple[bool, Optional[np.ndarray], int]:
        """Get the latest frame.

        Returns:
            (success, frame, frame_id). Frame is None if no frame available.
        """
        with self._lock:
            if self._frame is None:
                return False, None, 0
            return True, self._frame.copy(), self._frame_id

    def _capture_loop(self) -> None:
        """Background thread: continuously reads frames."""
        frame_interval = 1.0 / self._fps
        while self._running and self._cap and self._cap.isOpened():
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame
                    self._frame_id += 1
            else:
                logger.warning("Frame capture failed, retrying...")
                time.sleep(0.1)
            time.sleep(frame_interval * 0.5)  # slight sleep to prevent CPU spin

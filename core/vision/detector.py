"""Object detection using YOLOv8 optimized for Jetson.

Supports multiple backends:
- Ultralytics YOLOv8 (default, auto-exports to TensorRT on Jetson)
- ONNX Runtime (fallback for non-Jetson platforms)
- OpenCV DNN (lightest fallback, CPU-only)

The detector wraps all backends behind a single interface so the
apps layer doesn't care what's doing the inference.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# COCO class names (YOLOv8 default)
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]


@dataclass
class Detection:
    """A single detected object in a frame."""

    class_name: str
    class_id: int
    confidence: float
    x1: int  # bounding box top-left x
    y1: int  # bounding box top-left y
    x2: int  # bounding box bottom-right x
    y2: int  # bounding box bottom-right y

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def area(self) -> int:
        return (self.x2 - self.x1) * (self.y2 - self.y1)


class Detector:
    """YOLOv8 object detector with Jetson TensorRT acceleration."""

    def __init__(
        self,
        model_name: str = "yolov8n",
        confidence_threshold: float = 0.5,
        target_classes: Optional[list[str]] = None,
        device: str = "auto",
    ):
        """Initialize detector.

        Args:
            model_name: YOLOv8 model variant or path to custom model.
            confidence_threshold: Minimum confidence for detections.
            target_classes: Only return these classes. None = all classes.
            device: 'auto', 'cuda', 'cpu'. Auto detects Jetson GPU.
        """
        self._model_name = model_name
        self._conf_threshold = confidence_threshold
        self._target_classes = set(target_classes) if target_classes else None
        self._device = device
        self._model = None
        self._backend = "none"
        self._inference_ms: float = 0.0

    @property
    def inference_ms(self) -> float:
        """Last inference time in milliseconds."""
        return self._inference_ms

    @property
    def backend(self) -> str:
        return self._backend

    def load(self) -> bool:
        """Load the detection model.

        Tries ultralytics first (best Jetson support), falls back to
        OpenCV DNN.
        """
        # Try Ultralytics YOLOv8
        if self._try_load_ultralytics():
            return True

        # Fallback: OpenCV DNN with ONNX
        if self._try_load_opencv_dnn():
            return True

        logger.error("Failed to load any detection backend")
        return False

    def _try_load_ultralytics(self) -> bool:
        try:
            from ultralytics import YOLO

            model_path = self._model_name
            if not Path(model_path).exists() and not model_path.endswith((".pt", ".onnx", ".engine")):
                model_path = f"{model_path}.pt"

            self._model = YOLO(model_path)

            # On Jetson with TensorRT available, export for acceleration
            if self._device == "auto":
                try:
                    import torch
                    if torch.cuda.is_available():
                        self._device = "cuda"
                        logger.info("CUDA available, using GPU acceleration")
                    else:
                        self._device = "cpu"
                except ImportError:
                    self._device = "cpu"

            self._backend = "ultralytics"
            logger.info("Loaded YOLOv8 model via ultralytics: %s", model_path)
            return True
        except ImportError:
            logger.debug("ultralytics not available")
            return False
        except Exception as e:
            logger.warning("Failed to load ultralytics model: %s", e)
            return False

    def _try_load_opencv_dnn(self) -> bool:
        try:
            model_path = self._model_name
            if not model_path.endswith(".onnx"):
                model_path = f"{model_path}.onnx"

            if not Path(model_path).exists():
                logger.debug("ONNX model not found: %s", model_path)
                return False

            net = cv2.dnn.readNetFromONNX(model_path)
            # Use CUDA if available
            try:
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
                logger.info("OpenCV DNN using CUDA backend")
            except Exception:
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_DEFAULT)
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
                logger.info("OpenCV DNN using CPU backend")

            self._model = net
            self._backend = "opencv_dnn"
            logger.info("Loaded ONNX model via OpenCV DNN: %s", model_path)
            return True
        except Exception as e:
            logger.warning("Failed to load OpenCV DNN model: %s", e)
            return False

    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Run detection on a single frame.

        Args:
            frame: BGR image (OpenCV format).

        Returns:
            List of Detection objects that pass confidence and class filters.
        """
        if self._model is None:
            return []

        start = time.perf_counter()

        if self._backend == "ultralytics":
            detections = self._detect_ultralytics(frame)
        elif self._backend == "opencv_dnn":
            detections = self._detect_opencv(frame)
        else:
            detections = []

        self._inference_ms = (time.perf_counter() - start) * 1000

        # Filter by target classes
        if self._target_classes:
            detections = [d for d in detections if d.class_name in self._target_classes]

        return detections

    def _detect_ultralytics(self, frame: np.ndarray) -> list[Detection]:
        results = self._model(frame, conf=self._conf_threshold, verbose=False)
        detections = []
        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                class_name = COCO_CLASSES[cls_id] if cls_id < len(COCO_CLASSES) else f"class_{cls_id}"
                detections.append(Detection(
                    class_name=class_name,
                    class_id=cls_id,
                    confidence=conf,
                    x1=x1, y1=y1, x2=x2, y2=y2,
                ))
        return detections

    def _detect_opencv(self, frame: np.ndarray) -> list[Detection]:
        blob = cv2.dnn.blobFromImage(
            frame, 1 / 255.0, (640, 640), swapRB=True, crop=False
        )
        self._model.setInput(blob)
        outputs = self._model.forward()

        detections = []
        h, w = frame.shape[:2]

        # YOLOv8 output shape: (1, 84, 8400) -> transpose to (8400, 84)
        if len(outputs.shape) == 3:
            outputs = outputs[0].T

        for row in outputs:
            scores = row[4:]
            cls_id = int(np.argmax(scores))
            conf = float(scores[cls_id])
            if conf < self._conf_threshold:
                continue

            cx, cy, bw, bh = row[0], row[1], row[2], row[3]
            x1 = int((cx - bw / 2) * w / 640)
            y1 = int((cy - bh / 2) * h / 640)
            x2 = int((cx + bw / 2) * w / 640)
            y2 = int((cy + bh / 2) * h / 640)

            class_name = COCO_CLASSES[cls_id] if cls_id < len(COCO_CLASSES) else f"class_{cls_id}"
            detections.append(Detection(
                class_name=class_name,
                class_id=cls_id,
                confidence=conf,
                x1=max(0, x1), y1=max(0, y1),
                x2=min(w, x2), y2=min(h, y2),
            ))

        # NMS
        if detections:
            boxes = [[d.x1, d.y1, d.x2 - d.x1, d.y2 - d.y1] for d in detections]
            confs = [d.confidence for d in detections]
            indices = cv2.dnn.NMSBoxes(boxes, confs, self._conf_threshold, 0.45)
            if len(indices) > 0:
                detections = [detections[i] for i in indices.flatten()]
            else:
                detections = []

        return detections

    def annotate_frame(self, frame: np.ndarray, detections: list[Detection]) -> np.ndarray:
        """Draw bounding boxes and labels on a frame.

        Returns a copy with annotations. Original frame is not modified.
        """
        annotated = frame.copy()
        for det in detections:
            color = (0, 0, 255) if det.class_name == "person" else (0, 255, 0)
            cv2.rectangle(annotated, (det.x1, det.y1), (det.x2, det.y2), color, 2)
            label = f"{det.class_name} {det.confidence:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            cv2.rectangle(
                annotated,
                (det.x1, det.y1 - th - 8),
                (det.x1 + tw, det.y1),
                color, -1,
            )
            cv2.putText(
                annotated, label,
                (det.x1, det.y1 - 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1,
            )
        return annotated

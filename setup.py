"""Compatibility shim for older pip/setuptools.

Duplicates pyproject.toml metadata so that `pip install .` and
`pip install -e .` work on systems with pip < 22 or setuptools < 64
that cannot read PEP 621 project metadata from pyproject.toml.

The canonical source of truth remains pyproject.toml — update both
files if you change dependencies or metadata.
"""
from setuptools import setup, find_packages

setup(
    name="drone-platform",
    version="0.1.0",
    description="Security-first drone software platform for Jetson and x86_64 Linux",
    python_requires=">=3.8",
    packages=find_packages(include=["core*", "apps*", "tools*"]),
    py_modules=["cli"],
    install_requires=[
        "pymavlink>=2.4.40",
        "opencv-python>=4.8.0",
        "paho-mqtt>=1.6.1",
        "cryptography>=41.0.0",
        "click>=8.1.0",
        "pyyaml>=6.0",
        "numpy>=1.24.0",
        "rich>=13.0.0",
    ],
    extras_require={
        "jetson": [
            "onnxruntime-gpu>=1.16.0",
            "ultralytics>=8.0.0",
        ],
        "laptop": [
            "ultralytics>=8.0.0",
            "onnxruntime>=1.16.0",
        ],
        "vision": [
            "ultralytics>=8.0.0",
        ],
        "sim": [
            "dronekit-sitl>=3.3.0",
        ],
        "dev": [
            "pytest>=7.0",
            "pytest-asyncio>=0.21",
        ],
    },
    entry_points={
        "console_scripts": [
            "drone-cli=cli:main",
        ],
    },
)

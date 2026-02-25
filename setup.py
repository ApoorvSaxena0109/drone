"""Shim for editable installs on older setuptools (<64.0).

All configuration lives in pyproject.toml. This file exists only so that
`pip install -e .` works on systems where setuptools doesn't yet support
PEP 660.
"""
from setuptools import setup

setup()

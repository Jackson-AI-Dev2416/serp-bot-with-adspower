"""Resolve application and data directories for dev and PyInstaller exe runs."""

from __future__ import annotations

import sys
from pathlib import Path


def app_base_dir() -> Path:
  """Project root in dev; folder containing SERPBot.exe when frozen."""
  if getattr(sys, "frozen", False):
    return Path(sys.executable).resolve().parent
  return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
  return app_base_dir() / "data"


def ensure_data_dir() -> Path:
  path = data_dir()
  path.mkdir(parents=True, exist_ok=True)
  return path

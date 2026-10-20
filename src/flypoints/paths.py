"""Filesystem locations.

DATA_DIR is the repo-root ``data/`` folder, resolved from the package source (editable install).
STATE_DIR is the per-user state dir (token, cache, saved searches); call ``ensure_state_dir()``
before writing to it. Nothing is created at import time.
"""
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
STATE_DIR = Path.home() / ".flypoints"


def ensure_state_dir() -> Path:
    """Create STATE_DIR if missing and return it."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR

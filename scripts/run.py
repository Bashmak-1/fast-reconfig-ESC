"""Launcher: adds src/ to sys.path and starts the GUI.

Usage: python scripts/run.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fast_reconfig_esc.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

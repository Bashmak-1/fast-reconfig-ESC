"""Launcher for the updater: adds src/ to sys.path and runs the console updater.

Usage: python scripts/run_updater.py [--check] [--yes] ...
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from esc_hotline.updater_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

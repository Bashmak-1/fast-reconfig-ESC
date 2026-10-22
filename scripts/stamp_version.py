"""Stamp a release version (e.g. from a git tag `v1.2.3`) into the files the
build reads: src/esc_hotline/_version.py and scripts/version_info.txt.

Usage: python scripts/stamp_version.py v1.2.3
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(tag: str) -> int:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not m:
        print(f"Not a vX.Y.Z version: {tag!r}")
        return 1
    ver = ".".join(m.groups())
    tup = ", ".join(m.groups()) + ", 0"

    (ROOT / "src" / "esc_hotline" / "_version.py").write_text(
        '"""Single source of truth for the app version. Release CI overwrites this file\n'
        "from the git tag (scripts/stamp_version.py) before building; the value here is\n"
        'only what a plain source checkout reports."""\n'
        f'__version__ = "{ver}"\n',
        encoding="utf-8",
    )
    vi = ROOT / "scripts" / "version_info.txt"
    text = vi.read_text(encoding="utf-8")
    text = re.sub(r"(filevers|prodvers)=\([^)]*\)", rf"\1=({tup})", text)
    text = re.sub(r"(u'(?:File|Product)Version', u')[^']*", rf"\g<1>{ver}.0", text)
    vi.write_text(text, encoding="utf-8")
    print(f"Stamped {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

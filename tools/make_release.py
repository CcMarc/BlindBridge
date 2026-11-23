#!/usr/bin/env python3
"""Build the BlindBridge zip: the same file works for a first install and as an
update uploaded on the Settings page.

    python3 tools/make_release.py [output folder]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402


def release_files() -> list[Path]:
    return updater.release_files(ROOT)


def build(out_dir: Path) -> Path:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    target = Path(out_dir) / f"blindbridge-{version}.zip"
    target.write_bytes(updater.build_zip(ROOT))
    return target


if __name__ == "__main__":
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()))

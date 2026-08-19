#!/usr/bin/env python3
"""Stitch scripts/parts/*.py into the single-file helper.

The runtime artifact must stay one file: every wave archives it into its
receipts, pins its SHA-256, and workers run that copy by path. Source lives in
parts/, ordered by filename; this script concatenates them verbatim with a
banner line between parts.

Usage:
  python3 build_helper.py          # rebuild orca_luna_worker.py
  python3 build_helper.py --check  # exit 2 if the bundle is out of date
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PARTS = SCRIPTS / "parts"
BUNDLE = SCRIPTS / "orca_luna_worker.py"


def stitch() -> str:
    pieces: list[str] = []
    for path in sorted(PARTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        if pieces:
            pieces.append(f"# ==== part: {path.name} ====\n")
        pieces.append(text)
    return "".join(pieces)


def main() -> int:
    if not PARTS.is_dir():
        print(f"missing parts directory: {PARTS}", file=sys.stderr)
        return 2
    built = stitch()
    if "--check" in sys.argv[1:]:
        current = BUNDLE.read_text(encoding="utf-8") if BUNDLE.exists() else ""
        if current != built:
            print(
                "bundle is out of date: edit parts/ then run build_helper.py",
                file=sys.stderr,
            )
            return 2
        print("bundle matches parts")
        return 0
    BUNDLE.write_text(built, encoding="utf-8")
    print(f"wrote {BUNDLE} ({len(built.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

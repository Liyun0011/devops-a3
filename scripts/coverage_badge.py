#!/usr/bin/env python3
"""Turn coverage.py's JSON report into a shields.io endpoint badge.

Usage: coverage_badge.py coverage.json badges/coverage.json
The README shows it with
  https://img.shields.io/endpoint?url=<raw URL of badges/coverage.json>
so the badge changes whenever the committed JSON changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def badge(percent: float) -> dict:
    color = ("brightgreen" if percent >= 90 else "green" if percent >= 80
             else "yellow" if percent >= 70 else "red")
    return {"schemaVersion": 1, "label": "coverage", "message": f"{percent:.0f}%", "color": color}


def main(argv=None) -> int:
    src, dst = (argv or sys.argv[1:])[:2]
    percent = json.loads(Path(src).read_text())["totals"]["percent_covered"]
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(badge(percent)) + "\n", encoding="utf-8")
    print(f"coverage {percent:.1f}% -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

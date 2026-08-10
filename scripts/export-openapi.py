#!/usr/bin/env python3
"""离线导出确定性的 TelePilot OpenAPI 快照。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
OUTPUT = ROOT / "openapi" / "telepilot.openapi.json"

os.environ.setdefault("MASTER_KEY", "4QAy7kFzv2mkHRXTHwL_FEZABz0R3sqat5rBl4vDLXk=")
os.environ.setdefault("JWT_SECRET", "telepilot-openapi-export-secret-only")
sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        app.openapi(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    OUTPUT.write_text(payload + "\n", encoding="utf-8")
    print(OUTPUT.relative_to(ROOT))


if __name__ == "__main__":
    main()

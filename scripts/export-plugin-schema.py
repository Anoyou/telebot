#!/usr/bin/env python3
"""从运行时 Pydantic 模型导出确定性的 plugin.json Schema。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
OUTPUT = ROOT / "schemas" / "plugin.schema.json"

os.environ.setdefault("MASTER_KEY", "4QAy7kFzv2mkHRXTHwL_FEZABz0R3sqat5rBl4vDLXk=")
os.environ.setdefault("JWT_SECRET", "telepilot-plugin-schema-export-secret-only")
sys.path.insert(0, str(BACKEND))

from app.services.remote_plugin_service import PluginMetadataSchema  # noqa: E402


def main() -> None:
    schema = PluginMetadataSchema.model_json_schema(
        by_alias=True,
        ref_template="#/$defs/{model}",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://telepilot.local/schemas/plugin.schema.json"
    schema["title"] = "TelePilot plugin.json"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT.relative_to(ROOT))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""TelePilot recording replay CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Allow running this file directly from backend/scripts.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.worker.replay import replay_recording  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tp_replay",
        description="Replay TelePilot inbound JSONL recordings in dry-run mode.",
    )
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="replay one JSONL recording")
    run.add_argument("recording", type=Path, help="recording JSONL path")
    run.add_argument("--account-id", type=int, default=None, help="override account id")
    run.add_argument("--token", default="replay-token", help="mock bot token used for replay")
    run.add_argument("--compact", action="store_true", help="print compact JSON")
    return parser


async def _run(args: argparse.Namespace) -> int:
    result = await replay_recording(
        args.recording,
        account_id=args.account_id,
        token=args.token,
    )
    payload: dict[str, Any] = {
        "source": str(result.source) if result.source is not None else None,
        "account_id": result.account_id,
        "envelope_count": result.envelope_count,
        "action_events": result.action_events,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "run":
        return asyncio.run(_run(args))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

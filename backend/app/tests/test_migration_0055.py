from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0055_llm_provider_protocol_profiles.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0055", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0055_expands_and_safely_restores_protocol_constraint(monkeypatch) -> None:
    migration = _load_migration_module()
    dropped: list[tuple[str, str, str | None]] = []
    created: list[tuple[str, str, str]] = []
    statements: list[str] = []

    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, **kwargs: dropped.append((name, table, kwargs.get("type_"))),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: created.append((name, table, condition)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    migration.downgrade()

    assert migration.revision == "0055"
    assert migration.down_revision == "0054"
    assert dropped == [
        ("ck_llm_provider_protocol_profile", "llm_provider", "check"),
        ("ck_llm_provider_protocol_profile", "llm_provider", "check"),
    ]
    assert created == [
        (
            "ck_llm_provider_protocol_profile",
            "llm_provider",
            "protocol_profile IN ('standard', 'openai_responses', "
            "'deepseek_responses', 'codex_responses', 'claude_code_proxy')",
        ),
        (
            "ck_llm_provider_protocol_profile",
            "llm_provider",
            "protocol_profile IN ('standard', 'claude_code_proxy')",
        ),
    ]
    assert statements == [
        "UPDATE llm_provider SET protocol_profile = 'standard' "
        "WHERE protocol_profile NOT IN ('standard', 'claude_code_proxy')"
    ]

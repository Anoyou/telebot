from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0047_llm_provider_grok_cli_identity.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0047", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0047_chains_from_0046_and_allows_grok_cli() -> None:
    migration = _load_migration_module()

    assert migration.revision == "0047"
    assert migration.down_revision == "0046"
    assert "grok_cli" in migration._ALLOWED_IDENTITIES


def test_migration_0047_keeps_existing_identity_profiles() -> None:
    migration = _load_migration_module()

    assert set(migration._ALLOWED_IDENTITIES) >= {
        "auto",
        "minimal",
        "openai_sdk",
        "codex_cli",
        "codex_desktop",
        "claude_code",
        "claude_desktop",
        "grok_cli",
    }


def test_upgrade_and_downgrade_replace_the_constraint(monkeypatch) -> None:
    migration = _load_migration_module()
    dropped: list[tuple[str, str, str]] = []
    created: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, type_: dropped.append((name, table, type_)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, expression: created.append((name, table, expression)),
    )

    migration.upgrade()
    migration.downgrade()

    assert dropped == [
        ("ck_llm_provider_client_identity_profile", "llm_provider", "check"),
        ("ck_llm_provider_client_identity_profile", "llm_provider", "check"),
    ]
    assert "'grok_cli'" in created[0][2]
    assert "'grok_cli'" not in created[1][2]

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, text


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0040_llm_provider_client_identity.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0040", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0040_chains_from_0039() -> None:
    migration = _load_migration_module()
    assert migration.revision == "0040"
    assert migration.down_revision == "0039"


def test_backfill_sets_all_existing_providers_to_auto() -> None:
    """升级后所有既有 Provider 都应为 auto，确保不再发送 TelePilot UA。"""
    migration = _load_migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE llm_provider (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(64) NOT NULL,
                    provider VARCHAR(16) NOT NULL,
                    api_format VARCHAR(32) NOT NULL DEFAULT 'chat_completions',
                    client_identity_profile VARCHAR(32)
                )
                """
            )
        )
        for pid, fmt in ((1, "chat_completions"), (2, "anthropic_messages"), (3, "responses")):
            conn.execute(
                text(
                    "INSERT INTO llm_provider (id, name, provider, api_format) "
                    "VALUES (:id, :name, 'openai', :fmt)"
                ),
                {"id": pid, "name": f"p{pid}", "fmt": fmt},
            )
        migration._backfill_client_identity(conn)
        rows = conn.execute(
            text("SELECT client_identity_profile FROM llm_provider ORDER BY id")
        ).scalars().all()
        assert rows == ["auto", "auto", "auto"]
    engine.dispose()

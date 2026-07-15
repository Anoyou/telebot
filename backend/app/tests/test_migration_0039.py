from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from sqlalchemy import create_engine, text


def _load_migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0039_payout_scope_and_action_source.py"
    spec = importlib.util.spec_from_file_location("telepilot_migration_0039", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_downgrade_remaps_account_scoped_payout_keys_into_global_namespace() -> None:
    migration = _load_migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE payout_compensation (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    payout_key VARCHAR(80) NOT NULL,
                    payload JSON NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE action_event (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    payout_key VARCHAR(80),
                    params_summary JSON NOT NULL
                )
                """
            )
        )
        for account_id in (7, 8):
            conn.execute(
                text(
                    """
                    INSERT INTO payout_compensation (id, account_id, payout_key, payload)
                    VALUES (:id, :account_id, 'shared', :payload)
                    """
                ),
                {
                    "id": account_id,
                    "account_id": account_id,
                    "payload": json.dumps({"payout_key": "shared"}),
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO action_event (id, account_id, payout_key, params_summary)
                    VALUES (:id, :account_id, 'shared', :params_summary)
                    """
                ),
                {
                    "id": account_id,
                    "account_id": account_id,
                    "params_summary": json.dumps(
                        {"payout_key": "shared", "result": {"payout_key": "shared"}}
                    ),
                },
            )

        migration._restore_global_payout_keys(conn)
        compensation = conn.execute(
            text("SELECT account_id, payout_key, payload FROM payout_compensation ORDER BY account_id")
        ).mappings().all()
        actions = conn.execute(
            text("SELECT account_id, payout_key, params_summary FROM action_event ORDER BY account_id")
        ).mappings().all()

        assert compensation[0]["payout_key"] != compensation[1]["payout_key"]
        for payout_row, action_row in zip(compensation, actions, strict=True):
            assert payout_row["payout_key"] == action_row["payout_key"]
            assert json.loads(payout_row["payload"])["payout_key"] == payout_row["payout_key"]
            params = json.loads(action_row["params_summary"])
            assert params["payout_key"] == action_row["payout_key"]
            assert params["result"]["payout_key"] == action_row["payout_key"]

        conn.execute(text("CREATE UNIQUE INDEX uq_pc_legacy_key ON payout_compensation (payout_key)"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_ae_legacy_key ON action_event (payout_key) "
                "WHERE payout_key IS NOT NULL"
            )
        )

    engine.dispose()

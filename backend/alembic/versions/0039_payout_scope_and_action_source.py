"""scope payout idempotency and add durable action source keys

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-12
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def _drop_legacy_compensation_unique(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "sqlite":
        naming = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
        with op.batch_alter_table(
            "payout_compensation",
            naming_convention=naming,
            recreate="always",
        ) as batch:
            batch.drop_constraint("uq_payout_compensation_payout_key", type_="unique")
        return
    constraints = inspect(bind).get_unique_constraints("payout_compensation")
    legacy = next(
        (
            item.get("name")
            for item in constraints
            if item.get("column_names") == ["payout_key"] and item.get("name")
        ),
        None,
    )
    if not legacy:
        return
    op.drop_constraint(str(legacy), "payout_compensation", type_="unique")


def _backfill_and_canonicalize_payouts(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "postgresql":
        op.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = COALESCE(
                    NULLIF(BTRIM(params_summary->>'payout_key'), ''),
                    NULLIF(BTRIM(params_summary->'result'->>'payout_key'), '')
                )
                WHERE payout_key IS NULL
                  AND action_type = 'payout'
                  AND status IN ('OK', 'COMPENSATED')
                """
            )
        )
        op.execute(
            text(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY account_id, payout_key
                               ORDER BY id ASC
                           ) AS rn
                    FROM action_event
                    WHERE payout_key IS NOT NULL
                      AND action_type = 'payout'
                      AND status IN ('OK', 'COMPENSATED')
                )
                UPDATE action_event AS ae
                SET status = 'FAILED',
                    error_code = COALESCE(ae.error_code, 'historical_duplicate'),
                    error_summary = COALESCE(ae.error_summary, '0039 canonicalized duplicate payout')
                FROM ranked
                WHERE ae.id = ranked.id AND ranked.rn > 1
                """
            )
        )
    else:
        op.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = COALESCE(
                    NULLIF(json_extract(params_summary, '$.payout_key'), ''),
                    NULLIF(json_extract(params_summary, '$.result.payout_key'), '')
                )
                WHERE payout_key IS NULL
                  AND action_type = 'payout'
                  AND status IN ('OK', 'COMPENSATED')
                """
            )
        )
        op.execute(
            text(
                """
                UPDATE action_event
                SET status = 'FAILED',
                    error_code = COALESCE(error_code, 'historical_duplicate'),
                    error_summary = COALESCE(error_summary, '0039 canonicalized duplicate payout')
                WHERE id IN (
                    SELECT ae.id
                    FROM action_event AS ae
                    WHERE ae.payout_key IS NOT NULL
                      AND ae.action_type = 'payout'
                      AND ae.status IN ('OK', 'COMPENSATED')
                      AND ae.id > (
                          SELECT MIN(ae2.id)
                          FROM action_event AS ae2
                          WHERE ae2.account_id = ae.account_id
                            AND ae2.payout_key = ae.payout_key
                            AND ae2.action_type = 'payout'
                            AND ae2.status IN ('OK', 'COMPENSATED')
                      )
                )
                """
            )
        )


def _legacy_global_payout_key(account_id: object, payout_key: object) -> str:
    digest = hashlib.sha256(str(payout_key).encode("utf-8")).hexdigest()[:32]
    return f"legacy_{int(account_id)}_{digest}"


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _restore_global_payout_keys(bind: sa.engine.Connection) -> None:
    """Map account-scoped keys into the legacy global namespace before downgrade."""

    compensation_rows = bind.execute(
        text(
            """
            SELECT id, account_id, payout_key, payload
            FROM payout_compensation
            WHERE payout_key IS NOT NULL
            """
        )
    ).mappings()
    for row in compensation_rows:
        legacy_key = _legacy_global_payout_key(row["account_id"], row["payout_key"])
        payload = _json_object(row["payload"])
        payload["payout_key"] = legacy_key
        bind.execute(
            text(
                """
                UPDATE payout_compensation
                SET payout_key = :payout_key, payload = :payload
                WHERE id = :row_id
                """
            ),
            {
                "row_id": int(row["id"]),
                "payout_key": legacy_key,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )

    action_rows = bind.execute(
        text(
            """
            SELECT id, account_id, payout_key, params_summary
            FROM action_event
            WHERE payout_key IS NOT NULL
            """
        )
    ).mappings()
    for row in action_rows:
        legacy_key = _legacy_global_payout_key(row["account_id"], row["payout_key"])
        params_summary = _json_object(row["params_summary"])
        params_summary["payout_key"] = legacy_key
        result = params_summary.get("result")
        if isinstance(result, dict) and "payout_key" in result:
            result = dict(result)
            result["payout_key"] = legacy_key
            params_summary["result"] = result
        bind.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = :payout_key, params_summary = :params_summary
                WHERE id = :row_id
                """
            ),
            {
                "row_id": int(row["id"]),
                "payout_key": legacy_key,
                "params_summary": json.dumps(params_summary, ensure_ascii=False),
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    _drop_legacy_compensation_unique(bind)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("payout_compensation", recreate="always") as batch:
            batch.create_unique_constraint(
                "uq_payout_compensation_account_key",
                ["account_id", "payout_key"],
            )
    else:
        op.create_unique_constraint(
            "uq_payout_compensation_account_key",
            "payout_compensation",
            ["account_id", "payout_key"],
        )
    op.add_column(
        "payout_compensation",
        sa.Column("delivery_token", sa.String(length=64), nullable=True),
    )

    op.drop_index("uq_action_event_countable_payout_key", table_name="action_event")
    _backfill_and_canonicalize_payouts(bind)
    op.create_index(
        "uq_action_event_countable_payout_key",
        "action_event",
        ["account_id", "payout_key"],
        unique=True,
        postgresql_where=sa.text(
            "payout_key IS NOT NULL AND action_type = 'payout' AND status IN ('OK', 'COMPENSATED')"
        ),
        sqlite_where=sa.text(
            "payout_key IS NOT NULL AND action_type = 'payout' AND status IN ('OK', 'COMPENSATED')"
        ),
    )

    op.add_column("action_event", sa.Column("source_event_key", sa.String(length=200), nullable=True))
    op.create_index("ix_action_event_source_event_key", "action_event", ["source_event_key"])
    op.create_index(
        "uq_action_event_account_channel_source",
        "action_event",
        ["account_id", "channel", "source_event_key"],
        unique=True,
        postgresql_where=sa.text("source_event_key IS NOT NULL"),
        sqlite_where=sa.text("source_event_key IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    _restore_global_payout_keys(bind)
    op.drop_index("uq_action_event_account_channel_source", table_name="action_event")
    op.drop_index("ix_action_event_source_event_key", table_name="action_event")
    op.drop_column("action_event", "source_event_key")

    op.drop_index("uq_action_event_countable_payout_key", table_name="action_event")
    op.create_index(
        "uq_action_event_countable_payout_key",
        "action_event",
        ["payout_key"],
        unique=True,
        postgresql_where=sa.text(
            "payout_key IS NOT NULL AND action_type = 'payout' AND status IN ('OK', 'COMPENSATED')"
        ),
        sqlite_where=sa.text(
            "payout_key IS NOT NULL AND action_type = 'payout' AND status IN ('OK', 'COMPENSATED')"
        ),
    )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("payout_compensation", recreate="always") as batch:
            batch.drop_column("delivery_token")
            batch.drop_constraint("uq_payout_compensation_account_key", type_="unique")
            batch.create_unique_constraint("uq_payout_compensation_payout_key", ["payout_key"])
    else:
        op.drop_column("payout_compensation", "delivery_token")
        op.drop_constraint(
            "uq_payout_compensation_account_key",
            "payout_compensation",
            type_="unique",
        )
        op.create_unique_constraint(
            "payout_compensation_payout_key_key",
            "payout_compensation",
            ["payout_key"],
        )

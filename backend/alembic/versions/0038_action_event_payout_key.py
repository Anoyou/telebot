"""action_event.payout_key + partial unique for countable ledger rows

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_event", sa.Column("payout_key", sa.String(length=80), nullable=True))
    op.create_index("ix_action_event_payout_key", "action_event", ["payout_key"])

    bind = op.get_bind()
    dialect = bind.dialect.name

    # 回填：params_summary 在 PG 上是 JSON（非 JSONB），不能用 `?` 操作符。
    # 统一用 ->> 取文本并判断非空；SQLite 用 json_extract。
    if dialect == "postgresql":
        op.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = NULLIF(BTRIM(params_summary->>'payout_key'), '')
                WHERE payout_key IS NULL
                  AND NULLIF(BTRIM(params_summary->>'payout_key'), '') IS NOT NULL
                """
            )
        )
        op.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = NULLIF(BTRIM(params_summary->'result'->>'payout_key'), '')
                WHERE payout_key IS NULL
                  AND NULLIF(BTRIM(params_summary->'result'->>'payout_key'), '') IS NOT NULL
                """
            )
        )
    else:
        op.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = json_extract(params_summary, '$.payout_key')
                WHERE payout_key IS NULL
                  AND json_extract(params_summary, '$.payout_key') IS NOT NULL
                  AND json_extract(params_summary, '$.payout_key') != ''
                """
            )
        )
        op.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = json_extract(params_summary, '$.result.payout_key')
                WHERE payout_key IS NULL
                  AND json_extract(params_summary, '$.result.payout_key') IS NOT NULL
                  AND json_extract(params_summary, '$.result.payout_key') != ''
                """
            )
        )

    # 可计账状态（OK/COMPENSATED）下若已有重复 payout_key，保留最小 id，清空其余行的
    # payout_key（行仍在，只是不再参与 partial unique / 新幂等键），避免建索引失败。
    if dialect == "postgresql":
        op.execute(
            text(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY payout_key
                               ORDER BY id ASC
                           ) AS rn
                    FROM action_event
                    WHERE payout_key IS NOT NULL
                      AND action_type = 'payout'
                      AND status IN ('OK', 'COMPENSATED')
                )
                UPDATE action_event AS ae
                SET payout_key = NULL
                FROM ranked
                WHERE ae.id = ranked.id
                  AND ranked.rn > 1
                """
            )
        )
    else:
        # SQLite：用相关子查询清掉非最小 id 的重复键。
        op.execute(
            text(
                """
                UPDATE action_event
                SET payout_key = NULL
                WHERE id IN (
                    SELECT ae.id
                    FROM action_event AS ae
                    WHERE ae.payout_key IS NOT NULL
                      AND ae.action_type = 'payout'
                      AND ae.status IN ('OK', 'COMPENSATED')
                      AND ae.id > (
                          SELECT MIN(ae2.id)
                          FROM action_event AS ae2
                          WHERE ae2.payout_key = ae.payout_key
                            AND ae2.action_type = 'payout'
                            AND ae2.status IN ('OK', 'COMPENSATED')
                      )
                )
                """
            )
        )

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


def downgrade() -> None:
    op.drop_index("uq_action_event_countable_payout_key", table_name="action_event")
    op.drop_index("ix_action_event_payout_key", table_name="action_event")
    op.drop_column("action_event", "payout_key")

"""加密插件配置中的敏感字段（AccountFeature / PluginGlobalConfig）。

用法：
    python -m app.scripts.migrate_plugin_config_secrets --dry-run
    python -m app.scripts.migrate_plugin_config_secrets
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.base import AsyncSessionLocal
from app.db.models.feature import AccountFeature, Feature
from app.db.models.plugin_global_config import PluginGlobalConfig
from app.services.plugin_config_secrets import (
    count_encryptable_secrets,
    encrypt_config_secrets,
)


@dataclass
class MigrateResult:
    account_rows_scanned: int = 0
    account_rows_changed: int = 0
    global_rows_scanned: int = 0
    global_rows_changed: int = 0
    plain_fields: int = 0
    envelope_fields: int = 0
    failures: list[str] = field(default_factory=list)


def _schema_for_feature(feature: Feature | None) -> dict | None:
    if feature is None:
        return None
    schema = (feature.manifest or {}).get("config_schema")
    return schema if isinstance(schema, dict) else None


async def migrate_plugin_config_secrets(*, dry_run: bool = True) -> MigrateResult:
    result = MigrateResult()
    async with AsyncSessionLocal() as db:
        features = {
            row.key: row
            for row in (await db.execute(select(Feature))).scalars().all()
        }

        account_rows = (await db.execute(select(AccountFeature))).scalars().all()
        for row in account_rows:
            result.account_rows_scanned += 1
            schema = _schema_for_feature(features.get(row.feature_key))
            config = dict(row.config or {})
            counts = count_encryptable_secrets(config, schema=schema)
            result.plain_fields += counts["plain"]
            result.envelope_fields += counts["envelope"]
            if counts["plain"] <= 0:
                continue
            encrypted = encrypt_config_secrets(config, schema=schema)
            if encrypted == config:
                continue
            result.account_rows_changed += 1
            if not dry_run:
                row.config = encrypted
                flag_modified(row, "config")

        global_rows = (await db.execute(select(PluginGlobalConfig))).scalars().all()
        for row in global_rows:
            result.global_rows_scanned += 1
            schema = _schema_for_feature(features.get(row.plugin_key))
            config = dict(row.config or {})
            counts = count_encryptable_secrets(config, schema=schema)
            result.plain_fields += counts["plain"]
            result.envelope_fields += counts["envelope"]
            if counts["plain"] <= 0:
                continue
            encrypted = encrypt_config_secrets(config, schema=schema)
            if encrypted == config:
                continue
            result.global_rows_changed += 1
            if not dry_run:
                row.config = encrypted
                flag_modified(row, "config")

        if dry_run:
            await db.rollback()
        else:
            await db.commit()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移插件配置敏感字段为 secret:v1 信封")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args(argv)
    result = asyncio.run(migrate_plugin_config_secrets(dry_run=bool(args.dry_run)))
    mode = "dry-run" if args.dry_run else "apply"
    print(
        f"[{mode}] account_rows={result.account_rows_scanned} "
        f"account_changed={result.account_rows_changed} "
        f"global_rows={result.global_rows_scanned} "
        f"global_changed={result.global_rows_changed} "
        f"plain_fields={result.plain_fields} "
        f"envelope_fields={result.envelope_fields}"
    )
    if result.failures:
        for item in result.failures:
            print(f"failure: {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""MASTER_KEY 轮换脚本。

用法：
    python -m app.scripts.rekey --old "$OLD_MASTER_KEY" --new "$NEW_MASTER_KEY" --dry-run
    python -m app.scripts.rekey --old "$OLD_MASTER_KEY" --new "$NEW_MASTER_KEY"
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import MetaData, Table, create_engine, inspect, select, update
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class RekeyField:
    table: str
    pk_column: str
    value_column: str


@dataclass
class RekeyResult:
    scanned: int = 0
    changed: int = 0
    skipped: int = 0
    missing: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.failures)


SCALAR_FIELDS: tuple[RekeyField, ...] = (
    RekeyField("proxy", "id", "password_enc"),
    RekeyField("account", "id", "api_id_enc"),
    RekeyField("account", "id", "api_hash_enc"),
    RekeyField("account", "id", "session_enc"),
    RekeyField("llm_provider", "id", "api_key_enc"),
    RekeyField("notify_bot", "id", "bot_token_enc"),
    RekeyField("web_user", "id", "totp_secret_enc"),
    RekeyField("account_bot", "id", "bot_token_enc"),
    RekeyField("plugin_repo", "id", "credential_enc"),
)
SYSTEM_SETTING_ENCRYPTED_KEYS_BY_PREFIX = {
    "account_bot_transfer_notice:": ("interaction_bot_token_enc", "transfer_bot_token_enc"),
    "account_webhooks:": ("token_enc",),
}
SYSTEM_SETTING_LEGACY_PLAINTEXT_KEYS_BY_PREFIX = {
    "account_webhooks:": {"token": "token_enc"},
}
PLUGIN_SECRET_ENVELOPE_PREFIX = "secret:v1:"


def _build_fernet(raw_key: str, *, label: str) -> Fernet:
    try:
        return Fernet(raw_key.encode())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label} 不是合法 Fernet 主密钥") from exc


def _normalize_database_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "")


def _default_database_url() -> str:
    from app.settings import settings

    return settings.database_url_sync


def _reencrypt_token(value: Any, *, old: Fernet, new: Fernet) -> Any:
    if isinstance(value, str):
        return new.encrypt(old.decrypt(value.encode())).decode()
    if isinstance(value, bytes):
        return new.encrypt(old.decrypt(value))
    if isinstance(value, memoryview):
        return new.encrypt(old.decrypt(bytes(value)))
    raise TypeError(f"不支持的密文字段类型：{type(value).__name__}")


def _has_table(conn: Connection, table_name: str) -> bool:
    return inspect(conn).has_table(table_name)


def _load_table(conn: Connection, metadata: MetaData, table_name: str) -> Table:
    if table_name in metadata.tables:
        return metadata.tables[table_name]
    return Table(table_name, metadata, autoload_with=conn)


def _rekey_scalar_field(
    conn: Connection,
    metadata: MetaData,
    result: RekeyResult,
    field: RekeyField,
    *,
    old: Fernet,
    new: Fernet,
    dry_run: bool,
) -> None:
    if not _has_table(conn, field.table):
        result.missing += 1
        return

    table = _load_table(conn, metadata, field.table)
    if field.pk_column not in table.c or field.value_column not in table.c:
        result.missing += 1
        return

    pk_col = table.c[field.pk_column]
    value_col = table.c[field.value_column]
    rows = conn.execute(select(pk_col, value_col).where(value_col.is_not(None))).all()
    for row in rows:
        row_id = row[0]
        token = row[1]
        if token in ("", b""):
            result.skipped += 1
            continue
        result.scanned += 1
        try:
            new_token = _reencrypt_token(token, old=old, new=new)
        except (InvalidToken, TypeError, ValueError) as exc:
            result.failures.append(f"{field.table}.{field.value_column}#{row_id}: {exc}")
            continue
        result.changed += 1
        if not dry_run:
            conn.execute(update(table).where(pk_col == row_id).values({field.value_column: new_token}))


def _rekey_system_settings(
    conn: Connection,
    metadata: MetaData,
    result: RekeyResult,
    *,
    old: Fernet,
    new: Fernet,
    dry_run: bool,
) -> None:
    table_name = "system_setting"
    if not _has_table(conn, table_name):
        result.missing += 1
        return

    table = _load_table(conn, metadata, table_name)
    if "key" not in table.c or "value" not in table.c:
        result.missing += 1
        return

    key_col = table.c.key
    value_col = table.c.value
    for prefix, encrypted_keys in SYSTEM_SETTING_ENCRYPTED_KEYS_BY_PREFIX.items():
        rows = conn.execute(select(key_col, value_col).where(key_col.like(f"{prefix}%"))).all()
        for row in rows:
            setting_key = row[0]
            value = row[1]
            if not isinstance(value, dict):
                result.skipped += 1
                continue

            changed = False
            new_value = dict(value)
            migrated_encrypted_keys: set[str] = set()
            for plaintext_key, encrypted_key in SYSTEM_SETTING_LEGACY_PLAINTEXT_KEYS_BY_PREFIX.get(
                prefix, {}
            ).items():
                plain = str(new_value.get(plaintext_key) or "")
                if not plain:
                    continue
                if not new_value.get(encrypted_key):
                    result.scanned += 1
                    new_value[encrypted_key] = new.encrypt(plain.encode()).decode()
                    migrated_encrypted_keys.add(encrypted_key)
                    result.changed += 1
                new_value.pop(plaintext_key, None)
                changed = True

            for encrypted_key in encrypted_keys:
                token = new_value.get(encrypted_key)
                if not token:
                    result.skipped += 1
                    continue
                # 旧明文已直接用新密钥加密，不要再尝试用旧密钥解密。
                if encrypted_key in migrated_encrypted_keys:
                    continue
                result.scanned += 1
                try:
                    new_value[encrypted_key] = _reencrypt_token(str(token), old=old, new=new)
                except (InvalidToken, TypeError, ValueError) as exc:
                    result.failures.append(f"{table_name}.value[{encrypted_key}]#{setting_key}: {exc}")
                    continue
                changed = True
                result.changed += 1

            if changed and not dry_run:
                conn.execute(update(table).where(key_col == setting_key).values(value=new_value))


def _reencrypt_plugin_config_value(value: Any, *, old: Fernet, new: Fernet) -> tuple[Any, int, list[str]]:
    """Re-encrypt secret:v1 envelopes inside JSON config trees.

    Returns (new_value, changed_count, failures).
    """

    failures: list[str] = []

    def walk(node: Any, path: str) -> Any:
        nonlocal failures
        if isinstance(node, dict):
            return {str(k): walk(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(item, f"{path}[{idx}]") for idx, item in enumerate(node)]
        if isinstance(node, str) and node.startswith(PLUGIN_SECRET_ENVELOPE_PREFIX):
            token = node[len(PLUGIN_SECRET_ENVELOPE_PREFIX) :]
            try:
                plain = old.decrypt(token.encode())
                return f"{PLUGIN_SECRET_ENVELOPE_PREFIX}{new.encrypt(plain).decode()}"
            except (InvalidToken, TypeError, ValueError) as exc:
                failures.append(f"{path}: {exc}")
                return node
        return node

    changed_holder = {"n": 0}

    def walk_count(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                nv = walk_count(v, f"{path}.{k}" if path else str(k))
                out[str(k)] = nv
            return out
        if isinstance(node, list):
            return [walk_count(item, f"{path}[{idx}]") for idx, item in enumerate(node)]
        if isinstance(node, str) and node.startswith(PLUGIN_SECRET_ENVELOPE_PREFIX):
            token = node[len(PLUGIN_SECRET_ENVELOPE_PREFIX) :]
            try:
                plain = old.decrypt(token.encode())
                changed_holder["n"] += 1
                return f"{PLUGIN_SECRET_ENVELOPE_PREFIX}{new.encrypt(plain).decode()}"
            except (InvalidToken, TypeError, ValueError) as exc:
                failures.append(f"{path}: {exc}")
                return node
        return node

    new_value = walk_count(value, "")
    return new_value, changed_holder["n"], failures


def _rekey_plugin_json_configs(
    conn: Connection,
    metadata: MetaData,
    result: RekeyResult,
    *,
    old: Fernet,
    new: Fernet,
    dry_run: bool,
) -> None:
    """Re-encrypt plugin AccountFeature.config / PluginGlobalConfig.config envelopes."""

    targets = (
        ("account_feature", ("account_id", "feature_key"), "config"),
        ("plugin_global_config", ("plugin_key",), "config"),
    )
    for table_name, pk_cols, value_col_name in targets:
        if not _has_table(conn, table_name):
            result.missing += 1
            continue
        table = _load_table(conn, metadata, table_name)
        if value_col_name not in table.c or any(col not in table.c for col in pk_cols):
            result.missing += 1
            continue
        cols = [table.c[name] for name in pk_cols] + [table.c[value_col_name]]
        rows = conn.execute(select(*cols)).all()
        for row in rows:
            pk_values = row[:-1]
            value = row[-1]
            if not isinstance(value, dict):
                result.skipped += 1
                continue
            result.scanned += 1
            new_value, changed, failures = _reencrypt_plugin_config_value(value, old=old, new=new)
            for fail in failures:
                pk_label = ",".join(str(v) for v in pk_values)
                result.failures.append(f"{table_name}.config#{pk_label}:{fail}")
            if changed <= 0:
                result.skipped += 1
                continue
            result.changed += changed
            if not dry_run:
                where_clause = None
                for name, pk_val in zip(pk_cols, pk_values, strict=True):
                    clause = table.c[name] == pk_val
                    where_clause = clause if where_clause is None else (where_clause & clause)
                conn.execute(update(table).where(where_clause).values({value_col_name: new_value}))


def rekey_database(
    *,
    old_key: str,
    new_key: str,
    database_url: str | None = None,
    dry_run: bool = False,
    raise_on_failure: bool = True,
) -> RekeyResult:
    if old_key == new_key:
        raise ValueError("旧 MASTER_KEY 与新 MASTER_KEY 相同，已取消")

    old = _build_fernet(old_key, label="旧 MASTER_KEY")
    new = _build_fernet(new_key, label="新 MASTER_KEY")
    db_url = _normalize_database_url(database_url or _default_database_url())
    engine = create_engine(db_url, future=True)
    metadata = MetaData()
    result = RekeyResult()

    try:
        with engine.begin() as conn:
            for field in SCALAR_FIELDS:
                _rekey_scalar_field(conn, metadata, result, field, old=old, new=new, dry_run=dry_run)
            _rekey_system_settings(conn, metadata, result, old=old, new=new, dry_run=dry_run)
            _rekey_plugin_json_configs(conn, metadata, result, old=old, new=new, dry_run=dry_run)
            if result.failures and raise_on_failure:
                detail = "；".join(result.failures[:5])
                more = f"；另有 {result.failed - 5} 个失败" if result.failed > 5 else ""
                raise RuntimeError(f"部分密文字段无法用旧 MASTER_KEY 解密，数据库未写入任何变更：{detail}{more}")
    finally:
        engine.dispose()

    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="轮换 TelePilot 数据库内的 Fernet 密文字段")
    parser.add_argument("--old", required=True, help="旧 MASTER_KEY")
    parser.add_argument("--new", required=True, help="新 MASTER_KEY")
    parser.add_argument("--database-url", default=None, help="数据库 DSN；默认读取当前配置")
    parser.add_argument("--dry-run", action="store_true", help="只验证可重钥字段，不写入数据库")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        result = rekey_database(
            old_key=args.old,
            new_key=args.new,
            database_url=args.database_url,
            dry_run=args.dry_run,
            raise_on_failure=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"重钥失败：{exc}", file=sys.stderr)
        return 1

    action = "dry-run 验证" if args.dry_run else "重钥"
    print(
        f"{action}完成：扫描 {result.scanned} 个密文字段，"
        f"可更新 {result.changed} 个，跳过 {result.skipped} 个空值/非目标项，"
        f"缺失表或字段 {result.missing} 个。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""插件配置敏感字段的版本化加密信封。

落库形态：``secret:v1:<fernet-ciphertext>``。
API 始终返回遮罩；Worker 合并生效配置时才解密为明文。
明文旧值读兼容，写路径会加密新的敏感字段。
"""

from __future__ import annotations

from typing import Any

from ..crypto import decrypt_str, encrypt_str
from .redactor import REDACTED, is_sensitive_key

SECRET_ENVELOPE_PREFIX = "secret:v1:"
_MASK_PLACEHOLDERS = frozenset({"", REDACTED, "••••••••••••••••", "***"})


class PluginConfigDecryptionError(ValueError):
    """Raised when a runtime plugin config contains an unreadable envelope."""

    def __init__(self, field_path: str) -> None:
        self.field_paths = (field_path or "<root>",)
        super().__init__(
            f"插件配置敏感字段解密失败: {self.field_paths[0]}（请检查 MASTER_KEY 或重新保存配置）"
        )


def is_secret_envelope(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_ENVELOPE_PREFIX)


def wrap_secret(plain: str) -> str:
    text = str(plain or "")
    if not text or is_secret_envelope(text):
        return text
    return f"{SECRET_ENVELOPE_PREFIX}{encrypt_str(text)}"


def unwrap_secret(value: Any) -> str:
    """Decrypt envelope or return plain text for legacy values."""

    if value is None:
        return ""
    text = str(value)
    if not is_secret_envelope(text):
        return text
    return decrypt_str(text[len(SECRET_ENVELOPE_PREFIX) :])


def _schema_marks_sensitive(prop: dict[str, Any]) -> bool:
    if prop.get("x-sensitive") is True or prop.get("sensitive") is True:
        return True
    fmt = str(prop.get("format") or "").strip().lower()
    if fmt in {"password", "secret", "token"}:
        return True
    return str(prop.get("type") or "").strip().lower() == "password"


def _path_is_sensitive(key: str, prop: dict[str, Any] | None = None) -> bool:
    if is_sensitive_key(key):
        return True
    if isinstance(prop, dict) and _schema_marks_sensitive(prop):
        return True
    return False


def encrypt_config_secrets(
    config: dict[str, Any] | None,
    *,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recursively encrypt sensitive string fields for DB storage."""

    return _transform_config(dict(config or {}), schema=schema, mode="encrypt")


def decrypt_config_secrets(
    config: dict[str, Any] | None,
    *,
    schema: dict[str, Any] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Recursively decrypt secret envelopes for runtime use."""

    return _transform_config(
        dict(config or {}),
        schema=schema,
        mode="decrypt",
        strict_decrypt=strict,
    )


def mask_config_secrets(
    config: dict[str, Any] | None,
    *,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return API-safe config with sensitive values masked."""

    return _transform_config(dict(config or {}), schema=schema, mode="mask")


def count_encryptable_secrets(
    config: dict[str, Any] | None,
    *,
    schema: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Count sensitive plaintexts and already-encrypted envelopes (no secrets leaked)."""

    counters = {"plain": 0, "envelope": 0, "empty": 0}
    _count_walk(dict(config or {}), schema=schema, counters=counters)
    return counters


def _child_schema_for_value(prop: dict[str, Any] | None, value: Any) -> dict[str, Any] | None:
    """Descend schema for object/array nesting (including items.properties)."""

    if not isinstance(prop, dict):
        return None
    prop_type = str(prop.get("type") or "").strip().lower()
    if isinstance(value, dict):
        # object property schema, or array items that are objects
        if prop_type in {"", "object"} and isinstance(prop.get("properties"), dict):
            return prop
        if prop_type == "array" and isinstance(prop.get("items"), dict):
            items = prop["items"]
            if isinstance(items, dict) and (
                str(items.get("type") or "").strip().lower() in {"", "object"}
                or isinstance(items.get("properties"), dict)
            ):
                return items
        return prop if isinstance(prop.get("properties"), dict) else None
    if isinstance(value, list):
        items = prop.get("items")
        return items if isinstance(items, dict) else None
    return None


def _transform_config(
    value: Any,
    *,
    schema: dict[str, Any] | None,
    mode: str,
    parent_key: str | None = None,
    parent_prop: dict[str, Any] | None = None,
    strict_decrypt: bool = False,
    field_path: str = "",
) -> Any:
    if isinstance(value, dict):
        properties: dict[str, Any] = {}
        if isinstance(schema, dict):
            raw_props = schema.get("properties")
            if isinstance(raw_props, dict):
                properties = raw_props
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            prop = properties.get(key) if isinstance(properties.get(key), dict) else None
            child_schema = _child_schema_for_value(prop, item)
            out[key_text] = _transform_config(
                item,
                schema=child_schema,
                mode=mode,
                parent_key=key_text,
                parent_prop=prop,
                strict_decrypt=strict_decrypt,
                field_path=f"{field_path}.{key_text}" if field_path else key_text,
            )
        return out
    if isinstance(value, list):
        item_schema = None
        if isinstance(schema, dict) and isinstance(schema.get("items"), dict):
            item_schema = schema.get("items")
        elif isinstance(schema, dict) and str(schema.get("type") or "").strip().lower() != "array":
            # schema 已是 array items 对象定义（从父级 items 传入）
            item_schema = schema
        return [
            _transform_config(
                item,
                schema=item_schema if isinstance(item_schema, dict) else None,
                mode=mode,
                # 数组元素是 object 时，敏感键在元素字段名上，不再沿用父数组字段名。
                parent_key=None if isinstance(item, dict) else parent_key,
                parent_prop=None if isinstance(item, dict) else parent_prop,
                strict_decrypt=strict_decrypt,
                field_path=f"{field_path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    envelope = is_secret_envelope(value)
    if (
        parent_key is None
        or not _path_is_sensitive(parent_key, parent_prop)
    ) and not (envelope and mode in {"decrypt", "mask"}):
        return value
    if not isinstance(value, str):
        return value
    if value in _MASK_PLACEHOLDERS:
        return value if mode != "mask" else (REDACTED if value else "")
    if mode == "encrypt":
        return wrap_secret(value)
    if mode == "decrypt":
        try:
            return unwrap_secret(value)
        except Exception as exc:  # noqa: BLE001
            if strict_decrypt:
                raise PluginConfigDecryptionError(field_path or parent_key or "<root>") from exc
            # API / 迁移等非运行时读取保留信封；Worker 使用 strict=True 隔离故障插件。
            return value
    # mask
    if not value:
        return ""
    return REDACTED


def _count_walk(
    value: Any,
    *,
    schema: dict[str, Any] | None,
    counters: dict[str, int],
    parent_key: str | None = None,
    parent_prop: dict[str, Any] | None = None,
) -> None:
    if isinstance(value, dict):
        properties: dict[str, Any] = {}
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
            properties = schema["properties"]
        for key, item in value.items():
            prop = properties.get(key) if isinstance(properties.get(key), dict) else None
            child_schema = _child_schema_for_value(prop, item)
            _count_walk(
                item,
                schema=child_schema,
                counters=counters,
                parent_key=str(key),
                parent_prop=prop,
            )
        return
    if isinstance(value, list):
        item_schema = None
        if isinstance(schema, dict) and isinstance(schema.get("items"), dict):
            item_schema = schema.get("items")
        elif isinstance(schema, dict) and str(schema.get("type") or "").strip().lower() != "array":
            item_schema = schema
        for item in value:
            _count_walk(
                item,
                schema=item_schema if isinstance(item_schema, dict) else None,
                counters=counters,
                parent_key=None if isinstance(item, dict) else parent_key,
                parent_prop=None if isinstance(item, dict) else parent_prop,
            )
        return
    if parent_key is None or not _path_is_sensitive(parent_key, parent_prop):
        return
    if not isinstance(value, str) or value in _MASK_PLACEHOLDERS:
        if value in ("", None):
            counters["empty"] += 1
        return
    if is_secret_envelope(value):
        counters["envelope"] += 1
    else:
        counters["plain"] += 1


__all__ = [
    "PluginConfigDecryptionError",
    "SECRET_ENVELOPE_PREFIX",
    "count_encryptable_secrets",
    "decrypt_config_secrets",
    "encrypt_config_secrets",
    "is_secret_envelope",
    "mask_config_secrets",
    "unwrap_secret",
    "wrap_secret",
]

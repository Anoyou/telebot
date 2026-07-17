"""交互规则 service：账号级配置 JSON，仅 flush。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.system import SystemSetting
from . import account_bot_service


class InteractionRuleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _load_config(db: AsyncSession, account_id: int) -> dict[str, Any]:
    await account_bot_service.ensure_account(db, int(account_id))
    return await account_bot_service.get_transfer_notice_config(db, int(account_id))


async def _save_rules(db: AsyncSession, account_id: int, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    setting_key = account_bot_service.transfer_notice_setting_key(int(account_id))
    row = await db.get(SystemSetting, setting_key)
    current = account_bot_service.normalize_transfer_notice_config(row.value if row is not None else None)
    normalized = account_bot_service.normalize_interaction_rules(rules)
    current["rules"] = normalized
    data = account_bot_service.normalize_transfer_notice_config(current)
    if row is None:
        row = SystemSetting(key=setting_key, value=data)
        db.add(row)
    else:
        row.value = data
    await db.flush()
    return account_bot_service.normalize_interaction_rules(data.get("rules"))


async def list_rules(db: AsyncSession, account_id: int) -> list[dict[str, Any]]:
    cfg = await _load_config(db, account_id)
    return account_bot_service.normalize_interaction_rules(cfg.get("rules"))


async def get_rule(db: AsyncSession, account_id: int, rule_id: str) -> dict[str, Any] | None:
    rid = str(rule_id or "").strip()
    for rule in await list_rules(db, account_id):
        if str(rule.get("id")) == rid:
            return rule
    return None


async def save_rule(
    db: AsyncSession,
    account_id: int,
    *,
    rule_id: str | None = None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """创建或更新交互规则。有 rule_id 时只合并明确字段。"""

    rules = await list_rules(db, account_id)
    fields = dict(fields or {})
    rid = str(rule_id or fields.get("id") or "").strip()

    if rid:
        idx = next((i for i, r in enumerate(rules) if str(r.get("id")) == rid), None)
        if idx is None:
            raise InteractionRuleError("NOT_FOUND", f"交互规则 {rid} 不存在")
        merged = dict(rules[idx])
        for key, value in fields.items():
            if key == "id":
                continue
            if value is not None or key in fields:
                merged[key] = value
        merged["id"] = rid
        rules[idx] = merged
        saved = await _save_rules(db, account_id, rules)
        for rule in saved:
            if str(rule.get("id")) == rid:
                return rule
        return merged

    new_id = str(uuid.uuid4())[:8]
    created = {
        "id": new_id,
        "name": str(fields.get("name") or f"规则-{new_id}")[:64],
        "enabled": bool(fields.get("enabled", True)),
        **{k: v for k, v in fields.items() if k not in {"id"} and v is not None},
    }
    created["id"] = new_id
    rules.append(created)
    saved = await _save_rules(db, account_id, rules)
    for rule in saved:
        if str(rule.get("id")) == new_id:
            return rule
    return created


async def set_enabled(db: AsyncSession, account_id: int, rule_id: str, enabled: bool) -> dict[str, Any]:
    return await save_rule(
        db,
        account_id,
        rule_id=rule_id,
        fields={"enabled": bool(enabled)},
    )


async def delete_rule(db: AsyncSession, account_id: int, rule_id: str) -> dict[str, Any]:
    rid = str(rule_id or "").strip()
    rules = await list_rules(db, account_id)
    remaining = [r for r in rules if str(r.get("id")) != rid]
    if len(remaining) == len(rules):
        raise InteractionRuleError("NOT_FOUND", f"交互规则 {rid} 不存在")
    deleted = next(r for r in rules if str(r.get("id")) == rid)
    await _save_rules(db, account_id, remaining)
    return {"id": deleted.get("id"), "name": deleted.get("name"), "deleted": True}


__all__ = [
    "InteractionRuleError",
    "delete_rule",
    "get_rule",
    "list_rules",
    "save_rule",
    "set_enabled",
]

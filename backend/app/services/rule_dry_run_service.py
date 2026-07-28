"""通用 Rule 的无副作用试运行服务，供 REST API 之外的 Agent 复用。"""

from __future__ import annotations

import sys
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.feature import (
    FEATURE_AUTO_REPLY,
    FEATURE_AUTOREPEAT,
    FEATURE_CODEX_IMAGE,
    FEATURE_FORWARD,
    FEATURE_SCHEDULER,
)
from ..db.models.plugin import PLUGIN_TRUST_ORPHAN, InstalledPlugin
from ..db.models.rule import Rule
from ..settings import settings


async def _installed_match(
    db: AsyncSession,
    feature_key: str,
    *args: Any,
) -> tuple[bool, str | None]:
    installed = await db.get(InstalledPlugin, feature_key)
    if installed is None or not bool(installed.enabled):
        return False, f"{feature_key} 插件未安装或未全局启用"
    if str(installed.trust_tier or "") == PLUGIN_TRUST_ORPHAN:
        return False, f"{feature_key} 安装记录为 orphan，不能试运行"
    if installed.signature_ok is False or (
        installed.signature_ok is None
        and not bool(settings.plugin_allow_legacy_unsigned_plugins)
    ):
        return False, f"{feature_key} 签名状态不允许执行"
    if str(installed.last_install_error or "").strip():
        return False, f"{feature_key} 当前安装失败，不能试运行"

    from ..worker.plugins.loader import _installed_module_name, _load_installed_plugin

    loaded = _load_installed_plugin(feature_key)
    if feature_key not in loaded:
        return False, f"{feature_key} 插件加载失败"
    package_name = _installed_module_name(feature_key)
    for module_name in (f"{package_name}.plugin", package_name):
        module = sys.modules.get(module_name)
        dry_run = getattr(module, "_dry_run_match", None)
        if callable(dry_run):
            result = dry_run(*args)
            if hasattr(result, "__await__"):
                result = await result
            matched, output = result
            return bool(matched), str(output) if output is not None else None
    return False, f"{feature_key} 插件未提供 dry-run"


async def dry_run_rule(
    db: AsyncSession,
    rule: Rule,
    *,
    sample_message: str,
    sample_chat_type: str = "private",
    sample_chat_id: int | None = None,
) -> dict[str, Any]:
    """执行无真实发送的匹配/调度预览。"""

    key = str(rule.feature_key)
    cfg = dict(rule.config or {})
    if key == FEATURE_FORWARD:
        from ..worker.plugins.builtin.forward.plugin import _dry_run_match

        matched, output = _dry_run_match(cfg, sample_message, sample_chat_id)
    elif key == FEATURE_AUTO_REPLY:
        matched, output = await _installed_match(
            db,
            key,
            cfg,
            sample_message,
            sample_chat_type or "private",
            sample_chat_id,
        )
    elif key in {FEATURE_AUTOREPEAT, FEATURE_CODEX_IMAGE}:
        matched, output = await _installed_match(
            db,
            key,
            cfg,
            sample_message,
            sample_chat_id,
        )
    elif key == FEATURE_SCHEDULER:
        from .rule_service import normalize_scheduler_config

        normalized = await normalize_scheduler_config(db, cfg)
        next_fire = normalized.get("next_fire")
        action = normalized.get("action") if isinstance(normalized.get("action"), dict) else {}
        matched = False
        output = f"next fire at {next_fire or 'N/A'}"
        return {
            "matched": matched,
            "output": output,
            "detail": {
                "feature": key,
                "rule_id": rule.id,
                "kind": normalized.get("kind", "cron"),
                "next_fire": next_fire,
                "action_type": action.get("type", "send_message"),
                "target_chat_id": action.get("target_chat_id"),
                "note": "仅计算调度与动作摘要，不会真实执行。",
            },
        }
    else:
        matched, output = await _installed_match(
            db,
            key,
            cfg,
            sample_message,
            sample_chat_id,
        )
    return {
        "matched": bool(matched),
        "output": output,
        "detail": {
            "feature": key,
            "rule_id": rule.id,
            "sample_chat_type": sample_chat_type,
            "sample_chat_id": sample_chat_id,
            "note": "dry-run 不会发送消息或修改业务数据。",
        },
    }


__all__ = ["dry_run_rule"]

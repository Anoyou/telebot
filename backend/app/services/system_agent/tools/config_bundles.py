"""账号 Config Bundle 导出、预览与确认导入工作流。"""

from __future__ import annotations

from typing import Any

from ....schemas.config_bundle import ConfigBundleExport
from ....services.config_bundle_service import (
    BundleConfirmError,
    apply_bundle_confirm,
    assert_bundle_size,
    available_command_templates,
    available_feature_map,
    build_preview_context_digest,
    build_preview_signature,
    compare_bundles,
    load_config_bundle,
)
from ..context import ToolContext
from ..registry import PreparedAction, ToolRegistry, ToolSpec
from ._helpers import account_scope_filter


def _account_id(ctx: ToolContext, args: dict[str, Any]) -> int:
    raw = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if raw is None:
        raise ValueError("需要 account_id")
    return int(raw)


async def export_bundle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    try:
        bundle = await load_config_bundle(ctx.db, account_id)
    except BundleConfirmError as exc:
        raise ValueError(exc.message) from None
    assert_bundle_size(bundle)
    return {
        "account_id": account_id,
        "bundle": bundle.model_dump(mode="json"),
        "note": "Bundle 已递归移除 Token、密码等敏感字段。",
    }


async def _prepare(
    ctx: ToolContext,
    args: dict[str, Any],
) -> tuple[int, ConfigBundleExport, Any, str]:
    account_id = _account_id(ctx, args)
    try:
        source = ConfigBundleExport.model_validate(args.get("bundle"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("bundle 结构不符合规范") from exc
    content = assert_bundle_size(source)
    try:
        target = await load_config_bundle(ctx.db, account_id)
    except BundleConfirmError as exc:
        raise ValueError(exc.message) from None
    features = await available_feature_map(ctx.db)
    templates = await available_command_templates(ctx.db)
    report = compare_bundles(
        source,
        target,
        available_features=features,
        available_command_templates=templates,
    )
    signature = build_preview_signature(
        account_id=account_id,
        file_content=content,
        apply_conflicts=bool(args.get("apply_conflicts")),
        confirm_chat_id_conflicts=bool(args.get("confirm_chat_id_conflicts")),
        preview_context_digest=build_preview_context_digest(
            target=target,
            available_features=features,
            available_command_templates=templates,
        ),
    )
    report.preview_signature = signature
    return account_id, source, report, signature


async def dry_run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id, _source, report, _signature = await _prepare(ctx, args)
    return {"account_id": account_id, **report.model_dump(mode="json")}


async def import_preview(ctx: ToolContext, args: dict[str, Any]) -> PreparedAction:
    account_id, source, report, signature = await _prepare(ctx, args)
    canonical = {
        "account_id": account_id,
        "bundle": source.model_dump(mode="json"),
        "apply_conflicts": bool(args.get("apply_conflicts")),
        "confirm_chat_id_conflicts": bool(args.get("confirm_chat_id_conflicts")),
        "preview_signature": signature,
    }
    return PreparedAction(
        arguments=canonical,
        preview={
            "summary": f"向账号 #{account_id} 导入 Config Bundle",
            "account_id": account_id,
            "dry_run": report.model_dump(mode="json"),
            "warning": "确认时会重新校验目标配置与预览签名；冲突策略以本卡片为准。",
        },
    )


async def import_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id, source, report, expected_signature = await _prepare(ctx, args)
    if str(args.get("preview_signature") or "") != expected_signature:
        raise ValueError("预览与确认参数不一致或目标配置已变化，请重新 dry-run")
    templates = await available_command_templates(ctx.db)
    try:
        imported, skipped, conflicts, warnings = await apply_bundle_confirm(
            ctx.db,
            account_id=account_id,
            source=source,
            dry_run=report,
            available_command_templates=templates,
            apply_conflicts=bool(args.get("apply_conflicts")),
            confirm_chat_id_conflicts=bool(args.get("confirm_chat_id_conflicts")),
            web_user_id=ctx.web_user_id,
        )
    except BundleConfirmError as exc:
        raise ValueError(exc.message) from None
    return {
        "account_id": account_id,
        "imported": imported,
        "skipped": skipped,
        "conflicts": conflicts,
        "warnings": warnings,
        "business_changed": imported > 0,
    }


def _schema(*, require_bundle: bool) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "account_id": {"type": "integer"},
            "bundle": {"type": "object"},
            "apply_conflicts": {"type": "boolean"},
            "confirm_chat_id_conflicts": {"type": "boolean"},
        },
        "additionalProperties": False,
    }
    schema["required"] = ["account_id", "bundle"] if require_bundle else ["account_id"]
    return schema


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="config_bundles.export",
            description="导出账号的脱敏 Config Bundle，包括功能、规则、指令链接与忽略名单。",
            input_schema=_schema(require_bundle=False),
            read_handler=export_bundle,
        )
    )
    registry.register(
        ToolSpec(
            name="config_bundles.dry_run",
            description="对 Config Bundle 执行只读差异预览、冲突与聊天 ID 风险检查。",
            input_schema=_schema(require_bundle=True),
            read_handler=dry_run,
        )
    )
    registry.register(
        ToolSpec(
            name="config_bundles.import",
            channels=("web",),
            description="先 dry-run，再把 Config Bundle 导入目标账号；确认时重新校验签名。",
            input_schema=_schema(require_bundle=True),
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=import_preview,
            execute_handler=import_execute,
            runtime_effects=("reload_config", "reload_commands", "reload_ignored"),
        )
    )


__all__ = ["register"]

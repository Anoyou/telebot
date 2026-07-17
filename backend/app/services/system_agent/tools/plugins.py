"""远程插件安装包：列表 / 安装 / 更新 / 卸载 / 全局启停。"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import clamp_limit


def _plugin_view(row: Any) -> dict[str, Any]:
    if hasattr(row, "model_dump"):
        data = row.model_dump()
    elif isinstance(row, dict):
        data = dict(row)
    else:
        data = {
            "name": getattr(row, "name", None),
            "display_name": getattr(row, "display_name", None),
            "version": getattr(row, "version", None),
            "enabled": getattr(row, "enabled", None),
            "source_url": getattr(row, "source_url", None),
            "update_available": getattr(row, "update_available", None),
            "latest_version": getattr(row, "latest_version", None),
            "description": getattr(row, "description", None),
        }
    # 不回显过大字段
    for key in ("event_subscriptions", "capabilities", "lint_warnings"):
        if key in data and data[key] is not None:
            data[key] = data[key] if key != "lint_warnings" else list(data[key] or [])[:20]
    return data


def _err(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if code:
        return f"{code}: {message}"
    return message


async def list_installed(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    rows = await svc.list_installed(ctx.db)
    limit = clamp_limit(args.get("limit"), default=100, maximum=200)
    items = [_plugin_view(r) for r in rows[:limit]]
    return {
        "count": len(items),
        "plugins": items,
        "note": "这是安装包全局状态（InstalledPlugin）；账号级启停请用 features.set_enabled。",
    }


async def get_plugin(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or args.get("plugin_key") or "").strip()
    if not name:
        return {"error": "name_required", "message": "需要 name"}
    row = await svc.get_by_name(ctx.db, name)
    if row is None:
        return {"error": "not_found", "message": f"插件 {name} 不存在"}
    return {"plugin": _plugin_view(row)}


async def check_updates_read(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or "").strip() or None
    summary = await svc.check_updates(ctx.db, name=name)
    return {
        "total": summary.total,
        "checked": summary.checked,
        "update_available": summary.update_available,
        "failed": summary.failed,
        "business_changed": False,
        "note": "仅检查更新标记，不会自动安装。可用 plugins.update 升级。",
    }


async def install_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    source_url = str(args.get("source_url") or args.get("url") or "").strip()
    name = str(args.get("name") or "").strip() or None
    if not source_url:
        raise ValueError("需要 source_url（Git HTTPS/SSH）")
    return {
        "summary": f"安装远程插件 {name or source_url}",
        "source_url": source_url,
        "name": name,
        "default_enabled": bool(args.get("default_enabled", False)),
        "warning": "危险：将从 Git 克隆插件并写入安装记录；需确认源可信。",
    }


async def install_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    source_url = str(args.get("source_url") or args.get("url") or "").strip()
    name = str(args.get("name") or "").strip() or None
    default_enabled = bool(args.get("default_enabled", False))
    try:
        row = await svc.install(
            ctx.db,
            source_url,
            name=name,
            default_enabled=default_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_err(exc)) from None
    plugin_name = getattr(row, "name", None) or name
    # 供 runtime_effects 使用
    if ctx.action is not None:
        args_store = dict(ctx.action.arguments or {})
        args_store["plugin_name"] = plugin_name
        ctx.action.arguments = args_store
    return {
        "plugin": _plugin_view(row),
        "plugin_name": plugin_name,
        "business_changed": True,
    }


async def update_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or args.get("plugin_key") or "").strip()
    if not name:
        raise ValueError("需要 name")
    row = await svc.get_by_name(ctx.db, name)
    if row is None:
        raise ValueError(f"插件 {name} 不存在")
    return {
        "summary": f"更新远程插件 {name}",
        "plugin": _plugin_view(row),
        "warning": "危险：将拉取远程最新版本并覆盖安装目录。",
    }


async def update_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or args.get("plugin_key") or "").strip()
    row = await svc.get_by_name(ctx.db, name)
    if row is None:
        raise ValueError(f"插件 {name} 不存在")
    if ctx.action is not None:
        args_store = dict(ctx.action.arguments or {})
        args_store["plugin_name"] = name
        ctx.action.arguments = args_store
    return {
        "plugin": _plugin_view(row),
        "plugin_name": name,
        "requested": True,
        "business_changed": False,
        "note": "Action 提交后执行插件更新与 Worker 重载。",
    }


async def uninstall_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or args.get("plugin_key") or "").strip()
    if not name:
        raise ValueError("需要 name")
    row = await svc.get_by_name(ctx.db, name)
    if row is None:
        raise ValueError(f"插件 {name} 不存在或不是可卸载的远程安装包")
    return {
        "summary": f"卸载远程插件 {name}",
        "plugin": _plugin_view(row),
        "warning": "危险：删除安装记录、功能矩阵相关行与插件目录，不可恢复。",
    }


async def uninstall_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or args.get("plugin_key") or "").strip()
    try:
        # 只改库；目录在 commit 后由 runtime_effects=plugin_fs_cleanup 删除
        ok = await svc.uninstall(ctx.db, name, remove_files=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_err(exc)) from None
    if not ok:
        # 禁止标 executed 后误跑 plugin_fs_cleanup
        raise ValueError(f"插件 {name} 不存在或不可卸载（仅远程安装包）")
    if ctx.action is not None:
        args_store = dict(ctx.action.arguments or {})
        args_store["plugin_name"] = name
        ctx.action.arguments = args_store
    return {"name": name, "deleted": True, "business_changed": True}


async def set_package_enabled_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or args.get("plugin_key") or "").strip()
    enabled = bool(args.get("enabled"))
    if not name:
        raise ValueError("需要 name")
    row = await svc.get_by_name(ctx.db, name)
    if row is None:
        raise ValueError(f"插件 {name} 不存在")
    return {
        "summary": f"{'启用' if enabled else '禁用'}安装包全局状态 {name}",
        "plugin": _plugin_view(row),
        "target_enabled": enabled,
        "note": "这是 InstalledPlugin.enabled 全局开关，不是账号级 features.set_enabled。",
    }


async def set_package_enabled_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import remote_plugin_service as svc

    name = str(args.get("name") or args.get("plugin_key") or "").strip()
    enabled = bool(args.get("enabled"))
    try:
        row = await svc.set_enabled(ctx.db, name, enabled=enabled, bootstrap_accounts=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_err(exc)) from None
    if ctx.action is not None:
        args_store = dict(ctx.action.arguments or {})
        args_store["plugin_name"] = name
        ctx.action.arguments = args_store
    return {"plugin": _plugin_view(row), "business_changed": True}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="plugins.list_installed",
            description="列出已安装的远程/本地导入插件包（全局状态，非账号级启停）。",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_installed,
        )
    )
    registry.register(
        ToolSpec(
            name="plugins.get",
            description="获取单个已安装插件包详情。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "plugin_key": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_plugin,
        )
    )
    registry.register(
        ToolSpec(
            name="plugins.check_updates",
            description="检查已安装远程插件是否有更新。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            min_role="operator",
            read_handler=check_updates_read,
        )
    )
    registry.register(
        ToolSpec(
            name="plugins.install",
            description="从 Git URL 安装远程插件包。",
            input_schema={
                "type": "object",
                "properties": {
                    "source_url": {"type": "string"},
                    "url": {"type": "string"},
                    "name": {"type": "string"},
                    "default_enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=install_preview,
            execute_handler=install_execute,
            runtime_effects=("plugin_reload",),
        )
    )
    registry.register(
        ToolSpec(
            name="plugins.update",
            description="更新已安装远程插件到最新版本。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "plugin_key": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=update_preview,
            execute_handler=update_execute,
            runtime_effects=("plugin_update",),
        )
    )
    registry.register(
        ToolSpec(
            name="plugins.uninstall",
            description="卸载远程插件包（危险，不可恢复）。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "plugin_key": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=uninstall_preview,
            execute_handler=uninstall_execute,
            runtime_effects=("plugin_fs_cleanup", "plugin_reload"),
        )
    )
    registry.register(
        ToolSpec(
            name="plugins.set_package_enabled",
            description="启停插件安装包全局开关（InstalledPlugin.enabled）。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "plugin_key": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["enabled"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            preview_handler=set_package_enabled_preview,
            execute_handler=set_package_enabled_execute,
            runtime_effects=("plugin_reload",),
        )
    )

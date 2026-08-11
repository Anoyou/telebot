"""插件远程仓库：列表 / 浏览 / 添加 / 删除 / 从仓库安装。"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec


def _repo_view(row: Any) -> dict[str, Any]:
    return {
        "id": getattr(row, "id", None),
        "name": getattr(row, "name", None),
        "url": getattr(row, "url", None),
        "description": getattr(row, "description", None),
        "auth_type": getattr(row, "auth_type", None),
        "has_credential": bool(getattr(row, "credential_enc", None)),
        "added_at": getattr(row, "added_at", None).isoformat()
        if getattr(row, "added_at", None)
        else None,
    }


def _plugin_in_repo_view(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        data = item.model_dump()
    elif isinstance(item, dict):
        data = dict(item)
    else:
        data = {
            "name": getattr(item, "name", None),
            "display_name": getattr(item, "display_name", None),
            "version": getattr(item, "version", None),
            "installed": getattr(item, "installed", None),
            "description": getattr(item, "description", None),
        }
    data.pop("credential", None)
    data.pop("token", None)
    return data


def _err(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if code:
        return f"{code}: {message}"
    return message


async def list_repos(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    rows = await svc.list_repos(ctx.db)
    return {"count": len(rows), "repos": [_repo_view(r) for r in rows]}


async def list_repo_plugins(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    repo_id = args.get("repo_id") or args.get("id")
    if repo_id in (None, ""):
        return {"error": "repo_id_required", "message": "需要 repo_id"}
    try:
        plugins = await svc.list_plugins_in_repo(
            ctx.db,
            int(repo_id),
            force_refresh=bool(args.get("force_refresh")),
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": "list_failed", "message": _err(exc)}
    return {
        "repo_id": int(repo_id),
        "count": len(plugins),
        "plugins": [_plugin_in_repo_view(p) for p in plugins],
    }


async def refresh_repo_plugins(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    result = await list_repo_plugins(ctx, {**args, "force_refresh": True})
    if "error" not in result:
        result["refreshed"] = True
    return result


async def create_repo_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or args.get("source_url") or "").strip()
    if not url:
        raise ValueError("需要 url")
    return {
        "summary": f"添加插件仓库 {url}",
        "url": url,
        "name": args.get("name"),
        "has_credential": bool(args.get("credential") or args.get("token")),
        "note": "仅保存仓库索引；安装插件需另调 plugin_repos.install_plugin。",
    }


async def create_repo_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    url = str(args.get("url") or args.get("source_url") or "").strip()
    token = args.get("credential") or args.get("token")
    auth_type = args.get("auth_type")
    if token and not auth_type:
        auth_type = "github_token"
    try:
        row = await svc.create_repo(
            ctx.db,
            url,
            name=str(args.get("name") or "").strip() or None,
            description=str(args.get("description") or "").strip() or None,
            auth_type=str(auth_type) if auth_type else None,
            credential=str(token) if token else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_err(exc)) from None
    return {"repo": _repo_view(row), "business_changed": True}


async def credential_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    repo_id = int(args.get("repo_id") or args.get("id"))
    row = await svc.get_repo(ctx.db, repo_id)
    clear = bool(args.get("clear")) or str(args.get("auth_type") or "").lower() in {
        "none",
        "public",
    }
    return {
        "summary": f"{'清除' if clear else '更新'}插件仓库 #{repo_id} 的访问凭据",
        "repo": _repo_view(row),
        "target_auth_type": "none" if clear else (args.get("auth_type") or "github_token"),
        "has_new_token": bool(args.get("token") or args.get("credential")),
        "warning": "新凭据只会加密保存，不会在结果中回显。",
    }


async def credential_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    repo_id = int(args.get("repo_id") or args.get("id"))
    clear = bool(args.get("clear")) or str(args.get("auth_type") or "").lower() in {
        "none",
        "public",
    }
    token = None if clear else (args.get("token") or args.get("credential"))
    if not clear and not str(token or "").strip():
        raise ValueError("更新私有仓库凭据需要 token；清除凭据请传 clear=true")
    try:
        row = await svc.update_repo_credential(
            ctx.db,
            repo_id,
            auth_type="none" if clear else str(args.get("auth_type") or "github_token"),
            token=None if clear else str(token),
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_err(exc)) from None
    return {"repo": _repo_view(row), "business_changed": True}


async def bulk_update_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    repo_id = int(args.get("repo_id") or args.get("id"))
    row = await svc.get_repo(ctx.db, repo_id)
    plugins = await svc.list_plugins_in_repo(ctx.db, repo_id, force_refresh=True)
    updates = [item for item in plugins if bool(getattr(item, "update_available", False))]
    return {
        "summary": f"更新仓库 #{repo_id} 中 {len(updates)} 个已安装插件",
        "repo": _repo_view(row),
        "updates": [_plugin_in_repo_view(item) for item in updates],
        "warning": "危险：会替换本机已安装插件文件，并让 Worker 重新加载；不会自动修改插件配置。",
    }


async def bulk_update_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    repo_id = int(args.get("repo_id") or args.get("id"))
    row = await svc.get_repo(ctx.db, repo_id)
    return {
        "repo_id": repo_id,
        "repo_name": row.name,
        "requested": True,
        "business_changed": False,
        "note": "Action 提交后执行批量更新并记录逐插件结果。",
    }


async def delete_repo_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    repo_id = int(args.get("repo_id") or args.get("id"))
    row = await svc.get_repo(ctx.db, repo_id)
    return {
        "summary": f"删除插件仓库 #{repo_id} {row.name}",
        "repo": _repo_view(row),
        "warning": "仅从目录索引移除，不会卸载已安装插件。",
    }


async def delete_repo_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc

    repo_id = int(args.get("repo_id") or args.get("id"))
    try:
        row = await svc.get_repo(ctx.db, repo_id)
        repo_url = str(row.url)
        ok = await svc.delete_repo(ctx.db, repo_id, remove_cache=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_err(exc)) from None
    if not ok:
        raise ValueError(f"插件仓库 #{repo_id} 不存在或已删除")
    if ctx.action is not None:
        stored = dict(ctx.action.arguments or {})
        stored["repo_url"] = repo_url
        ctx.action.arguments = stored
    return {
        "id": repo_id,
        "repo_url": repo_url,
        "deleted": True,
        "business_changed": True,
    }


async def install_from_repo_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    repo_id = args.get("repo_id")
    plugin_name = str(args.get("plugin_name") or args.get("name") or "").strip()
    if not plugin_name:
        raise ValueError("需要 plugin_name")
    if repo_id in (None, ""):
        raise ValueError("需要 repo_id")
    return {
        "summary": f"从仓库 #{repo_id} 安装插件 {plugin_name}",
        "repo_id": repo_id,
        "plugin_name": plugin_name,
        "default_enabled": bool(args.get("default_enabled", False)),
        "warning": "危险：将安装插件代码到本机 plugins/installed。",
    }


async def install_from_repo_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import plugin_repo_service as svc
    from ....services.remote_plugin_service import remote_plugin_view_from_installed

    plugin_name = str(args.get("plugin_name") or args.get("name") or "").strip()
    default_enabled = bool(args.get("default_enabled", False))
    try:
        row = await svc.install_plugin_from_repo(
            ctx.db,
            int(args.get("repo_id")),
            plugin_name,
            default_enabled=default_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_err(exc)) from None

    # install_plugin_from_repo 可能返回 RemotePluginView 或 ORM
    if hasattr(row, "key") and not hasattr(row, "name"):
        view = remote_plugin_view_from_installed(row)
        name = view.name
        plugin = view.model_dump() if hasattr(view, "model_dump") else {"name": name}
    else:
        name = getattr(row, "name", plugin_name)
        plugin = row.model_dump() if hasattr(row, "model_dump") else {"name": name}

    if ctx.action is not None:
        store = dict(ctx.action.arguments or {})
        store["plugin_name"] = name
        ctx.action.arguments = store
    return {"plugin": plugin, "plugin_name": name, "business_changed": True}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="plugin_repos.refresh",
            channels=("web",),
            description="强制刷新插件仓库缓存并返回最新插件目录。",
            input_schema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "integer"},
                    "id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=refresh_repo_plugins,
        )
    )
    registry.register(
        ToolSpec(
            name="plugin_repos.list",
            channels=("web",),
            description="列出已保存的插件远程仓库（不含凭据明文）。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            min_role="viewer",
            read_handler=list_repos,
        )
    )
    registry.register(
        ToolSpec(
            name="plugin_repos.list_plugins",
            channels=("web",),
            description="浏览某个仓库内的插件列表。",
            input_schema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "integer"},
                    "id": {"type": "integer"},
                    "force_refresh": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_repo_plugins,
        )
    )
    registry.register(
        ToolSpec(
            name="plugin_repos.update_credential",
            channels=("web",),
            description="更新或清除已保存插件仓库的加密访问凭据。",
            input_schema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "integer"},
                    "id": {"type": "integer"},
                    "auth_type": {"type": "string"},
                    "token": {"type": "string"},
                    "credential": {"type": "string"},
                    "clear": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            secret_argument_names=("token", "credential"),
            preview_handler=credential_preview,
            execute_handler=credential_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="plugin_repos.update_installed",
            channels=("web",),
            description="批量更新指定仓库中已有新版本的已安装插件。",
            input_schema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "integer"},
                    "id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=bulk_update_preview,
            execute_handler=bulk_update_execute,
            runtime_effects=("plugin_repo_bulk_update",),
            runtime_retryable=False,
        )
    )
    registry.register(
        ToolSpec(
            name="plugin_repos.create",
            channels=("web",),
            description="添加插件远程仓库。私有仓库可带 credential/token（会加密保存）。",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "source_url": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "auth_type": {"type": "string"},
                    "credential": {"type": "string"},
                    "token": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            secret_argument_names=("credential", "token"),
            preview_handler=create_repo_preview,
            execute_handler=create_repo_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="plugin_repos.delete",
            channels=("web",),
            description="删除已保存的插件仓库索引（不卸载已安装插件）。",
            input_schema={
                "type": "object",
                "properties": {"repo_id": {"type": "integer"}, "id": {"type": "integer"}},
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_repo_preview,
            execute_handler=delete_repo_execute,
            runtime_effects=("plugin_repo_cache_cleanup",),
        )
    )
    registry.register(
        ToolSpec(
            name="plugin_repos.install_plugin",
            channels=("web",),
            description="从使用者已保存的插件仓库安装插件。",
            input_schema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "integer"},
                    "plugin_name": {"type": "string"},
                    "name": {"type": "string"},
                    "default_enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=install_from_repo_preview,
            execute_handler=install_from_repo_execute,
            runtime_effects=("plugin_reload",),
        )
    )

"""出口代理与 Telegram 设备档案工作流。"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from python_socks import ProxyConnectionError, ProxyError, ProxyType
from python_socks.async_.asyncio import Proxy as AsyncProxy
from sqlalchemy import select, update

from ....crypto import decrypt_str, encrypt_str
from ....db.models.account import Account, DeviceProfile, Proxy
from ....db.models.command import LLMProvider
from ....schemas.device_profile import DeviceProfileOut
from ....services import proxy_probe_cache
from ....services.network_service import get_network_info
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec

_VALID_PROXY_TYPES = {"socks5", "http", "https", "mtproxy"}
_TG_HOST = "149.154.167.50"
_TG_PORT = 443


def _proxy_view(row: Proxy) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": row.type,
        "host": row.host,
        "port": row.port,
        "username": row.username,
        "has_password": bool(row.password_enc),
    }


def _device_view(row: DeviceProfile) -> dict[str, Any]:
    return DeviceProfileOut.model_validate(row).model_dump(mode="json")


def _mark_gateway_candidate_sync(ctx: ToolContext) -> None:
    ctx.gateway_candidate_sync = True


def _parse_proxy_args(args: dict[str, Any]) -> dict[str, Any]:
    data = dict(args)
    raw_host = str(data.get("host") or data.get("url") or "").strip()
    if "://" in raw_host:
        parsed = urlsplit(raw_host)
        proxy_type = parsed.scheme.lower()
        if proxy_type not in _VALID_PROXY_TYPES or not parsed.hostname:
            raise ValueError("代理 URL 无效或类型不受支持")
        data["type"] = proxy_type
        data["host"] = parsed.hostname
        if parsed.port is not None:
            data["port"] = parsed.port
        if parsed.username is not None and data.get("username") is None:
            data["username"] = unquote(parsed.username)
        if parsed.password is not None and data.get("password") is None:
            data["password"] = unquote(parsed.password)
    else:
        data["host"] = raw_host
    if data.get("type") is not None:
        data["type"] = str(data["type"]).lower()
        if data["type"] not in _VALID_PROXY_TYPES:
            raise ValueError("代理类型必须是 socks5、http、https 或 mtproxy")
    if data.get("port") is not None:
        port = int(data["port"])
        if not 1 <= port <= 65535:
            raise ValueError("代理端口必须在 1 到 65535 之间")
        data["port"] = port
    return data


async def list_proxies(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = list((await ctx.db.execute(select(Proxy).order_by(Proxy.id.asc()))).scalars().all())
    return {"count": len(rows), "proxies": [_proxy_view(row) for row in rows]}


async def network_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    info = await get_network_info(force=bool(args.get("force")))
    return info.model_dump(mode="json")


async def get_proxy(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    proxy_id = int(args.get("id") or args.get("proxy_id"))
    row = await ctx.db.get(Proxy, proxy_id)
    if row is None:
        return {"error": "not_found", "message": f"代理 #{proxy_id} 不存在"}
    accounts = list(
        (
            await ctx.db.execute(
                select(Account.id, Account.display_name, Account.phone)
                .where(Account.proxy_id == proxy_id)
                .order_by(Account.id)
            )
        ).all()
    )
    providers = list(
        (
            await ctx.db.execute(
                select(LLMProvider.id, LLMProvider.name, LLMProvider.default_model)
                .where(LLMProvider.proxy_id == proxy_id)
                .order_by(LLMProvider.id)
            )
        ).all()
    )
    return {
        "proxy": _proxy_view(row),
        "usage": {
            "accounts": [{"id": item[0], "name": item[1], "phone": item[2]} for item in accounts],
            "providers": [{"id": item[0], "name": item[1], "model": item[2]} for item in providers],
            "total": len(accounts) + len(providers),
        },
    }


async def _probe_endpoint(host: str, port: int) -> str | None:
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3.0)
        return None
    except TimeoutError:
        return f"连接代理入口 {host}:{port} 超时"
    except OSError as exc:
        return f"代理入口 {host}:{port} 不可达：{exc}"
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


async def test_proxy(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    proxy_id = int(args.get("id") or args.get("proxy_id"))
    row = await ctx.db.get(Proxy, proxy_id)
    if row is None:
        raise ValueError(f"代理 #{proxy_id} 不存在")
    error = await _probe_endpoint(row.host, row.port)
    if error:
        return {"ok": False, "proxy_id": proxy_id, "error": error}
    password = decrypt_str(row.password_enc) if row.password_enc else None
    started = time.monotonic()
    try:
        if row.type in {"socks5", "http", "https"}:
            proxy = AsyncProxy(
                proxy_type={
                    "socks5": ProxyType.SOCKS5,
                    "http": ProxyType.HTTP,
                    "https": ProxyType.HTTP,
                }[row.type],
                host=row.host,
                port=row.port,
                username=row.username or None,
                password=password or None,
            )
            sock = await asyncio.wait_for(proxy.connect(dest_host=_TG_HOST, dest_port=_TG_PORT), timeout=8.0)
            sock.close()
        elif row.type == "mtproxy":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            await asyncio.get_running_loop().run_in_executor(None, sock.connect, (row.host, row.port))
            sock.close()
        else:
            return {"ok": False, "proxy_id": proxy_id, "error": "不支持的代理类型"}
    except (ProxyConnectionError, ProxyError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "proxy_id": proxy_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    latency_ms = int((time.monotonic() - started) * 1000)
    await proxy_probe_cache.set_probe(
        proxy_id,
        ok=True,
        exit_ip=None,
        country=None,
        region=None,
        city=None,
        latency_ms=latency_ms,
    )
    return {"ok": True, "proxy_id": proxy_id, "latency_ms": latency_ms}


async def save_proxy_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_proxy_args(args)
    proxy_id = parsed.get("id") or parsed.get("proxy_id")
    if proxy_id not in (None, ""):
        row = await ctx.db.get(Proxy, int(proxy_id))
        if row is None:
            raise ValueError(f"代理 #{proxy_id} 不存在")
        usage = await get_proxy(ctx, {"proxy_id": int(proxy_id)})
        return {
            "summary": f"更新代理 #{proxy_id} {row.host}:{row.port}",
            "mode": "update",
            "current": _proxy_view(row),
            "target_fields": {
                key: ("***" if key == "password" and value else value)
                for key, value in parsed.items()
                if key not in {"id", "proxy_id", "url"}
            },
            "has_password_input": bool(parsed.get("password")),
            "affected_accounts": usage.get("usage", {}).get("accounts", []),
            "affected_providers": usage.get("usage", {}).get("providers", []),
            "note": "更新后会刷新引用 Provider 的 AI 指令，并重启直接引用该代理的账号 Worker。",
        }
    if not parsed.get("host") or parsed.get("port") is None or not parsed.get("type"):
        raise ValueError("创建代理需要 type、host 与 port，或完整代理 URL")
    return {
        "summary": f"创建 {parsed['type']} 代理 {parsed['host']}:{parsed['port']}",
        "mode": "create",
        "proxy": {
            "type": parsed["type"],
            "host": parsed["host"],
            "port": parsed["port"],
            "username": parsed.get("username"),
            "has_password": bool(parsed.get("password")),
        },
    }


async def save_proxy_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_proxy_args(args)
    proxy_id = parsed.get("id") or parsed.get("proxy_id")
    if proxy_id not in (None, ""):
        row = await ctx.db.get(Proxy, int(proxy_id))
        if row is None:
            raise ValueError(f"代理 #{proxy_id} 不存在")
        for key in ("type", "host", "port", "username"):
            if key in parsed and parsed[key] is not None:
                setattr(row, key, parsed[key])
        if bool(parsed.get("clear_password")):
            row.password_enc = None
        elif parsed.get("password") not in (None, ""):
            row.password_enc = encrypt_str(str(parsed["password"]))
        account_ids = list(
            (
                await ctx.db.execute(
                    select(Account.id).where(Account.proxy_id == int(proxy_id))
                )
            )
            .scalars()
            .all()
        )
        provider_rows = list(
            (
                await ctx.db.execute(
                    select(LLMProvider.id, LLMProvider.execution_backend).where(
                        LLMProvider.proxy_id == int(proxy_id)
                    )
                )
            ).all()
        )
        provider_ids = [int(item[0]) for item in provider_rows]
        if provider_ids and row.type == "mtproxy":
            raise ValueError(
                "被 LLM Provider 引用的代理不能改为 MTProxy；请先解除引用或使用 HTTP/SOCKS5"
            )
        if any(str(item[1] or "direct") == "codex_gateway" for item in provider_rows):
            _mark_gateway_candidate_sync(ctx)
        if ctx.action is not None:
            stored = dict(ctx.action.arguments or {})
            stored["reload_account_ids"] = [int(value) for value in account_ids]
            stored["restart_account_ids"] = [int(value) for value in account_ids]
            stored["reload_ai_command_accounts"] = bool(provider_ids)
            ctx.action.arguments = stored
        mode = "update"
    else:
        row = Proxy(
            type=str(parsed["type"]),
            host=str(parsed["host"]),
            port=int(parsed["port"]),
            username=parsed.get("username"),
            password_enc=(encrypt_str(str(parsed["password"])) if parsed.get("password") else None),
        )
        ctx.db.add(row)
        mode = "create"
    await ctx.db.flush()
    return {"mode": mode, "proxy": _proxy_view(row), "business_changed": True}


async def delete_proxy_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    proxy_id = int(args.get("id") or args.get("proxy_id"))
    detail = await get_proxy(ctx, {"id": proxy_id})
    if detail.get("error"):
        raise ValueError(detail["message"])
    if int(detail["usage"]["total"]) > 0:
        raise ValueError("代理仍被账号或 Provider 使用，请先解除引用")
    return {
        "summary": f"删除代理 #{proxy_id}",
        **detail,
        "warning": "删除代理不可恢复。",
    }


async def delete_proxy_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    proxy_id = int(args.get("id") or args.get("proxy_id"))
    detail = await get_proxy(ctx, {"id": proxy_id})
    if detail.get("error"):
        raise ValueError(detail["message"])
    if int(detail["usage"]["total"]) > 0:
        raise ValueError("代理仍被账号或 Provider 使用，请先解除引用")
    row = await ctx.db.get(Proxy, proxy_id)
    await ctx.db.delete(row)
    await ctx.db.flush()
    return {"deleted": True, "proxy_id": proxy_id, "business_changed": True}


async def list_devices(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = list(
        (await ctx.db.execute(select(DeviceProfile).order_by(DeviceProfile.id.asc()))).scalars().all()
    )
    return {"count": len(rows), "device_profiles": [_device_view(row) for row in rows]}


async def save_device_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    profile_id = args.get("id") or args.get("profile_id")
    if profile_id not in (None, ""):
        row = await ctx.db.get(DeviceProfile, int(profile_id))
        if row is None:
            raise ValueError(f"设备档案 #{profile_id} 不存在")
        return {
            "summary": f"更新设备档案 #{profile_id} {row.name}",
            "mode": "update",
            "current": _device_view(row),
            "target_fields": {key: value for key, value in args.items() if key not in {"id", "profile_id"}},
            "note": "设备信息只影响后续新登录，已有 Telegram session 不会改变。",
        }
    required = ("name", "device_model", "system_version", "app_version")
    if any(not str(args.get(key) or "").strip() for key in required):
        raise ValueError("创建设备档案需要 name、device_model、system_version、app_version")
    return {
        "summary": f"创建设备档案「{args['name']}」",
        "mode": "create",
        "target_fields": args,
        "note": "设备信息只影响后续新登录，已有 Telegram session 不会改变。",
    }


async def save_device_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    profile_id = args.get("id") or args.get("profile_id")
    if profile_id not in (None, ""):
        row = await ctx.db.get(DeviceProfile, int(profile_id))
        if row is None:
            raise ValueError(f"设备档案 #{profile_id} 不存在")
        for key in (
            "name",
            "device_model",
            "system_version",
            "app_version",
            "lang_code",
            "system_lang_code",
        ):
            if key in args and args[key] is not None:
                setattr(row, key, args[key])
        mode = "update"
    else:
        row = DeviceProfile(
            name=str(args["name"]).strip(),
            device_model=str(args["device_model"]).strip(),
            system_version=str(args["system_version"]).strip(),
            app_version=str(args["app_version"]).strip(),
            lang_code=str(args.get("lang_code") or "zh"),
            system_lang_code=str(args.get("system_lang_code") or "zh-Hans"),
            is_default=False,
        )
        ctx.db.add(row)
        mode = "create"
    if bool(args.get("is_default")):
        await ctx.db.execute(update(DeviceProfile).values(is_default=False))
        row.is_default = True
    elif args.get("is_default") is False and row.is_default:
        raise ValueError("不能直接取消默认设备档案，请把另一条设为默认")
    await ctx.db.flush()
    await ctx.db.refresh(row)
    return {
        "mode": mode,
        "device_profile": _device_view(row),
        "business_changed": True,
    }


async def delete_device_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    profile_id = int(args.get("id") or args.get("profile_id"))
    row = await ctx.db.get(DeviceProfile, profile_id)
    if row is None:
        raise ValueError(f"设备档案 #{profile_id} 不存在")
    if row.is_default:
        raise ValueError("默认设备档案不能删除，请先把另一条设为默认")
    account_ids = list(
        (await ctx.db.execute(select(Account.id).where(Account.device_profile_id == profile_id)))
        .scalars()
        .all()
    )
    return {
        "summary": f"删除设备档案 #{profile_id} {row.name}",
        "device_profile": _device_view(row),
        "affected_account_ids": account_ids,
        "warning": "引用它的账号会改为使用默认设备档案；已有 session 不受影响。",
    }


async def delete_device_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    profile_id = int(args.get("id") or args.get("profile_id"))
    row = await ctx.db.get(DeviceProfile, profile_id)
    if row is None:
        raise ValueError(f"设备档案 #{profile_id} 不存在")
    if row.is_default:
        raise ValueError("默认设备档案不能删除，请先把另一条设为默认")
    await ctx.db.delete(row)
    await ctx.db.flush()
    return {"deleted": True, "profile_id": profile_id, "business_changed": True}


def _obj(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def register(registry: ToolRegistry) -> None:
    proxy_id = {"id": {"type": "integer"}, "proxy_id": {"type": "integer"}}
    registry.register(
        ToolSpec(
            name="network.status",
            channels=("web",),
            description="探测后端当前公网出口 IP、国家/地区与 ISP；force=true 强制刷新缓存。",
            input_schema=_obj({"force": {"type": "boolean"}}),
            read_handler=network_status,
        )
    )
    registry.register(
        ToolSpec(
            name="proxies.list",
            channels=("web",),
            description="列出出口代理，绝不返回密码明文。",
            input_schema=_obj({}),
            read_handler=list_proxies,
        )
    )
    registry.register(
        ToolSpec(
            name="proxies.get",
            channels=("web",),
            description="读取代理详情和账号/Provider 引用情况。",
            input_schema=_obj(proxy_id),
            read_handler=get_proxy,
        )
    )
    registry.register(
        ToolSpec(
            name="proxies.test",
            channels=("web",),
            description="真实测试代理入口及到 Telegram DC 的连通性。",
            input_schema=_obj(proxy_id),
            min_role="operator",
            read_handler=test_proxy,
        )
    )
    proxy_fields = {
        **proxy_id,
        "url": {"type": "string"},
        "type": {"type": "string"},
        "host": {"type": "string"},
        "port": {"type": "integer"},
        "username": {"type": "string"},
        "password": {"type": "string"},
        "clear_password": {"type": "boolean"},
    }
    registry.register(
        ToolSpec(
            name="proxies.save",
            channels=("web",),
            description="创建或更新出口代理，支持直接粘贴代理 URL。",
            input_schema=_obj(proxy_fields),
            read_only=False,
            min_role="admin",
            preview_handler=save_proxy_preview,
            execute_handler=save_proxy_execute,
            secret_argument_names=("password",),
            runtime_effects=("reload_commands", "restart_affected_workers"),
        )
    )
    registry.register(
        ToolSpec(
            name="proxies.delete",
            channels=("web",),
            description="删除未被账号或 Provider 引用的代理。",
            input_schema=_obj(proxy_id),
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_proxy_preview,
            execute_handler=delete_proxy_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="devices.list",
            channels=("web",),
            description="列出 Telegram 设备伪装档案。",
            input_schema=_obj({}),
            read_handler=list_devices,
        )
    )
    device_fields = {
        "id": {"type": "integer"},
        "profile_id": {"type": "integer"},
        "name": {"type": "string"},
        "device_model": {"type": "string"},
        "system_version": {"type": "string"},
        "app_version": {"type": "string"},
        "lang_code": {"type": "string"},
        "system_lang_code": {"type": "string"},
        "is_default": {"type": "boolean"},
    }
    registry.register(
        ToolSpec(
            name="devices.save",
            channels=("web",),
            description="创建、更新或设为默认 Telegram 设备档案。",
            input_schema=_obj(device_fields),
            read_only=False,
            min_role="admin",
            preview_handler=save_device_preview,
            execute_handler=save_device_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="devices.delete",
            channels=("web",),
            description="删除非默认 Telegram 设备档案。",
            input_schema=_obj({"id": {"type": "integer"}, "profile_id": {"type": "integer"}}),
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_device_preview,
            execute_handler=delete_device_execute,
        )
    )


__all__ = ["register"]

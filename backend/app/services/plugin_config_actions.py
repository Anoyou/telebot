"""Generic plugin configuration action execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt_str
from ..db.models.account import Account, Proxy
from ..db.models.feature import Feature
from ..db.models.plugin import InstalledPlugin
from ..settings import settings as app_settings
from ..util.proxy import ProxyConfigError, parse_proxy_url
from ..worker.plugins.base import Plugin, PluginContext, get_plugin
from ..worker.plugins.http_facade import PluginHTTP


class PluginConfigActionError(RuntimeError):
    """Base error for plugin config actions."""


class PluginConfigActionNotFound(PluginConfigActionError):
    """Raised when an action is not declared by the plugin."""


class PluginConfigActionUnavailable(PluginConfigActionError):
    """Raised when plugin code or handler is not available."""


def declared_config_actions(
    feature: Feature | Mapping[str, Any] | None,
    installed_plugin: InstalledPlugin | Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return config action declarations from feature or installed plugin metadata."""

    manifest = _manifest_dict(feature)
    installed_manifest = _installed_manifest_dict(installed_plugin)
    raw = manifest.get("config_actions")
    if raw is None:
        raw = installed_manifest.get("config_actions")
    schema = manifest.get("config_schema")
    if raw is None and isinstance(schema, Mapping):
        raw = schema.get("x-config-actions")
    installed_schema = installed_manifest.get("config_schema")
    if raw is None and isinstance(installed_schema, Mapping):
        raw = installed_schema.get("x-config-actions")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping) and str(item.get("key") or "").strip()]


async def run_plugin_config_action(
    db: AsyncSession,
    *,
    account: Account,
    feature: Feature,
    action_key: str,
    effective_config: Mapping[str, Any],
    current_config: Mapping[str, Any] | None = None,
    action_input: Mapping[str, Any] | None = None,
    installed_plugin: InstalledPlugin | Mapping[str, Any] | None = None,
    log: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run a declared plugin config action and normalize its result."""

    action = _find_action(feature, action_key, installed_plugin=installed_plugin)
    plugin_cls = await _load_plugin_class(
        db,
        feature.key,
        account.id,
        installed_plugin=installed_plugin,
    )
    if not _plugin_overrides_config_action(plugin_cls):
        raise PluginConfigActionUnavailable(f"插件 {feature.key} 未实现配置动作处理器")

    runtime_config = _merge_current_config(effective_config, current_config)
    manifest = _plugin_manifest_dict(plugin_cls, feature)
    account_proxy_url: str | None = None
    account_proxy_error: str | None = None
    if _manifest_uses_http_facade(manifest):
        account_proxy_url, account_proxy_error = await _account_proxy_resolution(db, account)
    from ..worker.plugins import loader as plugin_loader

    ctx = PluginContext(
        account_id=account.id,
        feature_key=feature.key,
        config=runtime_config,
        data_dir=plugin_loader._plugin_data_dir(feature.key),  # noqa: SLF001 - shared plugin runtime contract
        log=log,
        http=_build_http_facade(
            account.id,
            feature.key,
            manifest,
            runtime_config,
            account_proxy_url=account_proxy_url,
            account_proxy_error=account_proxy_error,
        ),
        ai=_build_ai_facade(account.id, feature.key, manifest),
        account_proxy_url=account_proxy_url,
        account_proxy_error=account_proxy_error,
    )
    plugin = plugin_cls()
    payload = {
        "input": dict(action_input or {}),
        "config": runtime_config,
        "action": action,
    }
    result = await plugin.on_config_action(ctx, action_key, payload)
    if result is None:
        raise PluginConfigActionUnavailable(f"插件 {feature.key} 未处理配置动作 {action_key}")
    if not isinstance(result, Mapping):
        raise PluginConfigActionError("配置动作必须返回对象")
    return _normalize_action_result(result)


def _manifest_dict(feature: Feature | Mapping[str, Any] | None) -> dict[str, Any]:
    if feature is None:
        return {}
    if isinstance(feature, Mapping):
        manifest = feature.get("manifest", feature)
    else:
        manifest = getattr(feature, "manifest", None)
    return dict(manifest or {}) if isinstance(manifest, Mapping) else {}


def _installed_manifest_dict(installed_plugin: InstalledPlugin | Mapping[str, Any] | None) -> dict[str, Any]:
    if installed_plugin is None:
        return {}
    if isinstance(installed_plugin, Mapping):
        manifest = installed_plugin.get("manifest_json") or installed_plugin.get("manifest") or installed_plugin
    else:
        manifest = getattr(installed_plugin, "manifest_json", None)
    return dict(manifest or {}) if isinstance(manifest, Mapping) else {}


def _find_action(
    feature: Feature,
    action_key: str,
    *,
    installed_plugin: InstalledPlugin | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    key = str(action_key or "").strip()
    for action in declared_config_actions(feature, installed_plugin=installed_plugin):
        if str(action.get("key") or "").strip() == key:
            return action
    raise PluginConfigActionNotFound(f"插件 {feature.key} 未声明配置动作 {key}")


async def _load_plugin_class(
    db: AsyncSession,
    plugin_key: str,
    account_id: int,
    *,
    installed_plugin: InstalledPlugin | Mapping[str, Any] | None = None,
) -> type[Plugin]:
    cls = get_plugin(plugin_key)
    if cls is not None:
        if getattr(cls, "_source", None) != "installed":
            return cls

        from ..worker.plugins import loader as plugin_loader

        stale, _reason = plugin_loader._installed_plugin_runtime_drift(  # noqa: SLF001
            cls,
            None,
            installed_plugin,
        )
        if not stale:
            return cls
        plugin_loader._clear_installed_module_cache(plugin_key)  # noqa: SLF001

    from ..worker.plugins import loader as plugin_loader

    if plugin_loader._builtin_plugin_path(plugin_key) is not None:  # noqa: SLF001
        plugin_loader._load_builtin_plugin(plugin_key)  # noqa: SLF001
        cls = get_plugin(plugin_key)
        if cls is not None:
            return cls

    if plugin_loader._installed_plugin_exists(plugin_key):  # noqa: SLF001
        auth = await plugin_loader._authorize_installed_plugin(  # noqa: SLF001
            db,
            plugin_key,
            account_id=account_id,
        )
        if not auth.allowed:
            raise PluginConfigActionUnavailable(auth.last_error or "installed 插件未通过授权检查")
        plugin_loader._load_installed_plugin(plugin_key)  # noqa: SLF001
        cls = get_plugin(plugin_key)
        if cls is not None:
            return cls

    raise PluginConfigActionUnavailable(f"插件 {plugin_key} 未加载或不存在")


def _plugin_overrides_config_action(plugin_cls: type[Plugin]) -> bool:
    return getattr(plugin_cls, "on_config_action", None) is not getattr(Plugin, "on_config_action", None)


def _plugin_manifest_dict(plugin_cls: type[Plugin], feature: Feature) -> dict[str, Any]:
    manifest = getattr(plugin_cls, "_manifest", None)
    if manifest is not None and hasattr(manifest, "to_dict"):
        return dict(manifest.to_dict())
    if isinstance(manifest, Mapping):
        return dict(manifest)
    return dict(feature.manifest or {})


def _merge_current_config(
    effective_config: Mapping[str, Any],
    current_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(effective_config or {})
    for key, value in dict(current_config or {}).items():
        if _should_skip_masked_secret(key, value):
            continue
        merged[str(key)] = value
    return merged


def _should_skip_masked_secret(key: str, value: Any) -> bool:
    if not _is_sensitive_config_key(key):
        return False
    if value in (None, "", "***"):
        return True
    return isinstance(value, str) and set(value.strip()) in ({"*"}, {"•"})


def _is_sensitive_config_key(key: str) -> bool:
    import re

    return bool(re.search(r"(^|_)(api_key|access_token|auth_token|bot_token|token|tokens|secret|password|passwd|pwd)$", key, re.I))


def _build_http_facade(
    account_id: int,
    plugin_key: str,
    manifest: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    account_proxy_url: str | None,
    account_proxy_error: str | None,
) -> Any:
    if not _manifest_uses_http_facade(manifest):
        return None
    allowed_hosts = [str(item).strip() for item in manifest.get("allowed_hosts") or [] if str(item).strip()]
    return PluginHTTP.from_context(
        PluginContext(
            account_id=account_id,
            feature_key=plugin_key,
            config=dict(config),
            account_proxy_url=account_proxy_url,
            account_proxy_error=account_proxy_error,
        ),
        allowed_hosts=allowed_hosts,
        manifest_http=manifest.get("http") if isinstance(manifest.get("http"), Mapping) else None,
    )


def _manifest_uses_http_facade(manifest: Mapping[str, Any]) -> bool:
    permissions = {str(item) for item in manifest.get("permissions") or []}
    if "external_http" not in permissions:
        return False
    return any(str(item).strip() for item in manifest.get("allowed_hosts") or [])


def _build_ai_facade(account_id: int, plugin_key: str, manifest: Mapping[str, Any]) -> Any:
    permissions = {str(item) for item in manifest.get("permissions") or []}
    if "ai_text" not in permissions:
        return None
    from ..worker.plugins.ai_facade import PluginAI
    from .llm_usage_service import ensure_llm_usage_callback_registered

    # 配置动作运行在 Web 进程，不经过账号 worker 的启动流程；必须在当前
    # 进程注册 Usage 持久化回调，否则模型调用成功也不会出现在 AI 中心。
    ensure_llm_usage_callback_registered()
    return PluginAI(account_id=account_id, plugin_key=plugin_key)


async def _account_proxy_resolution(
    db: AsyncSession,
    account: Account,
) -> tuple[str | None, str | None]:
    """解析配置动作的插件 HTTP 出口，并保留显式 direct 模式。

    缺失、历史、不受支持或损坏的账号代理会投影为错误状态。默认
    ``account_proxy`` 请求由 ``PluginHTTP`` 在 DNS/transport 前拒绝；只有
    manifest 与账号配置共同明确选择 direct 时才会忽略该代理错误。
    """

    proxy_id = getattr(account, "proxy_id", None)
    proxy = await db.get(Proxy, proxy_id) if proxy_id else None
    if proxy_id is not None and proxy is None:
        return None, f"账号绑定的代理 #{proxy_id} 不存在"
    if proxy is None:
        try:
            parsed_default = parse_proxy_url(app_settings.tg_default_proxy)
        except ProxyConfigError as exc:
            return None, f"全局默认代理配置无效：{exc}"
        if parsed_default is None:
            return None, None
        ptype, host, port, _rdns, username, password = parsed_default
        return _http_proxy_resolution(ptype, host, port, username, password or "")

    proxy_type = str(proxy.type or "").lower()
    if proxy_type == "socks4":
        return None, "账号使用 SOCKS4 Telegram 代理，插件 HTTP 不支持该代理类型"
    if proxy_type not in {"socks5", "http", "https"}:
        return None, f"账号代理类型 {proxy.type!r} 不能用于插件 HTTP"

    try:
        password = decrypt_str(proxy.password_enc) if proxy.password_enc else ""
    except Exception as exc:  # noqa: BLE001 - 坏代理凭据不能被解释为直连
        del exc
        return None, "账号代理凭据无法解密，请重新保存或更换代理"
    if "://" in proxy.host:
        try:
            parsed = parse_proxy_url(proxy.host)
        except ProxyConfigError as exc:
            return None, f"账号代理地址无效：{exc}"
        ptype, host, port, _rdns, parsed_user, parsed_password = parsed
        return _http_proxy_resolution(
            ptype,
            host,
            port,
            proxy.username or parsed_user,
            password or parsed_password or "",
        )
    return _http_proxy_resolution(proxy.type, proxy.host, proxy.port, proxy.username, password)


def _http_proxy_resolution(
    ptype: str,
    host: str,
    port: int,
    username: str | None,
    password: str,
) -> tuple[str | None, str | None]:
    if str(ptype or "").lower() == "socks4":
        return None, "账号使用 SOCKS4 Telegram 代理，插件 HTTP 不支持该代理类型"
    try:
        return _build_proxy_url(ptype, host, port, username, password), None
    except ProxyConfigError as exc:
        return None, str(exc)


def _build_proxy_url(ptype: str, host: str, port: int, username: str | None, password: str) -> str:
    scheme = "socks5" if str(ptype or "").lower() == "socks5" else "http" if str(ptype or "").lower() in {"http", "https"} else ""
    if not scheme:
        raise ProxyConfigError(f"代理类型 {ptype!r} 不能用于插件 HTTP，拒绝回落直连")
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += f":{quote(password, safe='')}"
        auth += "@"
    return f"{scheme}://{auth}{host}:{int(port)}"


def _normalize_action_result(result: Mapping[str, Any]) -> dict[str, Any]:
    config_patch = result.get("config_patch") if isinstance(result.get("config_patch"), Mapping) else {}
    payload_result = result.get("result") if isinstance(result.get("result"), Mapping) else {}
    return {
        "success": bool(result.get("success", True)),
        "message": str(result.get("message") or "") or None,
        "toast": str(result.get("toast") or "") or None,
        "config_patch": dict(config_patch),
        "result": dict(payload_result),
    }


__all__ = [
    "PluginConfigActionError",
    "PluginConfigActionNotFound",
    "PluginConfigActionUnavailable",
    "declared_config_actions",
    "run_plugin_config_action",
]

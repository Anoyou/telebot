"""Telethon 客户端工厂。

负责从 Account/Proxy 数据库记录恢复 Telethon 客户端实例：
- session 串经 master_key 解密后塞入 ``StringSession``
- api_id/api_hash 同样从加密字段中解出
- 如果挂了出口代理，构造 Telethon 接受的 proxy 元组
- 账号未绑定 Proxy 行时，自动 fallback 到 ``settings.tg_default_proxy``（全局兜底，
  本机测试场景常用，比如开发机本地启动了一个 SOCKS5 代理走出去 TG）
- 设备伪装信息（device_model / system_version / app_version / lang_code / system_lang_code）
  来自 ``ResolvedDeviceProfile``，由调用方先用 ``services.device_profile.resolve_for_account``
  解析好传进来；不传则使用硬编码兜底
"""
from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from ..crypto import decrypt_bytes, decrypt_str
from ..db.models.account import Account, Proxy
from ..services.device_profile import HARDCODED_FALLBACK, ResolvedDeviceProfile
from ..util.proxy import get_default_proxy_tuple, parse_proxy_url


def build_proxy_tuple(proxy: Proxy | None):
    """构造 Telethon 接受的代理元组。

    Telethon 的 PySocks 兼容 tuple 形式：
        (proxy_type, addr, port, rdns, username, password)
    传入 ``None`` 时回落 ``settings.tg_default_proxy``；仍然没有则真正直连。
    """
    if not proxy:
        return get_default_proxy_tuple()
    password = decrypt_str(proxy.password_enc) if proxy.password_enc else None
    if "://" in proxy.host:
        parsed = parse_proxy_url(proxy.host)
        if parsed is not None:
            ptype, host, port, rdns, parsed_user, parsed_password = parsed
            return (ptype, host, port, rdns, proxy.username or parsed_user, password or parsed_password)
    return (
        proxy.type,                  # "socks5" | "http" | "mtproxy"
        proxy.host,
        proxy.port,
        True,                        # 强制走远端 DNS，避免本地 DNS 泄漏
        proxy.username,
        password,
    )


def build_client(
    account: Account,
    proxy: Proxy | None = None,
    profile: ResolvedDeviceProfile | None = None,
) -> TelegramClient:
    """根据账号记录构造一个未连接的 Telethon 客户端。

    profile=None 时使用兜底 (HARDCODED_FALLBACK)；正常路径里 worker 启动时调用
    ``services.device_profile.resolve_for_account`` 拿到 profile 再传入。
    """
    session_str = decrypt_bytes(account.session_enc).decode()
    api_id = int(decrypt_str(account.api_id_enc))
    api_hash = decrypt_str(account.api_hash_enc)
    p = profile or HARDCODED_FALLBACK
    return TelegramClient(
        StringSession(session_str),
        api_id,
        api_hash,
        proxy=build_proxy_tuple(proxy),
        request_retries=3,
        connection_retries=5,
        retry_delay=2,
        # 显式固化 Telethon 默认行为：FloodWait ≤ 60s 由 Telethon 内部自动 sleep 重试，
        # 超过 60s 才抛 FloodWaitError 交给上层（交互动作 except 会喂限速引擎降级）。
        flood_sleep_threshold=60,
        **p.telethon_kwargs(),
        # 关键：Telethon 默认 sequential_updates=False。我们的事件 handler 写在 plugin 里，
        # 互相不应该并发触发同一规则，但跨规则可以；保持默认即可。
    )

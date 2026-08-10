"""账号相关 schema。"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class AccountStartLoginRequest(BaseModel):
    """绑定向导第 1 步：录入 API 凭据 + 手机号。"""
    api_id: int
    api_hash: str
    phone: str
    # 为空 = 新增账号；有值 = 重新登录并覆盖该账号的 session / API 凭据。
    account_id: int | None = None
    proxy_id: int | None = None
    # 设备伪装：影响 TG 设备列表里看到的 device_model / system_version / app_version；
    # 不传 = 用系统默认 profile
    device_profile_id: int | None = None


class AccountStartLoginResponse(BaseModel):
    """返回临时 login_token，后续步骤须带此 token。"""
    login_token: str
    phone_code_hash: str | None = None


class AccountConfirmCodeRequest(BaseModel):
    login_token: str
    code: str


class AccountConfirm2FARequest(BaseModel):
    login_token: str
    password: str


class AccountConfirmResponse(BaseModel):
    """登录成功返回创建好的账号 ID。"""
    account_id: int
    require_2fa: bool = False
    display_name: str | None = None


class AccountUpdateRequest(BaseModel):
    display_name: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    template_id: int | None = None
    proxy_id: int | None = None
    # 改 device_profile_id 不会影响**现有 session**：TG 端显示的设备名绑在 auth_key 上。
    # 想生效必须重新登录走 wizard。
    device_profile_id: int | None = None


class ProxySummary(BaseModel):
    """挂在账号摘要上的代理一行信息——绝不返回明文密码 / 用户名也只是肉眼标识。

    用途：概览页 / 账号列表页一眼看出 "这个账号走哪个代理出网"，
    避免去开 ProxyManager 找。
    """

    id: int
    type: str          # socks5 / http / https
    host: str
    port: int
    label: str | None = None  # 友好名字；后端可能给 host:port 兜底
    # ── 出口探测缓存（30 min TTL；见 services/proxy_probe_cache.py）──
    # 这些字段反映"上次主动测时"的真实出口，比 host:port 更有用——
    # 住宅代理入口在 SG 而出口在 ID 是常态。
    exit_country: str | None = None  # ISO-2 (CN / US / JP)
    exit_ip: str | None = None
    probed_at: int | None = None     # epoch 秒；前端用 "N min 前"
    probe_ok: bool | None = None     # 上次探测是否成功；None=没探测过

    model_config = ConfigDict(from_attributes=True)


class AccountSummary(BaseModel):
    id: int
    phone: str
    display_name: str | None
    # Telegram 身份信息（client.get_me() 回填，可空）
    tg_user_id: int | None = None
    tg_username: str | None = None
    status: str
    tags: list[str] | None = None
    enabled_features: int = 0
    cold_start_until: date | None = None
    created_at: datetime
    # 出网通道：proxy=None ⇒ 该账号走主进程默认出口（DIRECT 或全局 TG_DEFAULT_PROXY）；
    # 非 None ⇒ 走指定的 proxy 行。前端展示这条让用户一眼知道"该账号是哪个国家/IP 出去的"。
    proxy: ProxySummary | None = None

    model_config = ConfigDict(from_attributes=True)


class AccountDetail(AccountSummary):
    notes: str | None = None
    template_id: int | None = None
    proxy_id: int | None = None
    device_profile_id: int | None = None


class AccountCloneConfigRequest(BaseModel):
    from_account_id: int
    features: list[str] = Field(default_factory=list)

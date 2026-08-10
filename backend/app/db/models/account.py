"""TG 账号、出口代理、拟人化配置。"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base


class Proxy(Base):
    """出口代理。

    管理面仅允许 SOCKS5/HTTP/HTTPS；旧库可能仍含 MTProxy 或 SOCKS4。
    SOCKS4 只保留 Telegram 既有绑定兼容，不能供 HTTP/LLM/Gateway 使用。
    """

    __tablename__ = "proxy"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    # password 须用 master_key 加密
    password_enc: Mapped[str | None] = mapped_column(String, nullable=True)


class DeviceProfile(Base):
    """设备伪装库：一条 profile = 一组 (device_model, system_version, app_version, lang_code,
    system_lang_code)。被账号 ``device_profile_id`` 引用。

    is_default：全表只允许一条为 True，由 API 层在写入时维护（自动把其它行置 False）。
    新账号登录时如果调用方没指定 profile，就用 is_default 的那一条；都没有则回退到
    硬编码兜底（在 ``services.device_profile.resolve`` 里实现）。
    """

    __tablename__ = "device_profile"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_model: Mapped[str] = mapped_column(String(128), nullable=False)
    system_version: Mapped[str] = mapped_column(String(64), nullable=False)
    app_version: Mapped[str] = mapped_column(String(64), nullable=False)
    lang_code: Mapped[str] = mapped_column(String(16), nullable=False, default="zh")
    system_lang_code: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-Hans"
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Account.status 枚举值（不用 PG ENUM，方便迁移）
ACCOUNT_STATUS_ACTIVE = "active"
ACCOUNT_STATUS_PAUSED = "paused"
ACCOUNT_STATUS_FLOODWAIT = "floodwait"
ACCOUNT_STATUS_DEAD = "dead"
ACCOUNT_STATUS_LOGIN_REQUIRED = "login_required"


class Account(Base):
    """一个 TG 账号 = 一个 session = 一个 worker 进程。"""

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # 来自 Telethon ``client.get_me()``：用户数字 ID 与 @username（不含 @）
    # 登录成功 / worker 启动连上 TG 时回填；旧账号在迁移后为空，重新登录后填上
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # api_id / api_hash 全部加密落盘
    api_id_enc: Mapped[str] = mapped_column(String, nullable=False)
    api_hash_enc: Mapped[str] = mapped_column(String, nullable=False)
    # session 是 Telethon ``StringSession.save()`` 序列化后的字符串再编码为 bytes 后用主密钥加密
    session_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default=ACCOUNT_STATUS_LOGIN_REQUIRED, index=True)
    template_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("rate_limit_template.id"), nullable=True
    )
    proxy_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("proxy.id"), nullable=True)
    # 设备伪装：决定 TG 设备列表里显示的 device_model / system_version / app_version；
    # 空 = 走系统默认 profile（device_profile.is_default = true）
    device_profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("device_profile.id", ondelete="SET NULL"), nullable=True
    )
    cold_start_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    humanize: Mapped[HumanizeConfig] = relationship(
        "HumanizeConfig", back_populates="account", uselist=False, cascade="all, delete-orphan"
    )


class HumanizeConfig(Base):
    """每账号一份的拟人化配置（PRD §L.3）。"""

    __tablename__ = "humanize_config"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), primary_key=True
    )
    jitter_pct: Mapped[int] = mapped_column(SmallInteger, default=15)
    typing_simulate: Mapped[bool] = mapped_column(Boolean, default=True)
    typing_min_ms: Mapped[int] = mapped_column(Integer, default=1000)
    typing_max_ms: Mapped[int] = mapped_column(Integer, default=3000)
    typing_probability: Mapped[int] = mapped_column(SmallInteger, default=80)
    read_before_reply: Mapped[bool] = mapped_column(Boolean, default=True)
    active_window_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    active_window_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    cold_start_days: Mapped[int] = mapped_column(SmallInteger, default=7)

    account: Mapped[Account] = relationship("Account", back_populates="humanize")


# ── Sudo 用户（Sprint5）──────────────────────────────────────
class SudoUser(Base):
    """授权其他 TG 用户通过独立前缀触发命令。

    - ``allowed_chat_ids``：白名单对话 ID 列表；NULL/空 = 不允许，``["*"]`` = 显式允许全部
    - ``allowed_commands``：白名单命令列表；NULL/空 = 不允许，``["*"]`` = 显式允许全部
    """

    __tablename__ = "sudo_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    allowed_chat_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allowed_commands: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

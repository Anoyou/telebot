"""通知 Bot 配置模型（Sprint4 #2D）。

安全约定：
- ``bot_token_enc`` 必须是 Fernet 加密后的 token（见 ``app.crypto.encrypt_str``）
- 任何 GET 接口不得返回明文 token，只返回 ``has_token: bool``
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class NotifyBot(Base):
    """项目通知用 Telegram Bot。"""

    __tablename__ = "notify_bot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    bot_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_account_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    default_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

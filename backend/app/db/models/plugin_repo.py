"""插件仓库登记表（阶段 F：可浏览的 Git 仓库列表）。

与 ``remote_plugin`` 表的区别：
- ``remote_plugin`` 是“已安装的单个第三方插件”——每行对应一份在
  ``plugins/installed/<name>/`` 下落地的代码。
- ``plugin_repo`` 是“可浏览的插件仓库”——每行是一个 git URL，仓库内可能
  包含**多个**插件子目录；用户可在 UI 里列出仓库内插件并选择安装。

两表彼此独立：``plugin_repo`` 仅是“目录索引”，``remote_plugin`` 真正决定
worker loader 是否会加载该插件。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class PluginRepo(Base):
    """插件仓库表：每行 = 一个用户保存下来的 Git 仓库。

    ``url`` 字段唯一：同一个仓库 URL 只允许保存一次，避免出现多个重复条目。
    ``name`` 仅作展示用，不参与 git 操作。
    """

    __tablename__ = "plugin_repo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    credential_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def has_credentials(self) -> bool:
        """是否已保存私有仓库凭证；不暴露密文本身。"""
        return bool(self.credential_enc)


__all__ = ["PluginRepo"]

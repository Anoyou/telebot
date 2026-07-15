"""forward 插件 manifest。

Config Schema 说明：
- level: "global" 的字段为全局配置，所有账号共享
- 无 level 或 level: "account" 的字段为账号级配置
- 配置合并顺序：schema defaults < global config < account config
"""

from __future__ import annotations

from app.db.models.feature import FEATURE_FORWARD
from app.worker.plugins.manifest import Manifest

# 顶层导出常量；loader 扫描时读取
MANIFEST = Manifest(
    key=FEATURE_FORWARD,
    display_name="消息转发",
    version="0.2.0",
    author="builtin",
    description="按规则把 incoming 消息转发到指定 chat（4 种 mode + 风控接入 + FloodWait 兜底）",
    category="automation",
    permissions=["read_chat", "send_message", "send_file"],
    # rule.config 的 JSON Schema —— 前端可据此渲染表单 / 做兜底校验
    # level 字段说明：
    #   - "global": 全局配置，所有账号共享
    #   - "account": 账号级配置，按账号隔离（默认）
    config_schema={
        "type": "object",
        "x-ui-mode": "rules",
        "x-usage-guide": "消息转发通过规则触发：先新建规则，选择来源范围、匹配方式和目标 chat_id，再启用当前账号的功能总开关。它只处理 incoming 消息，本账号自己发送的消息不会被转发；规则保存后立即生效。",
        "additionalProperties": False,
        "required": ["target_chat_id", "mode"],
        "properties": {
            # 源筛选：all = 任何 incoming；peers = 指定 chat_id 列表；keyword = 文本包含关键词；duplicate = 重复消息检测
            "source_kind": {"enum": ["all", "peers", "keyword", "duplicate"]},
            "source_peers": {"type": "array", "items": {"type": "integer"}},
            "keyword": {"type": "string"},
            # duplicate 模式参数
            "duplicate_window": {"type": "integer", "description": "时间窗口（秒），默认 60"},
            "duplicate_threshold": {"type": "integer", "description": "不同用户数阈值（同一用户多次发送只算1人），默认 3"},
            # 目标 chat_id（Telethon 形式：私聊正数 / 普通群 -xxx / 超级群与频道 -100xxx）
            "target_chat_id": {"type": "integer"},
            # 4 种转发方式
            "mode": {
                "enum": ["forward_native", "copy_text", "quote", "link_only"],
            },
            # include_media=False 时仅转纯文本，遇到含媒体消息直接跳过
            "include_media": {"type": "boolean"},
            # copy / quote 模式下可选的固定前缀
            "header": {"type": "string"},
        },
    },
)

__all__ = ["MANIFEST"]

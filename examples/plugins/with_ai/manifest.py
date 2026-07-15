"""with_ai 示例模块 manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="with_ai",
    display_name="AI 示例",
    version="0.1.0",
    author="examples",
    description="演示第三方模块通过 ctx.ai 使用平台文本 LLM facade。",
    usage="安装后使用账号命令 ai_providers 查看可用 provider，使用 ai_complete 文本 调用平台统一 LLM facade。该示例不订阅 Telegram Event Bus。",
    category="utility",
    permissions=["ai_text", "edit_message"],
    config_schema={
        "type": "object",
        "x-ui-mode": "single",
        "additionalProperties": False,
        "properties": {
            "provider_tag": {
                "type": "string",
                "title": "Provider 标签",
                "default": "chat",
                "description": "可选：优先选择带有该 tag 的可用 provider。",
            },
        },
    },
)

__all__ = ["MANIFEST"]

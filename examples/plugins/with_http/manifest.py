"""with_http 示例模块 manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="with_http",
    display_name="HTTP 示例",
    version="0.1.0",
    author="examples",
    description="演示第三方模块通过 ctx.http 发起受控 HTTP 请求。",
    usage="安装后使用账号命令 http_status 发起受控 HTTP 请求；URL 必须匹配 allowed_hosts。该示例不订阅 Telegram Event Bus。",
    category="utility",
    permissions=["external_http", "edit_message"],
    allowed_hosts=["api.github.com"],
    config_schema={
        "type": "object",
        "x-ui-mode": "single",
        "additionalProperties": False,
        "properties": {
            "url": {
                "type": "string",
                "title": "请求地址",
                "default": "https://api.github.com/zen",
                "description": "必须匹配 manifest.allowed_hosts。",
            },
        },
    },
)

__all__ = ["MANIFEST"]

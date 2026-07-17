"""System Agent 稳定系统 Prompt。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo


def build_system_prompt(
    *,
    timezone_name: str,
    channel: str,
    role: str,
    account_id: int | None,
    version: str,
    agent_enabled: bool,
    ai_enabled: bool,
    command_prefix: str,
) -> str:
    """构建不包含业务校验细节的稳定系统 Prompt。"""

    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
        timezone_name = "UTC"
    now_local = datetime.now(tz)
    now_utc = datetime.now(UTC)

    account_line = (
        f"当前账号上下文：account_id={account_id}"
        if account_id is not None
        else "当前账号上下文：系统级（未绑定单一账号）"
    )

    return f"""你是 TelePilot 的 System Agent（系统助手）。你帮助管理员用自然语言查询和理解系统状态。

## 系统概念
- 账号（Account）：Telegram UserBot 账号，可暂停/恢复，有 worker 进程。
- 功能（Feature）：内置能力或已安装插件在账号上的启用状态，落在 AccountFeature。
- 通用 Rule：按 feature_key 挂在账号上的规则（如 scheduler、auto_reply）。
- 交互规则（Interaction rules）：保存在账号级配置 JSON 中，不属于通用 Rule 表。
- Scheduler：feature_key="scheduler" 的 Rule，支持 cron/once/interval。
- Provider：模型提供商；指令（CommandTemplate）可绑定 Provider。
- 插件：安装包全局状态与账号级启停不同；账号启停统一走 features。
- 日志：运行日志、审计日志、ActionEvent。
- 台账：资金入账/出账汇总与明细。

## 当前上下文
- TelePilot 版本：{version}
- 系统时区：{timezone_name}
- 本地时间：{now_local.isoformat()}
- UTC 时间：{now_utc.isoformat()}
- 渠道：{channel}
- 用户角色：{role}
- {account_line}
- 指令前缀：{command_prefix or "/"}
- System Agent 开关：{"开启" if agent_enabled else "关闭"}
- AI 总开关：{"开启" if ai_enabled else "关闭"}

## 行为边界
1. 查询必须调用工具获取实时数据，禁止根据聊天记忆编造系统状态。
2. 本阶段仅有只读工具。若用户要求写操作（创建/修改/删除/启停），说明当前版本尚未开放写操作，并给出对应 Web 页面入口。
3. 配置数据由你直接解释；不要要求不存在的 explain_* 工具。
4. get_* / list_* 工具返回的确定性字段（如 next_run_at、enabled、触发条件）是解释的依据。
5. 工具失败时明确说明业务是否发生变化（只读工具不应改变业务）。
6. 绝不在回答中复述 API Key、Token、密码或其它密钥。
7. “今天/今日”必须按系统时区的本地日界线理解。
8. 交互规则与通用 Rule 表是两套概念，不要混用。
9. 回答简洁、用中文，必要时用结构化列表。
10. 未接入的长尾能力（代理、Webhook、插件安装包管理、系统重启等）应说明限制并给出页面入口。
"""


def session_title_from_message(text: str) -> str:
    """首条用户消息去除换行后截取前 30 个字符。"""

    cleaned = " ".join(str(text or "").split())
    return cleaned[:30] if cleaned else "新对话"


def provider_setup_hint() -> dict[str, Any]:
    return {
        "web_path": "/ai?tab=providers",
        "message": "请先在 AI 中心配置一个支持 tools 的模型提供商，并在系统助手配置中选定。",
    }


__all__ = [
    "build_system_prompt",
    "provider_setup_hint",
    "session_title_from_message",
]

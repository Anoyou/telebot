"""System Agent 渐进式工具披露与领域路由。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .registry import ToolSpec

DOMAIN_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    "accounts": ("Telegram 账号、暂停恢复、Worker 重启", ("账号", "account", "worker", "登录号")),
    "commands": ("自定义指令、命令模板与账号启用范围", ("指令", "命令", "command", "模板")),
    "features": ("账号功能、插件功能启停与状态", ("功能", "feature", "能力开关")),
    "interaction": ("交互 Bot 规则、触发条件、暂停与启停", ("交互", "interaction", "触发", "暂停规则")),
    "ledger": ("资金台账、收入、支出与余额", ("台账", "收入", "支出", "余额", "资金", "账目", "ledger")),
    "logs": ("运行日志、错误、事件与诊断", ("日志", "报错", "错误", "异常", "trace", "log")),
    "memory": ("长期偏好记忆的查询与保存", ("记住", "偏好", "长期记忆", "别再问", "remember", "preference")),
    "plugin_repos": ("插件仓库、官方库与从仓库安装", ("插件仓库", "仓库", "repository", "repo")),
    "plugins": ("已安装插件、检查更新、安装卸载与全局启停", ("插件", "plugin", "扩展")),
    "product": ("TelePilot 产品版本、更新日志与界面入口", ("更新日志", "版本日志", "changelog", "最近更新")),
    "providers": ("模型 Provider 的列表、保存、验证与删除", ("provider", "提供商", "模型服务", "api key", "密钥")),
    "routing": ("AI 指令的 auto/fixed 路由", ("路由", "routing", "auto", "fixed")),
    "rules": ("通用 Rule 的查询、保存、启停与删除", ("规则", "rule")),
    "scheduler": ("定时任务、计划、立即执行与启停", ("定时", "计划任务", "scheduler", "cron", "几点执行")),
    "system": ("系统版本、健康状态、运行上下文与网络信息", ("系统状态", "健康", "版本", "时区", "上下文", "health")),
    "system_ops": ("系统检查更新、应用更新与重启", ("系统更新", "在线更新", "检查更新", "重启系统", "重启应用")),
}

_ACTION_HINTS = (
    "查",
    "看",
    "列出",
    "多少",
    "创建",
    "添加",
    "修改",
    "更新",
    "删除",
    "停用",
    "禁用",
    "启用",
    "暂停",
    "恢复",
    "执行",
    "重启",
)
_REFERENCE_HINTS = ("它", "这个", "这条", "刚才", "上一个", "那个", "继续")
_GENERAL_HELP_HINTS = (
    "怎么使用agent",
    "怎么用agent",
    "如何使用agent",
    "怎么使用你",
    "怎么用你",
    "你能做什么",
    "帮助",
    "help",
)
_PRODUCT_HELP_HINTS = ("更新日志", "版本日志", "changelog", "最近更新", "这版更新", "更新了什么")


@dataclass(frozen=True)
class ToolRoute:
    domains: tuple[str, ...]
    source: str
    reason: str = ""


def tool_domain(spec: ToolSpec) -> str:
    return spec.name.split(".", 1)[0]


def available_domains(specs: Iterable[ToolSpec]) -> set[str]:
    return {tool_domain(spec) for spec in specs}


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def route_locally(
    text: str,
    *,
    available: set[str],
    memory_state: dict[str, Any] | None = None,
) -> ToolRoute | None:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if any(hint in normalized for hint in _GENERAL_HELP_HINTS):
        return ToolRoute((), "local", "general_help")

    if "product" in available and any(hint in normalized for hint in _PRODUCT_HELP_HINTS):
        return ToolRoute(("product",), "local", "product_changelog")

    matched: list[str] = []
    for domain, (_description, keywords) in DOMAIN_CATALOG.items():
        if domain not in available:
            continue
        if any(keyword.replace(" ", "").lower() in normalized for keyword in keywords):
            matched.append(domain)

    if "interaction" in matched and "规则" in normalized and "通用" not in normalized:
        matched = [domain for domain in matched if domain != "rules"]
    if "plugin_repos" in matched and "plugins" in matched and "插件仓库" in normalized:
        matched = [domain for domain in matched if domain != "plugins"]
    if matched:
        return ToolRoute(tuple(dict.fromkeys(matched[:3])), "local", "keyword_match")

    state = memory_state if isinstance(memory_state, dict) else {}
    previous = [
        str(item)
        for item in (state.get("last_domains") or [])
        if str(item) in available
    ]
    if previous and any(hint in normalized for hint in _REFERENCE_HINTS):
        return ToolRoute(tuple(previous[:3]), "memory", "reference_to_previous_domain")

    if any(hint in normalized for hint in _ACTION_HINTS):
        return None
    # 无 CJK 且无领域关键词：交给模型路由，避免英文请求被误判为「无需实时数据」
    if not _contains_cjk(str(text or "")):
        return None
    return ToolRoute((), "local", "no_live_system_data_needed")


def router_system_prompt(available: set[str]) -> str:
    catalog = [
        {"domain": domain, "description": DOMAIN_CATALOG.get(domain, (domain, ()))[0]}
        for domain in sorted(available)
    ]
    return (
        "你是 TelePilot 的工具领域路由器。只判断当前用户请求是否需要读取或修改系统实时数据。"
        "普通问答、使用说明和闲聊不需要工具。若需要工具，最多选择 3 个领域。"
        "只返回 JSON，不要 Markdown："
        '{"needs_tools":true|false,"domains":["domain"],"reason":"short"}。'
        f"\n可用领域：{json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))}"
    )


def parse_model_route(text: str, *, available: set[str]) -> ToolRoute | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not bool(payload.get("needs_tools")):
        return ToolRoute((), "model", str(payload.get("reason") or "direct_answer")[:120])
    domains = tuple(
        dict.fromkeys(
            str(item)
            for item in (payload.get("domains") or [])
            if str(item) in available
        )
    )[:3]
    if not domains:
        return None
    return ToolRoute(domains, "model", str(payload.get("reason") or "")[:120])


def select_tool_specs(specs: Iterable[ToolSpec], route: ToolRoute) -> list[ToolSpec]:
    allowed = set(route.domains)
    if not allowed:
        return []
    return [spec for spec in specs if tool_domain(spec) in allowed]


__all__ = [
    "DOMAIN_CATALOG",
    "ToolRoute",
    "available_domains",
    "parse_model_route",
    "route_locally",
    "router_system_prompt",
    "select_tool_specs",
    "tool_domain",
]

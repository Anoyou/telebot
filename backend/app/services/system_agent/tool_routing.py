"""System Agent 渐进式工具披露与领域路由。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .registry import ToolSpec
from .secrets import (
    looks_like_provider_credential_paste,
    looks_like_standalone_provider_key,
)

# 插件动态域（plugin_<key>）运行时追加，不写入此常量本体
_DYNAMIC_DOMAIN_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {}

DOMAIN_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    "accounts": (
        "Telegram 账号资料、配置复制、暂停恢复与 Worker 重启",
        (
            "账号",
            "account",
            "accounts",
            "worker",
            "登录号",
            "userbot",
            "暂停账号",
            "恢复账号",
            "重启worker",
            "restart worker",
            "修改账号",
            "账号备注",
            "账号标签",
            "复制账号配置",
            "克隆配置",
        ),
    ),
    "account_bots": (
        "账号管理 Bot、授权用户、测试发送、polling runtime 与死信队列",
        (
            "管理bot",
            "账号bot",
            "bot授权用户",
            "授权tg用户",
            "pollingruntime",
            "account bot",
            "management bot",
            "bot死信",
            "polling死信",
            "polling dlq",
            "重放死信",
        ),
    ),
    "aliases": (
        "命令别名与账号级指令映射",
        ("别名", "命令别名", "alias", "aliases", "指令别名"),
    ),
    "commands": (
        "自定义指令、命令模板与账号启用范围",
        ("指令", "命令", "command", "commands", "模板", "command template", "自定义指令"),
    ),
    "dispatch": (
        "消息命中模拟、Router 投递统计与临时 Debug Trace",
        (
            "命中调试",
            "命中模拟",
            "模拟消息",
            "路由trace",
            "routerdebug",
            "router trace",
            "投递统计",
            "dispatch",
        ),
    ),
    "config_bundles": (
        "账号配置包的脱敏导出、差异预览与确认导入",
        (
            "配置包",
            "配置备份",
            "configbundle",
            "config bundle",
            "导出配置",
            "导入配置",
            "迁移配置",
        ),
    ),
    "capabilities": (
        "平台能力模块启停与运行状态",
        (
            "平台能力",
            "能力模块",
            "暂停ai模块",
            "interactionbot模块",
            "webhook模块",
            "台账模块",
            "命中调试模块",
            "capability",
        ),
    ),
    "features": (
        "账号功能、插件功能启停及账号/全局配置",
        (
            "功能",
            "feature",
            "features",
            "能力开关",
            "功能开关",
            "enable feature",
            "账号插件",
            "插件启用",
            "启用插件",
            "停用插件",
            "禁用插件",
            "插件配置",
            "全局配置",
            "账号级配置",
            "feature config",
        ),
    ),
    "rate_limits": (
        "账号风控阈值、模板、实时用量、事件和临时严格覆盖",
        (
            "风控", "限速", "频率限制", "rate limit", "ratelimit", "floodwait策略",
            "风控模板", "限流模板", "临时调严", "严格模式", "限流用量", "限流事件",
        ),
    ),
    "humanize": (
        "账号拟人化、打字模拟、活跃时段与冷启动",
        ("拟人化", "打字模拟", "活跃时段", "冷启动", "humanize", "jitter"),
    ),
    "interaction": (
        "交互 Bot 规则、触发条件、暂停与启停",
        (
            "交互",
            "interaction",
            "触发",
            "暂停规则",
            "交互规则",
            "interaction bot",
            "关键词规则",
            "按钮规则",
            "交互bot配置",
            "通知模板",
            "可信bot",
        ),
    ),
    "ignored": (
        "账号忽略名单与最近活跃 Peer",
        (
            "忽略名单",
            "忽略peer",
            "忽略用户",
            "忽略群",
            "忽略会话",
            "忽略账号",
            "ignored peer",
            "ignored",
            "最近peer",
            "最近会话",
        ),
    ),
    "ledger": (
        "资金台账、收入、支出与余额",
        (
            "台账",
            "收入",
            "支出",
            "余额",
            "资金",
            "账目",
            "ledger",
            "payout",
            "balance",
            "income",
            "expense",
            "今日收入",
        ),
    ),
    "logs": (
        "运行日志、错误、事件与诊断",
        (
            "日志",
            "报错",
            "错误",
            "异常",
            "trace",
            "log",
            "logs",
            "error",
            "errors",
            "exception",
            "最近有什么报错",
            "失败任务",
            "runtime log",
            "诊断",
        ),
    ),
    "source": (
        "部署源码的只读搜索与按行查看",
        (
            "源码",
            "代码",
            "实现",
            "函数",
            "文件",
            "调用链",
            "source",
            "source code",
            "code path",
            "stack trace",
            "堆栈",
            "根因",
            "debug",
        ),
    ),
    "web": (
        "公开互联网搜索与来源检索",
        (
            "联网",
            "互联网",
            "网页搜索",
            "网上搜索",
            "搜索网络",
            "官网",
            "官方文档",
            "最新资料",
            "读取网页",
            "总结网页",
            "总结链接",
            "网址",
            "url",
            "http://",
            "https://",
            "最近发文",
            "最新发文",
            "推文",
            "官推",
            "公开动态",
            "x 动态",
            "twitter",
            "tweet",
            "web search",
            "internet search",
            "search online",
            "online docs",
            "read url",
        ),
    ),
    "webhooks": (
        "账号入站 Webhook 状态、入口与 Token 轮换",
        ("webhook", "webhooks", "入站回调", "回调token", "hook入口"),
    ),
    "usage": (
        "LLM 调用记录、Token、延迟与插件用量",
        (
            "llm用量",
            "llm调用",
            "调用记录",
            "token用量",
            "模型用量",
            "插件ai用量",
            "usage",
        ),
    ),
    "memory": (
        "长期偏好记忆的查询与保存",
        (
            "记住",
            "偏好",
            "长期记忆",
            "别再问",
            "remember",
            "preference",
            "preferences",
            "memory",
            "记忆",
            "我的偏好",
        ),
    ),
    "network": (
        "后端公网出口 IP、地区与 ISP",
        ("网络环境", "公网ip", "出口ip", "当前ip", "isp", "network status"),
    ),
    "notifications": (
        "项目通知 Bot 路由、凭据来源与测试发送",
        (
            "通知bot",
            "通知机器人",
            "告警bot",
            "notifybot",
            "notify bot",
            "通知通道",
            "告警通道",
        ),
    ),
    "message_templates": (
        "消息模板目录、变量渲染、HTML 校验与测试发送",
        (
            "消息模板",
            "模板实验室",
            "渲染模板",
            "模板预览",
            "测试发送",
            "message template",
        ),
    ),
    "plugin_repos": (
        "使用者接入的插件仓库与从仓库安装",
        (
            "插件仓库",
            "仓库",
            "repository",
            "repo",
            "plugin repo",
            "remote plugin",
        ),
    ),
    "plugins": (
        "已安装插件、检查更新、安装卸载与全局启停",
        (
            "插件",
            "plugin",
            "plugins",
            "扩展",
            "extension",
            "已安装插件",
            "卸载插件",
            "安装插件",
            "plugin update",
        ),
    ),
    "product": (
        "TelePilot 产品版本、更新日志与界面入口",
        (
            "更新日志",
            "版本日志",
            "changelog",
            "最近更新",
            "release notes",
            "这版更新",
            "更新了什么",
            "界面入口",
        ),
    ),
    "providers": (
        "模型 Provider 的列表、保存、验证与删除",
        (
            "provider",
            "providers",
            "提供商",
            "模型服务",
            "api key",
            "密钥",
            "模型健康",
            "测活",
            "冷却",
            "fallback",
            "模型状态",
            "provider 状态",
            "liveness",
            "cooldown",
            "runtime health",
            "模型提供商",
            "api_key",
            "apikey",
            "api-key",
            "base_url",
            "baseurl",
            "令牌",
            "测一下能不能用",
            "测通",
            "连通性",
            "接入模型",
            "加个模型",
            "添加模型",
        ),
    ),
    "proxies": (
        "出口代理的列表、测试、保存与删除",
        ("代理", "proxy", "proxies", "socks5", "mtproxy", "出口ip", "代理连通"),
    ),
    "routing": (
        "AI 指令的 auto/fixed 路由",
        ("路由", "routing", "auto", "fixed", "指令路由", "route mode"),
    ),
    "rules": (
        "通用 Rule 的查询、保存、启停与删除",
        ("规则", "rule", "rules", "通用规则"),
    ),
    "scheduler": (
        "定时任务、计划、立即执行与启停",
        (
            "定时",
            "计划任务",
            "scheduler",
            "cron",
            "几点执行",
            "定时任务",
            "schedule",
            "interval",
            "每日任务",
        ),
    ),
    "settings": (
        "全局系统设置、命令前缀、时区、配额、日志保留与安全策略",
        (
            "系统设置",
            "命令前缀",
            "全局时区",
            "日志保留",
            "登录安全",
            "llm配额",
            "付款限额",
            "更新目标",
            "ui偏好",
            "system settings",
        ),
    ),
    "safety": (
        "系统全局总闸",
        ("全局总闸", "总闸", "kill switch", "killswitch", "停止所有worker"),
    ),
    "system": (
        "系统版本、健康状态、运行上下文与网络信息",
        (
            "系统状态",
            "健康",
            "版本",
            "时区",
            "上下文",
            "health",
            "system status",
            "version",
            "timezone",
            "系统信息",
            "你是谁",
            "你能做什么",
            "有哪些工具",
            "权限",
            "配置助手",
            "agent 配置",
            "助手配置",
            "what can you do",
            "who are you",
            "your tools",
            "your permissions",
        ),
    ),
    "system_ops": (
        "系统检查更新、应用更新与重启",
        (
            "系统更新",
            "在线更新",
            "检查更新",
            "重启系统",
            "重启应用",
            "升级",
            "update",
            "upgrade",
            "restart system",
            "apply update",
            "检查升级",
        ),
    ),
    "sudo": (
        "Sudo 用户及聊天/指令授权范围",
        ("sudo", "sudo用户", "管理用户", "授权用户", "指令权限", "聊天权限"),
    ),
    "devices": (
        "Telegram 设备伪装档案",
        ("设备档案", "设备伪装", "device profile", "device_model", "系统版本伪装"),
    ),
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
    "list",
    "show",
    "check",
    "create",
    "delete",
    "enable",
    "disable",
    "restart",
)
_REFERENCE_HINTS = (
    "它",
    "这个",
    "这条",
    "刚才",
    "上一个",
    "那个",
    "继续",
    "重试",
    "再试一次",
    "it",
    "that",
    "again",
    "retry",
)
_CORRECTION_HINTS = (
    "排查错",
    "查错",
    "不对",
    "你肯定",
    "重新查",
    "wrong place",
    "not right",
    "incorrect",
    "check again",
)
_GENERAL_HELP_HINTS = (
    "怎么使用agent",
    "怎么用agent",
    "如何使用agent",
    "怎么使用你",
    "怎么用你",
    "你能做什么",
    "你有哪些能力",
    "有哪些工具",
    "你的权限",
    "你是谁",
    "帮助",
    "help",
    "what can you do",
    "who are you",
    "your tools",
    "your capabilities",
)
_PRODUCT_HELP_HINTS = (
    "更新日志",
    "版本日志",
    "changelog",
    "最近更新",
    "这版更新",
    "更新了什么",
    "release notes",
)


@dataclass(frozen=True)
class ToolRoute:
    domains: tuple[str, ...]
    source: str
    reason: str = ""


def tool_domain(spec: ToolSpec) -> str:
    if spec.name in {"system.check_update", "system.apply_update", "system.restart"}:
        return "system_ops"
    return spec.name.split(".", 1)[0]


def available_domains(specs: Iterable[ToolSpec]) -> set[str]:
    return {tool_domain(spec) for spec in specs}


def domain_catalog() -> dict[str, tuple[str, tuple[str, ...]]]:
    """内置域 + 动态插件域的合并视图。"""

    merged = dict(DOMAIN_CATALOG)
    merged.update(_DYNAMIC_DOMAIN_CATALOG)
    return merged


def register_dynamic_domain(
    domain: str,
    description: str,
    keywords: tuple[str, ...] | list[str],
) -> None:
    key = str(domain or "").strip()
    if not key:
        return
    kw = tuple(str(item).strip() for item in keywords if str(item).strip())[:12]
    _DYNAMIC_DOMAIN_CATALOG[key] = (str(description or key)[:200], kw or (key,))


def unregister_dynamic_domains(domains: Iterable[str] | None = None) -> None:
    if domains is None:
        _DYNAMIC_DOMAIN_CATALOG.clear()
        return
    for domain in domains:
        _DYNAMIC_DOMAIN_CATALOG.pop(str(domain), None)


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def has_explicit_web_intent(text: str) -> bool:
    """识别路由模型失败时仍不能丢弃的明确公开信息检索意图。"""

    normalized = re.sub(r"\s+", "", str(text or "").lower())
    recency_hints = ("最近", "最新", "近期", "latest", "recent")
    public_activity_hints = (
        "发文",
        "发布了什么",
        "说了什么",
        "公开动态",
        "x动态",
        "推文",
        "官推",
        "twitter",
        "tweet",
    )
    return any(hint in normalized for hint in recency_hints) and any(
        hint in normalized for hint in public_activity_hints
    )


def route_locally(
    text: str,
    *,
    available: set[str],
    memory_state: dict[str, Any] | None = None,
) -> ToolRoute | None:
    raw_lower = str(text or "").lower()
    normalized = re.sub(r"\s+", "", raw_lower)
    state = memory_state if isinstance(memory_state, dict) else {}
    previous = [str(item) for item in (state.get("last_domains") or []) if str(item) in available]
    general_help = any(
        (
            bool(re.search(r"(?<![a-z0-9_-])help(?![a-z0-9_-])", raw_lower))
            if hint.lower() == "help"
            else re.sub(r"\s+", "", hint.lower()) in normalized
        )
        for hint in _GENERAL_HELP_HINTS
    )
    if general_help:
        return ToolRoute((), "local", "general_help")

    if "product" in available and any(
        re.sub(r"\s+", "", hint.lower()) in normalized for hint in _PRODUCT_HELP_HINTS
    ):
        return ToolRoute(("product",), "local", "product_changelog")

    # Base URL + API Key 同现：锁定 providers，且不把带密钥的轮次交给联网/源码工具
    if looks_like_provider_credential_paste(text):
        if "providers" in available:
            return ToolRoute(("providers",), "local", "provider_credential_paste")
        return ToolRoute((), "local", "provider_credential_paste_without_provider_tools")

    # 上一轮已经是 Provider 测活时，允许用户在下一轮只补发一个 Key。
    # Key 仅用于当前轮路由与工具调用，不写入 memory_state。
    if "providers" in previous and looks_like_standalone_provider_key(text):
        return ToolRoute(("providers",), "memory", "provider_key_continuation")

    matched: list[str] = []
    for domain, (_description, keywords) in domain_catalog().items():
        if domain not in available:
            continue
        if any(keyword.replace(" ", "").lower() in normalized for keyword in keywords):
            matched.append(domain)

    if "interaction" in matched and "规则" in normalized and "通用" not in normalized:
        matched = [domain for domain in matched if domain != "rules"]
    if "ignored" in matched:
        # “忽略账号/用户/群”中的“账号”只是在描述忽略对象，不能让通用账号域抢走路由。
        matched = [domain for domain in matched if domain != "accounts"]
    if any(domain in matched for domain in ("account_bots", "rate_limits", "humanize")):
        # 这些领域的 account_id 只是作用域，不需要再披露通用账号维护工具。
        matched = [domain for domain in matched if domain != "accounts"]
    if "message_templates" in matched and any(
        token in normalized for token in ("消息模板", "模板实验室", "渲染", "预览", "测试发送")
    ):
        matched = [domain for domain in matched if domain != "commands"]
    if "settings" in matched and any(
        token in normalized
        for token in (
            "系统设置",
            "命令前缀",
            "全局时区",
            "日志保留",
            "登录安全",
            "llm配额",
            "付款限额",
            "更新目标",
            "ui偏好",
        )
    ):
        matched = [domain for domain in matched if domain not in {"logs", "system"}]
    plugin_diagnostic_intent = "plugins" in matched and any(
        token in normalized
        for token in (
            "debug",
            "排障",
            "排查",
            "诊断",
            "报错",
            "错误",
            "异常",
            "不工作",
            "根因",
            "修复方式",
            "怎么修",
        )
    )
    if plugin_diagnostic_intent:
        # 插件诊断需要同时看到安装状态、运行日志和只读插件源码；
        # 写工具会由 diagnostics 技能的 diagnostic_safe 门禁自动排除。
        matched = ["plugins"]
        matched.extend(domain for domain in ("logs", "source") if domain in available)
    if (
        "plugin_repos" in matched
        and "plugins" in matched
        and (
            "插件仓库" in normalized
            or "仓库" in normalized
            or "pluginrepo" in normalized
            or "repository" in normalized
        )
    ):
        matched = [domain for domain in matched if domain != "plugins"]
    plugin_account_toggle = (
        "features" in available
        and bool({"plugins", "plugin_repos"}.intersection(matched))
        and "账号" in normalized
        and "插件" in normalized
        and any(token in normalized for token in ("启用", "停用", "禁用", "开启", "关闭"))
    )
    if plugin_account_toggle:
        install_intent = any(token in normalized for token in ("安装", "install"))
        if install_intent:
            # 安装与账号启用是两个有依赖的 Action。当前轮只披露安装域，
            # 安装成功后下一轮再读取 feature matrix 并生成启用 Action。
            matched = [domain for domain in matched if domain in {"plugins", "plugin_repos"}]
        else:
            matched = ["features"]
    # feature + account 同现时优先 features（账号级功能开关）
    if (
        "features" in matched
        and "accounts" in matched
        and ("feature" in normalized or "功能" in normalized or "插件" in normalized)
    ):
        matched = [domain for domain in matched if domain != "accounts"] or matched
    # 日志排障应允许模型从运行证据继续追到部署源码，而不要求用户再补一句“看代码”。
    diagnostic_intent = (
        "排障",
        "排查",
        "定位",
        "根因",
        "为什么",
        "原因",
        "debug",
        "diagnos",
    )
    if (
        "logs" in matched
        and "source" in available
        and any(token in normalized for token in diagnostic_intent)
        and "source" not in matched
    ):
        matched.append("source")
    if matched:
        return ToolRoute(tuple(dict.fromkeys(matched[:3])), "local", "keyword_match")
    if previous and any(re.sub(r"\s+", "", hint.lower()) in normalized for hint in _CORRECTION_HINTS):
        return ToolRoute(tuple(previous[:3]), "memory", "correction_to_previous_domain")
    if previous and any(hint in normalized for hint in _REFERENCE_HINTS):
        return ToolRoute(tuple(previous[:3]), "memory", "reference_to_previous_domain")

    if any(hint in normalized for hint in _ACTION_HINTS):
        return None
    # 无 CJK 且无领域关键词：交给模型路由，避免英文请求被误判为「无需实时数据」
    if not _contains_cjk(str(text or "")):
        return None
    return ToolRoute((), "local", "no_live_system_data_needed")


def router_system_prompt(available: set[str]) -> str:
    catalog_map = domain_catalog()
    catalog = [
        {"domain": domain, "description": catalog_map.get(domain, (domain, ()))[0]}
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
        dict.fromkeys(str(item) for item in (payload.get("domains") or []) if str(item) in available)
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
    "domain_catalog",
    "has_explicit_web_intent",
    "parse_model_route",
    "register_dynamic_domain",
    "route_locally",
    "router_system_prompt",
    "unregister_dynamic_domains",
    "select_tool_specs",
    "tool_domain",
]

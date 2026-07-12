"""集中式 LLM 客户端身份目录与解析器（0.57.0 阶段 A）。

背景
----
过去所有普通 LLM 请求都共用 ``TelePilot/<version> LLM Client`` 这个产品 UA。
部分上游（要求 Codex / Claude Code 身份的反代或官方端点）会因此拒绝协议本身
完全正确的请求。本模块把"客户端身份"从三个协议 Client 里抽出来集中管理：

- ``client_identity_profile`` 与 ``protocol_profile`` **相互独立**：
  前者只控制 UA 与身份相关、且不改变模型语义的安全请求头；
  后者（仅 Anthropic Messages 的 ``standard / claude_code_proxy``）控制协议语义
  与 beta 头。切换身份**绝不**自动打开任何 beta 能力。
- 身份必须依据"本次实际协议"（``effective api_format``）解析，而不是 Provider
  的默认协议。例如 Provider 默认 chat_completions，但联网搜索临时切到 responses，
  本次请求就必须使用 Codex 身份。

安全红线
--------
- 只发送可复核的 UA 和安全辅助头；不复制 OAuth token、账户 ID、设备证明、
  客户端签名或浏览器 Cookie。
- ``minimal`` 不附加任何产品模拟头，仅保留协议必需头。
- 无法用真实捕获 / 上游开源实现验证的档案（Codex Desktop / Claude Desktop）
  保持 ``verified=False`` 且 ``selectable=False``，前端不可选，探测也不使用。

证据来源
--------
所有产品身份的 UA / originator / x-app 均来自本机安装的真实客户端与其上游开源
实现（见各档案 ``evidence`` 字段）。禁止凭记忆猜测。
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Any

from ..db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
)

# ── 身份档案枚举 ────────────────────────────────────────────
CLIENT_IDENTITY_AUTO = "auto"
CLIENT_IDENTITY_MINIMAL = "minimal"
CLIENT_IDENTITY_OPENAI_SDK = "openai_sdk"
CLIENT_IDENTITY_CODEX_CLI = "codex_cli"
CLIENT_IDENTITY_CODEX_DESKTOP = "codex_desktop"
CLIENT_IDENTITY_CLAUDE_CODE = "claude_code"
CLIENT_IDENTITY_CLAUDE_DESKTOP = "claude_desktop"

# 数据库 / schema 允许写入的取值（含 auto / minimal）。
ALL_CLIENT_IDENTITY_PROFILES = {
    CLIENT_IDENTITY_AUTO,
    CLIENT_IDENTITY_MINIMAL,
    CLIENT_IDENTITY_OPENAI_SDK,
    CLIENT_IDENTITY_CODEX_CLI,
    CLIENT_IDENTITY_CODEX_DESKTOP,
    CLIENT_IDENTITY_CLAUDE_CODE,
    CLIENT_IDENTITY_CLAUDE_DESKTOP,
}

DEFAULT_CLIENT_IDENTITY_PROFILE = CLIENT_IDENTITY_AUTO

# ``auto`` 按本次实际协议解析出的默认身份。
_AUTO_IDENTITY_BY_FORMAT = {
    LLM_API_FORMAT_CHAT_COMPLETIONS: CLIENT_IDENTITY_OPENAI_SDK,
    LLM_API_FORMAT_RESPONSES: CLIENT_IDENTITY_CODEX_CLI,
    LLM_API_FORMAT_ANTHROPIC_MESSAGES: CLIENT_IDENTITY_CLAUDE_CODE,
}

# 协议检测的身份探测顺序（标准身份成功即停止）。
IDENTITY_PROBE_ORDER = {
    LLM_API_FORMAT_CHAT_COMPLETIONS: (
        CLIENT_IDENTITY_OPENAI_SDK,
        CLIENT_IDENTITY_MINIMAL,
    ),
    LLM_API_FORMAT_RESPONSES: (
        CLIENT_IDENTITY_CODEX_CLI,
        CLIENT_IDENTITY_MINIMAL,
    ),
    LLM_API_FORMAT_ANTHROPIC_MESSAGES: (
        CLIENT_IDENTITY_CLAUDE_CODE,
        CLIENT_IDENTITY_MINIMAL,
    ),
}


@dataclass(frozen=True)
class ClientIdentity:
    """解析后的客户端身份档案。

    字段
    ----
    - ``profile``      档案名（openai_sdk / codex_cli / ...）。
    - ``api_formats``  该档案适用的协议集合。
    - ``user_agent``   最终 UA；``None`` 表示不覆盖（minimal 走协议默认或不发 UA）。
    - ``extra_headers``身份相关的安全辅助头（不含 Authorization / api-key）。
    - ``source``       证据来源说明（本机客户端版本 / 上游源码路径）。
    - ``captured_at``  证据采集日期。
    - ``client_version`` 采集时的客户端版本。
    - ``verified``     是否有可复核证据；False 的档案前端不可选、探测不使用。
    - ``experimental`` 是否为实验/占位档案。
    """

    profile: str
    api_formats: frozenset[str]
    user_agent: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    source: str = ""
    captured_at: str = ""
    client_version: str = ""
    verified: bool = True
    experimental: bool = False

    def headers(self) -> dict[str, str]:
        """返回本次请求应附加的身份头（含 UA）。

        注意：不含 Authorization / x-api-key —— 那些由各协议 Client 自行装配，
        避免密钥进入身份目录。
        """
        result: dict[str, str] = {}
        if self.user_agent:
            result["User-Agent"] = self.user_agent
        for key, value in self.extra_headers.items():
            result[key] = value
        return result

    def summary(self) -> dict[str, Any]:
        """脱敏摘要（用于诊断 / 路由摘要，可安全返回给前端 / 插件）。"""
        return {
            "profile": self.profile,
            "user_agent": self.user_agent,
            "source": self.source,
            "captured_at": self.captured_at,
            "client_version": self.client_version,
            "verified": self.verified,
            "experimental": self.experimental,
        }


# ────────────────────────────────────────────────────────────
# UA 组装辅助
# ────────────────────────────────────────────────────────────

def _os_slug() -> str:
    """返回小写 OS 标识（用于 Codex UA 的 os 段）。"""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    return system or "unknown"


def _os_version() -> str:
    release = platform.release() or "unknown"
    return release


def _arch_slug() -> str:
    machine = platform.machine().lower()
    return machine or "unknown"


def _codex_user_agent(client_version: str) -> str:
    """复刻 Codex CLI 的 UA 结构。

    上游 ``codex-rs/login/src/auth/default_client.rs::get_codex_user_agent``：
        ``"{originator}/{version} ({os} {os_version}; {arch}) {terminal}"``
    我们没有真实终端字符串，用稳定占位 ``unknown`` 收尾，其余段落如实反映
    运行主机，避免伪造。
    """
    return (
        f"codex_cli_rs/{client_version} "
        f"({_os_slug()} {_os_version()}; {_arch_slug()}) unknown"
    )


# ────────────────────────────────────────────────────────────
# 身份目录
#
# 每个产品档案都必须有可复核证据：本机安装的真实客户端 + 其上游开源实现。
# 版本升级时先更新 fixture，再更新此处常量。
# ────────────────────────────────────────────────────────────

# Codex CLI 采集自本机 codex-cli 0.143.0（/opt/homebrew 安装）与上游
# openai/codex 源码（codex-rs/login/src/auth/default_client.rs：
# DEFAULT_ORIGINATOR="codex_cli_rs"、get_codex_user_agent 结构；
# 请求头 "originator" + "User-Agent"）。
_CODEX_CLI_VERSION = "0.143.0"

# Claude Code 采集自本机 @anthropic-ai/claude-code 2.1.205：
# UA 前缀 ``claude-cli/<version> (external, cli)``、请求头 ``x-app: cli``、
# ``anthropic-version: 2023-06-01``（后者由 Anthropic Client 统一装配）。
_CLAUDE_CODE_VERSION = "2.1.205"

# OpenAI 官方 Python SDK 采集自上游 openai-python 2.45.0
# (src/openai/_base_client.py::user_agent → ``OpenAI/Python <version>``)。
_OPENAI_SDK_VERSION = "2.45.0"


def _build_catalog() -> dict[str, ClientIdentity]:
    catalog: dict[str, ClientIdentity] = {}

    catalog[CLIENT_IDENTITY_MINIMAL] = ClientIdentity(
        profile=CLIENT_IDENTITY_MINIMAL,
        api_formats=frozenset(
            {
                LLM_API_FORMAT_CHAT_COMPLETIONS,
                LLM_API_FORMAT_RESPONSES,
                LLM_API_FORMAT_ANTHROPIC_MESSAGES,
            }
        ),
        # minimal 不附加任何产品模拟头，也不覆盖 UA —— 让 httpx 走其默认，
        # 或由协议 Client 保持协议必需头。这里 user_agent=None 表示不注入身份 UA。
        user_agent=None,
        extra_headers={},
        source="协议必需头，无产品模拟",
        verified=True,
    )

    catalog[CLIENT_IDENTITY_OPENAI_SDK] = ClientIdentity(
        profile=CLIENT_IDENTITY_OPENAI_SDK,
        api_formats=frozenset(
            {LLM_API_FORMAT_CHAT_COMPLETIONS, LLM_API_FORMAT_RESPONSES}
        ),
        user_agent=f"OpenAI/Python {_OPENAI_SDK_VERSION}",
        extra_headers={"X-Stainless-Lang": "python"},
        source="openai-python _base_client.py user_agent",
        captured_at="2026-07-12",
        client_version=_OPENAI_SDK_VERSION,
        verified=True,
    )

    catalog[CLIENT_IDENTITY_CODEX_CLI] = ClientIdentity(
        profile=CLIENT_IDENTITY_CODEX_CLI,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        user_agent=_codex_user_agent(_CODEX_CLI_VERSION),
        # 上游对每个 Responses 请求发送 "originator" 头；session-id 等元数据是
        # 单次会话动态值，属于身份无关的运行态信息，这里不伪造固定值。
        extra_headers={"originator": "codex_cli_rs"},
        source="codex-cli 0.143.0 本机二进制 + codex-rs default_client.rs",
        captured_at="2026-07-12",
        client_version=_CODEX_CLI_VERSION,
        verified=True,
    )

    catalog[CLIENT_IDENTITY_CLAUDE_CODE] = ClientIdentity(
        profile=CLIENT_IDENTITY_CLAUDE_CODE,
        api_formats=frozenset({LLM_API_FORMAT_ANTHROPIC_MESSAGES}),
        user_agent=f"claude-cli/{_CLAUDE_CODE_VERSION} (external, cli)",
        # 上游 Claude Code 请求发送 ``x-app: cli``。anthropic-version 由
        # Anthropic Client 统一装配（本身就是协议必需头，不算身份模拟）。
        extra_headers={"x-app": "cli"},
        source="@anthropic-ai/claude-code 2.1.205 本机二进制",
        captured_at="2026-07-12",
        client_version=_CLAUDE_CODE_VERSION,
        verified=True,
    )

    # ── 未验证 / 实验档案 ──────────────────────────────────
    # 阶段 F 约束：没有真实捕获 / 上游开源实现 / 可复核证据前，Desktop 档案保持
    # 不可选、不参与探测。占位存在只为让枚举、迁移、前端 disabled 项一致。
    catalog[CLIENT_IDENTITY_CODEX_DESKTOP] = ClientIdentity(
        profile=CLIENT_IDENTITY_CODEX_DESKTOP,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        user_agent=None,
        extra_headers={},
        source="未获得可复核的 Codex Desktop 请求头证据",
        verified=False,
        experimental=True,
    )
    catalog[CLIENT_IDENTITY_CLAUDE_DESKTOP] = ClientIdentity(
        profile=CLIENT_IDENTITY_CLAUDE_DESKTOP,
        api_formats=frozenset({LLM_API_FORMAT_ANTHROPIC_MESSAGES}),
        user_agent=None,
        extra_headers={},
        source="未获得可复核的 Claude Desktop 请求头证据",
        verified=False,
        experimental=True,
    )

    return catalog


_CATALOG: dict[str, ClientIdentity] = _build_catalog()


def normalize_identity_profile(value: str | None) -> str:
    """把任意输入规范化为合法档案名；未知值降级为 auto。"""
    candidate = (value or "").strip().lower()
    if candidate in ALL_CLIENT_IDENTITY_PROFILES:
        return candidate
    return DEFAULT_CLIENT_IDENTITY_PROFILE


def default_identity_for_format(api_format: str | None) -> str:
    """给定实际协议，返回 auto 映射的默认身份档案名。"""
    fmt = (api_format or "").strip().lower()
    return _AUTO_IDENTITY_BY_FORMAT.get(fmt, CLIENT_IDENTITY_MINIMAL)


def is_identity_compatible(profile: str, api_format: str | None) -> bool:
    """固定身份是否兼容给定协议。auto / minimal 兼容所有协议。"""
    normalized = normalize_identity_profile(profile)
    if normalized in {CLIENT_IDENTITY_AUTO, CLIENT_IDENTITY_MINIMAL}:
        return True
    identity = _CATALOG.get(normalized)
    if identity is None:
        return False
    fmt = (api_format or "").strip().lower()
    return fmt in identity.api_formats


def resolve_identity(
    configured_profile: str | None,
    effective_api_format: str | None,
) -> ClientIdentity:
    """根据"本次实际协议"解析出要使用的客户端身份。

    - ``auto`` → 按 ``effective_api_format`` 映射默认身份。
    - 固定档案但与本次协议不兼容（例如联网搜索切协议后原固定身份失效）→ 回落到
      该协议的 auto 默认身份，保证请求仍带合适身份而不是错协议的身份。
    - 未验证 / 实验档案 → 回落到该协议的 auto 默认身份，绝不发送未验证头。
    """
    normalized = normalize_identity_profile(configured_profile)
    fmt = (effective_api_format or "").strip().lower()

    if normalized == CLIENT_IDENTITY_AUTO:
        target = default_identity_for_format(fmt)
    else:
        target = normalized

    identity = _CATALOG.get(target)
    if identity is None:
        identity = _CATALOG[CLIENT_IDENTITY_MINIMAL]

    # 协议不兼容或未验证 → 回落 auto 默认身份。
    if fmt and fmt not in identity.api_formats:
        fallback = default_identity_for_format(fmt)
        identity = _CATALOG.get(fallback, _CATALOG[CLIENT_IDENTITY_MINIMAL])
    if not identity.verified:
        fallback = default_identity_for_format(fmt)
        identity = _CATALOG.get(fallback, _CATALOG[CLIENT_IDENTITY_MINIMAL])

    return identity


def get_identity(profile: str) -> ClientIdentity | None:
    """按档案名取身份（未知返回 None）。"""
    return _CATALOG.get(normalize_identity_profile(profile))


def selectable_identities() -> list[dict[str, Any]]:
    """返回前端可选的身份档案清单（含 auto / minimal 与已验证产品档案）。

    未验证 / 实验档案标记 ``selectable=False``，前端渲染为禁用项。
    """
    items: list[dict[str, Any]] = [
        {
            "profile": CLIENT_IDENTITY_AUTO,
            "selectable": True,
            "verified": True,
            "experimental": False,
            "api_formats": sorted(_AUTO_IDENTITY_BY_FORMAT.keys()),
            "source": "按本次实际协议自动解析",
        }
    ]
    for profile in (
        CLIENT_IDENTITY_MINIMAL,
        CLIENT_IDENTITY_OPENAI_SDK,
        CLIENT_IDENTITY_CODEX_CLI,
        CLIENT_IDENTITY_CODEX_DESKTOP,
        CLIENT_IDENTITY_CLAUDE_CODE,
        CLIENT_IDENTITY_CLAUDE_DESKTOP,
    ):
        identity = _CATALOG[profile]
        item = identity.summary()
        item["selectable"] = identity.verified
        item["api_formats"] = sorted(identity.api_formats)
        items.append(item)
    return items


__all__ = [
    "ALL_CLIENT_IDENTITY_PROFILES",
    "CLIENT_IDENTITY_AUTO",
    "CLIENT_IDENTITY_CLAUDE_CODE",
    "CLIENT_IDENTITY_CLAUDE_DESKTOP",
    "CLIENT_IDENTITY_CODEX_CLI",
    "CLIENT_IDENTITY_CODEX_DESKTOP",
    "CLIENT_IDENTITY_MINIMAL",
    "CLIENT_IDENTITY_OPENAI_SDK",
    "DEFAULT_CLIENT_IDENTITY_PROFILE",
    "IDENTITY_PROBE_ORDER",
    "ClientIdentity",
    "default_identity_for_format",
    "get_identity",
    "is_identity_compatible",
    "normalize_identity_profile",
    "resolve_identity",
    "selectable_identities",
]

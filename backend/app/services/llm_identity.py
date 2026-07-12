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
import re
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
    """返回 Codex UA os 段的 OS 名。

    格式对照本机 codex 原生二进制（``login/src/auth/default_client.rs``）与真实
    抓包：macOS 拼作 ``Mac OS``（带空格、首字母大写），不是 ``macos``。
    """
    system = platform.system()
    if system == "Darwin":
        return "Mac OS"
    if system == "Windows":
        return "Windows"
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


def _codex_desktop_user_agent(core_version: str, app_build: str) -> str:
    """复刻 Codex Desktop 的 UA 结构。

    证据来自本机 Surge 抓包（Codex Desktop 0.144.0-alpha.4、app 构建号
    26.707.51957）：
        ``Codex Desktop/{core_version} ({os} {os_version}; {arch}) unknown``
        ``(Codex Desktop; {app_build})``
    ``core_version`` 是 codex 核心版本、``app_build`` 是桌面 app 构建号，两者都由
    可配置版本项提供；OS/arch 如实反映运行主机，终端段用稳定占位 ``unknown``。
    session-id / installation_id / turn-metadata 等运行态与设备指纹**不进目录**。
    """
    return (
        f"Codex Desktop/{core_version} "
        f"({_os_slug()} {_os_version()}; {_arch_slug()}) unknown "
        f"(Codex Desktop; {app_build})"
    )


# ────────────────────────────────────────────────────────────
# 身份目录
#
# 每个产品档案都必须有可复核证据：本机安装的真实客户端 + 其上游开源实现。
# 版本升级时先更新 fixture，再更新此处常量。
# ────────────────────────────────────────────────────────────

# ── 版本号（仅 UA 里的版本段，可被系统设置覆盖）────────────
#
# 重要边界：这里只维护"版本号"这一个可随上游发版漂移的字段。UA 的**结构**、
# 请求头字段名/值都必须由人工对照真实客户端核对后写入下方 catalog，绝不随版本
# 自动变化。运维可通过 ``GET {npm/PyPI}`` 查询最新版本号并手动刷新（见
# ``apply_version_overrides``），但结构变更仍需重新核对证据。
#
# Codex CLI 采集自本机 codex-cli 0.143.0（/opt/homebrew 安装）与上游
# openai/codex 源码（codex-rs/login/src/auth/default_client.rs：
# DEFAULT_ORIGINATOR="codex_cli_rs"、get_codex_user_agent 结构；
# 请求头 "originator" + "User-Agent"）。
#
# Claude Code 采集自本机 @anthropic-ai/claude-code 2.1.205 原生二进制：
# UA 模板 ``claude-cli/<version> (external, <entrypoint>[, agent-sdk/<v>...])``。
# 二进制内真实构造为
# ``claude-cli/${version} (external, ${CLAUDE_CODE_ENTRYPOINT ?? "cli"}...)``：
# ``entrypoint`` 是运行时环境变量，裸终端默认 ``cli``（本档案即取此默认值），
# 经 agent SDK / desktop-3p 入口启动时才会拼上 ``claude-desktop-3p`` /
# ``agent-sdk/<ver>`` 等动态段——那些属于单次运行态，不是固定身份，故不写死。
# 请求头 ``x-app: cli``（后台任务时上游发 ``cli-bg``，此处取前台默认 ``cli``）；
# ``anthropic-version: 2023-06-01`` 由 Anthropic Client 统一装配。
#
# OpenAI 官方 Python SDK 采集自上游 openai-python 2.45.0
# (src/openai/_base_client.py::user_agent → ``OpenAI/Python <version>``)。

# 采集时核对过 UA 结构的真实版本（DB 覆盖缺失时回落这些）。
_DEFAULT_CLIENT_VERSIONS: dict[str, str] = {
    "codex_cli": "0.143.0",
    "claude_code": "2.1.205",
    "openai_sdk": "2.45.0",
    # Codex Desktop 需两段版本：codex 核心版本（可含 -alpha.N 预发布后缀）与
    # 桌面 app 构建号。均来自本机 Surge 抓包（2026-07-12）。
    "codex_desktop_core": "0.144.0-alpha.4",
    "codex_desktop_build": "26.707.51957",
}

# 当前生效版本；``apply_version_overrides`` 可用系统设置里的值覆盖（只改版本号）。
_CLIENT_VERSIONS: dict[str, str] = dict(_DEFAULT_CLIENT_VERSIONS)


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
        user_agent=f"OpenAI/Python {_CLIENT_VERSIONS['openai_sdk']}",
        extra_headers={"X-Stainless-Lang": "python"},
        source="openai-python _base_client.py user_agent",
        captured_at="2026-07-12",
        client_version=_CLIENT_VERSIONS["openai_sdk"],
        verified=True,
    )

    catalog[CLIENT_IDENTITY_CODEX_CLI] = ClientIdentity(
        profile=CLIENT_IDENTITY_CODEX_CLI,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        user_agent=_codex_user_agent(_CLIENT_VERSIONS["codex_cli"]),
        # 上游对每个 Responses 请求发送 "originator" 头；session-id 等元数据是
        # 单次会话动态值，属于身份无关的运行态信息，这里不伪造固定值。
        extra_headers={"originator": "codex_cli_rs"},
        source="codex-cli 0.143.0 本机二进制 + codex-rs default_client.rs",
        captured_at="2026-07-12",
        client_version=_CLIENT_VERSIONS["codex_cli"],
        verified=True,
    )

    catalog[CLIENT_IDENTITY_CLAUDE_CODE] = ClientIdentity(
        profile=CLIENT_IDENTITY_CLAUDE_CODE,
        api_formats=frozenset({LLM_API_FORMAT_ANTHROPIC_MESSAGES}),
        user_agent=f"claude-cli/{_CLIENT_VERSIONS['claude_code']} (external, cli)",
        # 上游 Claude Code 请求发送 ``x-app: cli``。anthropic-version 由
        # Anthropic Client 统一装配（本身就是协议必需头，不算身份模拟）。
        extra_headers={"x-app": "cli"},
        source="@anthropic-ai/claude-code 2.1.205 本机二进制",
        captured_at="2026-07-12",
        client_version=_CLIENT_VERSIONS["claude_code"],
        verified=True,
    )

    # ── Codex Desktop：本机 Surge 抓包实证（0.144.0-alpha.4 / build 26.707.51957）──
    # 抓包实测请求头只提取两项身份字段：
    #   originator: Codex Desktop
    #   user-agent: Codex Desktop/{core} ({os} {osver}; {arch}) unknown (Codex Desktop; {build})
    # 明确排除的抓包内容（阶段 F / 安全边界）：Authorization Bearer、session-id、
    # thread-id、x-client-request-id、x-codex-window-id、installation_id、
    # x-codex-turn-metadata（含仓库路径 / git commit / 设备指纹）等运行态与机密；
    # 以及 x-codex-beta-features、x-openai-internal-codex-responses-lite 等会改变
    # 上游语义 / 开 beta 的头——身份切换不得改语义，一律不带。
    catalog[CLIENT_IDENTITY_CODEX_DESKTOP] = ClientIdentity(
        profile=CLIENT_IDENTITY_CODEX_DESKTOP,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        user_agent=_codex_desktop_user_agent(
            _CLIENT_VERSIONS["codex_desktop_core"],
            _CLIENT_VERSIONS["codex_desktop_build"],
        ),
        extra_headers={"originator": "Codex Desktop"},
        source=(
            "本机 Surge 抓包 Codex Desktop 0.144.0-alpha.4（app build 26.707.51957）；"
            "证据来自单台机器的 alpha 预发布版，stable 版 UA 可能变，需再核对"
        ),
        captured_at="2026-07-13",
        client_version=_CLIENT_VERSIONS["codex_desktop_core"],
        verified=True,
    )
    catalog[CLIENT_IDENTITY_CLAUDE_DESKTOP] = ClientIdentity(
        profile=CLIENT_IDENTITY_CLAUDE_DESKTOP,
        api_formats=frozenset({LLM_API_FORMAT_ANTHROPIC_MESSAGES}),
        user_agent=None,
        extra_headers={},
        source="未获得可复核的 Claude Desktop 请求头证据（按用户决定暂不落地）",
        verified=False,
        experimental=True,
    )

    return catalog


_CATALOG: dict[str, ClientIdentity] = _build_catalog()


# 允许被系统设置覆盖版本号的档案键（与 _DEFAULT_CLIENT_VERSIONS 对齐）。
_VERSION_OVERRIDE_KEYS = frozenset(_DEFAULT_CLIENT_VERSIONS.keys())

# 版本号必须形如 x.y[.z...]，可带一个 -alpha.N / -beta.N / -rc.N 预发布后缀
# （Codex Desktop 用 0.144.0-alpha.4 这类格式）。仅允许字母/数字/点/连字符，
# 拒绝空格、引号、控制字符等任何可能污染 UA 头的内容。
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}(?:-[A-Za-z]+(?:\.\d+)?)?$")


def default_client_versions() -> dict[str, str]:
    """返回采集时核对过的默认版本号（DB 覆盖缺失时的回落值）。"""
    return dict(_DEFAULT_CLIENT_VERSIONS)


def current_client_versions() -> dict[str, str]:
    """返回当前生效的版本号（含已应用的覆盖）。"""
    return dict(_CLIENT_VERSIONS)


def is_valid_version(value: str | None) -> bool:
    """校验版本号字符串是否为安全的纯数字点分格式。"""
    return bool(value and _VERSION_RE.match(str(value).strip()))


def apply_version_overrides(overrides: dict[str, str] | None) -> dict[str, str]:
    """用系统设置里的版本号覆盖 UA 版本段，并重建身份目录。

    只接受 ``_VERSION_OVERRIDE_KEYS`` 里的键、且值必须通过 ``is_valid_version``；
    非法/未知项忽略。缺省项回落到采集默认值。**只改版本号，不动 UA 结构与头。**
    返回应用后的生效版本映射。
    """
    global _CATALOG
    effective = dict(_DEFAULT_CLIENT_VERSIONS)
    for key, value in (overrides or {}).items():
        if key in _VERSION_OVERRIDE_KEYS and is_valid_version(value):
            effective[key] = str(value).strip()
    _CLIENT_VERSIONS.clear()
    _CLIENT_VERSIONS.update(effective)
    _CATALOG = _build_catalog()
    return dict(_CLIENT_VERSIONS)


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
    "apply_version_overrides",
    "current_client_versions",
    "default_client_versions",
    "default_identity_for_format",
    "get_identity",
    "is_identity_compatible",
    "is_valid_version",
    "normalize_identity_profile",
    "resolve_identity",
    "selectable_identities",
]

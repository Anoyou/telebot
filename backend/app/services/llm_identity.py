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
- 历史 Desktop 档案不再单独模拟，统一兼容映射到对应 CLI 身份。

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
CLIENT_IDENTITY_CODEX_TUI = "codex_tui"
# 仅用于兼容旧配置；normalize 后不会作为可选档案暴露。
CLIENT_IDENTITY_CODEX_CLI = "codex_cli"
CLIENT_IDENTITY_CODEX_EXEC = "codex_exec"
CLIENT_IDENTITY_CODEX_DESKTOP = "codex_desktop"
CLIENT_IDENTITY_CLAUDE_CODE = "claude_code"
CLIENT_IDENTITY_CLAUDE_DESKTOP = "claude_desktop"
CLIENT_IDENTITY_GROK_CLI = "grok_cli"

# 数据库 / schema 允许写入的取值（含 auto / minimal）。
ALL_CLIENT_IDENTITY_PROFILES = {
    CLIENT_IDENTITY_AUTO,
    CLIENT_IDENTITY_MINIMAL,
    CLIENT_IDENTITY_OPENAI_SDK,
    CLIENT_IDENTITY_CODEX_TUI,
    CLIENT_IDENTITY_CODEX_DESKTOP,
    CLIENT_IDENTITY_CLAUDE_CODE,
    CLIENT_IDENTITY_CLAUDE_DESKTOP,
    CLIENT_IDENTITY_GROK_CLI,
}

DEFAULT_CLIENT_IDENTITY_PROFILE = CLIENT_IDENTITY_AUTO

# ``auto`` 按本次实际协议解析出的默认身份。
_AUTO_IDENTITY_BY_FORMAT = {
    LLM_API_FORMAT_CHAT_COMPLETIONS: CLIENT_IDENTITY_OPENAI_SDK,
    LLM_API_FORMAT_RESPONSES: CLIENT_IDENTITY_OPENAI_SDK,
    LLM_API_FORMAT_ANTHROPIC_MESSAGES: CLIENT_IDENTITY_CLAUDE_CODE,
}

# 协议检测的身份探测顺序（标准身份成功即停止）。
IDENTITY_PROBE_ORDER = {
    LLM_API_FORMAT_CHAT_COMPLETIONS: (
        CLIENT_IDENTITY_OPENAI_SDK,
        CLIENT_IDENTITY_MINIMAL,
    ),
    LLM_API_FORMAT_RESPONSES: (
        CLIENT_IDENTITY_OPENAI_SDK,
        CLIENT_IDENTITY_CODEX_TUI,
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
    - ``profile``      档案名（openai_sdk / codex_tui / codex_desktop / ...）。
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


def _stainless_os_slug() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "MacOS"
    if system == "windows":
        return "Windows"
    if system == "linux":
        return "Linux"
    return platform.system() or "Unknown"


def _stainless_arch_slug() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine == "x86_64":
        return "x64"
    if machine in {"i386", "i686", "x86"}:
        return "x32"
    return machine or "unknown"


def _grok_os_slug() -> str:
    system = platform.system().lower()
    return "macos" if system == "darwin" else (system or "unknown")


def _grok_arch_slug() -> str:
    machine = platform.machine().lower()
    return "aarch64" if machine in {"arm64", "aarch64"} else (machine or "unknown")


def _codex_tui_user_agent(client_version: str) -> str:
    """复刻本机交互式 Codex TUI 的 UA 结构。

    上游 ``codex-rs/login/src/auth/default_client.rs::get_codex_user_agent``：
        ``"{originator}/{version} ({os} {os_version}; {arch}) {terminal}"``
    终端段使用用户提供的真实 Apple Terminal 抓包；不复制会话或安装标识。
    """
    return (
        f"codex-tui/{client_version} "
        f"({_os_slug()} {_os_version()}; {_arch_slug()}) Apple_Terminal/487 "
        f"(codex-tui; {client_version})"
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
        f"({_os_slug()} {_os_version()}; {_arch_slug()}) dumb "
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
# Codex TUI 与 Desktop 均来自用户真实抓包；只保留 UA、originator 与随机链路 ID，
# 不复制内部 beta、设备、安装、窗口或工作区元数据。
#
# Claude Code 采集自本机 @anthropic-ai/claude-code 2.1.220 原生二进制：
# UA 模板 ``claude-cli/<version> (external, <entrypoint>[, agent-sdk/<v>...])``。
# 二进制内真实构造为
# ``claude-cli/${version} (external, ${CLAUDE_CODE_ENTRYPOINT ?? "cli"}...)``：
# 当前模型抓包使用 ``cli`` 入口。
# 请求头 ``x-app: cli``（后台任务时上游发 ``cli-bg``，此处取前台默认 ``cli``）；
# ``anthropic-version: 2023-06-01`` 由 Anthropic Client 统一装配。
#
# OpenAI 官方 Python SDK 采集自上游 openai-python 2.45.0
# (src/openai/_base_client.py::user_agent → ``OpenAI/Python <version>``)。

# 采集时核对过 UA 结构的真实版本（DB 覆盖缺失时回落这些）。
_DEFAULT_CLIENT_VERSIONS: dict[str, str] = {
    "codex_tui": "0.145.0",
    "codex_desktop_core": "0.146.0-alpha.3.1",
    "codex_desktop_build": "26.721.41059",
    "claude_code": "2.1.220",
    "claude_sdk": "0.94.0",
    "openai_sdk": "2.45.0",
    "grok_cli": "0.2.112",
}

# 当前生效版本；``apply_version_overrides`` 可用系统设置里的值覆盖（只改版本号）。
_CLIENT_VERSIONS: dict[str, str] = dict(_DEFAULT_CLIENT_VERSIONS)

# 系统设置键：存放运维手动填写的 UA 版本覆盖（JSON: {version_key: version}）。
CLIENT_IDENTITY_VERSIONS_SETTING_KEY = "llm_client_identity_versions"

# 每个版本键的元数据：前端展示名 + 只读检测源。
# ``registry`` 非空表示可自动检测最新版本；None 表示只能手动填写。
# （Codex Desktop 的核心版本与 app 构建号都没有公共 registry）。
_VERSION_KEY_META: dict[str, dict[str, str | None]] = {
    "codex_tui": {"label": "Codex TUI", "registry": "npm:@openai/codex"},
    "codex_desktop_core": {"label": "Codex Desktop 核心", "registry": None},
    "codex_desktop_build": {"label": "Codex Desktop 构建", "registry": None},
    "claude_code": {"label": "Claude Code", "registry": "npm:@anthropic-ai/claude-code"},
    "claude_sdk": {"label": "Anthropic JS SDK", "registry": "npm:@anthropic-ai/sdk"},
    "openai_sdk": {"label": "OpenAI Python SDK", "registry": "pypi:openai"},
    "grok_cli": {"label": "Grok CLI", "registry": "cli:grok-update-check"},
}


def version_key_metadata() -> dict[str, dict[str, str | None]]:
    """返回版本键元数据（展示名 + 远端检测源）的副本。"""
    return {k: dict(v) for k, v in _VERSION_KEY_META.items()}


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
        api_formats=frozenset({LLM_API_FORMAT_CHAT_COMPLETIONS, LLM_API_FORMAT_RESPONSES}),
        user_agent=f"AsyncOpenAI/Python {_CLIENT_VERSIONS['openai_sdk']}",
        extra_headers={
            "X-Stainless-Lang": "python",
            "X-Stainless-Package-Version": _CLIENT_VERSIONS["openai_sdk"],
            "X-Stainless-OS": _stainless_os_slug(),
            "X-Stainless-Arch": _stainless_arch_slug(),
            "X-Stainless-Runtime": platform.python_implementation() or "unknown",
            "X-Stainless-Runtime-Version": platform.python_version() or "unknown",
            "X-Stainless-Async": "async:asyncio",
        },
        source="Surge 本机抓包：openai-python 2.45.0 AsyncOpenAI",
        captured_at="2026-07-26",
        client_version=_CLIENT_VERSIONS["openai_sdk"],
        verified=True,
    )

    catalog[CLIENT_IDENTITY_CODEX_TUI] = ClientIdentity(
        profile=CLIENT_IDENTITY_CODEX_TUI,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        user_agent=_codex_tui_user_agent(_CLIENT_VERSIONS["codex_tui"]),
        # 动态请求链路 ID 由 ResponsesClient 在实例化时生成，不写入固定目录。
        extra_headers={"originator": "codex-tui"},
        source="用户真实抓包：交互式 Codex TUI 0.145.0 Responses",
        captured_at="2026-07-27",
        client_version=_CLIENT_VERSIONS["codex_tui"],
        verified=True,
    )

    catalog[CLIENT_IDENTITY_CODEX_DESKTOP] = ClientIdentity(
        profile=CLIENT_IDENTITY_CODEX_DESKTOP,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        user_agent=_codex_desktop_user_agent(
            _CLIENT_VERSIONS["codex_desktop_core"],
            _CLIENT_VERSIONS["codex_desktop_build"],
        ),
        extra_headers={"originator": "Codex Desktop"},
        source="用户真实抓包：Codex Desktop Responses",
        captured_at="2026-07-27",
        client_version=_CLIENT_VERSIONS["codex_desktop_core"],
        verified=True,
    )

    catalog[CLIENT_IDENTITY_CLAUDE_CODE] = ClientIdentity(
        profile=CLIENT_IDENTITY_CLAUDE_CODE,
        api_formats=frozenset({LLM_API_FORMAT_ANTHROPIC_MESSAGES}),
        user_agent=f"claude-cli/{_CLIENT_VERSIONS['claude_code']} (external, cli)",
        # 上游 Claude Code 请求发送 ``x-app: cli``。anthropic-version 由
        # Anthropic Client 统一装配（本身就是协议必需头，不算身份模拟）。
        extra_headers={
            "x-app": "cli",
            "X-Stainless-Arch": _stainless_arch_slug(),
            "X-Stainless-Lang": "js",
            "X-Stainless-Package-Version": _CLIENT_VERSIONS["claude_sdk"],
            "X-Stainless-Retry-Count": "0",
            "X-Stainless-Runtime": "node",
            "X-Stainless-Runtime-Version": "v26.3.0",
            "X-Stainless-OS": _stainless_os_slug(),
        },
        source="用户真实抓包：Claude Code 2.1.220 CLI / Anthropic JS SDK 0.94.0",
        captured_at="2026-07-27",
        client_version=_CLIENT_VERSIONS["claude_code"],
        verified=True,
    )

    # Grok CLI：只保留真实抓包中不改变认证语义的稳定身份字段。
    # x-xai-token-auth / x-authenticateresponse 属于 CLI 认证语义，不复制。
    catalog[CLIENT_IDENTITY_GROK_CLI] = ClientIdentity(
        profile=CLIENT_IDENTITY_GROK_CLI,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        user_agent=(
            f"grok-pager/{_CLIENT_VERSIONS['grok_cli']} "
            f"grok-shell/{_CLIENT_VERSIONS['grok_cli']} "
            f"({_grok_os_slug()}; {_grok_arch_slug()})"
        ),
        extra_headers={
            "x-grok-client-version": _CLIENT_VERSIONS["grok_cli"],
            "x-grok-client-identifier": "grok-pager",
        },
        source="用户真实抓包：Grok pager/shell 0.2.112 Responses",
        captured_at="2026-07-27",
        client_version=_CLIENT_VERSIONS["grok_cli"],
        verified=True,
    )

    return catalog


_CATALOG: dict[str, ClientIdentity] = _build_catalog()


# 允许被系统设置覆盖版本号的档案键（与 _DEFAULT_CLIENT_VERSIONS 对齐）。
_VERSION_OVERRIDE_KEYS = frozenset(_DEFAULT_CLIENT_VERSIONS.keys())

# 版本号必须形如 x.y[.z...]，可带一个 -alpha.N / -beta.N / -rc.N 预发布后缀
# （Codex Desktop 用 0.144.0-alpha.4 这类格式）。仅允许字母/数字/点/连字符，
# 拒绝空格、引号、控制字符等任何可能污染 UA 头的内容。
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}(?:-[A-Za-z]+(?:\.\d+)*)?$")


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
    normalized_overrides = dict(overrides or {})
    if "codex_tui" not in normalized_overrides and "codex_cli" in normalized_overrides:
        normalized_overrides["codex_tui"] = normalized_overrides["codex_cli"]
    for key, value in normalized_overrides.items():
        if key in _VERSION_OVERRIDE_KEYS and is_valid_version(value):
            effective[key] = str(value).strip()
    _CLIENT_VERSIONS.clear()
    _CLIENT_VERSIONS.update(effective)
    _CATALOG = _build_catalog()
    return dict(_CLIENT_VERSIONS)


async def load_version_overrides_from_db() -> dict[str, str]:
    """启动时从 ``system_setting`` 读取运维保存的版本覆盖并应用。

    读不到 / 反序列化失败时静默保持默认版本，绝不因配置缺失阻塞启动。
    返回应用后的生效版本映射。
    """
    from ..db.base import AsyncSessionLocal
    from ..db.models.system import SystemSetting

    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, CLIENT_IDENTITY_VERSIONS_SETTING_KEY)
            raw = row.value if row is not None else None
    except Exception:  # noqa: BLE001 - 启动期任何 DB 异常都不应阻塞
        return dict(_CLIENT_VERSIONS)

    overrides: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str):
                overrides[key] = value
    return apply_version_overrides(overrides)


def normalize_identity_profile(value: str | None) -> str:
    """把任意输入规范化；Codex 旧 CLI/exec 值统一映射到 TUI。"""
    candidate = (value or "").strip().lower()
    if candidate in {CLIENT_IDENTITY_CODEX_CLI, CLIENT_IDENTITY_CODEX_EXEC}:
        return CLIENT_IDENTITY_CODEX_TUI
    if candidate == CLIENT_IDENTITY_CLAUDE_DESKTOP:
        return CLIENT_IDENTITY_CLAUDE_CODE
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
    *,
    recommended_profile: str | None = None,
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
        recommended = normalize_identity_profile(recommended_profile)
        target = (
            recommended
            if recommended not in {CLIENT_IDENTITY_AUTO, CLIENT_IDENTITY_MINIMAL}
            else default_identity_for_format(fmt)
        )
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


def validate_identity_for_save(profile: str | None, api_format: str | None) -> str | None:
    """保存时校验固定身份是否可用（阶段 F 收口 #2）。

    返回中文错误说明；``None`` 表示校验通过。契约：
    - ``auto`` / ``minimal``：始终允许（兼容所有协议）。
    - 未知档案名：拒绝（避免存进无法解析的值）。
    - 未验证 / 实验档案（如 Claude Desktop）：拒绝——不能保存一个执行时必然回落的
      固定身份，否则保存值、路由摘要与实际请求头会不一致。
    - 与本次协议不兼容（如给 anthropic provider 选 codex_tui）：拒绝。

    这样"保存成功的固定身份"与"执行时真正生效的身份"保持一致，不再静默回落。
    """
    normalized = normalize_identity_profile(profile)
    if normalized in {CLIENT_IDENTITY_AUTO, CLIENT_IDENTITY_MINIMAL}:
        return None
    identity = _CATALOG.get(normalized)
    if identity is None:
        return f"未知的客户端身份档案：{profile}"
    if not identity.verified:
        return (
            f"客户端身份 {normalized} 尚无可复核证据、不可作为固定身份保存"
            "（执行时会回落到自动身份）。请选择 auto 或已验证档案。"
        )
    fmt = (api_format or "").strip().lower()
    if fmt and fmt not in identity.api_formats:
        allowed = "、".join(sorted(identity.api_formats)) or "（无）"
        return (
            f"客户端身份 {normalized} 与协议 {fmt} 不兼容"
            f"（该身份仅适用于：{allowed}）。请改用 auto 或兼容档案。"
        )
    return None


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
        CLIENT_IDENTITY_CODEX_TUI,
        CLIENT_IDENTITY_CODEX_DESKTOP,
        CLIENT_IDENTITY_CLAUDE_CODE,
        CLIENT_IDENTITY_GROK_CLI,
    ):
        identity = _CATALOG[profile]
        item = identity.summary()
        item["selectable"] = identity.verified
        item["api_formats"] = sorted(identity.api_formats)
        items.append(item)
    return items


_REQUEST_PROFILE_META: dict[str, dict[str, Any]] = {
    CLIENT_IDENTITY_OPENAI_SDK: {
        "label": "OpenAI SDK（标准 API）",
        "description": "标准 OpenAI API / Chat Completions 身份；普通兼容站也可改用最小身份。",
        "version_keys": ["openai_sdk"],
    },
    CLIENT_IDENTITY_CODEX_TUI: {
        "label": "Codex TUI",
        "description": "交互式 Codex TUI 的 Responses 身份。",
        "version_keys": ["codex_tui"],
    },
    CLIENT_IDENTITY_CODEX_DESKTOP: {
        "label": "Codex Desktop",
        "description": "Codex Desktop 的 Responses 身份。",
        "version_keys": ["codex_desktop_core", "codex_desktop_build"],
    },
    CLIENT_IDENTITY_CLAUDE_CODE: {
        "label": "Claude Code CLI",
        "description": "Anthropic Messages / Claude Code 兼容代理身份，不模拟 Claude Desktop。",
        "version_keys": ["claude_code", "claude_sdk"],
    },
    CLIENT_IDENTITY_GROK_CLI: {
        "label": "Grok CLI",
        "description": "xAI Responses 兼容身份，只保留已复核且不改变鉴权语义的字段。",
        "version_keys": ["grok_cli"],
    },
}

_IDENTITY_HEADER_DESCRIPTIONS = {
    "user-agent": "声明请求所模拟的客户端及版本；由系统按档案生成。",
    "x-stainless-lang": "OpenAI SDK 生成器使用的语言标识。",
    "x-stainless-package-version": "OpenAI SDK 包版本，与可配置版本号同步。",
    "x-stainless-os": "运行 TelePilot 的操作系统类型，按 Stainless 规范生成。",
    "x-stainless-arch": "运行 TelePilot 的 CPU 架构，按 Stainless 规范生成。",
    "x-stainless-runtime": "运行 TelePilot 的 Python 实现。",
    "x-stainless-runtime-version": "运行 TelePilot 的 Python 版本。",
    "x-stainless-async": "声明使用 asyncio 异步请求路径。",
    "x-stainless-retry-count": "Claude Code 本次模型请求的 SDK 重试计数。",
    "originator": "Codex TUI 或 Desktop 的来源标识。",
    "x-app": "Anthropic 上游用于区分 Claude Code 前台 CLI 的固定标识。",
    "x-grok-client-version": "xAI 上游使用的 Grok CLI 版本标识。",
    "x-grok-client-identifier": "Grok pager 的稳定客户端标识。",
}


def request_configuration_profiles() -> list[dict[str, Any]]:
    """返回请求配置弹窗使用的完整抓包头清单与处理方式。"""

    output: list[dict[str, Any]] = []
    for profile in (
        CLIENT_IDENTITY_OPENAI_SDK,
        CLIENT_IDENTITY_CODEX_TUI,
        CLIENT_IDENTITY_CODEX_DESKTOP,
        CLIENT_IDENTITY_CLAUDE_CODE,
        CLIENT_IDENTITY_GROK_CLI,
    ):
        identity = _CATALOG[profile]
        meta = _REQUEST_PROFILE_META[profile]
        runtime_headers: list[dict[str, Any]] = []
        if profile == CLIENT_IDENTITY_CODEX_TUI:
            runtime_headers = [
                {
                    "name": name,
                    "value": (
                        "按 Provider 隔离的稳定会话伪名"
                        if name in {"session-id", "thread-id"}
                        else "每次 HTTP 请求唯一 UUID"
                    ),
                    "description": (
                        "Codex Responses 会话链路标识；由 MASTER_KEY 做 HMAC，不外发原始业务 ID。"
                        if name in {"session-id", "thread-id"}
                        else "Codex Responses 单次请求标识，与会话 ID 分离。"
                    ),
                    "configurable": False,
                    "management": "runtime",
                }
                for name in ("session-id", "thread-id", "x-client-request-id")
            ]
        elif profile == CLIENT_IDENTITY_CODEX_DESKTOP:
            runtime_headers = [
                {
                    "name": name,
                    "value": (
                        "按 Provider 隔离的稳定会话伪名"
                        if name == "session_id"
                        else "每次 HTTP 请求唯一 UUID"
                    ),
                    "description": (
                        "Codex Desktop 会话标识；由 MASTER_KEY 做 HMAC，不外发原始业务 ID。"
                        if name == "session_id"
                        else "Codex Desktop 单次请求标识，与会话 ID 分离。"
                    ),
                    "configurable": False,
                    "management": "runtime",
                }
                for name in ("session_id", "x-client-request-id")
            ]
        elif profile == CLIENT_IDENTITY_CLAUDE_CODE:
            runtime_headers = [
                {
                    "name": "X-Claude-Code-Session-Id",
                    "value": "按 Provider 隔离的稳定会话伪名",
                    "description": "Claude Code 会话标识；由 MASTER_KEY 做 HMAC，不外发原始业务 ID。",
                    "configurable": False,
                    "management": "runtime",
                }
            ]
        elif profile == CLIENT_IDENTITY_GROK_CLI:
            runtime_headers = [
                {
                    "name": name,
                    "value": value,
                    "description": description,
                    "configurable": False,
                    "management": "runtime",
                }
                for name, value, description in (
                    ("x-grok-conv-id", "稳定会话伪名", "按 Provider 隔离的 Grok 会话标识。"),
                    ("x-grok-session-id", "与 conv-id 一致", "Grok 临时会话标识。"),
                    ("x-grok-req-id", "随机 UUID", "Grok 单次请求标识。"),
                    ("x-grok-turn-idx", "1", "单次 headless 调用的轮次编号。"),
                    ("x-grok-model-override", "当前模型 ID", "Grok 本次请求的模型覆盖值。"),
                )
            ]

        auth_header = "x-api-key" if profile == CLIENT_IDENTITY_CLAUDE_CODE else "Authorization"
        protocol_headers = [
            {
                "name": auth_header,
                "value": "<由 Provider API Key 生成，界面不回显>",
                "description": "请求鉴权头；值来自加密保存的 Provider API Key，不能在客户端档案中覆盖。",
                "configurable": False,
                "management": "protocol",
            },
            {
                "name": "Accept",
                "value": "text/event-stream 或 application/json",
                "description": "按流式或完整响应模式自动选择。",
                "configurable": False,
                "management": "protocol",
            },
            {
                "name": "Content-Type",
                "value": "application/json",
                "description": "JSON 推理请求的协议内容类型。",
                "configurable": False,
                "management": "protocol",
            },
        ]
        if profile == CLIENT_IDENTITY_CLAUDE_CODE:
            protocol_headers.extend(
                [
                    {
                        "name": "anthropic-version",
                        "value": "2023-06-01",
                        "description": "Anthropic Messages 协议版本，由协议客户端固定发送。",
                        "configurable": False,
                        "management": "protocol",
                    },
                    {
                        "name": "anthropic-beta",
                        "value": "按模型能力与兼容模式条件生成",
                        "description": "会改变 Anthropic API 语义，只能由协议能力映射生成，不能作为身份头手填。",
                        "configurable": False,
                        "management": "protocol",
                    },
                ]
            )

        transport_headers = [
            {
                "name": "Host",
                "value": "由 Base URL 解析",
                "description": "HTTP 客户端根据目标地址自动生成。",
                "configurable": False,
                "management": "transport",
            },
            {
                "name": "Content-Length",
                "value": "按请求体字节数生成",
                "description": "HTTP 客户端在发送时自动计算。",
                "configurable": False,
                "management": "transport",
            },
        ]

        excluded_headers: list[dict[str, Any]] = []
        if profile in {CLIENT_IDENTITY_CODEX_TUI, CLIENT_IDENTITY_CODEX_DESKTOP}:
            excluded_headers = [
                {
                    "name": "x-codex-beta-features",
                    "value": "<不复制>",
                    "description": "Codex 内部实验开关，会改变上游能力与接口语义。",
                    "configurable": False,
                    "management": "excluded",
                },
                {
                    "name": "x-codex-window-id",
                    "value": "<不复制>",
                    "description": "Codex 宿主窗口的运行时标识，TelePilot 没有对应窗口语义。",
                    "configurable": False,
                    "management": "excluded",
                },
                {
                    "name": "x-codex-turn-metadata",
                    "value": "<不复制，包含 installation/session/thread/turn/window 等元数据>",
                    "description": "包含设备安装 ID、会话链路和本地运行环境信息，不外发也不允许配置。",
                    "configurable": False,
                    "management": "excluded",
                },
            ]
        elif profile == CLIENT_IDENTITY_GROK_CLI:
            excluded_headers = [
                {
                    "name": name,
                    "value": "<不复制>",
                    "description": description,
                    "configurable": False,
                    "management": "excluded",
                }
                for name, description in (
                    ("x-xai-token-auth", "Grok CLI 的专用 Token 鉴权字段，不能由普通 Provider API Key 模拟。"),
                    ("x-authenticateresponse", "Grok CLI 登录挑战响应，属于账号与设备鉴权语义。"),
                    ("x-grok-agent-id", "Grok Agent Identity 没有 TelePilot 对等语义，不复制。"),
                )
            ]
        output.append(
            {
                "profile": profile,
                "label": meta["label"],
                "description": meta["description"],
                "api_formats": sorted(identity.api_formats),
                "version_keys": list(meta["version_keys"]),
                "source": identity.source,
                "headers": [
                    {
                        "name": name,
                        "value": value,
                        "description": _IDENTITY_HEADER_DESCRIPTIONS.get(
                            name.casefold(), "客户端身份档案固定请求头。"
                        ),
                        "configurable": False,
                        "management": "fixed",
                    }
                    for name, value in identity.headers().items()
                ]
                + runtime_headers
                + protocol_headers
                + transport_headers
                + excluded_headers,
            }
        )
    return output


__all__ = [
    "ALL_CLIENT_IDENTITY_PROFILES",
    "CLIENT_IDENTITY_AUTO",
    "CLIENT_IDENTITY_CLAUDE_CODE",
    "CLIENT_IDENTITY_CLAUDE_DESKTOP",
    "CLIENT_IDENTITY_CODEX_CLI",
    "CLIENT_IDENTITY_CODEX_EXEC",
    "CLIENT_IDENTITY_CODEX_DESKTOP",
    "CLIENT_IDENTITY_CODEX_TUI",
    "CLIENT_IDENTITY_GROK_CLI",
    "CLIENT_IDENTITY_MINIMAL",
    "CLIENT_IDENTITY_OPENAI_SDK",
    "DEFAULT_CLIENT_IDENTITY_PROFILE",
    "IDENTITY_PROBE_ORDER",
    "CLIENT_IDENTITY_VERSIONS_SETTING_KEY",
    "ClientIdentity",
    "apply_version_overrides",
    "current_client_versions",
    "default_client_versions",
    "version_key_metadata",
    "default_identity_for_format",
    "get_identity",
    "is_identity_compatible",
    "is_valid_version",
    "load_version_overrides_from_db",
    "normalize_identity_profile",
    "resolve_identity",
    "request_configuration_profiles",
    "selectable_identities",
    "validate_identity_for_save",
]

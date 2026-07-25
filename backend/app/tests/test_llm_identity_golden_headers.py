"""阶段 F 收口 #7：身份 UA golden fixture + minimal 传输层头断言。

目标：
- 用 golden 快照锁定每个已验证身份 ``headers()`` 的**确切**输出（UA 结构 +
  产品模拟头），任何漂移都会让测试失败，逼迫先更新证据再改档案。
- ``minimal`` / ``auto→minimal`` 绝不携带任何产品模拟 UA/头；并在真实 httpx
  传输层断言：minimal 请求不会带上伪造的产品身份（codex/claude/openai 字样）。
"""

from __future__ import annotations

import platform

import httpx
import pytest

from app.db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
)
from app.services import llm_identity
from app.services.llm_identity import get_identity, resolve_identity


def _os_seg() -> str:
    # 与 llm_identity._os_slug / _os_version / _arch_slug 对齐，动态构造预期 UA。
    system = platform.system()
    os_slug = "Mac OS" if system == "Darwin" else (system or "unknown")
    return f"{os_slug} {platform.release() or 'unknown'}; {platform.machine().lower() or 'unknown'}"


def _stainless_os() -> str:
    return {"Darwin": "MacOS", "Windows": "Windows", "Linux": "Linux"}.get(
        platform.system(), platform.system() or "Unknown"
    )


def _stainless_arch() -> str:
    machine = platform.machine().lower()
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(machine, machine or "unknown")


def _grok_platform() -> str:
    system = platform.system().lower()
    os_slug = "macos" if system == "darwin" else (system or "unknown")
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else (machine or "unknown")
    return f"{os_slug}; {arch}"


def test_golden_openai_sdk_headers() -> None:
    ident = get_identity("openai_sdk")
    assert ident is not None
    assert ident.verified is True
    assert ident.headers() == {
        "User-Agent": "AsyncOpenAI/Python 2.45.0",
        "X-Stainless-Lang": "python",
        "X-Stainless-Package-Version": "2.45.0",
        "X-Stainless-OS": _stainless_os(),
        "X-Stainless-Arch": _stainless_arch(),
        "X-Stainless-Runtime": platform.python_implementation(),
        "X-Stainless-Runtime-Version": platform.python_version(),
        "X-Stainless-Async": "async:asyncio",
    }


def test_golden_codex_cli_headers() -> None:
    ident = get_identity("codex_cli")
    assert ident is not None
    assert ident.headers() == {
        "User-Agent": f"codex_exec/0.145.0 ({_os_seg()}) dumb (codex_exec; 0.145.0)",
        "originator": "codex_exec",
    }


def test_golden_claude_code_headers() -> None:
    ident = get_identity("claude_code")
    assert ident is not None
    assert ident.headers() == {
        "User-Agent": "claude-cli/2.1.220 (external, sdk-cli)",
        "x-app": "cli",
    }


def test_legacy_codex_desktop_uses_codex_cli_headers() -> None:
    ident = get_identity("codex_desktop")
    assert ident is not None
    assert ident.headers() == {
        "User-Agent": f"codex_exec/0.145.0 ({_os_seg()}) dumb (codex_exec; 0.145.0)",
        "originator": "codex_exec",
    }


def test_golden_grok_cli_headers() -> None:
    ident = get_identity("grok_cli")
    assert ident is not None
    assert ident.verified is True
    assert ident.headers() == {
        "User-Agent": f"grok-shell/0.2.112 ({_grok_platform()})",
        "x-grok-client-mode": "headless",
        "x-grok-client-version": "0.2.112",
        "x-grok-client-identifier": "grok-shell",
    }


def test_golden_minimal_emits_no_product_headers() -> None:
    ident = get_identity("minimal")
    assert ident is not None
    assert ident.user_agent is None
    assert ident.headers() == {}


@pytest.mark.parametrize(
    ("api_format", "profile"),
    [
        (LLM_API_FORMAT_CHAT_COMPLETIONS, "minimal"),
        (LLM_API_FORMAT_RESPONSES, "minimal"),
        (LLM_API_FORMAT_ANTHROPIC_MESSAGES, "minimal"),
    ],
)
def test_minimal_resolves_without_product_ua(api_format: str, profile: str) -> None:
    ident = resolve_identity(profile, api_format)
    assert ident.profile == "minimal"
    assert ident.headers() == {}


@pytest.mark.asyncio
async def test_minimal_transport_headers_carry_no_fabricated_product_ua() -> None:
    """传输层断言：minimal 身份构造的请求头不含伪造的产品身份 UA。

    通过真实 httpx MockTransport 捕获最终发出的请求头。minimal 只应带上协议必需头，
    绝不出现 codex/claude/openai 等产品模拟 UA 或 originator/x-app 产品头。
    """
    from app.services.llm_client import _llm_headers

    ident = resolve_identity("minimal", LLM_API_FORMAT_CHAT_COMPLETIONS)
    headers = _llm_headers(identity=ident)

    captured: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as cli:
        await cli.post("https://x.example/v1/chat/completions", headers=headers, json={})

    ua = captured.get("user-agent", "")
    # httpx 会自带默认 UA（诚实地标识为 python-httpx/*），但绝不能出现产品模拟字样。
    for token in ("codex", "claude", "openai/python", "codex desktop"):
        assert token not in ua.lower()
    # minimal 不注入任何产品模拟头。
    assert "originator" not in captured
    assert "x-app" not in captured
    assert "x-stainless-lang" not in captured


def test_default_client_versions_locked() -> None:
    """默认版本号 golden：漂移时必须显式更新证据。"""
    assert llm_identity.default_client_versions() == {
        "codex_cli": "0.145.0",
        "claude_code": "2.1.220",
        "openai_sdk": "2.45.0",
        "grok_cli": "0.2.112",
    }

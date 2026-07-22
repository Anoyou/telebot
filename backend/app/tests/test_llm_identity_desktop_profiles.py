"""阶段 F：Desktop 档案证据门槛契约测试。

约束：客户端身份档案必须只影响协议要求的 headers，不改变请求正文和 Provider 路由语义。
- 只有存在真实捕获 / 上游开源实现 / 可复核证据时，Desktop 档案才能标 verified、可选。
- 无证据的档案必须保持不可选、不参与探测，请求时回落到本次协议的 auto 默认身份，
  且**绝不**发送任何模拟 UA / 身份头。
- 任何 Desktop 档案都不得携带凭证性 / 设备指纹字段
  （authorization / cookie / account / device / session / x-api-key）。

证据现状：
- ``codex_desktop``：已由本机 Surge 抓包核对（0.144.0-alpha.4，app build 26.707.51957），
  UA + originator 落地、verified、可选。
- ``claude_desktop``：仍无可复核证据，保持 unverified、不可选、解析回落。
"""

from __future__ import annotations

from app.db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_RESPONSES,
)
from app.services import llm_identity
from app.services.llm_identity import (
    CLIENT_IDENTITY_CLAUDE_CODE,
    CLIENT_IDENTITY_CLAUDE_DESKTOP,
    CLIENT_IDENTITY_CODEX_CLI,
    CLIENT_IDENTITY_CODEX_DESKTOP,
    CLIENT_IDENTITY_GROK_CLI,
    get_identity,
    resolve_identity,
    selectable_identities,
)

# 全部 Desktop 档案（无论验证与否，都受"不得携带凭证字段"约束）。
_ALL_DESKTOP_PROFILES = (CLIENT_IDENTITY_CODEX_DESKTOP, CLIENT_IDENTITY_CLAUDE_DESKTOP)
# 仍无可复核证据、必须保持未验证的档案。
_UNVERIFIED_DESKTOP_PROFILES = (CLIENT_IDENTITY_CLAUDE_DESKTOP,)


# ── codex_desktop：有真实抓包证据 → verified + 真实 UA ────────
def test_codex_desktop_is_verified_with_real_captured_ua() -> None:
    identity = get_identity(CLIENT_IDENTITY_CODEX_DESKTOP)
    assert identity is not None
    assert identity.verified is True
    # UA 必须是真实抓包结构，且不含伪造的动态运行态字段。
    assert identity.user_agent is not None
    assert identity.user_agent.startswith("Codex Desktop/")
    assert "(Codex Desktop;" in identity.user_agent
    # originator 头是抓包实测的显示名。
    assert identity.extra_headers.get("originator") == "Codex Desktop"
    # 证据来源与采集版本必须存在（防空证据伪造）。
    assert identity.source.strip()
    assert identity.client_version.strip()


def test_codex_desktop_excludes_beta_and_runtime_headers() -> None:
    # 阶段 F：身份切换不得开 beta、不得带运行态 / 设备指纹。
    identity = get_identity(CLIENT_IDENTITY_CODEX_DESKTOP)
    assert identity is not None
    banned_tokens = (
        "beta",
        "x-openai-internal",
        "session-id",
        "thread-id",
        "window-id",
        "turn-metadata",
        "installation",
        "x-client-request-id",
    )
    for key in identity.extra_headers:
        low = key.lower()
        assert not any(tok in low for tok in banned_tokens), key


def test_codex_desktop_resolves_to_itself_on_responses() -> None:
    identity = resolve_identity(CLIENT_IDENTITY_CODEX_DESKTOP, LLM_API_FORMAT_RESPONSES)
    assert identity.profile == CLIENT_IDENTITY_CODEX_DESKTOP
    assert identity.verified is True


def test_codex_desktop_selectable_in_frontend_list() -> None:
    listing = {item["profile"]: item for item in selectable_identities()}
    assert listing[CLIENT_IDENTITY_CODEX_DESKTOP]["selectable"] is True


# ── claude_desktop：仍无证据 → 未验证、不可选、解析回落 ────────
def test_unverified_desktop_profiles_emit_no_headers() -> None:
    for profile in _UNVERIFIED_DESKTOP_PROFILES:
        identity = get_identity(profile)
        assert identity is not None
        assert identity.verified is False, f"{profile} 不得在无证据时标记为已验证"
        assert identity.experimental is True
        assert identity.user_agent is None
        assert identity.headers() == {}


def test_unverified_desktop_not_selectable_in_frontend_list() -> None:
    listing = {item["profile"]: item for item in selectable_identities()}
    for profile in _UNVERIFIED_DESKTOP_PROFILES:
        assert profile in listing
        assert listing[profile]["selectable"] is False


def test_requesting_claude_desktop_falls_back_to_verified_auto_identity() -> None:
    identity = resolve_identity(
        CLIENT_IDENTITY_CLAUDE_DESKTOP, LLM_API_FORMAT_ANTHROPIC_MESSAGES
    )
    assert identity.verified is True
    assert identity.profile == CLIENT_IDENTITY_CLAUDE_CODE
    assert identity.profile not in _UNVERIFIED_DESKTOP_PROFILES


# ── 通用防伪造闸门 ──────────────────────────────────────────
def test_verified_product_profiles_have_evidence_source() -> None:
    for profile in (
        CLIENT_IDENTITY_CODEX_CLI,
        CLIENT_IDENTITY_CLAUDE_CODE,
        CLIENT_IDENTITY_CODEX_DESKTOP,
        CLIENT_IDENTITY_GROK_CLI,
    ):
        identity = get_identity(profile)
        assert identity is not None
        assert identity.verified is True
        assert identity.source.strip()
        assert identity.client_version.strip()


def test_no_desktop_profile_carries_credential_like_headers() -> None:
    banned = ("authorization", "cookie", "account", "device", "session", "x-api-key")
    for profile in _ALL_DESKTOP_PROFILES:
        identity = llm_identity.get_identity(profile)
        assert identity is not None
        for key in identity.extra_headers:
            assert not any(b in key.lower() for b in banned)

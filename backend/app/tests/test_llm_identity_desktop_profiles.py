"""阶段 F：Desktop 档案证据门槛契约测试。

约束（docs/LLM-CLIENT-IDENTITY-ROUTING-PLAN.md 阶段 F）：
- 没有可复核证据前，Codex/Claude Desktop 档案必须保持不可选、不参与探测、
  且**绝不**发送任何模拟 UA / 身份头。
- 请求这些档案时必须回落到本次协议的 auto 默认身份（已验证档案）。
- 这些占位档案不得携带凭证性字段（account id / cookie / device 等）。

若将来补齐真实捕获证据，应先更新 fixture，再把 verified 置 True，
届时本测试需相应调整——这是有意的"防伪造"闸门。
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
    get_identity,
    resolve_identity,
    selectable_identities,
)

_DESKTOP_PROFILES = (CLIENT_IDENTITY_CODEX_DESKTOP, CLIENT_IDENTITY_CLAUDE_DESKTOP)


def test_desktop_profiles_are_unverified_and_experimental() -> None:
    for profile in _DESKTOP_PROFILES:
        identity = get_identity(profile)
        assert identity is not None
        assert identity.verified is False, f"{profile} 不得在无证据时标记为已验证"
        assert identity.experimental is True
        # 未验证档案不得携带任何模拟 UA / 身份头。
        assert identity.user_agent is None
        assert identity.headers() == {}


def test_desktop_profiles_not_selectable_in_frontend_list() -> None:
    listing = {item["profile"]: item for item in selectable_identities()}
    for profile in _DESKTOP_PROFILES:
        assert profile in listing
        assert listing[profile]["selectable"] is False


def test_requesting_codex_desktop_falls_back_to_verified_auto_identity() -> None:
    # 请求未验证的 codex_desktop（responses 协议）→ 回落到已验证的 codex_cli。
    identity = resolve_identity(CLIENT_IDENTITY_CODEX_DESKTOP, LLM_API_FORMAT_RESPONSES)
    assert identity.verified is True
    assert identity.profile == CLIENT_IDENTITY_CODEX_CLI


def test_requesting_claude_desktop_falls_back_to_verified_auto_identity() -> None:
    identity = resolve_identity(
        CLIENT_IDENTITY_CLAUDE_DESKTOP, LLM_API_FORMAT_ANTHROPIC_MESSAGES
    )
    assert identity.verified is True
    assert identity.profile == CLIENT_IDENTITY_CLAUDE_CODE


def test_desktop_profiles_never_emit_headers_via_resolve() -> None:
    # 即使显式请求，解析结果也不会带 Desktop 占位（回落到已验证档案），
    # 且解析出的身份头绝不为空以外的伪造值——这里断言回落身份是已验证的。
    for profile, fmt in (
        (CLIENT_IDENTITY_CODEX_DESKTOP, LLM_API_FORMAT_RESPONSES),
        (CLIENT_IDENTITY_CLAUDE_DESKTOP, LLM_API_FORMAT_ANTHROPIC_MESSAGES),
    ):
        identity = resolve_identity(profile, fmt)
        assert identity.verified is True
        assert identity.profile not in _DESKTOP_PROFILES


def test_verified_product_profiles_have_evidence_source() -> None:
    # 已验证产品档案必须带证据来源与采集版本（防止空证据伪造）。
    for profile in (CLIENT_IDENTITY_CODEX_CLI, CLIENT_IDENTITY_CLAUDE_CODE):
        identity = get_identity(profile)
        assert identity is not None
        assert identity.verified is True
        assert identity.source.strip()
        assert identity.client_version.strip()


def test_no_desktop_profile_carries_credential_like_headers() -> None:
    # 占位档案不得含 account / cookie / device / authorization 等凭证字段。
    banned = ("authorization", "cookie", "account", "device", "session", "x-api-key")
    for profile in _DESKTOP_PROFILES:
        identity = llm_identity.get_identity(profile)
        assert identity is not None
        for key in identity.extra_headers:
            assert not any(b in key.lower() for b in banned)

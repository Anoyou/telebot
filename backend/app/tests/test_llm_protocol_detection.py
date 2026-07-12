"""阶段 B：身份感知协议检测测试。

覆盖：
- 诊断分类：401/403/404/429/5xx/超时/空响应/非 JSON 准确归类。
- 身份顺序探测：标准身份成功即停止，不再尝试其它身份。
- client_rejected 时才继续尝试下一个身份；其它错误不换身份。
- 推荐协议 + 推荐身份 + identity_attempts 结构。
- 脱敏：错误文本不含 api_key / base_url。
"""

from __future__ import annotations

import httpx
import pytest

from app.services import llm_diagnostics as diag

# 捕获真实 AsyncClient 类：测试会 monkeypatch commands.httpx.AsyncClient，
# 工厂内部必须用真实类构造，否则递归。
_REAL_ASYNC_CLIENT = httpx.AsyncClient


# ── 诊断分类 ────────────────────────────────────────────────


def test_classify_401_auth_failed() -> None:
    assert diag.classify_status_code(401, "invalid api key") == diag.DIAG_AUTH_FAILED


def test_classify_403_client_rejected_when_identity_hint() -> None:
    assert (
        diag.classify_status_code(403, "this endpoint requires the Codex CLI client")
        == diag.DIAG_CLIENT_REJECTED
    )


def test_classify_403_plain_permission_is_auth_failed() -> None:
    assert diag.classify_status_code(403, "forbidden") == diag.DIAG_AUTH_FAILED


def test_classify_404_model_missing_vs_protocol() -> None:
    assert (
        diag.classify_status_code(404, "The model gpt-x does not exist")
        == diag.DIAG_MODEL_MISSING
    )
    assert diag.classify_status_code(404, "Not Found") == diag.DIAG_PROTOCOL_REJECTED


def test_classify_429_rate_limited() -> None:
    assert diag.classify_status_code(429, "rate limit exceeded") == diag.DIAG_RATE_LIMITED


def test_classify_5xx_upstream() -> None:
    assert diag.classify_status_code(502, "bad gateway") == diag.DIAG_UPSTREAM_ERROR


def test_classify_timeout_exception() -> None:
    assert diag.classify_exception(httpx.ConnectTimeout("t")) == diag.DIAG_TIMEOUT


def test_classify_network_exception() -> None:
    assert diag.classify_exception(httpx.ConnectError("boom")) == diag.DIAG_NETWORK_ERROR


def test_is_valid_json() -> None:
    assert diag.is_valid_json('{"a":1}') is True
    assert diag.is_valid_json("<html>not json</html>") is False


def test_redact_strips_secrets() -> None:
    out = diag.redact(
        "error with key sk-abcd1234efgh and url https://api.x.ai/v1 Bearer tok123",
        api_key="sk-abcd1234efgh",
        base_url="https://api.x.ai/v1",
    )
    assert "sk-abcd1234efgh" not in out
    assert "https://api.x.ai/v1" not in out
    assert "tok123" not in out


# ── 探测辅助（_probe_result 分类与脱敏）──────────────────────


def test_probe_result_classifies_and_redacts() -> None:
    from app.api.commands import _probe_result

    resp = httpx.Response(
        401, text="invalid key sk-secret999 for https://base.example/v1", request=httpx.Request("POST", "https://base.example/v1/chat/completions")
    )
    result = _probe_result(
        resp, 12, api_key="sk-secret999", base_url="https://base.example/v1", stage="protocol"
    )
    assert result.ok is False
    assert result.error_category == diag.DIAG_AUTH_FAILED
    assert result.stage == "protocol"
    assert result.suggestion
    assert "sk-secret999" not in (result.error or "")


def test_probe_result_empty_success_marked() -> None:
    from app.api.commands import _probe_result

    resp = httpx.Response(200, text="", request=httpx.Request("POST", "https://x/v1"))
    result = _probe_result(resp, 5, api_key="", base_url=None, stage="protocol")
    assert result.ok is True
    assert result.error_category == diag.DIAG_EMPTY_RESPONSE


# ── 端到端：身份顺序探测 ────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_stops_after_standard_identity_success(monkeypatch) -> None:
    """Chat Completions 标准身份（openai_sdk）成功后，不再尝试 minimal。"""
    from app.api import commands
    from app.schemas.command import DetectProviderProtocolsRequest

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fmt = "chat" if "chat/completions" in str(request.url) else (
            "responses" if str(request.url).endswith("/responses") else (
                "anthropic" if str(request.url).endswith("/messages") else "models"
            )
        )
        calls.append({"fmt": fmt, "ua": request.headers.get("User-Agent", "")})
        if fmt == "models":
            return httpx.Response(200, json={"data": []})
        if fmt == "chat":
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        # responses / anthropic 端点在该 provider 上不可用
        return httpx.Response(404, text="Not Found")

    def _fake_client(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(commands.httpx, "AsyncClient", _fake_client)

    # DB / audit 依赖：用最小 stub。
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(commands, "_require_ai_enabled", _noop)
    monkeypatch.setattr(commands.audit, "write", _noop)

    class _DB:
        async def commit(self):
            return None

    class _User:
        id = 1

    payload = DetectProviderProtocolsRequest(
        provider="openai",
        base_url="https://api.example/v1",
        api_key="sk-test",
        model="gpt-x",
    )
    resp = await commands.detect_provider_protocols(payload, _DB(), _User())

    # chat 成功 → 只用 openai_sdk 探一次，不再探 minimal。
    chat_attempts = [a for a in resp.identity_attempts if a.api_format == "chat_completions"]
    assert len(chat_attempts) == 1
    assert chat_attempts[0].client_identity_profile == "openai_sdk"
    assert resp.recommended_api_format == "chat_completions"
    assert resp.recommended_client_identity_profile == "openai_sdk"
    # UA 不含 TelePilot。
    assert all("TelePilot" not in c["ua"] for c in calls)


@pytest.mark.asyncio
async def test_detect_retries_identity_only_on_client_rejection(monkeypatch) -> None:
    """Responses 上游用 openai/minimal 均 client_rejected 时，会按顺序尝试 codex_cli。"""
    from app.api import commands
    from app.schemas.command import DetectProviderProtocolsRequest

    responses_uas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/responses"):
            responses_uas.append(request.headers.get("User-Agent", ""))
            ua = request.headers.get("User-Agent", "")
            if ua.startswith("codex_cli_rs/"):
                return httpx.Response(200, json={"output_text": "ok"})
            return httpx.Response(403, text="this API requires the Codex CLI client")
        if url.endswith("/chat/completions"):
            return httpx.Response(404, text="Not Found")
        if url.endswith("/messages"):
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, json={"data": []})

    def _fake_client(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(commands.httpx, "AsyncClient", _fake_client)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(commands, "_require_ai_enabled", _noop)
    monkeypatch.setattr(commands.audit, "write", _noop)

    class _DB:
        async def commit(self):
            return None

    class _User:
        id = 1

    payload = DetectProviderProtocolsRequest(
        provider="openai",
        base_url="https://api.example/v1",
        api_key="sk-test",
        model="gpt-x",
    )
    resp = await commands.detect_provider_protocols(payload, _DB(), _User())

    responses_attempts = [
        a for a in resp.identity_attempts if a.api_format == "responses"
    ]
    # codex_cli 身份成功 → 推荐 responses + codex_cli。
    assert resp.responses.ok is True
    assert resp.recommended_client_identity_profile == "codex_cli"
    profiles = [a.client_identity_profile for a in responses_attempts]
    assert "codex_cli" in profiles

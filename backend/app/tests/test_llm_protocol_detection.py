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


def test_client_rejected_suggestion_points_to_gateway_without_claiming_oauth_support() -> None:
    suggestion = diag.suggestion_for(diag.DIAG_CLIENT_REJECTED)
    assert "内置 Gateway" in suggestion
    assert "OAuth" not in suggestion


def test_classify_403_plain_permission_is_permission_denied() -> None:
    assert diag.classify_status_code(403, "forbidden") == diag.DIAG_PERMISSION_DENIED


def test_classify_404_model_missing_vs_protocol() -> None:
    assert (
        diag.classify_status_code(404, "The model gpt-x does not exist")
        == diag.DIAG_MODEL_MISSING
    )
    assert diag.classify_status_code(404, "Not Found") == diag.DIAG_ENDPOINT_MISSING


def test_classify_429_rate_limited() -> None:
    assert diag.classify_status_code(429, "rate limit exceeded") == diag.DIAG_RATE_LIMITED


def test_structured_error_code_has_priority_over_http_status() -> None:
    assert (
        diag.classify_status_code(429, '{"error":{"code":"insufficient_quota","message":"limit"}}')
        == diag.DIAG_QUOTA_EXHAUSTED
    )


def test_official_account_requirement_has_stable_fact() -> None:
    fact = diag.diagnose_http_error(
        403,
        '{"error":{"code":"official_account_required","message":"ChatGPT account required"}}',
        request_id="req-1",
        gateway_stage="upstream",
    )
    assert fact.category == diag.DIAG_OFFICIAL_ACCOUNT_REQUIRED
    assert fact.request_id == "req-1"
    assert fact.gateway_stage == "upstream"
    assert fact.retryable is False


def test_gateway_and_timeout_categories() -> None:
    assert diag.classify_status_code(503, '{"error":{"code":"gateway_overloaded"}}') == diag.DIAG_GATEWAY_OVERLOADED
    assert diag.classify_status_code(504, "gateway timeout") == diag.DIAG_TIMEOUT


def test_wrapped_upstream_400_is_request_invalid_and_not_retryable() -> None:
    fact = diag.diagnose_http_error(400, "Error from provider: upstream request failed")
    assert fact.category == diag.DIAG_REQUEST_INVALID
    assert fact.scope == "request_invalid"
    assert fact.retryable is False


def test_real_upstream_400_facts_override_wrapper_and_keep_trace_layers() -> None:
    fact = diag.diagnose_http_error(
        502,
        {
            "error": {
                "message": "Upstream request failed",
                "type": "upstream_error",
            },
            "upstream_errors": [
                {
                    "upstream_status_code": 400,
                    "message": "Unsupported parameter: max_output_tokens",
                    "detail": {"detail": "Unsupported parameter: max_output_tokens"},
                    "request_id": "80a1f4a9-0e88-4a6e-bd97-310a1fb144a7",
                    "client_request_id": "53a17c9a-d53a-4df5-8509-13dc7ad36231",
                }
            ],
        },
        request_id="5f7a7c52-0757-4627-945e-2935595fb921",
    )

    assert fact.category == diag.DIAG_REQUEST_INVALID
    assert fact.scope == "request_invalid"
    assert fact.retryable is False
    assert fact.status_code == 502
    assert fact.upstream_status_code == 400
    assert fact.upstream_error_message == "Unsupported parameter: max_output_tokens"
    assert fact.upstream_error_detail == '{"detail":"Unsupported parameter: max_output_tokens"}'
    assert fact.upstream_request_id == "80a1f4a9-0e88-4a6e-bd97-310a1fb144a7"
    assert fact.client_request_id == "53a17c9a-d53a-4df5-8509-13dc7ad36231"
    assert fact.request_id == "5f7a7c52-0757-4627-945e-2935595fb921"
    rendered = diag.format_diagnostic_error(fact)
    assert rendered == "上游 HTTP 400：Unsupported parameter: max_output_tokens"
    assert "临时" not in rendered
    assert "5xx" not in rendered


def test_responses_failed_event_reads_nested_real_upstream_facts() -> None:
    fact = diag.diagnose_http_error(
        400,
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {"message": "Upstream request failed", "type": "upstream_error"},
                "upstream_status_code": 400,
                "upstream_error_detail": {
                    "detail": "Unsupported parameter: max_output_tokens"
                },
                "upstream_request_id": "sub2api-request",
                "client_request_id": "sub2api-client-request",
            },
        },
    )

    assert fact.category == diag.DIAG_REQUEST_INVALID
    assert fact.retryable is False
    assert fact.upstream_status_code == 400
    assert "max_output_tokens" in (fact.upstream_error_detail or "")
    assert fact.upstream_request_id == "sub2api-request"
    assert fact.client_request_id == "sub2api-client-request"


def test_classify_5xx_upstream() -> None:
    assert diag.classify_status_code(502, "bad gateway") == diag.DIAG_UPSTREAM_ERROR


def test_wrapper_text_cannot_invent_retryable_5xx() -> None:
    assert (
        diag.classify_message("Sub2API wrapper says temporary upstream 503")
        == diag.DIAG_INVALID_RESPONSE
    )


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


def test_structured_upstream_details_are_redacted() -> None:
    fact = diag.diagnose_http_error(
        400,
        {
            "error": {
                "upstream_status_code": 400,
                "upstream_error_message": "bad sk-secret123456",
                "upstream_error_detail": {"authorization": "Bearer token123"},
            }
        },
    )
    exposed = f"{fact.upstream_error_message} {fact.upstream_error_detail}"
    assert "sk-secret123456" not in exposed
    assert "token123" not in exposed


def test_structured_upstream_details_redact_json_keys_urls_and_unsafe_request_ids() -> None:
    fact = diag.diagnose_http_error(
        502,
        {
            "error": {
                "upstream_status_code": 400,
                "upstream_error_message": "invalid request",
                "upstream_error_detail": {
                    "apiKey": "opaque-secret-value",
                    "authorization": "Basic dXNlcjpwYXNz",
                    "endpoint": "https://tenant.example/private/v1",
                    "proxy": "socks5://user:pass@proxy.example:1080",
                },
                "upstream_request_id": "valid-upstream-id",
                "client_request_id": "invalid id with spaces",
            }
        },
        request_id="bad\nrequest-id",
    )

    exposed = f"{fact.upstream_error_message} {fact.upstream_error_detail}"
    for secret in (
        "opaque-secret-value",
        "dXNlcjpwYXNz",
        "tenant.example",
        "proxy.example",
        "user:pass",
    ):
        assert secret not in exposed
    assert fact.upstream_request_id == "valid-upstream-id"
    assert fact.client_request_id is None
    assert fact.request_id is None


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


def test_probe_result_empty_2xx_is_not_ok() -> None:
    """阶段 F 收口 #3：2xx 但空响应体不再判成功。"""
    from app.api.commands import _probe_result

    resp = httpx.Response(200, text="", request=httpx.Request("POST", "https://x/v1"))
    result = _probe_result(resp, 5, api_key="", base_url=None, stage="protocol")
    assert result.ok is False
    assert result.error_category == diag.DIAG_EMPTY_RESPONSE


def test_probe_result_2xx_non_json_is_not_ok() -> None:
    """2xx 但响应体非 JSON（HTML 登录页等）判为协议不可用。"""
    from app.api.commands import _probe_result

    resp = httpx.Response(
        200,
        text="<html><body>login</body></html>",
        request=httpx.Request("POST", "https://x/v1"),
    )
    result = _probe_result(
        resp, 5, api_key="", base_url=None, stage="protocol", api_format="chat_completions"
    )
    assert result.ok is False
    assert result.error_category == diag.DIAG_PROTOCOL_REJECTED


def test_probe_result_2xx_irrelevant_json_is_not_ok() -> None:
    """2xx 但 JSON 结构不符该协议（无关 JSON）判为协议不可用。"""
    from app.api.commands import _probe_result

    resp = httpx.Response(
        200, json={"ok": True}, request=httpx.Request("POST", "https://x/v1")
    )
    result = _probe_result(
        resp, 5, api_key="", base_url=None, stage="protocol", api_format="chat_completions"
    )
    assert result.ok is False
    assert result.error_category == diag.DIAG_PROTOCOL_REJECTED


def test_probe_result_2xx_valid_chat_structure_is_ok() -> None:
    """2xx + 合法 chat_completions 结构（含 refusal / tool call 亦可）判成功。"""
    from app.api.commands import _probe_result

    resp = httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]},
        request=httpx.Request("POST", "https://x/v1"),
    )
    result = _probe_result(
        resp, 5, api_key="", base_url=None, stage="protocol", api_format="chat_completions"
    )
    assert result.ok is True


def test_probe_result_2xx_valid_anthropic_and_responses_structures() -> None:
    """responses / anthropic_messages 的最小有效结构分别判成功。"""
    from app.api.commands import _probe_result

    resp_r = httpx.Response(
        200, json={"output_text": "hi"}, request=httpx.Request("POST", "https://x/v1")
    )
    assert _probe_result(resp_r, 5, api_key="", base_url=None, api_format="responses").ok is True

    resp_a = httpx.Response(
        200,
        json={"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        request=httpx.Request("POST", "https://x/v1"),
    )
    assert _probe_result(resp_a, 5, api_key="", base_url=None, api_format="anthropic_messages").ok is True


# ── 端到端：身份顺序探测 ────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_stops_after_standard_identity_success(monkeypatch) -> None:
    """Chat Completions 标准身份（openai_sdk）成功后，不再尝试 minimal。"""
    from app.api import commands
    from app.schemas.command import DetectProviderProtocolsRequest

    calls: list[dict] = []
    usage_calls: list[dict] = []

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

    async def _capture_usage(**kwargs):
        usage_calls.append(kwargs)

    monkeypatch.setattr(commands, "_emit_llm_diagnostic_usage", _capture_usage)

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
    assert len(usage_calls) == len(calls) == 4
    assert all(call["source"] == "diagnostic:protocol_detection" for call in usage_calls)
    assert sum(call["error"] is None for call in usage_calls) == 2
    assert sum(call["error"] is not None for call in usage_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
    ],
)
async def test_detect_prefers_official_deepseek_v4_responses(monkeypatch, model: str) -> None:
    from app.api import commands
    from app.schemas.command import DetectProviderProtocolsRequest

    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": model}]})
        if request.url.path == "/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "chat"}}]})
        if request.url.path == "/responses":
            return httpx.Response(
                200,
                json={
                    "object": "response",
                    "status": "completed",
                    "output_text": "responses",
                },
            )
        return httpx.Response(404, text="Not Found")

    def _fake_client(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(commands.httpx, "AsyncClient", _fake_client)

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(commands, "_require_ai_enabled", _noop)
    monkeypatch.setattr(commands.audit, "write", _noop)
    monkeypatch.setattr(commands, "_emit_llm_diagnostic_usage", _noop)

    class _DB:
        async def commit(self):
            return None

    class _User:
        id = 1

    result = await commands.detect_provider_protocols(
        DetectProviderProtocolsRequest(
            provider="openai",
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            model=model,
        ),
        _DB(),
        _User(),
    )

    assert result.chat_completions.ok is True
    assert result.responses.ok is True
    assert result.recommended_api_format == "responses"
    assert result.recommended_web_search_api_format == "responses"
    assert result.note is not None and model in result.note
    assert "/responses" in requested_paths
    assert "/v1/responses" not in requested_paths


@pytest.mark.asyncio
async def test_detect_retries_identity_only_on_client_rejection(monkeypatch) -> None:
    """Responses 上游用 openai/minimal 均 client_rejected 时，会按顺序尝试 codex_tui。"""
    from app.api import commands
    from app.schemas.command import DetectProviderProtocolsRequest

    responses_uas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/responses"):
            responses_uas.append(request.headers.get("User-Agent", ""))
            ua = request.headers.get("User-Agent", "")
            if ua.startswith("codex-tui/"):
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
    # codex_tui 身份成功 → 推荐 responses + codex_tui。
    assert resp.responses.ok is True
    assert resp.recommended_client_identity_profile == "codex_tui"
    profiles = [a.client_identity_profile for a in responses_attempts]
    assert "codex_tui" in profiles


@pytest.mark.asyncio
async def test_anthropic_models_probe_uses_x_api_key(monkeypatch) -> None:
    from app.api import commands
    from app.schemas.command import DetectProviderProtocolsRequest

    model_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            model_headers.update(request.headers)
        return httpx.Response(404, text="Not Found")

    def _fake_client(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(commands.httpx, "AsyncClient", _fake_client)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(commands, "_require_ai_enabled", _noop)
    monkeypatch.setattr(commands.audit, "write", _noop)
    monkeypatch.setattr(commands, "_emit_llm_diagnostic_usage", _noop)

    class _DB:
        async def commit(self):
            return None

    class _User:
        id = 1

    await commands.detect_provider_protocols(
        DetectProviderProtocolsRequest(
            provider="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-ant-test",
            model="claude-haiku-4-5",
        ),
        _DB(),
        _User(),
    )
    assert model_headers.get("x-api-key") == "sk-ant-test"
    assert "authorization" not in model_headers
    assert model_headers.get("anthropic-version") == "2023-06-01"


# ── 阶段 F 收口 #3：2xx 响应结构校验 ──────────────────────────
def _resp(status: int, payload_text: str) -> httpx.Response:
    return httpx.Response(
        status, text=payload_text, request=httpx.Request("POST", "https://x/v1")
    )


def test_probe_result_chat_valid_text() -> None:
    from app.api.commands import _probe_result

    body = '{"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}'
    r = _probe_result(_resp(200, body), 5, api_key="", stage="protocol", api_format="chat_completions")
    assert r.ok is True


def test_probe_result_chat_valid_toolcall() -> None:
    from app.api.commands import _probe_result

    body = '{"choices": [{"message": {"tool_calls": [{"id": "t1"}]}, "finish_reason": "tool_calls"}]}'
    r = _probe_result(_resp(200, body), 5, api_key="", stage="protocol", api_format="chat_completions")
    assert r.ok is True


def test_probe_result_responses_valid() -> None:
    from app.api.commands import _probe_result

    body = '{"object": "response", "output": [{"type": "message"}], "status": "completed"}'
    r = _probe_result(_resp(200, body), 5, api_key="", stage="protocol", api_format="responses")
    assert r.ok is True


def test_probe_result_anthropic_valid() -> None:
    from app.api.commands import _probe_result

    body = '{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}'
    r = _probe_result(_resp(200, body), 5, api_key="", stage="protocol", api_format="anthropic_messages")
    assert r.ok is True


def test_probe_result_html_login_page_rejected() -> None:
    from app.api.commands import _probe_result

    r = _probe_result(_resp(200, "<html><body>login</body></html>"), 5, api_key="", stage="protocol", api_format="chat_completions")
    assert r.ok is False
    assert r.error_category == diag.DIAG_PROTOCOL_REJECTED


def test_probe_result_irrelevant_json_rejected() -> None:
    from app.api.commands import _probe_result

    # 无关 JSON（如健康检查 / 模型列表）不得被判为协议可用。
    r = _probe_result(_resp(200, '{"ok": true, "data": []}'), 5, api_key="", stage="protocol", api_format="chat_completions")
    assert r.ok is False


def test_probe_result_wrong_protocol_shape_rejected() -> None:
    from app.api.commands import _probe_result

    # chat 结构发给 anthropic 协议校验 → 不符 → 拒绝。
    body = '{"choices": [{"message": {"content": "hi"}}]}'
    r = _probe_result(_resp(200, body), 5, api_key="", stage="protocol", api_format="anthropic_messages")
    assert r.ok is False

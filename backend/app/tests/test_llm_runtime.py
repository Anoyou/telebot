"""LLM Runtime 单元测试（Fallback、Retry、Usage Record）。

覆盖：
- Fallback chain 构造
- Retry 策略（可重试/不可重试错误）
- Usage record 记录
- 长消息分段
- 隐私日志脱敏
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm_client import LLMCallFailed, LLMError, LLMErrorScope, LLMResult
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_invoke import _api_format_for_call
from app.services.llm_runtime import (
    FallbackChain,
    UsageRecord,
    build_fallback_chain,
    preview_text_for_usage,
    request_preview_for_usage,
)
from app.worker.command import (
    _ensure_html_safe,
    _safe_exception_text,
    _safe_log_text,
    _split_long_message,
)


class _BudgetRedis:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.strings: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.zsets: dict[str, dict[str, int]] = {}

    async def ping(self) -> bool:
        return True

    async def eval(self, script, numkeys, *values):
        keys = list(values[:numkeys])
        args = list(values[numkeys:])
        async with self.lock:
            if "ZREMRANGEBYSCORE" in script:
                return self._acquire(keys, args)
            return self._settle(keys, args)

    def _acquire(self, keys, args):
        minute_key, daily_requests_key, daily_tokens_key, daily_premium_key, reservation_key = keys
        per_minute = int(args[0])
        daily_requests = int(args[1])
        daily_tokens = int(args[2])
        premium_daily = int(args[3])
        estimate = int(args[4])
        reserve_premium = int(args[5])
        now_ms = int(args[6])
        window_ms = int(args[7])
        reservation_id = str(args[10])

        zset = self.zsets.setdefault(minute_key, {})
        for member, score in list(zset.items()):
            if score <= now_ms - window_ms:
                zset.pop(member, None)

        minute_used = len(zset)
        request_used = self.strings.get(daily_requests_key, 0)
        token_used = self.strings.get(daily_tokens_key, 0)
        premium_used = self.strings.get(daily_premium_key, 0)

        if per_minute > 0 and minute_used + 1 > per_minute:
            return [0, "per_minute", minute_used, per_minute]
        if daily_requests > 0 and request_used + 1 > daily_requests:
            return [0, "daily_requests", request_used, daily_requests]
        if daily_tokens > 0 and token_used + estimate > daily_tokens:
            return [0, "daily_tokens", token_used, daily_tokens]
        if premium_daily > 0 and premium_used + reserve_premium > premium_daily:
            return [0, "premium_daily", premium_used, premium_daily]

        if per_minute > 0:
            zset[reservation_id] = now_ms
        self.hashes[reservation_key] = {
            "minute_active": 1 if per_minute > 0 else 0,
            "daily_request_units": 1 if daily_requests > 0 else 0,
            "token_limited": 1 if daily_tokens > 0 else 0,
            "token_units": estimate if daily_tokens > 0 else 0,
            "premium_limited": 1 if premium_daily > 0 else 0,
            "premium_units": reserve_premium if premium_daily > 0 else 0,
        }
        if daily_requests > 0:
            self.strings[daily_requests_key] = request_used + 1
        if daily_tokens > 0:
            self.strings[daily_tokens_key] = token_used + estimate
        if premium_daily > 0 and reserve_premium > 0:
            self.strings[daily_premium_key] = premium_used + reserve_premium
        return [1, "ok", minute_used + 1, token_used + estimate]

    def _settle(self, keys, args):
        (
            minute_key,
            daily_requests_key,
            daily_tokens_key,
            daily_premium_key,
            reservation_key,
            actual_premium_key,
        ) = keys
        actual_tokens = int(args[0])
        actual_premium = int(args[1])
        keep_request = int(args[2])
        reservation_id = str(args[3])
        reservation = self.hashes.pop(reservation_key, None)
        if reservation is None:
            return [0, "missing"]

        if keep_request <= 0:
            if reservation["minute_active"]:
                self.zsets.setdefault(minute_key, {}).pop(reservation_id, None)
            if reservation["daily_request_units"]:
                self.strings[daily_requests_key] = max(
                    0,
                    self.strings.get(daily_requests_key, 0) - reservation["daily_request_units"],
                )

        if reservation["token_limited"]:
            self.strings[daily_tokens_key] = max(
                0,
                self.strings.get(daily_tokens_key, 0) + actual_tokens - reservation["token_units"],
            )
        if reservation["premium_limited"]:
            if actual_premium_key == daily_premium_key:
                self.strings[daily_premium_key] = max(
                    0,
                    self.strings.get(daily_premium_key, 0) + actual_premium - reservation["premium_units"],
                )
            else:
                if reservation["premium_units"]:
                    self.strings[daily_premium_key] = max(
                        0,
                        self.strings.get(daily_premium_key, 0) - reservation["premium_units"],
                    )
                if actual_premium:
                    self.strings[actual_premium_key] = (
                        self.strings.get(actual_premium_key, 0) + actual_premium
                    )
        return [1, "ok"]


class _BrokenBudgetRedis:
    async def ping(self) -> bool:
        raise RuntimeError("redis down")


# ════════════════════════════════════════════════════════════
# 1) LLMProviderDTO 测试
# ════════════════════════════════════════════════════════════


def test_dto_from_dict() -> None:
    """从 dict 构造 LLMProviderDTO。"""
    d = {
        "id": 42,
        "name": "test-provider",
        "provider": "openai",
        "api_format": "chat_completions",
        "base_url": "https://api.example.com/v1",
        "default_model": "gpt-4o",
        "api_key_enc": "encrypted-key",
        "proxy_url": "socks5://127.0.0.1:1080",
        "modality": "text",
        "tags": ["chat", "code"],
        "cost_tier": 2,
    }
    dto = LLMProviderDTO.from_dict(d)

    assert dto.id == 42
    assert dto.name == "test-provider"
    assert dto.provider == "openai"
    assert dto.api_format == "chat_completions"
    assert dto.base_url == "https://api.example.com/v1"
    assert dto.default_model == "gpt-4o"
    assert dto.api_key_enc == "encrypted-key"
    assert dto.proxy_url == "socks5://127.0.0.1:1080"
    assert dto.modality == "text"
    assert dto.tags == ["chat", "code"]
    assert dto.cost_tier == 2


def test_native_image_openai_chat_provider_uses_responses_for_text_model() -> None:
    """原生生图绑定普通 OpenAI 主模型时，应走 Responses image_generation 工具。"""
    dto = LLMProviderDTO(
        id=1,
        name="ChatGPT",
        provider="openai",
        default_model="gpt-5.5",
        api_format="chat_completions",
    )

    assert _api_format_for_call(dto, web_search=False, native_image=True) == "responses"


def test_native_image_openai_chat_provider_keeps_images_api_for_image_model() -> None:
    """gpt-image / dall-e 模型仍走 /images/generations。"""
    dto = LLMProviderDTO(
        id=1,
        name="Images",
        provider="openai",
        default_model="gpt-image-2",
        api_format="chat_completions",
    )

    assert _api_format_for_call(dto, web_search=False, native_image=True) is None


def test_dto_from_dict_missing_fields() -> None:
    """dict 缺少字段时有默认值。"""
    d = {"id": 1, "name": "test", "provider": "openai"}
    dto = LLMProviderDTO.from_dict(d)

    assert dto.id == 1
    assert dto.name == "test"
    assert dto.api_format is None
    assert dto.base_url is None
    assert dto.default_model == ""
    assert dto.modality == "text"
    assert dto.tags == []
    assert dto.cost_tier == 2


@pytest.mark.asyncio
async def test_stream_sse_rejects_oversized_frame_without_newline() -> None:
    from app.services import llm_client

    class _Response:
        async def aiter_bytes(self):
            yield b"data: " + (b"x" * 64)

    with pytest.raises(LLMError, match="单个 SSE 事件"):
        async for _line in llm_client._iter_limited_sse_lines(
            _Response(),
            line_limit_bytes=32,
            total_limit_bytes=128,
        ):
            pass


def test_dto_has_api_key() -> None:
    """has_api_key 属性正确判断。"""
    # ollama 不需要 key
    dto_ollama = LLMProviderDTO(id=1, name="ollama", provider="ollama", api_key_enc=None)
    assert dto_ollama.has_api_key is True
    assert dto_ollama.is_ollama is True

    # 有 key
    dto_with_key = LLMProviderDTO(id=2, name="openai", provider="openai", api_key_enc="sk-xxx")
    assert dto_with_key.has_api_key is True

    # 没 key
    dto_no_key = LLMProviderDTO(id=3, name="openai", provider="openai", api_key_enc=None)
    assert dto_no_key.has_api_key is False


# ════════════════════════════════════════════════════════════
# 2) FallbackChain 测试
# ════════════════════════════════════════════════════════════


def test_fallback_chain_primary_first() -> None:
    """FallbackChain.all_providers 顺序正确：primary 在前。"""
    p1 = LLMProviderDTO(id=1, name="primary", provider="openai")
    p2 = LLMProviderDTO(id=2, name="fallback1", provider="openai")
    p3 = LLMProviderDTO(id=3, name="fallback2", provider="openai")

    chain = FallbackChain(primary=p1, fallbacks=[p2, p3])
    providers = chain.all_providers

    assert providers[0].name == "primary"
    assert providers[1].name == "fallback1"
    assert providers[2].name == "fallback2"


def test_build_fallback_chain() -> None:
    """build_fallback_chain 正确构造 chain。"""
    providers = {
        1: LLMProviderDTO(id=1, name="primary", provider="openai", tags=["chat"], cost_tier=2),
        2: LLMProviderDTO(
            id=2, name="fallback", provider="openai", tags=["chat"], cost_tier=1, api_key_enc="key"
        ),
        3: LLMProviderDTO(
            id=3, name="same-tag-cheaper", provider="openai", tags=["chat"], cost_tier=1, api_key_enc="key"
        ),
    }
    primary = providers[1]

    chain = build_fallback_chain(
        primary=primary,
        providers=providers,
        fallback_provider_id=2,
        matched_tag="chat",
    )

    assert chain.primary.id == 1
    # fallback_provider_id 应该被加入
    assert len(chain.fallbacks) >= 1


def test_build_fallback_chain_skips_same_id() -> None:
    """build_fallback_chain 跳过与 primary 相同 id 的 provider。"""
    providers = {
        1: LLMProviderDTO(id=1, name="primary", provider="openai"),
    }
    primary = providers[1]

    chain = build_fallback_chain(primary=primary, providers=providers)
    # 应该没有 fallback（因为没有其他 provider）
    assert len(chain.fallbacks) == 0


# ════════════════════════════════════════════════════════════
# 3) Retry 策略测试
# ════════════════════════════════════════════════════════════


def test_llm_error_retryable_flag() -> None:
    """LLMError.retryable 标志正确设置。"""
    # 网络错误
    err_network = LLMError("连接超时", retryable=True)
    assert err_network.retryable is True

    # 认证错误不可重试
    err_auth = LLMError("401 Unauthorized", retryable=False)
    assert err_auth.retryable is False


def test_llm_call_failed_error_type() -> None:
    """LLMCallFailed 正确记录错误类型。"""
    err = LLMCallFailed(
        "429 Rate Limited",
        provider_id=1,
        provider_name="test",
        error_type="rate_limit",
        status_code=429,
        retryable=True,
    )
    assert err.error_type == "rate_limit"
    assert err.status_code == 429
    assert err.retryable is True


# ════════════════════════════════════════════════════════════
# 4) Usage Record 测试
# ════════════════════════════════════════════════════════════


def test_usage_record_fields() -> None:
    """UsageRecord 包含所有必要字段。"""
    record = UsageRecord(
        provider_id=42,
        triggered_by_account_id=123,
        provider_name="test-provider",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        latency_ms=1500,
        success=True,
        used_fallback=False,
        fallback_chain=["test-provider"],
    )
    assert record.provider_id == 42
    assert record.triggered_by_account_id == 123
    assert record.provider_name == "test-provider"
    assert record.model == "gpt-4o"
    assert record.input_tokens == 100
    assert record.output_tokens == 50
    assert record.success is True
    assert record.used_fallback is False


def test_usage_preview_redacts_and_limits_text() -> None:
    """Usage 预览只保存脱敏后的截断文本。"""
    request_preview = request_preview_for_usage(
        "system token=abc12345",
        "user " + ("hello " * 500),
    )
    response_preview = preview_text_for_usage("response sk-testsecret1234567890")

    assert request_preview is not None
    assert "token=***" in request_preview
    assert "abc12345" not in request_preview
    assert len(request_preview) <= 2014
    assert response_preview == "response ***"


@pytest.mark.asyncio
async def test_llm_usage_persist_writes_triggered_by_account_id(monkeypatch) -> None:
    """UsageRecord 持久化时写入 triggered_by_account_id。"""
    from app.services import llm_usage_service

    record = UsageRecord(
        account_id=7,
        triggered_by_account_id=123,
        provider_id=42,
        provider_name="test-provider",
        model="gpt-4o",
        input_tokens=10,
        output_tokens=5,
        success=True,
        fallback_chain=["test-provider"],
        request_preview="system: hello",
        response_preview="world",
    )
    captured_rows = []

    class _FakeDB:
        def add(self, row) -> None:
            captured_rows.append(row)

        async def commit(self) -> None:
            return None

    class _FakeSession:
        def __init__(self) -> None:
            self._db = _FakeDB()

        async def __aenter__(self):
            return self._db

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(llm_usage_service, "AsyncSessionLocal", _FakeSession)
    await llm_usage_service._persist_usage(record)

    assert len(captured_rows) == 1
    assert captured_rows[0].triggered_by_account_id == 123
    assert captured_rows[0].request_preview == "system: hello"
    assert captured_rows[0].response_preview == "world"


# ════════════════════════════════════════════════════════════
# 5) 账号级 LLM 预算 reservation 测试
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_llm_budget_reservation_rejects_concurrent_over_minute_limit(monkeypatch) -> None:
    """并发请求同一账号时，Redis reservation 原子拒绝超出每分钟请求上限的请求。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    provider = LLMProviderDTO(id=1, name="primary", provider="openai", cost_tier=2)

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 2,
                "daily_requests": 0,
                "daily_tokens": 0,
                "premium_daily": 0,
            }
        ),
    )

    checks = await asyncio.gather(*[_rt._check_budget(7, provider, 10) for _ in range(5)])

    allowed = [check for check in checks if check.error is None]
    blocked = [check for check in checks if check.error is not None]
    assert len(allowed) == 2
    assert len(blocked) == 3
    assert all("每分钟调用次数已达上限" in str(check.error) for check in blocked)


@pytest.mark.asyncio
async def test_llm_budget_reservation_rejects_over_daily_request_limit(monkeypatch) -> None:
    """每日请求数到达上限后，同账号后续请求会被 Redis reservation 拒绝。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    provider = LLMProviderDTO(id=1, name="primary", provider="openai", cost_tier=2)

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 0,
                "daily_requests": 1,
                "daily_tokens": 0,
                "premium_daily": 0,
            }
        ),
    )

    first = await _rt._check_budget(7, provider, 10)
    second = await _rt._check_budget(7, provider, 10)

    assert first.error is None
    assert second.error is not None
    assert "今日调用次数已达上限" in second.error


@pytest.mark.asyncio
async def test_llm_budget_reservation_rejects_over_daily_token_limit(monkeypatch) -> None:
    """预估 token 会参与每日 token reservation，超额请求不会进入 provider 调用。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    provider = LLMProviderDTO(id=1, name="primary", provider="openai", cost_tier=2)

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 0,
                "daily_requests": 0,
                "daily_tokens": 15,
                "premium_daily": 0,
            }
        ),
    )

    first = await _rt._check_budget(7, provider, 10)
    second = await _rt._check_budget(7, provider, 10)

    assert first.error is None
    assert second.error is not None
    assert "今日 token 用量已达上限" in second.error


@pytest.mark.asyncio
async def test_llm_budget_redis_failure_is_fail_closed(monkeypatch) -> None:
    """配置预算后 Redis 异常必须拒绝调用，避免费用总闸失效。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    provider = LLMProviderDTO(id=1, name="primary", provider="openai", cost_tier=3)

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: _BrokenBudgetRedis())
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 1,
                "daily_requests": 1,
                "daily_tokens": 1,
                "premium_daily": 1,
            }
        ),
    )

    check = await _rt._check_budget(7, provider, 10)

    assert check.error == "LLM 预算服务暂不可用，已为避免失控费用拒绝本次调用。"
    assert check.scope == "backend_unavailable"
    assert check.ticket is None


@pytest.mark.asyncio
async def test_llm_budget_failed_call_releases_reservation(monkeypatch) -> None:
    """LLM 调用最终失败时，预扣的请求、token 和高价 provider 计数会释放。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    provider = LLMProviderDTO(id=1, name="primary", provider="openai", cost_tier=3)

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 10,
                "daily_requests": 10,
                "daily_tokens": 100,
                "premium_daily": 10,
            }
        ),
    )

    check = await _rt._check_budget(7, provider, 30)
    assert check.error is None
    assert check.ticket is not None
    assert any(value == 1 for key, value in redis.strings.items() if ":daily_requests:" in key)
    assert any(value == 30 for key, value in redis.strings.items() if ":daily_tokens:" in key)
    assert any(value == 1 for key, value in redis.strings.items() if ":daily_premium:" in key)

    await llm_account_budget.settle(
        check.ticket,
        actual_tokens=0,
        actual_provider=None,
        success=False,
    )

    assert all(value == 0 for key, value in redis.strings.items() if ":daily_requests:" in key)
    assert all(value == 0 for key, value in redis.strings.items() if ":daily_tokens:" in key)
    assert all(value == 0 for key, value in redis.strings.items() if ":daily_premium:" in key)
    assert all(len(zset) == 0 for zset in redis.zsets.values())


@pytest.mark.asyncio
async def test_llm_budget_interrupted_stream_keeps_conservative_charge(monkeypatch) -> None:
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    provider = LLMProviderDTO(id=1, name="primary", provider="openai", cost_tier=3)
    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 10,
                "daily_requests": 10,
                "daily_tokens": 100,
                "premium_daily": 10,
            }
        ),
    )

    check = await _rt._check_budget(7, provider, 30)
    assert check.ticket is not None
    await llm_account_budget.settle(
        check.ticket,
        actual_tokens=30,
        actual_provider=provider,
        success=False,
        charge=True,
    )

    assert any(value == 1 for key, value in redis.strings.items() if ":daily_requests:" in key)
    assert any(value == 30 for key, value in redis.strings.items() if ":daily_tokens:" in key)
    assert any(value == 1 for key, value in redis.strings.items() if ":daily_premium:" in key)


@pytest.mark.asyncio
async def test_llm_budget_premium_limit_is_per_provider(monkeypatch) -> None:
    """高价 provider 每日次数沿用旧语义：同账号下按 provider_id 分桶。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    premium_a = LLMProviderDTO(id=1, name="premium-a", provider="openai", cost_tier=3)
    premium_b = LLMProviderDTO(id=2, name="premium-b", provider="openai", cost_tier=3)

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 0,
                "daily_requests": 0,
                "daily_tokens": 0,
                "premium_daily": 1,
            }
        ),
    )

    first_a = await _rt._check_budget(7, premium_a, 10)
    first_b = await _rt._check_budget(7, premium_b, 10)
    second_a = await _rt._check_budget(7, premium_a, 10)

    assert first_a.error is None
    assert first_b.error is None
    assert second_a.error is not None
    assert "高价 LLM 今日调用次数已达上限" in second_a.error


@pytest.mark.asyncio
async def test_llm_budget_fallback_settlement_corrects_tokens_and_premium(monkeypatch) -> None:
    """首候选失败释放预留后，fallback 成功只结算实际用量。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt
    from app.services.llm_client import LLMResult

    redis = _BudgetRedis()
    primary = LLMProviderDTO(
        id=1, name="primary", provider="openai", cost_tier=3, default_model="primary-model"
    )
    fallback = LLMProviderDTO(
        id=2, name="fallback", provider="openai", cost_tier=1, default_model="fallback-model"
    )
    chain = FallbackChain(primary=primary, fallbacks=[fallback])

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 10,
                "daily_requests": 10,
                "daily_tokens": 100,
                "premium_daily": 10,
            }
        ),
    )

    async def fake_call(provider_dto, *args, **kwargs):
        if provider_dto.id == 1:
            raise LLMError("primary failed", retryable=True)
        return LLMResult(text="ok", model="fallback-model", input_tokens=3, output_tokens=4)

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)

    result, provider, used_fallback = await _rt.call_with_fallback(
        chain,
        "sys",
        "user",
        max_tokens=30,
        account_id=7,
    )

    assert result.text == "ok"
    assert provider.id == 2
    assert used_fallback is True
    assert any(value == 1 for key, value in redis.strings.items() if ":daily_requests:" in key)
    assert any(value == 7 for key, value in redis.strings.items() if ":daily_tokens:" in key)
    assert all(value == 0 for key, value in redis.strings.items() if ":daily_premium:" in key)


@pytest.mark.asyncio
async def test_llm_budget_fallback_settlement_moves_premium_bucket_to_actual_provider(monkeypatch) -> None:
    """fallback 到另一个高价 provider 时，premium 计数按实际 provider_id 结算。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt
    from app.services.llm_client import LLMResult

    redis = _BudgetRedis()
    primary = LLMProviderDTO(
        id=1, name="primary", provider="openai", cost_tier=3, default_model="primary-model"
    )
    fallback = LLMProviderDTO(
        id=2, name="fallback", provider="openai", cost_tier=3, default_model="fallback-model"
    )
    chain = FallbackChain(primary=primary, fallbacks=[fallback])

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 10,
                "daily_requests": 10,
                "daily_tokens": 100,
                "premium_daily": 10,
            }
        ),
    )

    async def fake_call(provider_dto, *args, **kwargs):
        if provider_dto.id == 1:
            raise LLMError("primary failed", retryable=True)
        return LLMResult(text="ok", model="fallback-model", input_tokens=3, output_tokens=4)

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)

    await _rt.call_with_fallback(chain, "sys", "user", max_tokens=30, account_id=7)

    primary_premium = [value for key, value in redis.strings.items() if ":daily_premium:1:" in key]
    fallback_premium = [value for key, value in redis.strings.items() if ":daily_premium:2:" in key]
    assert primary_premium == [0]
    assert fallback_premium == [1]


# ════════════════════════════════════════════════════════════
# 6) 隐私日志脱敏测试
# ════════════════════════════════════════════════════════════


def test_safe_exception_text_strips_sk_key() -> None:
    """_safe_exception_text 正确脱敏 sk- token。"""
    e = RuntimeError("auth failed: token sk-veryverysecret-XYZ rejected")
    msg = _safe_exception_text(e)
    assert "sk-veryverysecret-XYZ" not in msg
    assert "<redacted>" in msg


def test_safe_exception_text_strips_bearer_token() -> None:
    """_safe_exception_text 正确脱敏 Bearer token。"""
    e = RuntimeError("auth failed: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
    msg = _safe_exception_text(e)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in msg


def test_safe_log_text_does_not_log_full_content() -> None:
    """_safe_log_text 不记录完整原文。"""
    from app.worker.command import _safe_log_text

    long_text = "A" * 500
    msg = _safe_log_text(long_text, max_len=100)
    # 应该显示长度，而不是完整内容
    assert "<len=500>" in msg
    # 不应该包含完整的 500 个 A
    assert msg.count("A") < 200  # 预览最多 100 字符


def test_safe_log_text_masks_tokens() -> None:
    """_safe_log_text 脱敏 token。"""
    text = "sk-my-secret-key-12345"
    msg = _safe_log_text(text)
    assert "sk-my-secret" not in msg


# ════════════════════════════════════════════════════════════
# 6) 长消息分段测试
# ════════════════════════════════════════════════════════════


def test_split_long_message_short_text() -> None:
    """短文本不分割。"""
    text = "Hello, world!"
    parts = _split_long_message(text, threshold=4000)
    assert len(parts) == 1
    assert parts[0] == text


def test_split_long_message_by_paragraphs() -> None:
    """按段落分割长文本。"""
    para = "A" * 2000
    text = f"{para}\n\n{para}\n\n{para}"
    parts = _split_long_message(text, threshold=3000)
    # 应该至少有 2 段
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= 3000


def test_split_long_message_single_long_paragraph() -> None:
    """单个超长段落按句子分割。"""
    sentence = "这是测试句子。" * 500
    parts = _split_long_message(sentence, threshold=2000)
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= 2000


def test_split_long_message_empty() -> None:
    """空文本处理。"""
    parts = _split_long_message("", threshold=100)
    assert parts == [""]


def test_ensure_html_safe_closes_tags() -> None:
    """_ensure_html_safe 补全未闭合的 HTML 标签。"""
    text = "<b>未闭合的粗体</b>\n\n<i>未闭合的斜体"
    safe = _ensure_html_safe(text)
    # 应该补全 </i>
    assert "</i>" in safe
    # 已闭合的不应重复
    assert safe.count("</b>") == 1


def test_ensure_html_safe_preserves_valid() -> None:
    """有效的 HTML 保持不变。"""
    text = "<b>粗体</b>\n<i>斜体</i>\n<code>代码</code>"
    safe = _ensure_html_safe(text)
    assert safe == text


# ════════════════════════════════════════════════════════════
# 7) call_with_fallback 集成测试
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_call_with_fallback_success_on_primary(monkeypatch) -> None:
    """主 provider 成功时直接返回。"""
    from app.services import llm_runtime as _rt

    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        default_model="gpt-4o",
        api_key_enc="sk-test",
    )

    class _FakeClient:
        async def complete(self, system, user, max_tokens=512, images=None):
            from app.services.llm_client import LLMResult

            return LLMResult(text="ok", model="gpt-4o", input_tokens=1, output_tokens=1)

    async def fake_call(*a, **k):
        return _FakeClient()

    chain = FallbackChain(primary=primary)

    with patch("app.services.llm_runtime.build_client_from_dto", fake_call):
        result, provider, used_fb = await _rt.call_with_fallback(chain, "sys", "user", max_tokens=100)

    assert result.text == "ok"
    assert provider.id == 1
    assert used_fb is False


@pytest.mark.asyncio
async def test_call_with_fallback_falls_back_on_failure(monkeypatch) -> None:
    """主 provider 失败后尝试 fallback。"""
    from app.services import llm_runtime as _rt

    primary = LLMProviderDTO(
        id=1, name="primary", provider="openai", default_model="gpt-4o", api_key_enc="sk-test"
    )
    fallback = LLMProviderDTO(
        id=2, name="fallback", provider="openai", default_model="gpt-4o", api_key_enc="sk-test"
    )

    attempt_order = []

    class _PrimaryClient:
        async def complete(self, system, user, max_tokens=512, images=None):
            attempt_order.append("primary")
            from app.services.llm_client import LLMError

            raise LLMError("primary failed", retryable=True)

    class _FallbackClient:
        async def complete(self, system, user, max_tokens=512, images=None):
            attempt_order.append("fallback")
            from app.services.llm_client import LLMResult

            return LLMResult(text="fallback-ok", model="gpt-4o", input_tokens=1, output_tokens=1)

    async def fake_build(dto, **k):
        if dto.id == 1:
            return _PrimaryClient()
        return _FallbackClient()

    chain = FallbackChain(primary=primary, fallbacks=[fallback])

    with patch("app.services.llm_runtime.build_client_from_dto", fake_build):
        result, provider, used_fb = await _rt.call_with_fallback(chain, "sys", "user", max_tokens=100)

    assert result.text == "fallback-ok"
    assert provider.id == 2
    assert used_fb is True
    assert "primary" in attempt_order
    assert "fallback" in attempt_order


@pytest.mark.asyncio
async def test_call_with_fallback_skips_fallback_with_all_models_disabled(monkeypatch) -> None:
    from app.services import llm_runtime as _rt

    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        default_model="primary-model",
        models=[{"id": "primary-model", "enabled": True}],
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        default_model="fallback-model",
        models=[{"id": "fallback-model", "enabled": False}],
    )
    calls: list[int] = []

    async def fake_call(provider_dto, *_args, **_kwargs):
        calls.append(provider_dto.id)
        raise LLMError("temporary", retryable=True)

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)

    with pytest.raises(LLMCallFailed, match="没有已启用模型"):
        await _rt.call_with_fallback(
            FallbackChain(primary, [fallback]),
            "sys",
            "user",
            routed_model="primary-model",
        )

    assert calls == [primary.id]


@pytest.mark.asyncio
async def test_call_with_fallback_all_fail_raises(monkeypatch) -> None:
    """所有 provider 都失败时抛出 LLMCallFailed。"""
    from app.services import llm_runtime as _rt

    primary = LLMProviderDTO(
        id=1, name="primary", provider="openai", default_model="gpt-4o", api_key_enc="sk-test"
    )
    fallback = LLMProviderDTO(
        id=2, name="fallback", provider="openai", default_model="gpt-4o", api_key_enc="sk-test"
    )

    class _FailingClient:
        async def complete(self, system, user, max_tokens=512, images=None):
            # 不可重试的错误（认证失败）
            from app.services.llm_client import LLMError

            raise LLMError("401 Unauthorized", retryable=False)

    async def fake_build(*a, **k):
        return _FailingClient()

    chain = FallbackChain(primary=primary, fallbacks=[fallback])

    with patch("app.services.llm_runtime.build_client_from_dto", fake_build):
        with pytest.raises(LLMCallFailed) as exc_info:
            await _rt.call_with_fallback(chain, "sys", "user", max_tokens=100)

    # 最后失败的 provider 应该是 primary（因为第一个尝试）
    assert exc_info.value.error_type in ("auth", "unknown")


# ════════════════════════════════════════════════════════════
# 8) Scheduler FloodWait 修复测试
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_floodwait_calls_engine_correctly(monkeypatch) -> None:
    """scheduler FloodWaitError 时正确调用 engine.on_flood_wait。"""
    from app.worker.plugins.builtin.scheduler.plugin import SchedulerPlugin

    plugin = SchedulerPlugin()

    # Mock engine
    mock_engine = AsyncMock()
    mock_engine.on_flood_wait = AsyncMock()
    mock_engine.acquire = AsyncMock()
    mock_engine.acquire.return_value = AsyncMock()
    mock_engine.acquire.return_value.allowed = True
    mock_engine.acquire.return_value.wait_seconds = 0

    # Mock client
    mock_client = AsyncMock()

    class FakeFloodWaitError(Exception):
        seconds = 10

    mock_client.send_message = AsyncMock(side_effect=FakeFloodWaitError())

    class MockCtx:
        account_id = 1
        engine = mock_engine
        client = mock_client
        log = AsyncMock()

    ctx = MockCtx()

    # 触发 FloodWaitError
    await plugin._send_with_ratelimit(ctx, 123, "test message")

    # 验证 on_flood_wait 被调用（不带 peer_id）
    mock_engine.on_flood_wait.assert_called_once()
    args = mock_engine.on_flood_wait.call_args
    # 确认只传了 2 个参数（action 和 exc）
    assert len(args[0]) == 2
    assert args[0][0] == "send_message_group"
    assert isinstance(args[0][1], FakeFloodWaitError)


# ════════════════════════════════════════════════════════════
# 9) 真实错误包装 → retry / fallback 集成测试
# ════════════════════════════════════════════════════════════
#
# 这些用例走真实的 llm_client 包装路径（httpx 异常 / HTTP 状态码），
# 断言 LLMError.retryable 被正确设置，并驱动 runtime 的重试与 fallback。
# 回归的 bug：生产代码从不设置 retryable=True，导致 fallback 链永不推进、
# _call_with_retry 永不重试。


class _FakeHTTPResponse:
    """最小 httpx.Response 替身：只提供 llm_client 错误/成功路径用到的字段。"""

    def __init__(self, status_code: int, *, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _make_fake_async_client(primary_outcome, call_log: list[str]):
    """构造假的 httpx.AsyncClient：

    - base_url 含 "fail" 的 primary 按 primary_outcome 出错（httpx 异常实例或 4xx/5xx 响应）
    - 其它 provider（fallback）返回 200 成功 JSON
    每次 post 都记入 call_log，用于断言重试次数。
    """

    class _FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, **kwargs):
            call_log.append(url)
            if "fail" in url:
                if isinstance(primary_outcome, BaseException):
                    raise primary_outcome
                return primary_outcome
            return _FakeHTTPResponse(
                200,
                payload={
                    "choices": [{"message": {"content": "fallback-ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "model": "gpt-4o",
                },
            )

    return _FakeAsyncClient


def _openai_client_factory(dto, override_model=None, proxy_url=None):
    """按 DTO base_url 直接构造真实 OpenAIClient（跳过 DTO 密钥解密）。"""
    from app.services.llm_client import OpenAIClient

    return OpenAIClient(api_key="sk-test", base_url=dto.base_url, model=dto.default_model or "gpt-4o")


def _fail_ok_chain() -> FallbackChain:
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        base_url="https://fail.example/v1",
        default_model="gpt-4o",
        api_key_enc="sk-test",
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        base_url="https://ok.example/v1",
        default_model="gpt-4o",
        api_key_enc="sk-test",
    )
    return FallbackChain(primary=primary, fallbacks=[fallback])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected_retryable,expected_scope",
    [
        ("network", True, LLMErrorScope.TRANSIENT),
        (429, True, LLMErrorScope.TRANSIENT),
        (500, True, LLMErrorScope.TRANSIENT),
        (503, True, LLMErrorScope.TRANSIENT),
        (400, False, LLMErrorScope.REQUEST_INVALID),
        (401, False, LLMErrorScope.PROVIDER_LOCAL),
        (403, False, LLMErrorScope.PROVIDER_LOCAL),
        (404, False, LLMErrorScope.PROVIDER_LOCAL),
    ],
)
async def test_openai_client_wraps_typed_error(
    monkeypatch, outcome, expected_retryable, expected_scope
) -> None:
    """真实 OpenAIClient 同时保留重试属性与跨 provider 作用域。"""
    import httpx

    from app.services import llm_client
    from app.services.llm_client import OpenAIClient

    if outcome == "network":
        primary_outcome: object = httpx.ConnectError("connection refused")
    else:
        primary_outcome = _FakeHTTPResponse(outcome, text="upstream error")

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _make_fake_async_client(primary_outcome, []))

    client = OpenAIClient(api_key="sk-test", base_url="https://fail.example/v1", model="gpt-4o")
    with pytest.raises(LLMError) as exc_info:
        await client.complete("sys", "user", max_tokens=10)

    assert exc_info.value.retryable is expected_retryable
    assert exc_info.value.scope is expected_scope


@pytest.mark.asyncio
async def test_openai_client_classifies_policy_403_as_account_scope(monkeypatch) -> None:
    from app.services import llm_client
    from app.services.llm_client import OpenAIClient

    monkeypatch.setattr(
        llm_client.httpx,
        "AsyncClient",
        _make_fake_async_client(_FakeHTTPResponse(403, text="request blocked by safety policy"), []),
    )
    with pytest.raises(LLMError) as exc_info:
        await OpenAIClient(api_key="sk-test", base_url="https://fail.example/v1", model="gpt-4o").complete(
            "sys", "user", max_tokens=10
        )

    assert exc_info.value.scope is LLMErrorScope.ACCOUNT_POLICY


@pytest.mark.asyncio
async def test_call_with_fallback_network_error_retries_and_falls_back(monkeypatch) -> None:
    """真实网络异常经包装成可重试 LLMError → 先重试 primary，再推进到 fallback。"""
    import httpx

    from app.services import llm_client
    from app.services import llm_runtime as _rt

    call_log: list[str] = []
    monkeypatch.setattr(
        llm_client.httpx,
        "AsyncClient",
        _make_fake_async_client(httpx.ConnectError("connection refused"), call_log),
    )
    # 去掉退避真实 sleep，让重试瞬间完成
    monkeypatch.setattr(_rt, "_compute_retry_delay", lambda attempt: 0.0)

    result, provider, used_fb = await _rt.call_with_fallback(
        _fail_ok_chain(), "sys", "user", max_tokens=50, client_factory=_openai_client_factory
    )

    assert result.text == "fallback-ok"
    assert provider.id == 2
    assert used_fb is True
    # primary 被真实重试满 _MAX_RETRIES+1 次后才 fallback
    assert len([u for u in call_log if "fail" in u]) == _rt._MAX_RETRIES + 1
    assert any("ok.example" in u for u in call_log)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500])
async def test_call_with_fallback_retryable_status_falls_back(monkeypatch, status) -> None:
    """429 / 5xx 经包装成可重试 LLMError → 重试并 fallback。"""
    from app.services import llm_client
    from app.services import llm_runtime as _rt

    call_log: list[str] = []
    monkeypatch.setattr(
        llm_client.httpx,
        "AsyncClient",
        _make_fake_async_client(_FakeHTTPResponse(status, text="upstream error"), call_log),
    )
    monkeypatch.setattr(_rt, "_compute_retry_delay", lambda attempt: 0.0)

    result, provider, used_fb = await _rt.call_with_fallback(
        _fail_ok_chain(), "sys", "user", max_tokens=50, client_factory=_openai_client_factory
    )

    assert result.text == "fallback-ok"
    assert provider.id == 2
    assert used_fb is True
    assert len([u for u in call_log if "fail" in u]) == _rt._MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_call_with_fallback_provider_auth_error_skips_to_fallback(monkeypatch) -> None:
    """Provider 本地的 401 不重试当前线路，但允许切换健康 fallback。"""
    from app.services import llm_client
    from app.services import llm_runtime as _rt

    call_log: list[str] = []
    monkeypatch.setattr(
        llm_client.httpx,
        "AsyncClient",
        _make_fake_async_client(_FakeHTTPResponse(401, text="unauthorized"), call_log),
    )
    # 即便退避可用，认证错误也不该产生任何重试
    monkeypatch.setattr(_rt, "_compute_retry_delay", lambda attempt: 0.0)

    result, provider, used_fallback = await _rt.call_with_fallback(
        _fail_ok_chain(), "sys", "user", max_tokens=50, client_factory=_openai_client_factory
    )

    # primary 只调用一次（无重试），随后切换 fallback。
    assert [u for u in call_log if "fail" in u] == ["https://fail.example/v1/chat/completions"]
    assert any("ok.example" in u for u in call_log)
    assert result.text == "fallback-ok"
    assert provider.id == 2
    assert used_fallback is True


@pytest.mark.asyncio
async def test_call_with_fallback_request_invalid_stops_chain(monkeypatch) -> None:
    """请求本身无效时必须终止，不能把所有 4xx 都视为可 fallback。"""
    from app.services import llm_runtime as _rt

    calls: list[int] = []

    async def fake_call(provider_dto, *args, **kwargs):
        calls.append(provider_dto.id)
        raise LLMError(
            "messages 格式无效",
            scope=LLMErrorScope.REQUEST_INVALID,
        )

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)
    with pytest.raises(LLMCallFailed):
        await _rt.call_with_fallback(_fail_ok_chain(), "sys", "user", max_tokens=50)

    assert calls == [1]


@pytest.mark.asyncio
async def test_call_with_fallback_invalid_empty_result_uses_next_provider(monkeypatch) -> None:
    """无文本、无工具、无图片且无明确 stop 语义的 200 不是成功。"""
    from app.services import llm_runtime as _rt

    calls: list[int] = []

    async def fake_call(provider_dto, *args, **kwargs):
        calls.append(provider_dto.id)
        return LLMResult(
            text="" if provider_dto.id == 1 else "fallback-ok",
            model="model",
            input_tokens=1,
            output_tokens=1,
        )

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)
    result, provider, used_fallback = await _rt.call_with_fallback(
        _fail_ok_chain(), "sys", "user", max_tokens=50
    )

    assert calls == [1, 2]
    assert result.text == "fallback-ok"
    assert provider.id == 2
    assert used_fallback is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["refusal", "content_filter"])
async def test_call_with_fallback_accepts_explicit_legal_empty_stop(monkeypatch, stop_reason) -> None:
    """明确 refusal/content-filter 是合法空产物，不应误切 fallback。"""
    from app.services import llm_runtime as _rt
    from app.services.llm_protocol import stop_reason_from_provider

    calls: list[int] = []

    async def fake_call(provider_dto, *args, **kwargs):
        calls.append(provider_dto.id)
        return LLMResult(
            text="",
            model="model",
            input_tokens=1,
            output_tokens=0,
            stop_reason=stop_reason_from_provider(stop_reason),
            provider_status=stop_reason,
        )

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)
    result, provider, used_fallback = await _rt.call_with_fallback(
        _fail_ok_chain(), "sys", "user", max_tokens=50
    )

    assert result.text == ""
    assert provider.id == 1
    assert used_fallback is False
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["tool", "image_url", "image_data"])
async def test_call_with_fallback_accepts_non_text_product(monkeypatch, product) -> None:
    """tool-only 和 image-only 都是合法产物，不能被空文本规则误杀。"""
    from app.services import llm_runtime as _rt
    from app.services.llm_protocol import ToolCall

    calls: list[int] = []

    async def fake_call(provider_dto, *args, **kwargs):
        calls.append(provider_dto.id)
        result = LLMResult(text="", model="model", input_tokens=1, output_tokens=1)
        if product == "tool":
            result.tool_calls = [ToolCall("call-1", "lookup", {})]
        elif product == "image_url":
            result.image_urls = ["https://example.test/image.png"]
        else:
            result.image_data = ["data:image/png;base64,AAAA"]
        return result

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)
    result, provider, used_fallback = await _rt.call_with_fallback(
        _fail_ok_chain(), "sys", "user", max_tokens=50
    )

    assert result.text == ""
    assert provider.id == 1
    assert used_fallback is False
    assert calls == [1]


@pytest.mark.asyncio
async def test_call_with_fallback_reserves_input_plus_output_for_each_candidate(monkeypatch) -> None:
    """预算按输入+输出预留，且每个真正候选调用前都重新 gate。"""
    from app.services import llm_runtime as _rt

    checks: list[tuple[int, int]] = []
    called: list[int] = []

    async def check(account_id, provider, estimate):
        checks.append((provider.id, estimate))
        if provider.id == 2:
            return _rt.BudgetCheck(error="高价 LLM 今日调用次数已达上限（1/day）。")
        return _rt.BudgetCheck()

    async def fake_call(provider_dto, *args, **kwargs):
        called.append(provider_dto.id)
        raise LLMError("temporary", retryable=True)

    monkeypatch.setattr(_rt, "_check_budget", check)
    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)
    with pytest.raises(LLMCallFailed):
        await _rt.call_with_fallback(
            _fail_ok_chain(),
            "system",
            "x" * 360,
            max_tokens=10,
            account_id=7,
        )

    assert [provider_id for provider_id, _ in checks] == [1, 2]
    assert all(estimate >= 100 for _, estimate in checks)
    assert called == [1]


@pytest.mark.asyncio
async def test_premium_fallback_budget_gate_blocks_network_call(monkeypatch) -> None:
    """cheap primary 失败后，premium fallback 已达限时必须在网络调用前被拒绝。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    primary = LLMProviderDTO(id=1, name="cheap", provider="openai", cost_tier=1, default_model="cheap-model")
    premium = LLMProviderDTO(
        id=2, name="premium", provider="openai", cost_tier=3, default_model="premium-model"
    )
    premium_key = llm_account_budget._daily_premium_key(7, premium.id)
    redis.strings[premium_key] = 1
    called: list[int] = []

    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 0,
                "daily_requests": 0,
                "daily_tokens": 1000,
                "premium_daily": 1,
            }
        ),
    )

    async def fake_call(provider_dto, *args, **kwargs):
        called.append(provider_dto.id)
        raise LLMError("temporary", retryable=True)

    monkeypatch.setattr(_rt, "_call_with_retry", fake_call)
    with pytest.raises(LLMCallFailed) as exc_info:
        await _rt.call_with_fallback(
            FallbackChain(primary, [premium]),
            "system",
            "question",
            max_tokens=10,
            account_id=7,
        )

    assert exc_info.value.error_type == "budget_exceeded"
    assert called == [primary.id]


@pytest.mark.asyncio
async def test_premium_candidate_gate_is_atomic_under_concurrency(monkeypatch) -> None:
    """并发请求同时 gate 同一 premium provider 时，只能有一个预留成功。"""
    from app.services import llm_account_budget
    from app.services import llm_runtime as _rt

    redis = _BudgetRedis()
    premium = LLMProviderDTO(id=2, name="premium", provider="openai", cost_tier=3)
    monkeypatch.setattr(llm_account_budget, "get_redis", lambda: redis)
    monkeypatch.setattr(
        llm_account_budget,
        "_load_budget_limits",
        AsyncMock(
            return_value={
                "per_minute": 0,
                "daily_requests": 0,
                "daily_tokens": 0,
                "premium_daily": 1,
            }
        ),
    )

    checks = await asyncio.gather(
        _rt._check_budget(7, premium, 10),
        _rt._check_budget(7, premium, 10),
    )

    assert sum(check.error is None for check in checks) == 1
    assert sum(check.scope == "premium_daily" for check in checks) == 1


@pytest.mark.asyncio
async def test_call_with_fallback_records_latency(monkeypatch) -> None:
    """成功路径把测得的 latency_ms 写入 usage（回归 latency_ms 恒 0 的 bug）。"""
    from app.services import llm_runtime as _rt
    from app.services.llm_client import LLMResult

    captured: list[UsageRecord] = []

    async def _capture(record: UsageRecord) -> None:
        captured.append(record)

    monkeypatch.setattr(_rt, "_emit_usage", _capture)

    async def _slow_call(*args, **kwargs):
        await asyncio.sleep(0.02)
        return LLMResult(text="ok", model="gpt-4o", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_rt, "_call_with_retry", _slow_call)

    primary = LLMProviderDTO(
        id=1, name="primary", provider="openai", default_model="gpt-4o", api_key_enc="sk-test"
    )
    result, _provider, _used_fb = await _rt.call_with_fallback(
        FallbackChain(primary=primary), "sys", "user", max_tokens=50
    )

    assert result.text == "ok"
    assert len(captured) == 1
    assert captured[0].success is True
    assert captured[0].latency_ms >= 10


__all__ = []

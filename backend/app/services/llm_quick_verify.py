"""未落库 LLM 凭据的模型发现与真实流式验证。

安全边界：API Key 只在当前请求内存中存在，不写数据库、审计或诊断用量；
所有错误在进入 NDJSON 前再次按当前 Key 脱敏。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx

from ..crypto import encrypt_str
from .llm_client import (
    LLMError,
    LLMErrorScope,
    _safe_error_message,
    build_client_from_dto,
)
from .llm_dto import LLMProviderDTO
from .llm_protocol import normalize_base_url, provider_models_endpoint

_MODEL_EXCLUDES = (
    "embedding",
    "rerank",
    "whisper",
    "transcri",
    "moderation",
    "tts",
    "speech",
    "audio",
    "image",
    "dall-e",
    "realtime",
)
_MODEL_PREFERENCES = (
    "chat",
    "gpt",
    "claude",
    "grok",
    "deepseek",
    "glm",
    "qwen",
    "llama",
    "mistral",
    "gemini",
)
_MAX_DISCOVERED_MODELS = 100


def normalize_quick_verify_base_url(value: str) -> str:
    """规范化 URL，并拒绝可能在错误或导入配置中泄露的 userinfo。"""

    normalized = normalize_base_url(value)
    parsed = urlsplit(normalized)
    if parsed.username or parsed.password:
        raise ValueError("Base URL 不能包含用户名或密码，请单独填写 API Key。")
    return normalized


def suggested_provider(api_format: str, base_url: str, api_key: str) -> str:
    if api_format == "anthropic_messages":
        return "anthropic"
    hostname = (urlsplit(base_url).hostname or "").lower()
    if not api_key and hostname in {"localhost", "127.0.0.1", "::1"}:
        return "ollama"
    return "openai"


def suggested_name(base_url: str) -> str:
    hostname = urlsplit(base_url).hostname or "模型提供商"
    return hostname[:64]


def _safe_message(message: str, api_key: str) -> str:
    return _safe_error_message(message, api_key or None)


def _discovery_headers(api_format: str, api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if not api_key:
        return headers
    if api_format == "anthropic_messages":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _model_ids(data: object) -> list[str]:
    if isinstance(data, dict):
        items = data.get("data")
        if not isinstance(items, list):
            items = data.get("models")
    elif isinstance(data, list):
        items = data
    else:
        items = None
    if not isinstance(items, list):
        return []

    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
        else:
            continue
        if model_id and len(model_id) <= 128 and model_id not in seen:
            seen.add(model_id)
            output.append(model_id)
    return output


def _rank_chat_models(model_ids: list[str]) -> list[str]:
    candidates = [
        model_id
        for model_id in model_ids
        if not any(marker in model_id.lower() for marker in _MODEL_EXCLUDES)
    ]
    indexed = list(enumerate(candidates))
    indexed.sort(
        key=lambda pair: (
            0
            if any(marker in pair[1].lower() for marker in _MODEL_PREFERENCES)
            else 1,
            pair[0],
        )
    )
    return [model_id for _, model_id in indexed[:_MAX_DISCOVERED_MODELS]]


async def discover_models(
    *,
    base_url: str,
    api_key: str,
    api_format: str,
    timeout_seconds: int,
) -> list[str]:
    timeout = httpx.Timeout(
        min(float(timeout_seconds), 20.0),
        connect=min(float(timeout_seconds), 8.0),
    )
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(
                provider_models_endpoint(base_url, api_format),
                headers=_discovery_headers(api_format, api_key),
            )
    except httpx.HTTPError as exc:
        raise LLMError(
            _safe_message(f"模型列表请求失败（{type(exc).__name__}）。", api_key),
            retryable=True,
        ) from None

    if response.status_code >= 400:
        raise LLMError(
            _safe_message(
                f"模型列表接口返回 {response.status_code}: {response.text[:200]}",
                api_key,
            ),
            status_code=response.status_code,
        )
    try:
        data = response.json()
    except ValueError:
        raise LLMError("模型列表响应不是合法 JSON。") from None
    return _rank_chat_models(_model_ids(data))


def _stream_can_fallback(exc: Exception) -> bool:
    if isinstance(exc, NotImplementedError):
        return True
    if not isinstance(exc, LLMError):
        return False
    if exc.scope is LLMErrorScope.CAPABILITY_MISMATCH:
        return True
    if exc.status_code in {405, 406, 415, 501}:
        return True
    message = str(exc).lower()
    return exc.status_code in {400, 422} and "stream" in message and any(
        marker in message
        for marker in ("不支持", "unsupported", "not support", "unknown parameter")
    )


def _requires_manual_model(exc: Exception, *, auto_selected: bool) -> bool:
    if not auto_selected or not isinstance(exc, LLMError):
        return False
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "model_not_found",
            "model not found",
            "model does not exist",
            "no such model",
            "unknown model",
            "deployment not found",
            "模型不存在",
            "未找到模型",
        )
    )


def _discovery_requires_manual_model(exc: LLMError) -> bool:
    """鉴权失败应直接提示凭据问题，不能伪装成缺少模型 ID。"""

    return exc.status_code not in {401, 403}


async def quick_verify_events(
    *,
    base_url: str,
    api_key: str,
    api_format: str,
    model: str | None,
    system_prompt: str,
    message: str,
    max_tokens: int,
    timeout_seconds: int,
) -> AsyncIterator[dict[str, object]]:
    """生成单次快速验证 NDJSON 事件，不产生持久化副作用。"""

    explicit_model = (model or "").strip()
    models: list[str] = []
    auto_selected = not bool(explicit_model)
    if auto_selected:
        try:
            models = await discover_models(
                base_url=base_url,
                api_key=api_key,
                api_format=api_format,
                timeout_seconds=timeout_seconds,
            )
        except LLMError as exc:
            requires_model = _discovery_requires_manual_model(exc)
            yield {
                "type": "error",
                "ok": False,
                "error": (
                    f"无法自动获取可对话模型，请填写模型 ID 后重试。{_safe_message(str(exc), api_key)}"
                    if requires_model
                    else _safe_message(str(exc), api_key)
                ),
                "requires_model": requires_model,
                "models": [],
                "api_format": api_format,
            }
            return
        if not models:
            yield {
                "type": "error",
                "ok": False,
                "error": "模型列表中没有可自动选择的文本对话模型，请填写模型 ID 后重试。",
                "requires_model": True,
                "models": [],
                "api_format": api_format,
            }
            return
        selected_model = models[0]
    else:
        selected_model = explicit_model

    yield {
        "type": "discovery",
        "model": selected_model,
        "models": models,
        "api_format": api_format,
    }
    yield {
        "type": "start",
        "model": selected_model,
        "streaming": True,
        "api_format": api_format,
    }

    started = time.monotonic()
    text_parts: list[str] = []
    actual_model: str | None = None
    input_tokens = 0
    output_tokens = 0
    response_chars = 0
    streaming = True
    stream_fallback = False
    max_response_chars = max(16_384, max_tokens * 16)
    dto = LLMProviderDTO(
        id=0,
        name="quick-verify",
        provider=suggested_provider(api_format, base_url, api_key),
        api_format=api_format,
        protocol_profile="standard",
        client_identity_profile="auto",
        web_search_api_format="auto",
        base_url=base_url,
        default_model=selected_model,
        api_key_enc=encrypt_str(api_key) if api_key else None,
    )

    try:
        client = build_client_from_dto(dto)
        try:
            async with asyncio.timeout(timeout_seconds):
                try:
                    async for chunk in client.stream_complete(
                        system_prompt,
                        message,
                        max_tokens=max_tokens,
                        timeout_seconds=timeout_seconds,
                    ):
                        if chunk.model:
                            actual_model = chunk.model
                        if chunk.input_tokens is not None:
                            input_tokens = int(chunk.input_tokens)
                        if chunk.output_tokens is not None:
                            output_tokens = int(chunk.output_tokens)
                        if chunk.delta:
                            text_parts.append(chunk.delta)
                            response_chars += len(chunk.delta)
                            if response_chars > max_response_chars:
                                raise LLMError("模型流式输出超过快速验证内容上限。")
                            yield {
                                "type": "delta",
                                "model": actual_model or selected_model,
                                "delta": chunk.delta,
                            }
                except (LLMError, NotImplementedError) as exc:
                    if any(part.strip() for part in text_parts) or not _stream_can_fallback(exc):
                        raise
                    stream_fallback = True

                if stream_fallback:
                    streaming = False
                    elapsed = time.monotonic() - started
                    completed = await client.complete(
                        system_prompt,
                        message,
                        max_tokens=max_tokens,
                        timeout_seconds=max(1, timeout_seconds - int(elapsed)),
                    )
                    if len(completed.text or "") > max_response_chars:
                        raise LLMError("模型完整响应超过快速验证内容上限。")
                    text_parts = [completed.text or ""]
                    actual_model = completed.model or actual_model
                    input_tokens = int(completed.input_tokens or 0)
                    output_tokens = int(completed.output_tokens or 0)
        except TimeoutError:
            raise LLMError("快速验证超过总超时时间。", retryable=True) from None

        text = "".join(text_parts).strip()
        latency_ms = int((time.monotonic() - started) * 1000)
        if not text:
            yield {
                "type": "error",
                "ok": False,
                "model": actual_model or selected_model,
                "requested_model": selected_model,
                "latency_ms": latency_ms,
                "error": "上游请求已完成，但返回文本为空。",
                "requires_model": False,
                "models": models,
                "api_format": api_format,
            }
            return
        yield {
            "type": "done",
            "ok": True,
            "model": actual_model or selected_model,
            "requested_model": selected_model,
            "latency_ms": latency_ms,
            "response": text,
            "streaming": streaming,
            "stream_fallback": stream_fallback,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "models": models,
            "api_format": api_format,
            "base_url": base_url,
            "provider": suggested_provider(api_format, base_url, api_key),
            "suggested_name": suggested_name(base_url),
        }
    except asyncio.CancelledError:
        raise
    except LLMError as exc:
        yield {
            "type": "error",
            "ok": False,
            "model": actual_model or selected_model,
            "requested_model": selected_model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "response": "".join(text_parts).strip() or None,
            "error": _safe_message(str(exc), api_key),
            "requires_model": _requires_manual_model(exc, auto_selected=auto_selected),
            "models": models,
            "api_format": api_format,
        }
    except Exception as exc:  # noqa: BLE001
        yield {
            "type": "error",
            "ok": False,
            "model": actual_model or selected_model,
            "requested_model": selected_model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "response": "".join(text_parts).strip() or None,
            "error": _safe_message(f"快速验证失败（{type(exc).__name__}）。", api_key),
            "requires_model": False,
            "models": models,
            "api_format": api_format,
        }


__all__ = [
    "discover_models",
    "normalize_quick_verify_base_url",
    "quick_verify_events",
    "suggested_name",
    "suggested_provider",
]

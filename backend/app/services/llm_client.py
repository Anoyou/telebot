"""LLM provider 抽象 —— OpenAI / Anthropic / (占位) Ollama。

设计要点：
- 每个 provider 实现 ``LLMClient`` 接口；``complete`` 返 ``LLMResult``
- ``build_client`` 根据 ``LLMProvider`` ORM 行解密 api_key 并装配具体实现
- **安全红线**：解密后的 api_key 仅留在 client 实例内；不打 log，不 audit；
  错误路径用 ``_safe_error_message`` 兜底剥离任何含 sk-/secret-/Bearer 字样
- 视觉支持：``complete(images=[...])`` 接 PNG/JPEG 等字节，由各实现按各自厂商
  vision 协议封装到 multipart content。``images`` 留空 = 纯文本（向后兼容）

调用入口在 worker 进程 (``worker/command.py:_run_ai``)，所以这里 httpx 调用是 async。

V1 仅实现 openai/anthropic 两类常用接口；ollama 走 OpenAI-compatible 端点（``/v1/chat/completions``）由 OpenAIClient 复用。
"""

from __future__ import annotations

import base64
import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import __version__
from ..crypto import decrypt_str
from ..db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
    LLM_PROVIDER_OLLAMA,
    LLMProvider,
    default_api_format_for,
)
from .llm_dto import LLMProviderDTO

# 默认调用超时；prompt 较长 / TG 端用户体验角度都不宜过长
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# 本地桥接（如 grok-bridge）需要等待浏览器 JS 执行 + LLM 生成，超时更长
_LOCAL_TIMEOUT = httpx.Timeout(180.0, connect=10.0)

_LLM_USER_AGENT = f"TelePilot/{__version__} LLM Client"


def _llm_headers(
    *,
    content_type: str | None = "application/json",
    accept: str | None = None,
) -> dict[str, str]:
    headers = {"User-Agent": _LLM_USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    if accept:
        headers["Accept"] = accept
    return headers


def _timeout_for_call(base_url: str, timeout_seconds: int | None) -> httpx.Timeout:
    if timeout_seconds and timeout_seconds > 0:
        seconds = float(max(1, timeout_seconds))
        return httpx.Timeout(
            seconds,
            connect=min(5.0, seconds),
            pool=min(5.0, seconds),
            write=min(15.0, seconds),
            read=seconds,
        )
    if "127.0.0.1" in base_url or "localhost" in base_url:
        return _LOCAL_TIMEOUT
    return _HTTP_TIMEOUT


def _normalize_temperature(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(2.0, float(value)))


def _normalize_reasoning_effort(value: str | None) -> str | None:
    effort = (value or "").strip().lower()
    return effort if effort in {"minimal", "low", "medium", "high"} else None


@dataclass
class LLMResult:
    """LLM 调用的统一结果。"""

    text: str           # 模型回答正文
    model: str          # 实际使用的模型名（便于 TG 内回显）
    input_tokens: int   # 入 tokens；若供应商不返就给 0
    output_tokens: int  # 出 tokens；若供应商不返就给 0
    image_urls: list = field(default_factory=list)  # LLM 生成的图片 URL（如 Grok 文生图）
    image_data: list = field(default_factory=list)  # LLM 生成的图片 base64 data URI（如 Grok 文生图）
    sources: list = field(default_factory=list)  # 联网搜索来源：[{url,title?}, ...]


@dataclass(frozen=True)
class LLMStreamChunk:
    """Incremental text chunk from a provider streaming response."""

    delta: str = ""
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    done: bool = False


def _sniff_image_mime(data: bytes) -> str:
    """根据 magic bytes 判断图片 MIME 类型。

    支持 JPEG / PNG / WebP / GIF；其它一律返回 ``image/jpeg``（绝大多数 vision 模型
    会按 jpeg 兜底解码，比报错稳）。

    与 OpenAI/Anthropic 对 ``image/...`` 的接受集对齐。
    """
    if len(data) < 12:
        return "image/jpeg"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _to_data_url(data: bytes) -> str:
    """把图片字节编码为 ``data:image/...;base64,...`` data URL。

    OpenAI Chat Completions Vision / mimo / GLM-4V 等都接受这种 inline 形式，
    省去托管图床的麻烦。"""
    mime = _sniff_image_mime(data)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _normalize_image_data_uri(value: str, default_mime: str = "image/png") -> str:
    """把裸 base64 或 data URI 统一成 data URI，便于 worker 发送图片。"""
    raw = str(value or "").strip()
    if raw.startswith("data:") and ";base64," in raw:
        return raw
    return f"data:{default_mime};base64,{raw}"


def _extract_response_image_outputs(data: Any) -> tuple[list[str], list[str], str]:
    """从 Responses / 兼容返回体中提取生图结果。

    返回 ``(image_data, image_urls, output_text)``：
    - ``image_data`` 始终是 data URI；
    - ``image_urls`` 是可下载 URL；
    - ``output_text`` 用作图片 caption 或失败时的错误提示上下文。
    """
    image_data: list[str] = []
    image_urls: list[str] = []
    text_parts: list[str] = []

    def add_text(value: Any) -> None:
        if isinstance(value, str) and value:
            text_parts.append(value)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = str(node.get("type") or "")
            if "image_generation" in node_type or node_type in {"image", "output_image"}:
                for key in ("result", "b64_json", "image_base64", "partial_image_b64"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        image_data.append(_normalize_image_data_uri(value.strip()))
                for key in ("url", "image_url"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        image_urls.append(value.strip())
            if node_type in {"output_text", "text"}:
                add_text(node.get("text"))
            if (
                isinstance(node.get("text"), str)
                and "image_generation" not in node_type
                and node_type not in {"output_text", "text"}
            ):
                add_text(node.get("text"))
            for key in ("output", "content", "response", "data", "result", "message"):
                if key in node:
                    walk(node.get(key))
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(data, dict):
        add_text(data.get("output_text"))
    walk(data)

    # 去重并保持顺序
    def unique(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    return unique(image_data), unique(image_urls), "".join(text_parts).strip()


def _extract_response_sources(data: Any) -> list[dict[str, str]]:
    """从 Responses API 返回体里提取联网搜索来源。

    OpenAI Responses 的来源可能出现在两类位置：
    - ``output[].content[].annotations[]`` 的 ``url_citation``；
    - ``web_search_call.action.sources``（当请求 include 了 sources）。
    兼容反代时字段名可能略有差异，所以递归扫描常见 key。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(url: Any, title: Any = None) -> None:
        if not isinstance(url, str):
            return
        u = url.strip()
        if not u or u in seen:
            return
        seen.add(u)
        item = {"url": u}
        if isinstance(title, str) and title.strip():
            item["title"] = title.strip()
        out.append(item)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            typ = str(node.get("type") or "")
            if typ in {"url_citation", "citation"}:
                add(node.get("url"), node.get("title"))
            if isinstance(node.get("url"), str) and (
                "title" in node or "source" in typ or "citation" in typ
            ):
                add(node.get("url"), node.get("title"))
            web = node.get("web")
            if isinstance(web, dict):
                add(web.get("uri") or web.get("url"), web.get("title"))
            for key in ("sources", "annotations", "grounding_chunks", "groundingChunks", "output", "content", "action"):
                value = node.get(key)
                if value is not None:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return out[:12]


def _response_text(resp: Any) -> str:
    return str(getattr(resp, "text", "") or "")


def _response_content_type(resp: Any) -> str:
    headers = getattr(resp, "headers", {}) or {}
    try:
        return str(headers.get("content-type") or headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return ""


def _parse_responses_sse(text: str) -> dict[str, Any]:
    """把 Responses API 的 SSE 成功流折叠为普通 Responses JSON。

    部分 Codex/CLIProxyAPI 反代即使请求里带了 ``stream: false``，仍会返回
    ``text/event-stream``。这里优先使用 ``response.completed`` 里的完整响应；
    如果反代只给了文本增量，则退化为顶层 ``output_text``。
    """
    events: list[tuple[str, str]] = []
    event_name = "message"
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if data_lines:
            events.append((event_name, "\n".join(data_lines)))
        event_name = "message"
        data_lines = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip() or "message"
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    flush()

    delta_parts: list[str] = []
    done_text = ""
    last_response: dict[str, Any] | None = None
    error_payload: Any = None

    def text_from_stream() -> str:
        return (done_text or "".join(delta_parts)).strip()

    def with_stream_text(response: dict[str, Any]) -> dict[str, Any]:
        stream_text = text_from_stream()
        if not stream_text:
            return response
        image_data, image_urls, output_text = _extract_response_image_outputs(response)
        if output_text or image_data or image_urls:
            return response
        response = dict(response)
        response["output_text"] = stream_text
        return response

    for event_name, raw_data in events:
        if not raw_data or raw_data == "[DONE]":
            continue
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        payload_type = str(payload.get("type") or event_name or "")
        if payload_type in {"error", "response.error"}:
            error_payload = payload.get("error") or payload
            continue

        response = payload.get("response")
        if isinstance(response, dict):
            last_response = response
            if payload_type == "response.completed" or response.get("status") == "completed":
                return with_stream_text(response)

        if payload_type == "response.output_text.delta" and isinstance(payload.get("delta"), str):
            delta_parts.append(payload["delta"])
        elif payload_type == "response.output_text.done" and isinstance(payload.get("text"), str):
            done_text = payload["text"]

    if last_response and last_response.get("status") not in {"failed", "cancelled"}:
        image_data, image_urls, output_text = _extract_response_image_outputs(last_response)
        if output_text or image_data or image_urls:
            return last_response
    if delta_parts or done_text:
        return {"output_text": text_from_stream()}
    if error_payload is not None:
        raise ValueError(f"error event: {str(error_payload)[:200]}")
    raise ValueError("缺少 response.completed 或 output_text 增量事件")


def _decode_responses_payload(prefix: str, resp: Any, api_key: str | None) -> dict[str, Any]:
    content_type = _response_content_type(resp).lower()
    text = _response_text(resp)
    if "text/event-stream" in content_type or text.lstrip().startswith(("event:", "data:")):
        try:
            return _parse_responses_sse(text)
        except ValueError as exc:
            raise LLMError(
                _safe_error_message(f"{prefix} SSE 返回结构异常: {exc}", api_key)
            ) from None
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise _non_json_error(prefix, resp, exc, api_key) from None
    if not isinstance(data, dict):
        raise LLMError(f"{prefix} 返回结构异常: 顶层不是对象")
    return data


_RESPONSES_REMOVABLE_PARAMETERS = {
    "max_output_tokens": "max_output_tokens",
    "temperature": "temperature",
    "reasoning": "reasoning",
    "reasoning.effort": "reasoning",
    "stream": "stream",
}


def _unsupported_parameter_name(resp: Any) -> str | None:
    if int(getattr(resp, "status_code", 0) or 0) < 400:
        return None
    lowered = _response_text(resp).lower()
    if not (
        "unsupported parameter" in lowered
        or "unknown parameter" in lowered
        or "unrecognized parameter" in lowered
        or "invalid parameter" in lowered
    ):
        return None
    match = re.search(
        r"(?:unsupported|unknown|unrecognized|invalid)\s+parameter(?:s)?\s*[:=]?\s*[`'\"]?([a-z0-9_.-]+)",
        lowered,
    )
    if match:
        return match.group(1).strip("`'\" ")
    for parameter in _RESPONSES_REMOVABLE_PARAMETERS:
        if parameter in lowered:
            return parameter
    return None


def _is_unsupported_parameter(resp: Any, parameter: str) -> bool:
    return _unsupported_parameter_name(resp) == parameter.lower()


def _remove_unsupported_parameter(body: dict[str, Any], parameter: str) -> str | None:
    parameter = parameter.strip().lower()
    key = _RESPONSES_REMOVABLE_PARAMETERS.get(parameter)
    if key is None and "." in parameter:
        key = _RESPONSES_REMOVABLE_PARAMETERS.get(parameter.split(".", 1)[0])
    if key is None or key not in body:
        return None
    body.pop(key, None)
    return key


async def _post_responses_compatible(
    cli: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
) -> httpx.Response:
    current_body = dict(body)
    removed: set[str] = set()
    while True:
        resp = await cli.post(url, headers=headers, json=dict(current_body))
        parameter = _unsupported_parameter_name(resp)
        if not parameter:
            return resp
        removed_key = _remove_unsupported_parameter(current_body, parameter)
        if not removed_key or removed_key in removed:
            return resp
        removed.add(removed_key)


def _non_json_error(prefix: str, resp: Any, exc: json.JSONDecodeError, api_key: str | None) -> LLMError:
    content_type = _response_content_type(resp)
    status_code = int(getattr(resp, "status_code", 0) or 0)
    body = _response_text(resp).replace("\n", "\\n")[:200]
    if not body:
        body = "<empty>"
    return LLMError(
        _safe_error_message(
            f"{prefix} 返回非 JSON: status={status_code} content-type={content_type or 'unknown'} body={body} parse_error={exc}",
            api_key,
        )
    )


class LLMClient(ABC):
    """provider-agnostic 调用接口。"""

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """以 system + user 拼 prompt（可附图），返回回答与 token 统计。

        ``images`` 留空 = 纯文本路径（向后兼容老调用）；
        非空时各实现按自己的 vision 协议把图片塞进 user message 的 content 块里。
        """
        raise NotImplementedError

    async def stream_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Yield provider-native text deltas.

        Providers that have not wired a real streaming protocol must leave this
        explicit instead of emulating streaming from a completed response.
        """

        if False:
            yield LLMStreamChunk()
        raise NotImplementedError("当前 provider 协议尚未接入原生 streaming")

    async def transcribe(self, audio: bytes, model: str) -> str:
        """语音转写：把音频字节喂给 ``/audio/transcriptions`` 之类的 STT 端点。

        默认抛 ``NotImplementedError``——需要每个具体 client 自己实现（Anthropic 暂无）。

        ``model`` 由调用方指定（一般是 ``whisper-1``）；不复用 ``self._model``
        因为聊天模型与 STT 模型几乎总是不同的（gpt-4o-mini vs whisper-1）。
        """
        raise NotImplementedError(
            "本 provider 不支持语音转写（仅 OpenAI 兼容 /audio/transcriptions 端点支持）"
        )

    async def generate_image(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """原生图片生成入口。

        默认不支持；具体协议实现可返回 ``LLMResult.image_data`` 或
        ``LLMResult.image_urls``。参数列表刻意与 ``complete`` 对齐，方便
        fallback 调用层复用同一套 provider / retry / usage 管线。
        """
        raise NotImplementedError("当前 provider 协议尚未接入原生图片生成")


# ────────────────────────────────────────────────────────────
# OpenAI / OpenAI 兼容（含 Ollama）
# ────────────────────────────────────────────────────────────


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions 兼容协议。

    用 ``/v1/chat/completions`` 端点；Ollama (``/v1/chat/completions`` since 0.1.20+) 也走这里。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str,
        proxy_url: str | None = None,
    ):
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._model = model
        self._proxy_url = proxy_url

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        if web_search:
            raise LLMError("联网搜索需要使用 OpenAI Responses API（api_format=responses）")
        url = f"{self._base_url}/chat/completions"
        # Ollama 部署可能不需要 api_key；为空时不下发 Authorization 头
        headers = _llm_headers()
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # 视觉路径：content 改成数组，先 text 再 image_url（OpenAI / mimo / GLM-4V 均如此）
        if images:
            user_content: object = [
                {"type": "text", "text": user},
                *[
                    {"type": "image_url", "image_url": {"url": _to_data_url(img)}}
                    for img in images
                ],
            ]
        else:
            user_content = user
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning_effort"] = normalized_effort
        # httpx 0.28+ 用 proxy=<str> 单参数；socks5 需要 httpx[socks] 安装的 socksio
        # 当 proxy_url 为空时，显式 trust_env=False 避免 httpx 读取环境变量中的
        # HTTP_PROXY / NO_PROXY（NO_PROXY 含 ::1 会导致 httpx InvalidURL 崩溃）
        # 本地桥接（grok-bridge 等 localhost 服务）需要更长超时：浏览器 JS 执行 +
        # LLM 生成 + 图片 XHR 下载，整个过程可能超过 30 秒
        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            # 很多 httpx 异常 str() 是空（典型 SSL 握手 / ConnectError("")）；
            # 把异常类名 + 目标 host 也透出来，否则用户只看到 "网络异常: " 没法排查
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            # 不要把 api_key 回显到错误里；构造前先剥离
            raise LLMError(
                _safe_error_message(
                    f"OpenAI 接口返回 {resp.status_code}: {resp.text[:200]}{_hint_for_status(resp.status_code)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI 返回非 JSON: {exc}") from None

        # 标准 OpenAI 形态：choices[0].message.content
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"OpenAI 返回结构异常: {exc}") from None

        usage = data.get("usage") or {}
        raw_images = data.get("images") or []
        if not isinstance(raw_images, list):
            raw_images = []
        # 兼容两种格式：
        #   旧: ["url1", "url2"]（纯 URL 列表）
        #   新: [{"url": "url1", "data": "base64..."}, ...]（带 base64 数据）
        image_urls = []
        image_data = []
        for item in raw_images:
            if isinstance(item, dict):
                if item.get("url"):
                    image_urls.append(item["url"])
                if item.get("data"):
                    image_data.append(item["data"])
            elif isinstance(item, str):
                image_urls.append(item)
        return LLMResult(
            text=text.strip(),
            model=str(data.get("model", self._model)),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            image_urls=image_urls,
            image_data=image_data,
        )

    async def transcribe(self, audio: bytes, model: str) -> str:
        """OpenAI / 兼容厂商的 ``POST /audio/transcriptions``（Whisper 协议）。

        multipart/form-data 上传：``file=<bytes>``、``model=<id>``、可选 ``response_format=json``。
        返回 JSON ``{"text": "..."}``。
        """
        if not audio:
            raise LLMError("音频字节为空")
        if not model:
            raise LLMError("transcribe() 必须指定 model（如 'whisper-1'）")
        url = f"{self._base_url}/audio/transcriptions"
        headers = _llm_headers(content_type=None)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # 文件名给个通用后缀，让上游按二进制 audio 流处理
        files = {
            "file": ("audio.ogg", audio, "audio/ogg"),
        }
        data = {"model": model, "response_format": "json"}
        _is_local = "127.0.0.1" in self._base_url or "localhost" in self._base_url
        client_kwargs: dict[str, object] = {"timeout": _LOCAL_TIMEOUT if _is_local else _HTTP_TIMEOUT}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=headers, files=files, data=data)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"STT 接口返回 {resp.status_code}: {resp.text[:200]}{_hint_for_status(resp.status_code)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
            )
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"STT 返回非 JSON: {exc}") from None
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise LLMError(f"STT 返回缺少 text 字段：{str(payload)[:200]}")
        return text.strip()

    async def generate_image(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """OpenAI-compatible Images API: ``POST /images/generations``.

        这条路径适合把模板模型直接设为 ``gpt-image-*`` / ``dall-e-*`` 的
        Provider。若要用普通主模型配 ``image_generation`` 工具，请使用
        ``api_format=responses``，由 ``ResponsesClient.generate_image`` 处理。
        """
        if web_search:
            raise LLMError("图片生成不支持联网搜索，请关闭 web_search")
        if images:
            raise LLMError("当前 /images/generations 路径暂不支持参考图；请改用 api_format=responses 的 Provider")

        url = f"{self._base_url}/images/generations"
        headers = _llm_headers()
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        prompt = user.strip()
        if system.strip():
            prompt = f"{system.strip()}\n\n用户需求：{prompt}"
        body = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
        }

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"Images 接口返回 {resp.status_code}: {resp.text[:200]}{_hint_for_status(resp.status_code)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"Images 返回非 JSON: {exc}") from None

        image_data: list[str] = []
        image_urls: list[str] = []
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json") or item.get("base64") or item.get("data")
            if isinstance(b64, str) and b64.strip():
                image_data.append(_normalize_image_data_uri(b64.strip()))
            url_value = item.get("url")
            if isinstance(url_value, str) and url_value.strip():
                image_urls.append(url_value.strip())
        if not image_data and not image_urls:
            raise LLMError(f"Images 返回中没有图片数据：{str(data)[:200]}")

        usage = data.get("usage") or {}
        return LLMResult(
            text="",
            model=str(data.get("model") or self._model),
            input_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            image_urls=image_urls,
            image_data=image_data,
        )


# ────────────────────────────────────────────────────────────
# Anthropic Messages API
# ────────────────────────────────────────────────────────────


class AnthropicClient(LLMClient):
    """Anthropic ``/v1/messages`` 协议（Claude 系列）。"""

    # 文档要求的版本头；新版本兼容旧调用
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str,
        proxy_url: str | None = None,
    ):
        self._api_key = api_key
        self._base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self._model = model
        self._proxy_url = proxy_url

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        if web_search:
            raise LLMError("当前 Anthropic 调用路径尚未接入联网搜索；请使用 OpenAI Responses provider")
        url = f"{self._base_url}/messages"
        headers = {
            "User-Agent": _LLM_USER_AGENT,
            "x-api-key": self._api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            # 模拟 Claude 客户端的关键 headers
            # Anyrouter 等反代强制校验 anthropic-beta 含 context-1m-2025-08-07
            "anthropic-beta": "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,effort-2025-11-24",
            "anthropic-dangerous-direct-browser-access": "true",
            "x-app": "cli",
        }
        # 视觉路径：Anthropic 用 {"type":"image","source":{"type":"base64",...}}
        # 与 OpenAI 的 image_url 协议**不一样**，要分别构造
        if images:
            user_content: object = [
                *[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _sniff_image_mime(img),
                            "data": base64.b64encode(img).decode("ascii"),
                        },
                    }
                    for img in images
                ],
                {"type": "text", "text": user},
            ]
        else:
            user_content = user
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            # 使用流式（SSE）模式；Anyrouter 等 Claude Code 反代依赖流式协议分发
            "stream": True,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        # ── SSE 流式响应解析 ──────────────────────────────
        # 事件流生命周期：
        #   message_start → content_block_start → content_block_delta(×N)
        #   → content_block_stop → message_delta → message_stop
        #
        # 我们只需要：
        #   - message_start.message.model / .usage  → 模型名 + input_tokens
        #   - content_block_delta.delta.text         → 文本增量
        #   - message_delta.usage.output_tokens      → output_tokens
        text_parts: list[str] = []
        model_name = self._model
        input_tokens = 0
        output_tokens = 0

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        # 流式模式下，错误仍然可能作为普通 JSON 返回
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Anthropic 接口返回 {resp.status_code}: {error_body[:200]}{_hint_for_status(resp.status_code)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                        )
                    # 逐行解析 SSE 事件
                    current_event = ""
                    async for line in resp.aiter_lines():
                        line = line.rstrip("\r\n")
                        if line.startswith("event: "):
                            current_event = line[7:].strip()
                            continue
                        if line.startswith("data: "):
                            raw = line[6:]
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if current_event == "message_start":
                                msg = payload.get("message") or {}
                                model_name = str(msg.get("model", self._model))
                                usage = msg.get("usage") or {}
                                input_tokens = int(usage.get("input_tokens") or 0)
                            elif current_event == "content_block_delta":
                                delta = payload.get("delta") or {}
                                if delta.get("type") == "text_delta":
                                    text_parts.append(delta.get("text", ""))
                            elif current_event == "message_delta":
                                usage = payload.get("usage") or {}
                                output_tokens = int(usage.get("output_tokens") or 0)
                            # message_stop / content_block_start / content_block_stop → 忽略
                            continue
                        # 空行 = 事件分隔符（SSE 规范）
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        text = "".join(text_parts).strip()
        if not text:
            raise LLMError("Anthropic 返回空内容（SSE 流中未收到 text_delta 事件）")

        return LLMResult(
            text=text,
            model=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def stream_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        if web_search:
            raise LLMError("当前 Anthropic streaming 尚未接入联网搜索；请使用 OpenAI Responses provider")
        url = f"{self._base_url}/messages"
        headers = {
            "User-Agent": _LLM_USER_AGENT,
            "x-api-key": self._api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            "anthropic-beta": "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,effort-2025-11-24",
            "anthropic-dangerous-direct-browser-access": "true",
            "x-app": "cli",
        }
        if images:
            user_content: object = [
                *[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _sniff_image_mime(img),
                            "data": base64.b64encode(img).decode("ascii"),
                        },
                    }
                    for img in images
                ],
                {"type": "text", "text": user},
            ]
        else:
            user_content = user
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "stream": True,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        model_name = self._model
        input_tokens = 0
        output_tokens = 0
        final_sent = False

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Anthropic streaming 接口返回 {resp.status_code}: {error_body[:200]}{_hint_for_status(resp.status_code)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                        )

                    current_event = ""
                    async for line in resp.aiter_lines():
                        line = line.rstrip("\r\n")
                        if line.startswith("event: "):
                            current_event = line[7:].strip()
                            continue
                        if line.startswith("data: "):
                            raw = line[6:]
                            if raw == "[DONE]":
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if current_event == "message_start":
                                msg = payload.get("message") or {}
                                model_name = str(msg.get("model", self._model))
                                usage = msg.get("usage") or {}
                                input_tokens = int(usage.get("input_tokens") or 0)
                            elif current_event == "content_block_delta":
                                delta = payload.get("delta") or {}
                                if delta.get("type") == "text_delta":
                                    text = delta.get("text")
                                    if isinstance(text, str) and text:
                                        yield LLMStreamChunk(delta=text, model=model_name)
                            elif current_event == "message_delta":
                                usage = payload.get("usage") or {}
                                output_tokens = int(usage.get("output_tokens") or 0)
                            elif current_event == "error":
                                error = payload.get("error") or payload
                                raise LLMError(
                                    _safe_error_message(
                                        f"Anthropic streaming 返回错误事件: {str(error)[:200]}",
                                        self._api_key,
                                    )
                                )
                            elif current_event == "message_stop":
                                final_sent = True
                                yield LLMStreamChunk(
                                    model=model_name,
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    done=True,
                                )
                                return
                            continue
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        if not final_sent:
            yield LLMStreamChunk(
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                done=True,
            )


# ────────────────────────────────────────────────────────────
# OpenAI Responses API（POST /responses，2024 出的新协议）
# ────────────────────────────────────────────────────────────


class ResponsesClient(LLMClient):
    """OpenAI Responses API（POST ``/responses``）。

    与 chat/completions 的差异：
    - 入参 ``input=[{role, content}]`` + ``instructions`` + ``model`` + ``max_output_tokens``
    - 出参 ``output=[{type:"message", content:[{type:"output_text", text:"..."}]}]``
      也可能直接给 ``output_text`` 顶层字符串（不同实现略有差异，都做兼容）
    - usage 字段是 ``input_tokens`` / ``output_tokens``（不是 prompt_tokens / completion_tokens）

    很多国内 OpenAI 兼容反代（如 anyrouter）只接 ``/responses`` 不接 ``/chat/completions``，
    所以这条 client 是必须的。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str,
        proxy_url: str | None = None,
    ):
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._model = model
        self._proxy_url = proxy_url

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        url = f"{self._base_url}/responses"
        headers = _llm_headers(accept="application/json")
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # 视觉路径：Responses API 的 content 是 [{"type":"input_text"}, {"type":"input_image"}]
        # （注意：不是 chat/completions 的 image_url 名字；OpenAI 把这两套协议命名拆开了）
        if images:
            input_content: object = [
                {"type": "input_text", "text": user},
                *[
                    {"type": "input_image", "image_url": _to_data_url(img)}
                    for img in images
                ],
            ]
        else:
            input_content = user
        body = {
            "model": self._model,
            # 用 instructions 字段传 system；input 列表按 role/content 给 user 输入
            "instructions": system,
            "input": [
                {"role": "user", "content": input_content},
            ],
            # Responses API 用 max_output_tokens（不是 max_tokens）
            "max_output_tokens": max_tokens,
            "stream": False,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning"] = {"effort": normalized_effort}
        if web_search:
            size = (web_search_context_size or "medium").lower()
            if size not in {"low", "medium", "high"}:
                size = "medium"
            body["tools"] = [{"type": "web_search", "search_context_size": size}]
            body["include"] = ["web_search_call.action.sources"]

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await _post_responses_compatible(cli, url, headers=headers, body=body)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"Responses 接口返回 {resp.status_code}: {_response_text(resp)[:200]}{_hint_for_status(resp.status_code)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
            )

        data = _decode_responses_payload("Responses", resp, self._api_key)

        # 解析 output：兼容多种形态
        # 形态 1：data["output_text"] = "..."（部分实现的便利字段）
        # 形态 2：data["output"] = [{"type":"message","content":[{"type":"output_text","text":"..."}]}]
        text = ""
        ot = data.get("output_text")
        if isinstance(ot, str):
            text = ot
        else:
            output_list = data.get("output") or []
            text_parts: list[str] = []
            for item in output_list if isinstance(output_list, list) else []:
                if not isinstance(item, dict):
                    continue
                content = item.get("content") or []
                if isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        t = c.get("text")
                        # type 通常是 output_text；保险起见全收
                        if isinstance(t, str):
                            text_parts.append(t)
            text = "".join(text_parts)

        # usage：input_tokens / output_tokens
        usage = data.get("usage") or {}
        return LLMResult(
            text=text.strip(),
            model=str(data.get("model", self._model)),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            sources=_extract_response_sources(data),
        )

    async def stream_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        url = f"{self._base_url}/responses"
        headers = _llm_headers(accept="text/event-stream")
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if images:
            input_content: object = [
                {"type": "input_text", "text": user},
                *[
                    {"type": "input_image", "image_url": _to_data_url(img)}
                    for img in images
                ],
            ]
        else:
            input_content = user
        body = {
            "model": self._model,
            "instructions": system,
            "input": [
                {"role": "user", "content": input_content},
            ],
            "max_output_tokens": max_tokens,
            "stream": True,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning"] = {"effort": normalized_effort}
        if web_search:
            size = (web_search_context_size or "medium").lower()
            if size not in {"low", "medium", "high"}:
                size = "medium"
            body["tools"] = [{"type": "web_search", "search_context_size": size}]
            body["include"] = ["web_search_call.action.sources"]

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        model_name = self._model
        input_tokens = 0
        output_tokens = 0
        final_sent = False

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Responses streaming 接口返回 {resp.status_code}: {error_body[:200]}{_hint_for_status(resp.status_code)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                        )

                    current_event = ""
                    async for line in resp.aiter_lines():
                        line = line.rstrip("\r\n")
                        if line.startswith("event:"):
                            current_event = line.removeprefix("event:").strip()
                            continue
                        if line.startswith("data:"):
                            raw = line.removeprefix("data:").strip()
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            payload_type = str(payload.get("type") or current_event or "")
                            if payload_type in {"error", "response.error"}:
                                error = payload.get("error") or payload
                                raise LLMError(
                                    _safe_error_message(
                                        f"Responses streaming 返回错误事件: {str(error)[:200]}",
                                        self._api_key,
                                    )
                                )
                            response = payload.get("response")
                            if isinstance(response, dict):
                                model_name = str(response.get("model") or model_name)
                                usage = response.get("usage") or {}
                                input_tokens = int(usage.get("input_tokens") or input_tokens or 0)
                                output_tokens = int(usage.get("output_tokens") or output_tokens or 0)
                                status = str(response.get("status") or "")
                                if payload_type == "response.completed" or status == "completed":
                                    final_sent = True
                                    yield LLMStreamChunk(
                                        model=model_name,
                                        input_tokens=input_tokens,
                                        output_tokens=output_tokens,
                                        done=True,
                                    )
                                    return
                                if status in {"failed", "cancelled"}:
                                    raise LLMError(
                                        _safe_error_message(
                                            f"Responses streaming 结束状态异常: {status}",
                                            self._api_key,
                                        )
                                    )
                            if payload_type == "response.output_text.delta":
                                delta = payload.get("delta")
                                if isinstance(delta, str) and delta:
                                    yield LLMStreamChunk(delta=delta, model=model_name)
                            elif payload_type == "response.output_text.done":
                                text = payload.get("text")
                                if isinstance(text, str) and text and not final_sent:
                                    # done 事件只用于兼容不发送 completed 的反代；不重复输出文本。
                                    continue
                            continue
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        if not final_sent:
            yield LLMStreamChunk(
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                done=True,
            )

    async def transcribe(self, audio: bytes, model: str) -> str:
        """OpenAI Responses 协议厂商一般也在同一个 base_url 下挂 ``/audio/transcriptions``——
        直接复用 Whisper 协议（与 ``OpenAIClient.transcribe`` 同实现）。"""
        # 复用 OpenAIClient 的 transcribe；二者只差 chat/responses 那条主 endpoint
        return await OpenAIClient.transcribe(self, audio, model)  # type: ignore[arg-type]

    async def generate_image(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """Responses API image_generation tool: ``POST /responses``.

        这条路径适合普通主模型（例如 gpt-5.x）调用原生图片生成工具。无参考图
        时显式 ``action=generate``，有参考图时交给 ``auto``，让上游决定生成或编辑。
        """
        if web_search:
            raise LLMError("图片生成不支持联网搜索，请关闭 web_search")
        url = f"{self._base_url}/responses"
        headers = _llm_headers(accept="application/json")
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        if images:
            input_content: object = [
                {"type": "input_text", "text": user},
                *[
                    {"type": "input_image", "image_url": _to_data_url(img)}
                    for img in images
                ],
            ]
        else:
            input_content = user

        image_tool: dict[str, Any] = {"type": "image_generation"}
        if images:
            image_tool["action"] = "auto"
        else:
            image_tool["action"] = "generate"

        body = {
            "model": self._model,
            "instructions": system,
            "input": [
                {"role": "user", "content": input_content},
            ],
            "tools": [image_tool],
            "tool_choice": {"type": "image_generation"},
            "max_output_tokens": max_tokens,
            "stream": False,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning"] = {"effort": normalized_effort}

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await _post_responses_compatible(cli, url, headers=headers, body=body)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"Responses 生图接口返回 {resp.status_code}: {_response_text(resp)[:200]}{_hint_for_status(resp.status_code)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
            )
        data = _decode_responses_payload("Responses 生图", resp, self._api_key)

        image_data, image_urls, output_text = _extract_response_image_outputs(data)
        if not image_data and not image_urls:
            hint = output_text or str(data)[:200]
            raise LLMError(f"Responses 生图返回中没有图片数据：{hint}")
        usage = data.get("usage") or {}
        return LLMResult(
            text=output_text,
            model=str(data.get("model") or self._model),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            image_urls=image_urls,
            image_data=image_data,
            sources=_extract_response_sources(data),
        )


# ────────────────────────────────────────────────────────────
# 工厂 & 安全工具
# ────────────────────────────────────────────────────────────


class LLMError(Exception):
    """LLM 调用层统一异常；message 已脱敏。"""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable  # 是否可重试（timeout/429/5xx/网络错误）


class LLMCallFailed(Exception):
    """LLM 调用失败（捕获后用于 fallback 决策）。"""

    def __init__(
        self,
        message: str,
        provider_id: int | None = None,
        provider_name: str | None = None,
        error_type: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.error_type = error_type  # "timeout" / "network" / "rate_limit" / "auth" / "server_error"
        self.status_code = status_code
        self.retryable = retryable


def _is_retryable_status(status_code: int) -> bool:
    """HTTP 状态码是否值得重试：429 限流与 5xx 服务端错误可重试，4xx 认证/配置类不可重试。"""
    return status_code == 429 or 500 <= status_code < 600


def _safe_error_message(msg: str, api_key: str | None) -> str:
    """把可能含敏感信息的错误文本脱敏。

    - 若 api_key 出现在 msg 中，整段替换为 ``<redacted>``
    - 兜底过滤 ``sk-...`` / ``Bearer ...`` 形态
    - 过滤其他常见 token 格式
    """
    import re

    if not msg:
        return ""
    out = msg
    if api_key:
        out = out.replace(api_key, "<redacted>")
    # 统一截断，避免长串敏感数据透出
    if len(out) > 400:
        out = out[:400] + "..."
    # 正则过滤常见 token 格式（独立于 api_key 变量）
    # sk- 开头的 key
    out = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "<sk>", out)
    # Bearer token
    out = re.sub(r"Bearer\s+[A-Za-z0-9_.\-]{8,}", "Bearer <token>", out)
    # 常见的其他 key 格式
    out = re.sub(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"]?[A-Za-z0-9_.\-]{8,}['\"]?", r"\1=<redacted>", out)
    return out


# Cloudflare 5xx 错误码的人话翻译（用户最常碰到 520，且不是应用问题）
_CF_5XX_HINTS: dict[int, str] = {
    520: "上游返回异常（Cloudflare 520 = 反代连不上目标 / 上游崩了；不是本项目代码问题）",
    521: "上游服务器拒绝连接（Cloudflare 521）",
    522: "上游连接超时（Cloudflare 522）",
    523: "上游不可达（Cloudflare 523）",
    524: "上游处理超时（Cloudflare 524；常见于慢模型 + 反代严格超时）",
    525: "SSL 握手失败（Cloudflare 525）",
    526: "SSL 证书无效（Cloudflare 526）",
}


def _hint_for_status(status: int) -> str:
    """根据 HTTP 状态码给一句人话提示，便于用户区分"我配错了"还是"反代/上游挂了"。"""
    if status in _CF_5XX_HINTS:
        return f"  ↳ {_CF_5XX_HINTS[status]}"
    if status == 401 or status == 403:
        return "  ↳ api_key 无效 / 权限不够"
    if status == 404:
        return "  ↳ model 名不对 / 端点不存在；试试 Fetch 模型列表选一条已支持的"
    if status == 429:
        return "  ↳ 限流，等会儿再试 / 或换一条不那么紧的反代"
    if 500 <= status < 600:
        return "  ↳ 服务器侧错误（不是 api_key / model 问题）"
    return ""


def _describe_http_error(exc: BaseException, base_url: str | None) -> str:
    """把 httpx 异常翻译成"用户能看懂的报错"。

    httpx 很多异常的 ``str(exc)`` 是空字符串（``ConnectError("")`` / SSL 握手错），
    单纯透 ``f"网络异常: {exc}"`` 会变成 "网络异常: " 难以排查。这里：

    - 总带上异常类名：``ConnectError`` / ``ReadTimeout`` / ``ProxyError`` / ``SSLError`` 等
    - 总带上目标 host（不带路径）：让用户一眼看出是 anthropic.com 还是 openai.com 不通
    - 细节为空时给一个建议性提示（"可能是 SSL/DNS/代理"）
    """
    name = type(exc).__name__
    detail = str(exc).strip()
    host = ""
    if base_url:
        try:
            from urllib.parse import urlparse

            host = urlparse(base_url).netloc or base_url
        except Exception:  # noqa: BLE001
            host = base_url

    parts = [f"网络异常 {name}"]
    if host:
        parts.append(f"→ {host}")
    if detail:
        parts.append(f": {detail}")
    else:
        parts.append("（无详情；常见原因：连不到目标域名 / SSL 握手失败 / 代理未生效）")
    return " ".join(parts)


def build_client(
    provider_row: LLMProvider,
    override_model: str | None = None,
    proxy_url: str | None = None,
    api_format_override: str | None = None,
) -> LLMClient:
    """根据 ORM 行装配具体 LLMClient。

    协议路由（以 ``api_format`` 为准；老数据没这字段时按 ``provider`` 厂商兜底）：
    - ``chat_completions``     → ``OpenAIClient``        ``POST /chat/completions``
    - ``responses``            → ``ResponsesClient``     ``POST /responses``
    - ``anthropic_messages``   → ``AnthropicClient``     ``POST /messages``

    - 解密 api_key（若该 provider 行没有 key 字段则 client 拿空串）
    - ``override_model`` 优先于 provider.default_model
    - ``proxy_url`` 给 None 表示直连；socks5/http/https 都接受 httpx URL
    """
    api_key = ""
    if provider_row.api_key_enc:
        api_key = decrypt_str(provider_row.api_key_enc)
    model = (override_model or provider_row.default_model or "").strip()
    if not model:
        raise ValueError("LLM provider 没配 default_model，且当次调用也未提供 model 覆盖")

    # api_format_override 用于联网搜索等单次调用协议覆盖；否则按 provider 配置。
    fmt = (
        api_format_override
        or getattr(provider_row, "api_format", None)
        or default_api_format_for(provider_row.provider)
    )

    if fmt == LLM_API_FORMAT_CHAT_COMPLETIONS:
        # ollama 兜底 base_url（chat_completions 也兼容）
        base = provider_row.base_url
        if not base and provider_row.provider == LLM_PROVIDER_OLLAMA:
            base = "http://localhost:11434/v1"
        return OpenAIClient(
            api_key="" if provider_row.provider == LLM_PROVIDER_OLLAMA else api_key,
            base_url=base,
            model=model,
            proxy_url=proxy_url,
        )
    if fmt == LLM_API_FORMAT_RESPONSES:
        return ResponsesClient(
            api_key=api_key, base_url=provider_row.base_url, model=model, proxy_url=proxy_url
        )
    if fmt == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        return AnthropicClient(
            api_key=api_key, base_url=provider_row.base_url, model=model, proxy_url=proxy_url
        )
    raise ValueError(f"未知 api_format: {fmt}")


def build_client_from_dto(
    dto: LLMProviderDTO,
    override_model: str | None = None,
    proxy_url: str | None = None,
    api_format_override: str | None = None,
) -> LLMClient:
    """根据 LLMProviderDTO 装配具体 LLMClient。

    与 build_client 等效，但输入是 DTO 而非 ORM 行。
    proxy_url 以参数传入优先，其次用 dto.proxy_url。

    Args:
        dto: LLMProviderDTO 对象
        override_model: 覆盖模型名（优先于 dto.default_model）
        proxy_url: 代理 URL（优先于 dto.proxy_url）
    """
    api_key = ""
    if dto.api_key_enc:
        api_key = decrypt_str(dto.api_key_enc)
    model = (override_model or dto.default_model or "").strip()
    if not model:
        raise ValueError("LLM provider 没配 default_model，且当次调用也未提供 model 覆盖")

    # api_format_override 用于联网搜索等单次调用协议覆盖；否则按 provider 配置。
    fmt = api_format_override or dto.api_format or default_api_format_for(dto.provider)

    # proxy 合并：参数传入 > dto 内置
    final_proxy = proxy_url if proxy_url else dto.proxy_url

    if fmt == LLM_API_FORMAT_CHAT_COMPLETIONS:
        base = dto.base_url
        if not base and dto.is_ollama:
            base = "http://localhost:11434/v1"
        return OpenAIClient(
            api_key="" if dto.is_ollama else api_key,
            base_url=base,
            model=model,
            proxy_url=final_proxy,
        )
    if fmt == LLM_API_FORMAT_RESPONSES:
        return ResponsesClient(
            api_key=api_key, base_url=dto.base_url, model=model, proxy_url=final_proxy
        )
    if fmt == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        return AnthropicClient(
            api_key=api_key, base_url=dto.base_url, model=model, proxy_url=final_proxy
        )
    raise ValueError(f"未知 api_format: {fmt}")


__all__ = [
    "AnthropicClient",
    "LLMCallFailed",
    "LLMClient",
    "LLMError",
    "LLMResult",
    "LLMStreamChunk",
    "OpenAIClient",
    "ResponsesClient",
    "build_client",
    "build_client_from_dto",
]

"""AI runtime extracted from worker command dispatch."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..services.llm_invoke import resolved_api_format_for_call

log = logging.getLogger(__name__)

_LONG_MESSAGE_THRESHOLD = 3900


def _format_llm_sources(sources: Any) -> str:
    """把 LLMResult.sources 转成模板可直接展示的纯文本来源列表。"""
    if not isinstance(sources, list) or not sources:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for item in sources:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
        else:
            url = str(item or "").strip()
            title = ""
        if not url or url in seen:
            continue
        seen.add(url)
        label = title or url
        lines.append(f"{len(lines) + 1}. {label}\n{url}")
        if len(lines) >= 8:
            break
    return "\n".join(lines)


def _api_format_render_context(provider: dict[str, Any], *, web_search: bool) -> dict[str, Any]:
    """Expose configured/effective API protocol fields for output templates."""
    from ..services.llm_dto import LLMProviderDTO

    dto = LLMProviderDTO.from_dict(provider)
    configured = (dto.api_format or "").strip() or "chat_completions"
    web_search_format = (dto.web_search_api_format or "").strip() or "auto"
    effective = resolved_api_format_for_call(dto, web_search=web_search)
    endpoint_map = {
        "chat_completions": "/chat/completions",
        "responses": "/responses",
        "anthropic_messages": "/messages",
    }
    return {
        "api_format": effective,
        "configured_api_format": configured,
        "web_search_api_format": web_search_format,
        "api_protocol": effective,
        "endpoint": endpoint_map.get(effective, effective),
        "web_search": "true" if web_search else "",
    }


def _model_display_name(model_id: str, provider: dict[str, Any]) -> str:
    """优先使用 provider.models[].label；没有 label 时把常见小写 ID 转成更适合展示的名称。"""
    model_id = str(model_id or "").strip()
    if not model_id:
        return ""
    for item in provider.get("models") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() != model_id:
            continue
        label = str(item.get("label") or "").strip()
        if label:
            return label
        break
    return _prettify_model_id(model_id)


def _prettify_model_id(model_id: str) -> str:
    words = model_id.replace("_", "-").split("-")
    pretty: list[str] = []
    idx = 0
    while idx < len(words):
        word = words[idx]
        lower = word.lower()
        if lower in {"gpt", "chatgpt"}:
            if idx + 1 < len(words):
                pretty.append(f"{lower.upper()}-{words[idx + 1]}")
                idx += 2
                continue
            pretty.append(lower.upper())
        elif lower in {"o1", "o3", "o4", "o4mini"}:
            pretty.append(lower.upper())
        elif lower == "deepseek":
            pretty.append("DeepSeek")
        elif lower in {"api", "tts", "stt"}:
            pretty.append(lower.upper())
        elif lower in {"v1", "v2", "v3", "v4", "v5"}:
            pretty.append(lower)
        elif lower.isdigit() and pretty and idx + 1 < len(words) and words[idx + 1].isdigit():
            pretty[-1] = f"{pretty[-1]} {lower}.{words[idx + 1]}"
            idx += 2
            continue
        elif lower:
            pretty.append(lower[:1].upper() + lower[1:])
        idx += 1
    return " ".join(pretty) or model_id


def _optional_float(value: Any, *, min_value: float, max_value: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < min_value or parsed > max_value:
        return None
    return parsed


def _optional_int(value: Any, *, min_value: int, max_value: int) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < min_value or parsed > max_value:
        return None
    return parsed


def _optional_reasoning_effort(value: Any) -> str | None:
    effort = str(value or "").strip().lower()
    return effort if effort in {"minimal", "low", "medium", "high", "xhigh"} else None


@dataclass(slots=True)
class _LLMCallOutcome:
    result: Any
    provider_dict: dict[str, Any]
    routing_note: str | None
    web_search: bool


@dataclass(slots=True)
class _LLMRequestSettings:
    system: str
    max_tokens: int
    temperature: float | None
    reasoning_effort: str | None
    timeout_seconds: int | None
    override_model: str | None
    routed_model: str | None = None


def _build_llm_request_settings(
    *,
    cfg: dict[str, Any],
    native_image_mode: bool,
    inline_model_override: str | None,
    inline_provider_override: int | None,
    routed_model: str | None = None,
) -> _LLMRequestSettings:
    # 普通 chat/search/vision 永远附加反幻觉约束；原生生图路径不附加，
    # 否则会把"只描述真实图像"之类的识图约束混进生成提示词。
    default_image_system = (
        "你是 TelePilot 的图片生成助手。用户会给出图片需求，你应尽可能调用当前"
        "模型或提供商的原生图片生成能力直接生成图片。优先保留用户原始创意，"
        "并补全必要的光线、构图、材质、色彩、镜头和氛围细节，让画面清晰、"
        "主体明确、审美稳定。"
    )
    base_system = cfg.get("system_prompt") or (
        default_image_system if native_image_mode else "你是简洁有用的中文助手。回答控制在 100 字内。"
    )
    anti_hallucination = (
        "\n\n[严格规则]\n"
        "1. 当且仅当 user 输入包含真实图像数据时，才描述图像。\n"
        "2. 如果 user 输入只有 [图片] / 📷 等占位符而无真实图像数据，"
        "必须直接回答\"未收到图像数据，无法识别\"，绝对禁止臆测、编造或推断图像内容。\n"
        "3. 同样禁止仅凭 user 提问中出现的关键词（如\"这是 X 的封面\"）就肯定它是 X。"
    )
    system = base_system if native_image_mode else base_system + anti_hallucination

    # 决策 model 优先级（阶段 F 收口 #6：区分"用户固定"与"auto 路由"）：
    #   1. inline @name:model 显式指定 → 用户固定 override_model（所有 provider 都用）
    #   2. inline @name（未指定 model）→ 清空 override，用 provider.default_model
    #   3. 模板固定了 cfg.model → 用户固定 override_model
    #   4. 否则 auto 路由选出的 routed_model → 只作为"建议模型"，fallback 时每个
    #      provider 按自身 enabled 集重选（不作为全局 override）。
    effective_override: str | None = None
    effective_routed: str | None = None
    if inline_model_override:
        effective_override = inline_model_override
    elif inline_provider_override is not None:
        effective_override = None
    elif cfg.get("model"):
        effective_override = cfg.get("model")
    else:
        effective_routed = routed_model

    return _LLMRequestSettings(
        system=system,
        max_tokens=int(cfg.get("max_tokens") or 512),
        temperature=_optional_float(cfg.get("temperature"), min_value=0.0, max_value=2.0),
        reasoning_effort=_optional_reasoning_effort(cfg.get("reasoning_effort")),
        timeout_seconds=_optional_int(cfg.get("timeout_seconds"), min_value=5, max_value=600),
        override_model=effective_override,
        routed_model=effective_routed,
    )


def _build_user_prompt(
    *,
    replied: Any,
    replied_text: str | None,
    user_q: str,
    quote: bool,
    image_bytes_list: list[bytes],
    transcribed_text: str | None,
) -> str:
    # 注意：当我们已经把图片字节单独传给 LLM 时，``replied_text`` 里的"📷 [图片]"占位符
    # 就**不要**再往 prompt 里塞了——否则模型会把占位符当成"用户在问一个看不见的图"
    # 反而触发"我看不到这张图，请描述"那种回答，反幻觉本意是想避免的恰恰这种。
    quoted_for_prompt = replied_text
    if image_bytes_list and replied_text is not None:
        # 用户没单独打字时 replied.text 是空，``replied_text`` 来自占位符 "📷 [图片]"——
        # 这种情况下从 prompt 里去掉，让模型自然把图片当作 user 输入的一部分回答
        original_text = (replied.text or replied.message or "") if replied is not None else ""
        if not original_text:
            quoted_for_prompt = None  # 占位符，不喂给模型
    # 转写文本同理：从 prompt 里替换占位符"🎤 [语音]"为真实转写
    if transcribed_text and replied_text is not None:
        original_text = (replied.text or replied.message or "") if replied is not None else ""
        if not original_text:
            # 把"🎤 [语音]"占位符替换为带[转写]标签的真文本
            quoted_for_prompt = f"[语音转写]\n{transcribed_text}"
    elif transcribed_text and replied_text is None:
        # self-msg 含语音、replied 为空——把转写直接塞进 prompt
        quoted_for_prompt = f"[语音转写]\n{transcribed_text}"

    if quote and quoted_for_prompt:
        return f"[原文]\n{quoted_for_prompt}\n\n[问题]\n{user_q or '解释/总结'}"
    if image_bytes_list:
        return user_q or (
            "请分别描述每张图。" if len(image_bytes_list) > 1 else "请描述这张图。"
        )
    if transcribed_text:
        return user_q or f"[语音转写]\n{transcribed_text}"
    return user_q or "请简要总结你能想到的内容"


def _build_provider_dtos(
    providers: dict[int, dict[str, Any]],
    *,
    image_bytes_list: list[bytes],
) -> dict[int, Any]:
    from ..services.llm_dto import LLMProviderDTO

    provider_dtos: dict[int, LLMProviderDTO] = {}
    for pid, raw_provider in (providers or {}).items():
        try:
            data = dict(raw_provider)
            data["id"] = int(data.get("id") or pid)
            dto = LLMProviderDTO.from_dict(data)
            if image_bytes_list and dto.modality.lower() not in ("vision", "multimodal"):
                continue
            provider_dtos[dto.id] = dto
        except Exception:  # noqa: BLE001
            continue
    return provider_dtos


async def _call_llm_provider(
    *,
    event: Any,
    cfg: dict[str, Any],
    tpl: dict[str, Any],
    account_id: int,
    triggered_by_account_id: int | None,
    ctx_providers: dict[int, dict[str, Any]],
    provider_dto: Any,
    provider_dtos: dict[int, Any],
    provider_dict: dict[str, Any],
    system: str,
    user_msg: str,
    override_model: str | None,
    routed_model: str | None = None,
    max_tokens: int,
    image_bytes_list: list[bytes],
    native_image_mode: bool,
    routing_mode: str,
    provider_id: Any,
    routing_matched_tag: str | None,
    routing_note: str | None,
    temperature: float | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
    stream: bool = False,
) -> _LLMCallOutcome | None:
    # B2 will replace this non-streaming call path; B1 keeps invoke's external
    # contract unchanged and always reaches this helper with stream=False.
    if stream:
        raise NotImplementedError("worker ai_runtime.invoke does not stream yet")

    from ..services.llm_client import LLMCallFailed, LLMError
    from ..services.llm_invoke import invoke as invoke_ai_runtime
    from .command import _humanize_llm_error

    fallback_provider_id_raw = cfg.get("routing_fallback_provider_id")
    if routing_mode == "auto":
        fallback_provider_id_raw = cfg.get("routing_fallback_provider_id") or provider_id
    try:
        fallback_provider_id = int(fallback_provider_id_raw) if fallback_provider_id_raw else None
    except (TypeError, ValueError):
        fallback_provider_id = None

    try:
        web_search = bool(cfg.get("web_search", False))
        web_search_context_size = str(cfg.get("web_search_context_size") or "medium")
        result, used_provider_dto, used_fallback = await invoke_ai_runtime(
            provider_dto,
            provider_dtos,
            system,
            user_msg,
            override_model=override_model,
            routed_model=routed_model,
            max_tokens=max_tokens,
            images=image_bytes_list or None,
            web_search=web_search,
            web_search_context_size=web_search_context_size,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            native_image=native_image_mode,
            account_id=account_id,
            # TODO(interactive-bot): 由上游交互 Bot 入口写入真实 trigger account id。
            triggered_by_account_id=triggered_by_account_id,
            source=f"command:{tpl.get('name') or 'ai'}",
            fallback_provider_id=fallback_provider_id,
            matched_tag=routing_matched_tag,
        )
        if used_provider_dto.id != provider_dto.id:
            provider_dict = ctx_providers.get(used_provider_dto.id) or used_provider_dto.to_dict()
            fb_note = f"fallback → @{used_provider_dto.name or used_provider_dto.id}"
            routing_note = f"{routing_note} · {fb_note}" if routing_note else fb_note
        return _LLMCallOutcome(
            result=result,
            provider_dict=provider_dict,
            routing_note=routing_note,
            web_search=web_search,
        )
    except LLMCallFailed as e:
        err_msg = _humanize_llm_error(e)
        if e.provider_name:
            err_msg = f"[{e.provider_name}] {err_msg}"
        await event.edit(f"✗ AI 调用失败：{err_msg}")
        return None
    except LLMError as e:
        await event.edit(f"✗ AI 调用失败：{_humanize_llm_error(e)}")
        return None
    except Exception as e:  # noqa: BLE001
        await event.edit(f"✗ AI 调用失败：{_humanize_llm_error(e)}")
        return None


async def _deliver_generated_images_or_fallback_text(
    *,
    client: Any,
    event: Any,
    account_id: int,
    cfg: dict[str, Any],
    tpl: dict[str, Any],
    provider_dict: dict[str, Any],
    result: Any,
    user_q: str,
    replied_text: str | None,
    routing_note: str | None,
    web_search: bool,
) -> Any | None:
    if not (result.image_urls or result.image_data):
        return result

    import base64 as _b64
    import io as _io
    import os as _os

    import httpx as _httpx
    gen_image_bytes: list[bytes] = []
    gen_image_exts: list[str] = []  # 与 gen_image_bytes 一一对应的文件扩展名

    # 优先使用 grok-bridge 通过 Safari 抓取的 base64 图片数据
    # （Safari 有 grok.com 的 cookie，可以下载私有图片；直接
    # HTTP 下载 assets.grok.com 会因缺少认证而 403）
    for data_uri in result.image_data[:3]:
        try:
            # data URI 格式: "data:image/jpeg;base64,/9j/4AAQ..."
            if data_uri and data_uri.startswith("data:") and ";base64," in data_uri:
                # 从 data URI 中提取 MIME 类型，推断文件扩展名
                mime_part = data_uri[len("data:"):data_uri.index(";")]
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                    "image/svg+xml": ".svg",
                }
                img_ext = ext_map.get(mime_part, ".jpg")

                b64_part = data_uri.split(";base64,", 1)[1]
                img_bytes = _b64.b64decode(b64_part)
                if len(img_bytes) > 100:
                    gen_image_bytes.append(img_bytes)
                    gen_image_exts.append(img_ext)
                    log.info(
                        "[ai] Got generated image from base64 data: %d bytes (%s)",
                        len(img_bytes),
                        mime_part,
                    )
                else:
                    log.warning("[ai] Base64 decoded image too small: %d bytes", len(img_bytes))
            else:
                log.warning("[ai] Invalid data URI format, skipping")
        except Exception as e:
            log.warning("[ai] Failed to decode base64 image data: %s: %s", type(e).__name__, e)

    # 如果 base64 数据不可用或全部解码失败，尝试 HTTP 下载
    if not gen_image_bytes and result.image_urls:
        # 下载图片：优先使用 provider 配置的 proxy_url，其次手动从
        # 环境变量读取代理（HTTP_PROXY/HTTPS_PROXY）。
        # 注意：不使用 httpx 的 trust_env=True，因为 NO_PROXY 中的
        # IPv6 CIDR（如 ::1/128）会导致 httpx URL 解析崩溃
        # （InvalidURL: Invalid port ':1'）。
        img_proxy = provider_dict.get("proxy_url")
        if not img_proxy:
            for _ek in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                _ev = _os.environ.get(_ek)
                if _ev:
                    img_proxy = _ev
                    break
        for img_url in result.image_urls[:3]:
            try:
                dl_kwargs: dict[str, object] = {"timeout": _httpx.Timeout(30.0, connect=10.0)}
                if img_proxy:
                    # httpx trust_env=True 会解析 NO_PROXY 中的 IPv6 CIDR
                    # （如 ::1/128），导致 URL 解析崩溃（Invalid port ':1'）。
                    # 因此用 trust_env=False + mounts 传入代理 transport，绕过
                    # proxy_map 构建中对 NO_PROXY 的解析。
                    dl_kwargs["trust_env"] = False
                    dl_kwargs["mounts"] = {"all://": _httpx.AsyncHTTPTransport(proxy=img_proxy)}
                else:
                    dl_kwargs["trust_env"] = False
                async with _httpx.AsyncClient(**dl_kwargs) as dl_cli:
                    img_resp = await dl_cli.get(img_url)
                    if img_resp.status_code == 200 and len(img_resp.content) > 100:
                        # 从 URL 或 Content-Type 推断扩展名
                        _url_ext = _os.path.splitext(_os.path.basename(img_url.split("?")[0]))[1].lower()
                        if _url_ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                            _ct = img_resp.headers.get("content-type", "")
                            _url_ext = ".jpg" if "png" not in _ct else ".png" if "jpeg" not in _ct else ".jpg"
                        gen_image_bytes.append(img_resp.content)
                        gen_image_exts.append(_url_ext)
                    else:
                        log.warning(
                            "[ai] Generated image download failed: url=%s status=%d size=%d",
                            img_url[:80],
                            img_resp.status_code,
                            len(img_resp.content),
                        )
            except Exception as e:
                log.warning(
                    "[ai] Failed to download generated image: %s: %s url=%s",
                    type(e).__name__,
                    e,
                    img_url[:80],
                )

    if gen_image_bytes:
        # 渲染文字 caption（复用现有模板系统）
        from ..services.llm_format import DEFAULT_TEMPLATE, render_output
        from .command import acquire_direct_userbot_action_rate_limit
        template = cfg.get("output_template") or DEFAULT_TEMPLATE
        raw_format = (cfg.get("output_format") or "html").lower()
        output_format = "html" if raw_format == "markdownv2" else raw_format
        escape_values = bool(cfg.get("escape_values", True))
        model_id = result.model or ""
        api_format_info = _api_format_render_context(provider_dict, web_search=web_search)
        render_ctx = {
            "answer": result.text or "",
            "question": user_q,
            "quoted": replied_text or "",
            "model": _model_display_name(model_id, provider_dict),
            "model_id": model_id,
            "provider": provider_dict.get("name", ""),
            "provider_kind": provider_dict.get("provider", ""),
            "command": tpl.get("name", ""),
            "mode": cfg.get("mode", "chat"),
            "in_tokens": result.input_tokens,
            "out_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
            "routing_note": (routing_note or "").replace("auto · ", ""),
            "sources": _format_llm_sources(result.sources),
            **api_format_info,
        }
        if escape_values and output_format == "html":
            escape_format: str | None = "html"
        else:
            escape_format = None
        caption = render_output(template, render_ctx, escape_format=escape_format)
        # Telegram caption 上限 1024 字符
        if len(caption) > 1024:
            caption = caption[:1020] + "..."
        parse_mode_arg: str | None
        if output_format == "html":
            parse_mode_arg = "html"
        elif output_format in ("markdown", "markdown_v1", "md"):
            parse_mode_arg = "md"
        else:
            parse_mode_arg = None
        # 发送第一张图 + caption
        # 用 BytesIO 包装并设置 .name 属性，让 Telethon 根据后缀识别为图片
        # （否则纯 bytes 会被当作无名文件发送，TG 显示为 "unnamed" 而非图片预览）
        _buf0 = _io.BytesIO(gen_image_bytes[0])
        _buf0.name = f"ai_image{gen_image_exts[0]}" if gen_image_exts else "ai_image.jpg"
        try:
            allowed, _ = await acquire_direct_userbot_action_rate_limit(
                client,
                account_id,
                "send_file",
                event.chat_id,
            )
            if not allowed:
                return None
            await client.send_file(
                event.chat_id, _buf0,
                caption=caption, parse_mode=parse_mode_arg,
            )
        except Exception:
            try:
                allowed, _ = await acquire_direct_userbot_action_rate_limit(
                    client,
                    account_id,
                    "send_file",
                    event.chat_id,
                )
                if not allowed:
                    return None
                await client.send_file(
                    event.chat_id, _buf0,
                    caption=caption[:1024],
                )
            except Exception:
                try:
                    await event.edit(caption[:4000])
                except Exception:
                    pass
        # 后续图片无 caption
        for idx, extra_bytes in enumerate(gen_image_bytes[1:], start=1):
            try:
                _buf = _io.BytesIO(extra_bytes)
                _ext = gen_image_exts[idx] if idx < len(gen_image_exts) else ".jpg"
                _buf.name = f"ai_image{_ext}"
                allowed, _ = await acquire_direct_userbot_action_rate_limit(
                    client,
                    account_id,
                    "send_file",
                    event.chat_id,
                )
                if not allowed:
                    continue
                await client.send_file(event.chat_id, _buf)
            except Exception:
                pass
        # 删掉 "思考中..." 命令消息
        try:
            await event.delete()
        except Exception:
            pass
        return None
    # 图片下载全部失败 → 在文本中附加图片 URL，而不是静默丢图
    # （用户看到 HTML 格式的文本就是因为这里静默 fall through 了）
    log.warning(
        "[ai] All %d generated image(s) failed to download; "
        "appending URL links to text response. URLs: %s",
        len(result.image_urls),
        [u[:80] for u in result.image_urls[:3]],
    )
    from ..services.llm_client import LLMResult

    if result.image_urls and not result.text:
        return LLMResult(  # type: ignore[call-arg]
            text="图片已生成但下载失败，请手动查看：\n"
            + "\n".join(f"· {u}" for u in result.image_urls[:3]),
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            image_urls=[],
            image_data=[],
        )
    if result.image_urls:
        return LLMResult(  # type: ignore[call-arg]
            text=result.text + "\n\n📷 图片已生成但下载失败：\n"
            + "\n".join(f"· {u}" for u in result.image_urls[:3]),
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            image_urls=[],
            image_data=[],
        )
    return result


async def _render_and_deliver_text_result(
    *,
    client: Any,
    event: Any,
    account_id: int,
    cfg: dict[str, Any],
    tpl: dict[str, Any],
    provider_dict: dict[str, Any],
    result: Any,
    user_q: str,
    replied_text: str | None,
    routing_note: str | None,
    self_msg: Any,
) -> None:
    from ..services.llm_format import DEFAULT_TEMPLATE, render_output
    from .command import _send_long_message, acquire_direct_userbot_action_rate_limit
    from .media import message_has_image

    template = cfg.get("output_template") or DEFAULT_TEMPLATE
    raw_format = (cfg.get("output_format") or "html").lower()
    # 老数据兼容：markdownv2 → 当 html
    output_format = "html" if raw_format == "markdownv2" else raw_format
    escape_values = bool(cfg.get("escape_values", True))
    # 发送方式：edit = 原地编辑命令消息（默认，保留 reply 链）；
    # send_new = 删掉命令再发一条新消息（不带 reply_to）——避免在被回复方那里留下"你回复了我"的痕迹
    send_mode = str(cfg.get("send_mode") or "edit").lower()
    # send_new 自带图守卫：命令消息**自身**含图（caption 触发模式）时走 send_new
    # 会把图也删掉、聊天记录里图就没了，体验差。这种情况降级到 edit——把图保留在
    # 原消息上，caption 改写为 AI 回答。用户配置不变，仅本次单回合降级。
    self_msg_has_image = message_has_image(self_msg)
    if send_mode == "send_new" and self_msg_has_image:
        log.warning(
            "[ai-debug] downgrading send_mode send_new -> edit (self-msg has image; "
            "send_new would delete the photo)"
        )
        send_mode = "edit"

    model_id = result.model or ""
    model_display = _model_display_name(model_id, provider_dict)
    api_format_info = _api_format_render_context(provider_dict, web_search=bool(cfg.get("web_search", False)))
    render_ctx = {
        "answer": result.text or "",
        "question": user_q,
        "quoted": replied_text or "",
        "model": model_display,
        "model_id": model_id,
        "provider": provider_dict.get("name", ""),
        "provider_kind": provider_dict.get("provider", ""),
        "command": tpl.get("name", ""),
        "mode": cfg.get("mode", "chat"),
        "in_tokens": result.input_tokens,
        "out_tokens": result.output_tokens,
        "total_tokens": result.input_tokens + result.output_tokens,
        "routing_note": (routing_note or "").replace("auto · ", ""),  # 去掉前缀让模板自己加
        "sources": _format_llm_sources(result.sources),
        **api_format_info,
    }

    # 转义模式：html 走 HTML 转义；plain / markdown_v1 不转义；老 mdv2 也不进这里（已映射到 html）
    if escape_values and output_format == "html":
        escape_format: str | None = "html"
    else:
        escape_format = None

    body = render_output(template, render_ctx, escape_format=escape_format)

    # parse_mode：telethon 1.36 sanitize_parse_mode 接受 md/markdown/htm/html
    # 我们这里用 'html' / 'md' / None（plain）
    parse_mode_arg: str | None
    if output_format == "html":
        parse_mode_arg = "html"
    elif output_format in ("markdown", "markdown_v1", "md"):
        parse_mode_arg = "md"
    else:
        parse_mode_arg = None  # plain

    # 检查消息长度，超过阈值时使用分段发送
    if len(body) > _LONG_MESSAGE_THRESHOLD:
        await _send_long_message(
            client,
            event.chat_id,
            body,
            event.id if send_mode == "edit" else None,
            parse_mode_arg,
            account_id=account_id,
        )
        # send_new 模式下也需要删除原命令消息
        if send_mode == "send_new":
            try:
                await event.delete()
            except Exception:  # noqa: BLE001
                pass
        return

    if send_mode == "send_new":
        # 删命令 + 发新消息（不附 reply_to）
        # 顺序：先发新消息，确保用户看到回答；再删命令——倒过来万一发失败，命令也没了，体验差
        try:
            allowed, _ = await acquire_direct_userbot_action_rate_limit(
                client,
                account_id,
                "send_message",
                event.chat_id,
            )
            if not allowed:
                return
            await client.send_message(
                event.chat_id, body, parse_mode=parse_mode_arg
            )
        except Exception as e:  # noqa: BLE001
            # 发送失败时退化为纯文本再试；都失败就把错误编辑回原命令消息（不删）
            try:
                allowed, _ = await acquire_direct_userbot_action_rate_limit(
                    client,
                    account_id,
                    "send_message",
                    event.chat_id,
                )
                if not allowed:
                    return
                await client.send_message(event.chat_id, body)
            except Exception:
                try:
                    await event.edit(
                        f"{result.text}\n\n— {model_display} · in {result.input_tokens} / out {result.output_tokens}\n\n"
                        f"⚠ 发送异常：{type(e).__name__}"
                    )
                except Exception:
                    pass
                return
        # 发送成功才删命令
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        await event.edit(body, parse_mode=parse_mode_arg)
    except Exception as e:  # noqa: BLE001
        # 解析失败时（用户模板有未闭合 HTML 标签 / 未转义的特殊字符）退化为纯文本
        # 避免命令彻底失败，让用户至少能看到答案
        try:
            await event.edit(body)
        except Exception:
            # 实在不行就最简化版，至少把答案露出来
            try:
                await event.edit(
                    f"{result.text}\n\n— {model_display} · in {result.input_tokens} / out {result.output_tokens}\n\n"
                    f"⚠ 模板渲染异常：{type(e).__name__}",
                )
            except Exception:
                pass


async def invoke(
    client,
    event,
    args,
    tpl: dict[str, Any],
    account_id: int,
    triggered_by_account_id: int | None = None,
) -> None:
    from .command import (
        _replied_media_placeholder,
        _safe_log_text,
        dispatch_plugin_command,
        get_command_context,
    )

    ctx = get_command_context()
    cfg: dict[str, Any] = tpl.get("config") or {}
    if ctx is None:
        await event.edit("✗ worker 命令上下文尚未初始化")
        return

    command_mode = str(cfg.get("mode") or "chat").strip().lower()
    if command_mode not in {"chat", "search", "image", "video"}:
        command_mode = "chat"
    if (tpl.get("name") or "") == "ai" and args:
        sub = str(args[0] or "").strip().lower()
        if sub in {"chat", "search", "image", "video"}:
            command_mode = sub
            args = args[1:]

    if command_mode == "video":
        dispatched = await dispatch_plugin_command(
            client,
            event,
            args,
            account_id,
            plugin_key=str(cfg.get("video_plugin_key") or "video_bridge"),
            method=str(cfg.get("video_plugin_method") or "") or None,
        )
        if not dispatched:
            await event.edit("✗ 视频生成后端不可用：请先安装并在账号插件中启用 video_bridge，或在模板里填写可用的视频插件。")
        return

    native_image_mode = command_mode == "image" and str(cfg.get("image_backend") or "codex_image") == "llm"

    if command_mode == "image" and str(cfg.get("image_backend") or "codex_image") == "codex_image":
        dispatched = await dispatch_plugin_command(
            client,
            event,
            args,
            account_id,
            plugin_key=str(cfg.get("image_plugin_key") or "codex_image"),
            method=str(cfg.get("image_plugin_method") or "") or None,
        )
        if not dispatched:
            await event.edit("✗ codex_image 插件命令不可用：请先在账号插件中启用并配置 codex_image")
        return

    provider_id = cfg.get("provider_id")
    if provider_id is None:
        await event.edit("✗ AI 命令未配置 provider_id（系统设置 → LLM Provider 里建一个，填回此处）")
        return

    if command_mode == "search":
        cfg = {**cfg, "web_search": True}

    # 每次 AI 调用前从 DB 刷新 provider 缓存，保证新建/修改/删除的 provider 立即可用。
    # Redis Pub/Sub 是 fire-and-forget，IPC 通知可能丢失导致 ctx.providers 永远过期。
    # AI 命令本身要调 LLM（耗时 1–30s），额外一次轻量 DB SELECT（~1ms）开销可忽略。
    #
    # 0.5.1：刷新失败时**不再静默吞**——log.exception 让真实异常进 worker log。
    # 此前用户报"新增 provider 后 ,ai @list 看不到"，根因是 _refresh_command_context
    # 静默失败导致 ctx 永远是老快照。改成显式 log 后用户在「日志中心 → 系统」就能看到。
    try:
        from .runtime import _refresh_command_context  # noqa: F811  # lazy import 避免循环依赖
        await _refresh_command_context(ctx.account_id)
    except Exception:  # noqa: BLE001
        log.exception("[ai] 刷新 provider 缓存失败 account=%s", ctx.account_id)
        # 刷新失败不阻塞命令执行，继续用内存缓存；下次 ,ai 再试

    # ── inline @override：本次调用临时覆盖 provider/model/路由模式 ─────
    # 解析 args[0]：@<name>[:<model>] / @auto / @list；非 @ 开头则不动。
    # 这一步必须放在"决策 provider_id"之前——它会把模板里配的 fixed
    # provider_id 替换成用户指定的，并可能改 routing_mode。
    from .inline_override import (
        InlineOverrideError,
        format_provider_list,
        parse_inline_override,
    )

    inline_provider_override: int | None = None
    inline_model_override: str | None = None
    inline_force_auto = False
    # 当前命令前缀 + 模板名——给"用法"提示用，避免硬编码 ",ai"。
    # 若 ctx.command_prefix 没设置就退回默认 ","；template name 取触发模板名。
    _cmd_prefix = (ctx.command_prefix if ctx else None) or ","
    _tpl_name = str(tpl.get("name") or "ai")
    try:
        inline, args = parse_inline_override(
            args, ctx.providers,
            cmd_prefix=_cmd_prefix, template_name=_tpl_name,
        )
    except InlineOverrideError as e:
        await event.edit(str(e))
        return
    if inline is not None:
        if inline.kind == "list":
            # 直接给可用列表——不调 LLM、不消费 tokens
            await event.edit(
                format_provider_list(
                    ctx.providers,
                    cmd_prefix=_cmd_prefix, template_name=_tpl_name,
                )
            )
            return
        if inline.kind == "refresh":
            try:
                from .runtime import _refresh_command_context  # lazy import avoid cycle

                await _refresh_command_context(ctx.account_id)
            except Exception as e:  # noqa: BLE001
                log.exception("[ai] 手动刷新 provider 缓存失败 account=%s", ctx.account_id)
                await event.edit(f"✗ 刷新 provider 缓存失败：{type(e).__name__}: {e}")
                return
            await event.edit(
                "✓ provider 缓存已刷新\n\n"
                + format_provider_list(
                    ctx.providers,
                    cmd_prefix=_cmd_prefix, template_name=_tpl_name,
                )
            )
            return
        if inline.kind == "auto":
            inline_force_auto = True
        else:  # provider
            inline_provider_override = inline.provider_id
            inline_model_override = inline.model

    # ── 拼 prompt 上下文（路由器与 LLM 都要看消息内容）─────────
    # 图片来源有 2×N 条：
    #   A) 被回复消息及其所在相册的全部图（,ai 的传统语义："回复某条→问那一条"）
    #   B) 命令消息自身及其所在相册的全部图（caption 触发模式：图 + ",ai 这是什么"）
    # 涵盖：photo / image-as-document（按文件发送的未压缩图）/ 静态贴纸（webp）。
    # 转发媒体的 file_reference 过期会自动重拉一次（见 media.download_image_bytes）。
    from .media import (
        collect_image_sources,
        download_audio_bytes,
        download_image_bytes,
        message_has_audio,
        message_has_image,
    )

    user_q = " ".join(args).strip()
    replied = await event.get_reply_message()
    quote = bool(cfg.get("quote_replied", True))
    replied_text: str | None = None
    if replied is not None:
        original = replied.text or replied.message or ""
        # 被回复消息没正文时（媒体类）给个 emoji+标签占位——同时也喂给 LLM，让它知道
        # 用户在问图/视频等不可读的内容，模型可以体面地说"我看不到这张图，你能描述吗"
        if not original:
            original = _replied_media_placeholder(replied)
        replied_text = original or None
    self_msg = getattr(event, "message", None)
    has_replied_image = message_has_image(replied)
    has_self_image = message_has_image(self_msg)
    has_any_image = has_replied_image or has_self_image
    has_replied_audio = message_has_audio(replied)
    has_self_audio = message_has_audio(self_msg)
    has_any_audio = has_replied_audio or has_self_audio
    log.warning(
        "[ai-debug] replied=%s text=%s q=%s img(replied=%s,self=%s) audio(replied=%s,self=%s)",
        replied is not None,
        _safe_log_text(replied_text or ""),
        _safe_log_text(user_q),
        has_replied_image, has_self_image, has_replied_audio, has_self_audio,
    )
    if native_image_mode and not user_q and not replied_text and not has_any_image:
        await event.edit(
            f"✗ 请提供图片提示词，例如：{_cmd_prefix}{_tpl_name} 一只戴飞行员护目镜的机器人"
        )
        return

    # ── 决策 provider_id（fixed / auto）────────────────────────
    # inline @override 在前面已解析；优先级：
    #   inline @<provider> 覆盖 cfg.routing_mode → 强制 fixed 走该 provider
    #   inline @auto       覆盖 cfg.routing_mode → 强制 auto
    #   都没给                按 cfg.routing_mode
    if inline_force_auto:
        routing_mode = "auto"
    elif inline_provider_override is not None:
        routing_mode = "fixed"
    else:
        routing_mode = str(cfg.get("routing_mode") or "fixed").lower()
    routing_note: str | None = None  # 自动路由时附加在结尾的说明
    routing_matched_tag: str | None = None
    routed_model: str | None = None  # 阶段 D：auto 路由选出的已启用模型（回落链的一环）
    chosen_provider_id = (
        inline_provider_override
        if inline_provider_override is not None
        else int(provider_id)
    )

    if routing_mode == "auto":
        # 局部 import 避免 worker 启动时强依赖
        from ..services.llm_router import pick_provider

        cls_id = cfg.get("classifier_provider_id")
        # 没显式配兜底就用 fixed 那条；保证 auto 模式失败也有 last resort
        fb_id = cfg.get("routing_fallback_provider_id") or provider_id
        try:
            decision = await pick_provider(
                user_q,
                replied_text,
                has_any_image,  # 替代原先只看 replied 的标志，让 self/album/document 都能命中视觉路由
                ctx.providers,
                classifier_provider_id=int(cls_id) if cls_id else None,
                fallback_provider_id=int(fb_id),
                account_id=account_id,
                triggered_by_account_id=triggered_by_account_id,
            )
        except ValueError as e:
            # 路由器找不到任何可用 provider
            await event.edit(f"✗ AI 路由失败：{e}")
            return
        except Exception as e:  # noqa: BLE001
            # 任何意外都不要让命令静默卡住
            await event.edit(f"✗ AI 路由异常：{type(e).__name__}: {str(e)[:120]}")
            return
        chosen_provider_id = decision.provider_id
        routed_model = getattr(decision, "model", None)
        routing_note = f"auto · {decision.reason}"
        routing_matched_tag = getattr(decision, "matched_tag", None)
    elif inline_provider_override is not None:
        # 给 footer 一个标记，让用户知道是 inline 覆盖来的（而不是模板默认）
        prov_name = ctx.providers.get(chosen_provider_id, {}).get("name") or chosen_provider_id
        routing_note = f"inline → @{prov_name}"

    provider_dict = ctx.providers.get(chosen_provider_id)
    if provider_dict is None:
        # 兜底自愈：上下文可能过期，现场强刷一次再查（避免"刚新增 provider 就说不存在"）
        try:
            from .runtime import _refresh_command_context  # lazy import avoid cycle

            await _refresh_command_context(ctx.account_id)
            provider_dict = ctx.providers.get(chosen_provider_id)
        except Exception:  # noqa: BLE001
            log.exception("[ai] provider miss 时刷新失败 account=%s pid=%s", ctx.account_id, chosen_provider_id)
            # 刷新失败时继续走"provider 不存在"的友好提示；不要把 DB/网络错误
            # 覆盖掉用户真正需要看到的 provider_id。

    if provider_dict is None:
        await event.edit(
            f"✗ provider_id={chosen_provider_id} 不存在或未加载\n\n"
            + format_provider_list(ctx.providers, cmd_prefix=_cmd_prefix, template_name=_tpl_name)
        )
        return

    # ── 视觉数据：聚合所有源（replied + self + album）→ 下载 → 喂给 vision ─
    # 反幻觉守卫：只有当 chosen provider 的 modality 在 {vision, multimodal} 才下载并发送图片；
    # 否则**显式拒答**，绝不让纯文本模型对着 "📷 [图片]" 占位符瞎编。
    chosen_modality = str(provider_dict.get("modality") or "text").lower()
    provider_supports_vision = chosen_modality in ("vision", "multimodal")
    provider_supports_audio = chosen_modality in ("audio", "multimodal")
    image_bytes_list: list[bytes] = []
    image_msgs: list[Any] = []
    if has_any_image:
        if not provider_supports_vision:
            # fixed 模式下用户绑了纯文本模型；auto 模式下规则也没把它路由到 vision provider
            # —— 不论哪种情况，让模型对着不存在的图片瞎答都是有害的，直接告诉用户
            tip = (
                f"✗ 消息含图，但当前选定的 provider 不支持识图（modality={chosen_modality}）。\n"
                "  · fixed 模式：换一个 modality=vision/multimodal 的 provider；或\n"
                "  · auto 模式：确认你已配置至少一条 modality=vision/multimodal 的 provider"
            )
            await event.edit(tip)
            return
        # 收集源消息（replied + self + 它们各自相册）
        try:
            image_msgs = await collect_image_sources(client, replied, self_msg)
        except Exception as e:  # noqa: BLE001
            await event.edit(f"✗ 图片预处理失败：{type(e).__name__}: {str(e)[:80]}")
            return
        # 逐条下载——任一失败就报清楚（不静默丢图）
        for src_msg in image_msgs:
            try:
                img_data = await download_image_bytes(client, src_msg)
            except ValueError as ve:
                # 用户层错误：撤回 / 超限——直接展示
                await event.edit(f"✗ {ve}")
                return
            except Exception as e:  # noqa: BLE001
                await event.edit(
                    f"✗ 图片下载失败：{type(e).__name__}: {str(e)[:80]}"
                )
                return
            image_bytes_list.append(img_data)
        log.warning(
            "[ai-debug] downloaded %d image(s) total %d bytes for provider=%s modality=%s",
            len(image_bytes_list), sum(len(b) for b in image_bytes_list),
            provider_dict.get("name"), chosen_modality,
        )

    # ── 音频数据：先 STT 转写为文字，再走标准 chat 流程 ─────────
    # 只在 provider modality∈{audio, multimodal} 时尝试；其它情况就拒，避免占位符瞎答。
    transcribed_text: str | None = None
    if has_any_audio and not has_any_image:
        # 含图时优先走 vision；同时含图含音的边角不在 V1 范围
        if not provider_supports_audio:
            tip = (
                f"✗ 消息含音频，但当前选定的 provider 不支持转写（modality={chosen_modality}）。\n"
                "  · fixed 模式：换一个 modality=audio/multimodal 的 provider；或\n"
                "  · auto 模式：确认你已配置至少一条 modality=audio/multimodal 的 provider"
            )
            await event.edit(tip)
            return
        audio_src = replied if has_replied_audio else self_msg
        try:
            audio_data = await download_audio_bytes(client, audio_src)
        except ValueError as ve:
            await event.edit(f"✗ {ve}")
            return
        except Exception as e:  # noqa: BLE001
            await event.edit(f"✗ 音频下载失败：{type(e).__name__}: {str(e)[:80]}")
            return
        log.warning(
            "[ai-debug] downloaded audio %d bytes for STT, provider=%s",
            len(audio_data), provider_dict.get("name"),
        )

    request_settings = _build_llm_request_settings(
        cfg=cfg,
        native_image_mode=native_image_mode,
        inline_model_override=inline_model_override,
        inline_provider_override=inline_provider_override,
        routed_model=routed_model,
    )

    # 占位回显，避免用户以为没反应（注意：edit 失败也要继续，非致命）
    # 一律简化为 "思考中..."；具体路由决策最终在 footer 的 {routing_note} 里展示
    try:
        await event.edit("思考中...")
    except Exception:  # noqa: BLE001
        pass

    # 使用 LLMProviderDTO 替代手搓 fake ORM row
    provider_dtos = _build_provider_dtos(
        ctx.providers or {},
        image_bytes_list=image_bytes_list,
    )
    provider_dto = provider_dtos.get(int(chosen_provider_id))
    if provider_dto is None:
        await event.edit(f"✗ AI provider 配置异常：provider_id={chosen_provider_id} 不可用于当前请求")
        return

    # ── STT：先把音频转写为文字，再走标准 chat 流程 ──────────
    # ``transcribe_model`` 由模板配（缺省 ``whisper-1``）——必须与 chat 模型分开，因为
    # 在 OpenAI / 兼容反代上 STT 是独立 model（``whisper-1`` / ``whisper-large`` 等）。
    if has_any_audio and not has_any_image:
        # STT 走 services.llm_invoke.transcribe()，和标准 LLM 调用一样写 usage；
        # 由于这是业务链路，会按账号预算做预扣。不同于诊断测活，不做预算豁免。
        from ..services.llm_client import LLMCallFailed, LLMError
        from ..services.llm_invoke import transcribe as transcribe_ai_runtime

        stt_model = str(cfg.get("transcribe_model") or "whisper-1").strip()
        try:
            transcribed_text = await transcribe_ai_runtime(
                provider_dto,
                audio_data,
                model=stt_model,
                override_model=request_settings.override_model,
                account_id=account_id,
                triggered_by_account_id=triggered_by_account_id,
                source="stt",
            )
        except NotImplementedError:
            await event.edit(
                "✗ 当前 provider 暂不支持语音转写（仅 OpenAI 兼容 /audio/transcriptions）"
            )
            return
        except (LLMCallFailed, LLMError) as e:
            await event.edit(f"✗ STT 调用失败：{e}")
            return
        except Exception as e:  # noqa: BLE001
            await event.edit(f"✗ STT 调用失败：{type(e).__name__}: {str(e)[:120]}")
            return
        log.warning("[ai-debug] STT got %d chars from %s", len(transcribed_text or ""), stt_model)

    user_msg = _build_user_prompt(
        replied=replied,
        replied_text=replied_text,
        user_q=user_q,
        quote=quote,
        image_bytes_list=image_bytes_list,
        transcribed_text=transcribed_text,
    )

    outcome = await _call_llm_provider(
        event=event,
        cfg=cfg,
        tpl=tpl,
        account_id=account_id,
        triggered_by_account_id=triggered_by_account_id,
        ctx_providers=ctx.providers,
        provider_dto=provider_dto,
        provider_dtos=provider_dtos,
        provider_dict=provider_dict,
        system=request_settings.system,
        user_msg=user_msg,
        override_model=request_settings.override_model,
        max_tokens=request_settings.max_tokens,
        image_bytes_list=image_bytes_list,
        native_image_mode=native_image_mode,
        routing_mode=routing_mode,
        provider_id=provider_id,
        routing_matched_tag=routing_matched_tag,
        routing_note=routing_note,
        temperature=request_settings.temperature,
        reasoning_effort=request_settings.reasoning_effort,
        timeout_seconds=request_settings.timeout_seconds,
        stream=False,
    )
    if outcome is None:
        return
    result = outcome.result
    provider_dict = outcome.provider_dict
    routing_note = outcome.routing_note

    # ── 处理 LLM 生成的图片（如 Grok 文生图）────────────────────
    result = await _deliver_generated_images_or_fallback_text(
        client=client,
        event=event,
        account_id=account_id,
        cfg=cfg,
        tpl=tpl,
        provider_dict=provider_dict,
        result=result,
        user_q=user_q,
        replied_text=replied_text,
        routing_note=routing_note,
        web_search=outcome.web_search,
    )
    if result is None:
        return

    # ── 用 output_template 渲染最终消息 ─────────────────────────
    # 默认走 HTML：Telethon 1.36 的 sanitize_parse_mode 不接受 'markdownv2' 字符串
    # （会抛 ValueError），所以改用 HTML——telethon 内置全功能支持，包括
    # <blockquote expandable> 折叠引用块。
    # 老配置里 output_format='markdownv2' 自动当 'html' 处理（容错）。
    await _render_and_deliver_text_result(
        client=client,
        event=event,
        account_id=account_id,
        cfg=cfg,
        tpl=tpl,
        provider_dict=provider_dict,
        result=result,
        user_q=user_q,
        replied_text=replied_text,
        routing_note=routing_note,
        self_msg=self_msg,
    )

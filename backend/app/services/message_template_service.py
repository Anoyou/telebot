"""消息模板实验室：模板发现、渲染校验与安全测试发送。"""

from __future__ import annotations

import re
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..account_bot_defaults import (
    DEFAULT_INTERACTION_RESPONSE_TEMPLATE,
    DEFAULT_DEBIT_NOTICE_TEMPLATE,
    DEFAULT_TRANSFER_NOTICE_TEMPLATE,
)
from ..db.models.system import SystemSetting
from ..schemas.message_template import (
    MessageTemplateCatalogGroup,
    MessageTemplateCatalogItem,
    MessageTemplateCatalogResponse,
    MessageTemplateEntitySummary,
    MessageTemplateRenderRequest,
    MessageTemplateRenderResponse,
    MessageTemplateTestSendRequest,
    MessageTemplateTestSendResponse,
    MessageTemplateValidationResult,
)
from . import account_bot_service, command_service, feature_service
from .event_trace import (
    TRACE_STATUS_FAILED,
    TRACE_STATUS_OK,
    finish_trace,
    record_action,
    start_trace,
    trace_log_context,
)
from .llm_format import DEFAULT_TEMPLATE as DEFAULT_AI_OUTPUT_TEMPLATE
from .llm_format import render_output

_PLACEHOLDER_RE = re.compile(r"\{(?!\?)(\w+)\}")
_COND_PLACEHOLDER_RE = re.compile(r"\{\?(\w+)\}")

_SYSTEM_GROUP = "系统内置"
_DEFAULT_PARSE_MODE = "HTML"

_DEFAULT_SAMPLE_VALUES: dict[str, Any] = {
    "payer_name": "Alice",
    "payer_user_id": "10001",
    "payer_user_id_line": "付款人ID：10001",
    "receiver_name": "Bob",
    "receiver_user_id": "10002",
    "receiver_user_id_line": "收款人ID：10002",
    "amount": "88.00",
    "status": "生成成功",
    "answer": "这是 AI 返回的示例回答。\n第二行会继续展示重点。\n第三行开始适合放进折叠引用。\n第四行用于预览长回答。",
    "answer_first_2": "这是 AI 返回的示例回答。\n第二行会继续展示重点。",
    "answer_rest": "第三行开始适合放进折叠引用。\n第四行用于预览长回答。",
    "question": "请总结这段消息",
    "quoted": "这是一段被回复消息的示例内容。",
    "display_input": "请总结这段消息",
    "display_input_first_2": "请总结这段消息",
    "display_input_rest": "",
    "provider": "示例 Provider",
    "provider_kind": "openai",
    "model_id": "gpt-5.5",
    "prompt": "一只蓝色机械蝴蝶停在玻璃花园里",
    "model": "gpt-5.5",
    "mode": "chat",
    "api_format": "chat_completions",
    "api_protocol": "chat_completions",
    "configured_api_format": "chat_completions",
    "web_search_api_format": "auto",
    "endpoint": "/chat/completions",
    "web_search": "",
    "in_tokens": "128",
    "out_tokens": "256",
    "total_tokens": "384",
    "routing_note": "按固定 Provider 调用",
    "sources": "https://example.com/source",
    "image_model": "gpt-image-2",
    "image_size": "1024x1024",
    "aspect_ratio": "1:1",
    "image_format": "png",
    "size": "1:1",
    "count": "1",
    "elapsed": "12s",
    "command": "cximg",
    "has_reference": "是",
    "revised_prompt": "A blue mechanical butterfly in a glass garden.",
    "response_id": "resp_demo_123",
    "error": "示例错误",
    "time": "12:00",
}


async def _trace_enabled(db: AsyncSession) -> bool:
    try:
        row = await db.get(SystemSetting, "log_retention")
        raw = getattr(row, "value", None) if row is not None else None
        return bool(raw.get("trace_enabled", True)) if isinstance(raw, dict) else True
    except Exception:  # noqa: BLE001
        return True

_ALLOWED_HTML_TAGS: dict[str, set[str]] = {
    "a": {"href"},
    "b": set(),
    "blockquote": {"expandable"},
    "code": {"class"},
    "del": set(),
    "em": set(),
    "i": set(),
    "ins": set(),
    "pre": set(),
    "s": set(),
    "span": {"class"},
    "strong": set(),
    "strike": set(),
    "tg-emoji": {"emoji-id"},
    "tg-spoiler": set(),
    "u": set(),
}


def _bad(code: str, message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def normalize_parse_mode(parse_mode: str | None) -> str | None:
    if parse_mode is None:
        return None
    value = str(parse_mode or "").strip()
    if not value or value.lower() in {"none", "plain", "text"}:
        return None
    lowered = value.lower().replace("_", "").replace("-", "")
    if lowered in {"html", "htm"}:
        return "HTML"
    if lowered in {"markdown", "md"}:
        return "Markdown"
    if lowered in {"markdownv2", "mdv2"}:
        return "MarkdownV2"
    raise _bad("MESSAGE_TEMPLATE_PARSE_MODE_INVALID", "parse_mode 只能是 HTML / Markdown / MarkdownV2 / none", 422)


def _is_template_field(field_key: str) -> bool:
    return field_key == "message_template" or field_key.endswith("_template")


def _field_title(feature_name: str, field_key: str, field_schema: dict[str, Any]) -> str:
    title = str(field_schema.get("title") or "").strip() or field_key
    return f"{feature_name} / {title}"


def _template_value(effective_config: dict[str, Any], field_key: str, field_schema: dict[str, Any]) -> str:
    if field_key in effective_config and effective_config[field_key] is not None:
        return str(effective_config[field_key])
    default = field_schema.get("default")
    return "" if default is None else str(default)


def _sample_value_for_key(key: str) -> Any:
    if key in _DEFAULT_SAMPLE_VALUES:
        return _DEFAULT_SAMPLE_VALUES[key]
    lowered = key.lower()
    if lowered.endswith("_id") or lowered == "id":
        return "10001"
    if "count" in lowered or "seconds" in lowered or "limit" in lowered:
        return "1"
    if "url" in lowered or "link" in lowered:
        return "https://example.com"
    if "name" in lowered:
        return "示例名称"
    return "示例值"


def _extract_placeholders(template: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in [*_PLACEHOLDER_RE.finditer(template or ""), *_COND_PLACEHOLDER_RE.finditer(template or "")]:
        key = match.group(1)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _sample_data_for_template(
    template: str,
    *,
    field_schema: dict[str, Any] | None = None,
    explicit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sample: dict[str, Any] = {}
    raw_schema_sample = (field_schema or {}).get("x-sample-data") or (field_schema or {}).get("sample_data")
    if isinstance(raw_schema_sample, dict):
        sample.update(raw_schema_sample)
    if explicit:
        sample.update(explicit)
    for key in _extract_placeholders(template):
        sample.setdefault(key, _sample_value_for_key(key))
    return sample


class _TelegramHtmlValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self._stack: list[str] = []

    def _fail(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: D401
        tag = tag.lower()
        allowed_attrs = _ALLOWED_HTML_TAGS.get(tag)
        if allowed_attrs is None:
            self._fail(f"不支持的 HTML 标签: <{tag}>")
            return

        attr_map = {name.lower(): value for name, value in attrs}
        extra_attrs = set(attr_map) - allowed_attrs
        if extra_attrs:
            self._fail(f"<{tag}> 包含不支持的属性: {', '.join(sorted(extra_attrs))}")
            return

        if tag == "span" and attr_map.get("class") != "tg-spoiler":
            self._fail('<span> 只允许 class="tg-spoiler"')
            return
        if tag == "code":
            class_name = str(attr_map.get("class") or "")
            if class_name and not class_name.startswith("language-"):
                self._fail('<code> 的 class 只能是 language- 开头')
                return
        if tag == "blockquote":
            expandable = attr_map.get("expandable")
            if expandable not in {None, "", "expandable", "true"}:
                self._fail('<blockquote> 的 expandable 属性格式不正确')
                return
        if tag == "a" and not str(attr_map.get("href") or "").strip():
            self._fail('<a> 必须带 href 属性')
            return
        if tag == "tg-emoji" and not str(attr_map.get("emoji-id") or "").strip():
            self._fail('<tg-emoji> 必须带 emoji-id 属性')
            return

        self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._fail(f"不支持自闭合 HTML 标签: <{tag} />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._stack:
            self._fail(f"多余的结束标签: </{tag}>")
            return
        open_tag = self._stack.pop()
        if open_tag != tag:
            self._fail(f"标签未闭合或顺序错误: <{open_tag}> / </{tag}>")

    def close(self) -> None:
        super().close()
        if self._stack:
            self._fail(f"存在未闭合标签: {', '.join(f'<{tag}>' for tag in self._stack)}")


def _validate_telegram_html(text: str) -> list[str]:
    parser = _TelegramHtmlValidator()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001
        parser.errors.append(f"{type(exc).__name__}: {exc}")
    return parser.errors


def _parse_mode_for_field(field_schema: dict[str, Any], effective_config: dict[str, Any]) -> str | None:
    raw = (
        field_schema.get("x-parse-mode")
        or field_schema.get("parse_mode")
        or effective_config.get("parse_mode")
        or _DEFAULT_PARSE_MODE
    )
    return normalize_parse_mode(str(raw) if raw is not None else None)


def _system_catalog_items(config: dict[str, Any]) -> list[MessageTemplateCatalogItem]:
    transfer_template = str(config.get("transfer_notice_template") or DEFAULT_TRANSFER_NOTICE_TEMPLATE)
    debit_template = str(config.get("debit_notice_template") or DEFAULT_DEBIT_NOTICE_TEMPLATE)
    response_template = str(config.get("response_template") or DEFAULT_INTERACTION_RESPONSE_TEMPLATE)
    transfer_sample = {
        "payer_name": _DEFAULT_SAMPLE_VALUES["payer_name"],
        "payer_user_id": _DEFAULT_SAMPLE_VALUES["payer_user_id"],
        "payer_user_id_line": _DEFAULT_SAMPLE_VALUES["payer_user_id_line"],
        "receiver_name": _DEFAULT_SAMPLE_VALUES["receiver_name"],
        "receiver_user_id": _DEFAULT_SAMPLE_VALUES["receiver_user_id"],
        "receiver_user_id_line": _DEFAULT_SAMPLE_VALUES["receiver_user_id_line"],
        "amount": _DEFAULT_SAMPLE_VALUES["amount"],
    }
    response_sample = {
        "payer_name": _DEFAULT_SAMPLE_VALUES["payer_name"],
        "payer_user_id": _DEFAULT_SAMPLE_VALUES["payer_user_id"],
        "receiver_name": _DEFAULT_SAMPLE_VALUES["receiver_name"],
        "receiver_user_id": _DEFAULT_SAMPLE_VALUES["receiver_user_id"],
        "amount": _DEFAULT_SAMPLE_VALUES["amount"],
    }
    return [
        MessageTemplateCatalogItem(
            id="system.transfer_notice_template",
            group=_SYSTEM_GROUP,
            feature_key=None,
            field_key="transfer_notice_template",
            title="转账通知模板",
            description="转账结果通知 Bot 发出的到账消息模板，默认带 language-转账成功 代码块标识。",
            template=transfer_template,
            sample_data=_sample_data_for_template(transfer_template, explicit=transfer_sample),
            parse_mode=_DEFAULT_PARSE_MODE,
        ),
        MessageTemplateCatalogItem(
            id="system.debit_notice_template",
            group=_SYSTEM_GROUP,
            feature_key=None,
            field_key="debit_notice_template",
            title="扣款测试通知模板",
            description="测试通知 Bot 处理 -数字 扣款命令时发出的扣减消息模板，默认带 language-扣减成功 代码块标识。",
            template=debit_template,
            sample_data=_sample_data_for_template(debit_template, explicit=transfer_sample),
            parse_mode=_DEFAULT_PARSE_MODE,
        ),
        MessageTemplateCatalogItem(
            id="system.response_template",
            group=_SYSTEM_GROUP,
            feature_key=None,
            field_key="response_template",
            title="交互 Bot 默认回复",
            description="转账通知命中后，交互 Bot 发给群内用户的默认回复模板。",
            template=response_template,
            sample_data=_sample_data_for_template(response_template, explicit=response_sample),
            parse_mode=_DEFAULT_PARSE_MODE,
        ),
    ]


def _parse_mode_for_ai_config(config: dict[str, Any]) -> str | None:
    raw = str(config.get("output_format") or "html").strip().lower()
    if raw in {"plain", "text", "none"}:
        return None
    if raw in {"markdown", "markdown_v1", "md"}:
        return "Markdown"
    if raw in {"markdownv2", "markdown_v2", "mdv2"}:
        return "HTML"
    return "HTML"


async def _ai_command_catalog_items(db: AsyncSession) -> list[MessageTemplateCatalogItem]:
    items: list[MessageTemplateCatalogItem] = []
    for template_row in await command_service.list_templates(db):
        if str(getattr(template_row, "type", "") or "") != "ai":
            continue
        config = getattr(template_row, "config", None)
        if not isinstance(config, dict):
            config = {}
        command_name = str(getattr(template_row, "name", "") or "").strip() or str(getattr(template_row, "id", "ai"))
        output_template = config.get("output_template")
        template = str(output_template) if str(output_template or "").strip() else DEFAULT_AI_OUTPUT_TEMPLATE
        description = str(getattr(template_row, "description", "") or "").strip()
        items.append(
            MessageTemplateCatalogItem(
                id=f"command.{command_name}.output_template",
                group="AI 指令模板",
                feature_key=None,
                field_key="output_template",
                title=f"AI 指令 / ,{command_name} 输出模板",
                description=description or "自定义 AI 命令返回给 Telegram 的最终消息模板。",
                template=template,
                sample_data=_sample_data_for_template(
                    template,
                    explicit={
                        "command": command_name,
                        "mode": str(config.get("mode") or "chat"),
                    },
                ),
                parse_mode=_parse_mode_for_ai_config(config),
            )
        )
    return items


def _group_items(items: list[MessageTemplateCatalogItem]) -> list[MessageTemplateCatalogGroup]:
    grouped: OrderedDict[str, list[MessageTemplateCatalogItem]] = OrderedDict()
    for item in items:
        grouped.setdefault(item.group, []).append(item)
    return [
        MessageTemplateCatalogGroup(group=group, title=group, items=group_items)
        for group, group_items in grouped.items()
    ]


async def build_catalog(db: AsyncSession, account_id: int) -> MessageTemplateCatalogResponse:
    await account_bot_service.ensure_account(db, account_id)
    system_config = await account_bot_service.get_transfer_notice_config(db, account_id)
    items = _system_catalog_items(system_config)
    items.extend(await _ai_command_catalog_items(db))

    for feature in await feature_service.list_features(db):
        manifest = getattr(feature, "manifest", None) or {}
        config_schema = manifest.get("config_schema") if isinstance(manifest, dict) else None
        properties = config_schema.get("properties") if isinstance(config_schema, dict) else None
        if not isinstance(properties, dict):
            continue
        template_fields = [
            (str(field_key), field_schema)
            for field_key, field_schema in properties.items()
            if isinstance(field_schema, dict) and _is_template_field(str(field_key))
        ]
        if not template_fields:
            continue

        feature_key = str(getattr(feature, "key", "") or "")
        feature_name = str(getattr(feature, "display_name", None) or feature_key)
        effective_config = await feature_service.get_effective_plugin_config(db, account_id, feature_key)
        for field_key, field_schema in template_fields:
            template = _template_value(effective_config, field_key, field_schema)
            items.append(
                MessageTemplateCatalogItem(
                    id=f"feature.{feature_key}.{field_key}",
                    group=feature_name,
                    feature_key=feature_key,
                    field_key=field_key,
                    title=_field_title(feature_name, field_key, field_schema),
                    description=str(field_schema.get("description") or "").strip(),
                    template=template,
                    sample_data=_sample_data_for_template(template, field_schema=field_schema),
                    parse_mode=_parse_mode_for_field(field_schema, effective_config),
                )
            )

    return MessageTemplateCatalogResponse(
        account_id=account_id,
        groups=_group_items(items),
        items=items,
    )


def _entity_type(raw_type: str) -> str:
    name = raw_type.removeprefix("MessageEntity")
    mapping = {
        "Pre": "pre",
        "Code": "code",
        "Blockquote": "blockquote",
        "Bold": "bold",
        "Italic": "italic",
        "Underline": "underline",
        "Strike": "strike",
        "TextUrl": "text_url",
        "Url": "url",
        "Mention": "mention",
    }
    if name in mapping:
        return mapping[name]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() or raw_type.lower()


def _summarize_entities(entities: list[Any]) -> list[MessageTemplateEntitySummary]:
    summaries: list[MessageTemplateEntitySummary] = []
    for entity in entities:
        raw_type = type(entity).__name__
        language = getattr(entity, "language", None)
        collapsed = getattr(entity, "collapsed", None)
        summaries.append(
            MessageTemplateEntitySummary(
                type=_entity_type(raw_type),
                raw_type=raw_type,
                offset=int(getattr(entity, "offset", 0) or 0),
                length=int(getattr(entity, "length", 0) or 0),
                language=(str(language) if language else None),
                collapsed=(bool(collapsed) if collapsed is not None else None),
            )
        )
    return summaries


def _validate_rendered_text(text: str, parse_mode: str | None) -> tuple[str, list[MessageTemplateEntitySummary], MessageTemplateValidationResult]:
    if parse_mode != "HTML":
        warning = [] if parse_mode is None else [f"{parse_mode} 暂不做实体解析，仅返回变量替换结果"]
        return text, [], MessageTemplateValidationResult(ok=True, warnings=warning, plain_text=text)
    html_errors = _validate_telegram_html(text)
    if html_errors:
        return (
            text,
            [],
            MessageTemplateValidationResult(
                ok=False,
                errors=html_errors,
                plain_text=text,
            ),
        )
    try:
        from telethon.extensions import html as telethon_html

        plain_text, entities = telethon_html.parse(text)
    except Exception as exc:  # noqa: BLE001
        return (
            text,
            [],
            MessageTemplateValidationResult(
                ok=False,
                errors=[f"{type(exc).__name__}: {exc}"],
                plain_text=text,
            ),
        )
    return (
        plain_text,
        _summarize_entities(list(entities or [])),
        MessageTemplateValidationResult(ok=True, plain_text=plain_text),
    )


def render_template(payload: MessageTemplateRenderRequest) -> MessageTemplateRenderResponse:
    parse_mode = normalize_parse_mode(payload.parse_mode)
    escape_format = "html" if parse_mode == "HTML" else "mdv2" if parse_mode == "MarkdownV2" else None
    text = render_output(payload.template, dict(payload.sample_data or {}), escape_format=escape_format)
    plain_text, entities, validation = _validate_rendered_text(text, parse_mode)
    return MessageTemplateRenderResponse(
        text=text,
        parse_mode=parse_mode,
        plain_text=plain_text,
        entities=entities,
        validation=validation,
    )


_TARGET_NOT_ALLOWED_MESSAGE = (
    "只能发送到当前账号已授权用户与 Bot 建立过的私聊。"
    "请到账号详情 → Bot 联动 → 授权用户添加并启用目标 Telegram 用户 ID，"
    "再让该用户私聊这个 Bot 发送 /start，系统记录 last_chat_id 后回到这里选择该用户或填写同一个 ID。"
)


async def _assert_authorized_private_target(
    db: AsyncSession,
    account_id: int,
    target_chat_id: int,
) -> None:
    if int(target_chat_id) <= 0:
        raise _bad(
            "MESSAGE_TEMPLATE_TARGET_NOT_ALLOWED",
            _TARGET_NOT_ALLOWED_MESSAGE,
            status.HTTP_403_FORBIDDEN,
        )
    users = await account_bot_service.list_bot_users(db, account_id)
    for user in users:
        if not bool(getattr(user, "enabled", False)):
            continue
        last_chat_id = getattr(user, "last_chat_id", None)
        tg_user_id = getattr(user, "tg_user_id", None)
        if last_chat_id is None or tg_user_id is None:
            continue
        if int(last_chat_id) == int(target_chat_id) and int(tg_user_id) == int(target_chat_id):
            return
    raise _bad(
        "MESSAGE_TEMPLATE_TARGET_NOT_ALLOWED",
        _TARGET_NOT_ALLOWED_MESSAGE,
        status.HTTP_403_FORBIDDEN,
    )


async def send_test_message(
    db: AsyncSession,
    payload: MessageTemplateTestSendRequest,
) -> MessageTemplateTestSendResponse:
    parse_mode = normalize_parse_mode(payload.parse_mode)
    await _assert_authorized_private_target(db, payload.account_id, payload.target_chat_id)
    _, _, validation = _validate_rendered_text(payload.text, parse_mode)
    if not validation.ok:
        raise _bad(
            "MESSAGE_TEMPLATE_TEST_SEND_INVALID",
            "测试消息内容存在 Telegram HTML 解析错误，请先修正模板后再发送。",
            422,
        )

    bot_config = await account_bot_service.get_bot_config(db, payload.account_id, create=False)
    token = account_bot_service.decrypt_bot_token(bot_config)
    trace = None
    if await _trace_enabled(db):
        trace = await start_trace(
            {
                "source": {
                    "account_id": payload.account_id,
                    "channel": "account_bot",
                    "type": "message_template_test",
                },
                "chat": {"id": int(payload.target_chat_id), "type": "private"},
                "message": {"chat_id": int(payload.target_chat_id), "text": payload.text},
            }
        )
    action = {
        "type": "send_message",
        "send_via": "account_bot",
        "chat_id": int(payload.target_chat_id),
        "text": payload.text,
        "context": trace_log_context(trace, plugin_key="message_template"),
    }
    try:
        result = await account_bot_service.send_message(
            token,
            int(payload.target_chat_id),
            payload.text,
            parse_mode=parse_mode,
        )
    except Exception as exc:  # noqa: BLE001
        await record_action(
            trace,
            action,
            TRACE_STATUS_FAILED,
            actual_send_via="account_bot",
            error_code="telegram_api_error",
            error=account_bot_service.sanitize_bot_error(exc, token=token),
        )
        await finish_trace(trace, TRACE_STATUS_FAILED)
        raise _bad(
            "MESSAGE_TEMPLATE_TEST_SEND_FAILED",
            account_bot_service.sanitize_bot_error(exc, token=token),
            status.HTTP_502_BAD_GATEWAY,
        ) from exc
    await record_action(trace, action, TRACE_STATUS_OK, actual_send_via="account_bot", result=result)
    await finish_trace(trace, TRACE_STATUS_OK)

    raw_message_id = result.get("message_id") if isinstance(result, dict) else None
    try:
        message_id = int(raw_message_id) if raw_message_id is not None else None
    except (TypeError, ValueError):
        message_id = None
    return MessageTemplateTestSendResponse(
        ok=True,
        target_chat_id=int(payload.target_chat_id),
        parse_mode=parse_mode,
        message_id=message_id,
        message="测试消息已发送。",
    )


__all__ = [
    "build_catalog",
    "normalize_parse_mode",
    "render_template",
    "send_test_message",
]

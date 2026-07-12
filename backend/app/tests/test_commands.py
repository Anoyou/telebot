"""自定义命令模板 + LLM provider 单元测试（Sprint2 #2）。

不连真 PG / 真 LLM。覆盖：
- LLMProvider api_key 加密 → 解密往返 + 出参不含明文（has_api_key:bool）
- CommandTemplate schema 校验：name 正则、type 与 config 结构必须匹配
- worker._run_template 对 reply_text / forward_to / 错误分支的派发
- worker._run_ai 在缺 provider_id / provider 不存在 / build_client 失败时的 friendly 错误
- LLM client 错误脱敏：api_key 不会出现在异常 message 中
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.crypto import decrypt_str, encrypt_str
from app.db.models.command import LLMProvider, normalize_protocol_profile
from app.schemas.command import (
    ChatTestModelsRequest,
    CommandTemplateBase,
    CommandTemplateCreate,
    LLMProviderCreate,
    LLMProviderUpdate,
)
from app.services import command_service
from app.services.command_service import _provider_to_out
from app.services.llm_client import (
    LLMError,
    OpenAIClient,
    _safe_error_message,
    build_client,
)
from app.services.llm_dto import LLMProviderDTO
from app.worker import command as wcmd


@pytest.fixture(autouse=True)
def _disable_ai_refresh(monkeypatch):
    from app.worker import runtime as worker_runtime

    monkeypatch.setattr(worker_runtime, "_refresh_command_context", AsyncMock(return_value=None))

# ════════════════════════════════════════════════════════════
# 1) 加密 + 出参屏蔽
# ════════════════════════════════════════════════════════════


def test_provider_api_key_encrypt_roundtrip() -> None:
    """encrypt_str 后存表，再 decrypt 还原；任何明文都不应等于密文。"""
    plain = "sk-abcdef1234567890"
    enc = encrypt_str(plain)
    assert enc != plain
    assert decrypt_str(enc) == plain


def test_provider_to_out_strips_api_key() -> None:
    """_provider_to_out 必须只返 has_api_key:bool，不含密文 / 明文。"""
    row = LLMProvider(
        id=1,
        name="openai-main",
        provider="openai",
        api_key_enc=encrypt_str("sk-very-secret"),
        base_url=None,
        default_model="gpt-4o-mini",
        created_at=datetime.now(UTC),
    )
    out = _provider_to_out(row)
    payload = out.model_dump()
    assert payload["has_api_key"] is True
    # api_key / api_key_enc 都不能出现在出参里
    assert "api_key" not in payload
    assert "api_key_enc" not in payload
    # 同样不能在序列化文本里出现明文敏感串
    assert "sk-very-secret" not in str(payload)


def test_provider_to_out_no_key() -> None:
    """没设置 api_key 的 provider 应该 has_api_key=False。"""
    row = LLMProvider(
        id=2,
        name="ollama-local",
        provider="ollama",
        api_key_enc=None,
        base_url="http://localhost:11434/v1",
        default_model="llama3:8b",
        created_at=datetime.now(UTC),
    )
    out = _provider_to_out(row)
    assert out.has_api_key is False


# ════════════════════════════════════════════════════════════
# 2) Pydantic schema 校验
# ════════════════════════════════════════════════════════════


def test_template_name_must_match_regex() -> None:
    """命令名只允许 [a-zA-Z0-9_]。"""
    # OK
    CommandTemplateCreate(name="hi_2", type="reply_text", config={"text": ""})
    # 含空格
    with pytest.raises(ValueError):
        CommandTemplateCreate(name="hi world", type="reply_text", config={"text": ""})
    # 含中文
    with pytest.raises(ValueError):
        CommandTemplateCreate(name="你好", type="reply_text", config={"text": ""})
    # 空串
    with pytest.raises(ValueError):
        CommandTemplateCreate(name="", type="reply_text", config={"text": ""})


def test_builtin_reserved_words_drop_removed_reboot_aliases() -> None:
    """PR1 后，reboot/rb 不应继续占用模板保留词。"""
    assert "reboot" not in command_service._BUILTIN_RESERVED_WORDS
    assert "rb" not in command_service._BUILTIN_RESERVED_WORDS


def test_builtin_reserved_words_keep_active_builtin_commands() -> None:
    """仍在用的内置命令继续保留冲突保护。"""
    assert "restart" in command_service._BUILTIN_RESERVED_WORDS
    assert "help" in command_service._BUILTIN_RESERVED_WORDS
    assert "version" in command_service._BUILTIN_RESERVED_WORDS


@pytest.mark.asyncio
async def test_validate_template_keywords_unique_still_rejects_active_builtin() -> None:
    """模板命令名仍不能与当前内置命令冲突。"""
    class _Rows:
        def scalars(self) -> _Rows:
            return self

        def all(self) -> list[object]:
            return []

    class _DB:
        async def execute(self, _query):
            return _Rows()

    with pytest.raises(HTTPException) as ei:
        await command_service._validate_template_keywords_unique(
            _DB(), name="restart", aliases=[], current_id=None
        )
    detail = ei.value.detail or {}
    assert detail.get("code") == "TEMPLATE_ALIAS_CONFLICT"


def test_template_reply_text_requires_text() -> None:
    """reply_text 必须有 text 字段。"""
    with pytest.raises(ValueError):
        CommandTemplateBase(name="x", type="reply_text", config={})
    # 允许空串
    CommandTemplateBase(name="x", type="reply_text", config={"text": ""})


def test_template_forward_to_requires_int_chat_id() -> None:
    """forward_to 的 target_chat_id 现已可选；给了就必须能转 int。"""
    # 缺省 / 空串都允许（运行时回退到当前 chat）
    t = CommandTemplateBase(name="x", type="forward_to", config={})
    assert "target_chat_id" not in t.config
    t2 = CommandTemplateBase(name="x", type="forward_to", config={"target_chat_id": ""})
    assert "target_chat_id" not in t2.config
    # 给了非整数仍然拒
    with pytest.raises(ValueError):
        CommandTemplateBase(name="x", type="forward_to", config={"target_chat_id": "abc"})
    # int 或 数字字符串都 OK，会归一为 int
    t3 = CommandTemplateBase(
        name="x", type="forward_to", config={"target_chat_id": -1001234567890}
    )
    assert t3.config["target_chat_id"] == -1001234567890
    t4 = CommandTemplateBase(
        name="x", type="forward_to", config={"target_chat_id": "100"}
    )
    assert t4.config["target_chat_id"] == 100


def test_template_forward_to_accepts_copy_media_mode() -> None:
    """forward_to.copy_media 用于复制贴纸/图片/文件等非纯文本消息。"""
    t = CommandTemplateBase(name="x", type="forward_to", config={"mode": "copy_media"})
    assert t.config["mode"] == "copy_media"


def test_template_forward_to_rejects_unknown_mode() -> None:
    """forward_to.mode 只允许已实现的转发/复制模式。"""
    with pytest.raises(ValueError):
        CommandTemplateBase(name="x", type="forward_to", config={"mode": "bad"})


def test_template_forward_to_delete_after() -> None:
    """forward_to.delete_after：必须是 0~3600 整数秒；0 / 缺省被丢弃。"""
    # 缺省 / 0 → 不写入 config
    t = CommandTemplateBase(name="x", type="forward_to", config={"delete_after": 0})
    assert "delete_after" not in t.config
    # 合法值保留
    t2 = CommandTemplateBase(name="x", type="forward_to", config={"delete_after": 5})
    assert t2.config["delete_after"] == 5
    # 超界 / 非整数拒绝
    with pytest.raises(ValueError):
        CommandTemplateBase(name="x", type="forward_to", config={"delete_after": -1})
    with pytest.raises(ValueError):
        CommandTemplateBase(name="x", type="forward_to", config={"delete_after": 4000})
    with pytest.raises(ValueError):
        CommandTemplateBase(name="x", type="forward_to", config={"delete_after": "abc"})


def test_template_ai_requires_provider_id() -> None:
    """ai 类型必须配 provider_id。"""
    with pytest.raises(ValueError):
        CommandTemplateBase(name="ai", type="ai", config={})
    CommandTemplateBase(name="ai", type="ai", config={"provider_id": 1})


def test_template_ai_send_mode_default() -> None:
    """send_mode 缺省时归一为 'edit'。"""
    t = CommandTemplateBase(name="ai", type="ai", config={"provider_id": 1})
    assert t.config["send_mode"] == "edit"


def test_template_ai_send_mode_send_new_accepted() -> None:
    """send_mode='send_new' 合法。"""
    t = CommandTemplateBase(
        name="ai", type="ai", config={"provider_id": 1, "send_mode": "send_new"}
    )
    assert t.config["send_mode"] == "send_new"


def test_template_ai_send_mode_invalid_rejected() -> None:
    """send_mode 只能是 'edit' / 'send_new'，其它值拒绝。"""
    with pytest.raises(ValueError):
        CommandTemplateBase(
            name="ai", type="ai", config={"provider_id": 1, "send_mode": "wat"}
        )


def test_template_run_plugin_requires_plugin_key() -> None:
    """run_plugin 必须配 plugin_key（占位类型也要校验基础结构）。"""
    with pytest.raises(ValueError):
        CommandTemplateBase(name="x", type="run_plugin", config={})
    CommandTemplateBase(
        name="x", type="run_plugin", config={"plugin_key": "forward"}
    )


def test_llm_provider_create_validates_provider() -> None:
    """provider 字段只能是 openai/anthropic/ollama。"""
    LLMProviderCreate(
        name="x", provider="openai", api_key="sk-x", default_model="gpt-4o"
    )
    with pytest.raises(ValueError):
        LLMProviderCreate(
            name="x", provider="bad-vendor", api_key="x", default_model="x"
        )


def test_protocol_profile_normalization_is_scoped_to_anthropic_messages() -> None:
    assert (
        normalize_protocol_profile("anthropic_messages", "claude_code_proxy")
        == "claude_code_proxy"
    )
    assert normalize_protocol_profile("responses", "claude_code_proxy") == "standard"
    assert normalize_protocol_profile("chat_completions", None) == "standard"


def test_provider_protocol_profile_defaults_and_validates() -> None:
    payload = LLMProviderCreate(
        name="anthropic-main",
        provider="anthropic",
        default_model="claude-haiku-4-5",
        api_format="anthropic_messages",
    )
    assert payload.protocol_profile == "standard"

    with pytest.raises(ValueError):
        LLMProviderCreate(
            name="anthropic-main",
            provider="anthropic",
            default_model="claude-haiku-4-5",
            api_format="anthropic_messages",
            protocol_profile="unknown",
        )


@pytest.mark.asyncio
async def test_create_provider_normalizes_profile_for_non_anthropic_protocol() -> None:
    class _Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class _DB:
        row: LLMProvider | None = None

        async def execute(self, _query):
            return _Result()

        def add(self, row: LLMProvider) -> None:
            self.row = row

        async def flush(self) -> None:
            assert self.row is not None
            self.row.id = 11
            self.row.created_at = datetime.now(UTC)

    db = _DB()
    out = await command_service.create_provider(
        db,
        LLMProviderCreate(
            name="responses-proxy",
            provider="openai",
            default_model="gpt-4o-mini",
            api_format="responses",
            protocol_profile="claude_code_proxy",
        ),
    )

    assert db.row is not None
    assert db.row.protocol_profile == "standard"
    assert out.protocol_profile == "standard"


@pytest.mark.asyncio
async def test_update_provider_resets_profile_when_leaving_anthropic_protocol() -> None:
    row = LLMProvider(
        id=12,
        name="anthropic-proxy",
        provider="anthropic",
        api_key_enc=None,
        base_url="https://proxy.example/v1",
        default_model="claude-haiku-4-5",
        api_format="anthropic_messages",
        protocol_profile="claude_code_proxy",
        created_at=datetime.now(UTC),
    )

    class _DB:
        async def get(self, _model, _pid):
            return row

        async def flush(self) -> None:
            return None

    out = await command_service.update_provider(
        _DB(),
        row.id,
        LLMProviderUpdate(api_format="responses"),
    )

    assert row.api_format == "responses"
    assert row.protocol_profile == "standard"
    assert out.protocol_profile == "standard"


def test_provider_out_and_dto_preserve_only_effective_protocol_profile() -> None:
    row = LLMProvider(
        id=13,
        name="anthropic-proxy",
        provider="anthropic",
        api_key_enc=None,
        base_url="https://proxy.example/v1",
        default_model="claude-haiku-4-5",
        api_format="anthropic_messages",
        protocol_profile="claude_code_proxy",
        created_at=datetime.now(UTC),
    )
    assert _provider_to_out(row).protocol_profile == "claude_code_proxy"
    assert LLMProviderDTO.from_orm_row(row).protocol_profile == "claude_code_proxy"

    row.api_format = "chat_completions"
    assert _provider_to_out(row).protocol_profile == "standard"
    dto = LLMProviderDTO.from_orm_row(row)
    assert dto.protocol_profile == "standard"
    assert dto.to_dict()["protocol_profile"] == "standard"


# ════════════════════════════════════════════════════════════
# 3) LLM client 错误脱敏
# ════════════════════════════════════════════════════════════


def test_safe_error_message_redacts_api_key() -> None:
    """错误消息含 api_key 时必须替换为 <redacted>。"""
    msg = "401 Unauthorized: api key sk-veryverysecret invalid"
    out = _safe_error_message(msg, "sk-veryverysecret")
    assert "sk-veryverysecret" not in out
    assert "<redacted>" in out


def test_safe_error_message_truncates_long() -> None:
    """超长错误消息应该被裁剪。"""
    long = "x" * 2000
    out = _safe_error_message(long, None)
    assert len(out) <= 410  # 400 + "..."


def test_build_client_unknown_provider() -> None:
    """未知 api_format → ValueError（之前测的是未知 provider，现在协议路由按 api_format 走）。"""
    row = LLMProvider(
        id=1,
        name="x",
        provider="openai",  # provider 合法，但 api_format 给个不存在的值
        api_key_enc=None,
        base_url=None,
        default_model="gpt-x",
        api_format="bogus_format",
        created_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError):
        build_client(row)


def test_build_client_requires_model() -> None:
    """default_model 与 override_model 都为空 → 报错（避免静默使用 None）。"""
    row = LLMProvider(
        id=1,
        name="x",
        provider="openai",
        api_key_enc=None,
        base_url=None,
        default_model="",
        created_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError):
        build_client(row, override_model=None)


@pytest.mark.asyncio
async def test_openai_client_error_status_redacts_key() -> None:
    """OpenAI 接口 4xx 时错误消息不能含 api_key 明文。"""
    cli = OpenAIClient(api_key="sk-secret-XYZ", base_url="https://api.example.com/v1", model="m")

    class _FakeResp:
        status_code = 401
        text = "auth failed: token sk-secret-XYZ rejected"

    fake_async_cli = AsyncMock()
    fake_async_cli.__aenter__.return_value = fake_async_cli
    fake_async_cli.post = AsyncMock(return_value=_FakeResp())

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake_async_cli):
        with pytest.raises(LLMError) as exc:
            await cli.complete("sys", "user")
    assert "sk-secret-XYZ" not in str(exc.value)


@pytest.mark.asyncio
async def test_openai_client_network_error_safe() -> None:
    """httpx 抛网络异常时也走脱敏逻辑。"""
    cli = OpenAIClient(api_key="sk-secret", base_url=None, model="gpt-x")

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(side_effect=httpx.ConnectError("dns"))
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        with pytest.raises(LLMError):
            await cli.complete("s", "u")


# ════════════════════════════════════════════════════════════
# 3.5) Proxy 透传给 httpx
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_client_passes_proxy_to_httpx() -> None:
    """OpenAIClient 拿到 proxy_url 时必须以 ``proxy=<url>`` 传给 httpx.AsyncClient。

    这条测试守住"配了代理但没生效"的回归——之前 _run_ai 直接 httpx.AsyncClient(timeout=...)
    根本不接 proxy 参数。
    """

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "model": "x",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_FakeResp())

    proxy_url = "socks5://user:pass@127.0.0.1:1080"
    with patch(
        "app.services.llm_client.httpx.AsyncClient", return_value=fake
    ) as mock_cls:
        cli = OpenAIClient(
            api_key="sk-x",
            base_url="https://api.example.com/v1",
            model="m",
            proxy_url=proxy_url,
        )
        await cli.complete("sys", "user")

    # 校验 httpx.AsyncClient 被构造时确实带了 proxy=<url>
    kwargs = mock_cls.call_args.kwargs
    assert kwargs.get("proxy") == proxy_url


@pytest.mark.asyncio
async def test_openai_client_no_proxy_kwarg_when_direct() -> None:
    """proxy_url=None 时不应给 httpx.AsyncClient 加 proxy 参数（避免 httpx 的 None 兼容性差异）。"""

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "model": "x",
                "usage": {},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_FakeResp())

    with patch(
        "app.services.llm_client.httpx.AsyncClient", return_value=fake
    ) as mock_cls:
        cli = OpenAIClient(
            api_key="sk-x", base_url="https://api.example.com/v1", model="m",
            proxy_url=None,
        )
        await cli.complete("sys", "user")

    kwargs = mock_cls.call_args.kwargs
    assert "proxy" not in kwargs


# ════════════════════════════════════════════════════════════
# 3.6) Vision：视觉模型支持（图片字节 → vision payload）
# ════════════════════════════════════════════════════════════
#
# 这几条守住"图片有没有真的被发到模型"的回归——之前
# _run_ai 只把 "📷 [图片]" 占位符塞 prompt，模型对着不存在的图瞎答。

# 1×1 像素的 PNG（magic bytes 用得到 / 太小不会被压缩；测 sniff_mime + 编码）
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_sniff_image_mime_recognises_png_jpeg_webp_gif() -> None:
    """magic bytes 识别 4 种主流图片格式，其它一律 jpeg 兜底。"""
    from app.services.llm_client import _sniff_image_mime

    assert _sniff_image_mime(_TINY_PNG) == "image/png"
    assert _sniff_image_mime(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"
    assert _sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4) == "image/webp"
    assert _sniff_image_mime(b"GIF89a" + b"\x00" * 10) == "image/gif"
    # 未知头 → jpeg 兜底（绝大多数 vision 模型按 jpeg 解码也能跑）
    assert _sniff_image_mime(b"unknown header bytes") == "image/jpeg"
    # 极短输入也别崩
    assert _sniff_image_mime(b"") == "image/jpeg"


@pytest.mark.asyncio
async def test_openai_client_vision_body_shape() -> None:
    """OpenAIClient 拿到 images 时，messages[user].content 必须是
    [{type:text}, {type:image_url, image_url:{url:data:image/...;base64,...}}] 数组。

    这是 OpenAI / mimo / GLM-4V / 大多数 OpenAI 兼容 vision API 的共同协议；
    回归这一条就是为了防止"图片字节被丢掉只发文字"的状况。"""

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "看到了"}}],
                "model": "mimo-v2.5",
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_FakeResp())

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = OpenAIClient(
            api_key="sk-x", base_url="https://api.example.com/v1", model="mimo-v2.5",
        )
        await cli.complete("sys", "这图里是什么", images=[_TINY_PNG])

    body = fake.post.call_args.kwargs["json"]
    user_msg = body["messages"][1]
    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert isinstance(content, list), "vision 调用时 user.content 必须是数组"
    assert content[0] == {"type": "text", "text": "这图里是什么"}
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), f"应是 PNG data URL，实际：{url[:40]}"


@pytest.mark.asyncio
async def test_openai_client_text_only_keeps_string_content() -> None:
    """没传 images 时 user.content 仍是字符串（向后兼容老调用，不破坏纯文本路径）。"""

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}}], "model": "m", "usage": {}}

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_FakeResp())

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = OpenAIClient(api_key="sk", base_url="https://x.example/v1", model="m")
        await cli.complete("sys", "纯文本", images=None)

    body = fake.post.call_args.kwargs["json"]
    headers = fake.post.call_args.kwargs["headers"]
    assert body["messages"][1]["content"] == "纯文本"
    # 0.57.0 起不再发送 TelePilot 产品 UA；直接构造的 client 无身份档案时不注入 UA。
    assert "TelePilot/" not in headers.get("User-Agent", "")


@pytest.mark.asyncio
async def test_anthropic_client_vision_body_shape() -> None:
    """AnthropicClient 拿到 images 时用
    [{type:image, source:{type:base64,media_type,data}}, {type:text}]——
    这与 OpenAI 的 image_url 协议**不一样**，必须分开测以防混用。

    Anthropic 客户端走 SSE 流式（``cli.stream(...)``），所以 mock 也得按
    async-context-manager 模式来——给一段最小可解析的 message_start /
    content_block_delta / message_delta SSE 即可。"""
    from app.services.llm_client import AnthropicClient

    # 极简 SSE 流：仅声明模型名 + 给一个 text delta + usage
    sse_lines = [
        "event: message_start",
        'data: {"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":1}}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好的"}}',
        "",
        "event: message_delta",
        'data: {"type":"message_delta","usage":{"output_tokens":1}}',
        "",
        "event: message_stop",
        'data: {"type":"message_stop"}',
        "",
    ]

    class _FakeStreamResp:
        status_code = 200

        async def aiter_lines(self):
            for line in sse_lines:
                yield line

        async def aiter_text(self):  # 错误路径才用得到
            yield ""

    class _FakeStreamCM:
        """``cli.stream(...)`` 返回的 async context manager。"""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return _FakeStreamResp()

        async def __aexit__(self, *exc):
            return False

    # 捕获 stream() 的 kwargs 用于 body 断言
    captured_kwargs: dict = {}

    def _make_stream(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeStreamCM(*args, **kwargs)

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.stream = _make_stream  # 不能用 AsyncMock：cli.stream 是同步函数返回 CM

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = AnthropicClient(api_key="sk", base_url=None, model="claude-x")
        await cli.complete("sys", "describe", images=[_TINY_PNG])

    body = captured_kwargs["json"]
    headers = captured_kwargs["headers"]
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    img_blk = content[0]
    assert img_blk["type"] == "image"
    assert img_blk["source"]["type"] == "base64"
    assert img_blk["source"]["media_type"] == "image/png"
    # data 是合法 base64
    import base64 as _b64
    assert _b64.b64decode(img_blk["source"]["data"]) == _TINY_PNG
    assert content[1] == {"type": "text", "text": "describe"}
    assert "anthropic-beta" not in headers
    assert "anthropic-dangerous-direct-browser-access" not in headers


@pytest.mark.asyncio
async def test_anthropic_proxy_profile_adds_only_explicit_compatibility_headers() -> None:
    from app.services.llm_client import AnthropicClient

    class _FakeStreamResp:
        status_code = 200

        async def aiter_lines(self):
            for line in (
                "event: message_start",
                'data: {"message":{"model":"claude","usage":{"input_tokens":1}}}',
                "",
                "event: content_block_delta",
                'data: {"delta":{"type":"text_delta","text":"ok"}}',
                "",
                "event: message_delta",
                'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}',
                "",
            ):
                yield line

        async def aiter_text(self):
            yield ""

    class _FakeStreamCM:
        async def __aenter__(self):
            return _FakeStreamResp()

        async def __aexit__(self, *_exc):
            return False

    captured: dict = {}

    def _stream(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeStreamCM()

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.stream = _stream
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = AnthropicClient(
            api_key="sk",
            base_url=None,
            model="claude",
            protocol_profile="claude_code_proxy",
        )
        result = await cli.complete("sys", "user")

    assert captured["headers"]["x-app"] == "cli"
    assert "claude-code" in captured["headers"]["anthropic-beta"]
    assert result.stop_reason.value == "completed"


@pytest.mark.asyncio
async def test_anthropic_client_stream_complete_yields_text_deltas() -> None:
    """stream_complete 应直接产出 Anthropic SSE 的 text_delta 增量。"""
    from app.services.llm_client import AnthropicClient

    sse_lines = [
        "event: message_start",
        'data: {"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":4}}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}',
        "",
        "event: message_delta",
        'data: {"type":"message_delta","usage":{"output_tokens":2}}',
        "",
        "event: message_stop",
        'data: {"type":"message_stop"}',
        "",
    ]

    class _FakeStreamResp:
        status_code = 200

        async def aiter_lines(self):
            for line in sse_lines:
                yield line

        async def aiter_text(self):
            yield ""

    class _FakeStreamCM:
        async def __aenter__(self):
            return _FakeStreamResp()

        async def __aexit__(self, *exc):
            return False

    captured_kwargs: dict = {}

    def _make_stream(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeStreamCM()

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.stream = _make_stream

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = AnthropicClient(api_key="sk", base_url=None, model="claude-x")
        chunks = [chunk async for chunk in cli.stream_complete("sys", "user")]

    assert captured_kwargs["json"]["stream"] is True
    assert [chunk.delta for chunk in chunks if chunk.delta] == ["你", "好"]
    assert chunks[-1].done is True
    assert chunks[-1].model == "claude-x"
    assert chunks[-1].input_tokens == 4
    assert chunks[-1].output_tokens == 2


@pytest.mark.asyncio
async def test_responses_client_vision_body_shape() -> None:
    """ResponsesClient 拿到 images 时用 [{type:input_text}, {type:input_image, image_url:...}]——
    OpenAI Responses 协议字段名跟 chat/completions 不同，独立测。"""
    from app.services.llm_client import ResponsesClient

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"output_text": "ok", "model": "gpt-x", "usage": {}}

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_FakeResp())

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = ResponsesClient(api_key="sk", base_url=None, model="gpt-x")
        await cli.complete("sys", "describe", images=[_TINY_PNG])

    body = fake.post.call_args.kwargs["json"]
    content = body["input"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "input_text", "text": "describe"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_responses_client_stream_complete_yields_text_deltas() -> None:
    """Responses streaming 应设置 stream=true，并 yield output_text.delta。"""
    from app.services.llm_client import ResponsesClient

    sse_lines = [
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"hello"}',
        "",
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":" world"}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{"model":"gpt-stream","status":"completed",'
        '"usage":{"input_tokens":3,"output_tokens":2}}}',
        "",
    ]

    class _FakeStreamResp:
        status_code = 200

        async def aiter_lines(self):
            for line in sse_lines:
                yield line

        async def aiter_text(self):
            yield ""

    class _FakeStreamCM:
        async def __aenter__(self):
            return _FakeStreamResp()

        async def __aexit__(self, *exc):
            return False

    captured_kwargs: dict = {}

    def _make_stream(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeStreamCM()

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.stream = _make_stream

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = ResponsesClient(api_key="sk", base_url=None, model="gpt-x")
        chunks = [chunk async for chunk in cli.stream_complete("sys", "user")]

    body = captured_kwargs["json"]
    assert body["stream"] is True
    assert [chunk.delta for chunk in chunks if chunk.delta] == ["hello", " world"]
    assert chunks[-1].done is True
    assert chunks[-1].model == "gpt-stream"
    assert chunks[-1].input_tokens == 3
    assert chunks[-1].output_tokens == 2


def test_build_client_passes_proxy_to_openai() -> None:
    """build_client 应把 proxy_url 传到具体 client 实例上。"""
    row = LLMProvider(
        id=1,
        name="x",
        provider="openai",
        api_key_enc=None,
        base_url=None,
        default_model="m",
        created_at=datetime.now(UTC),
    )
    cli = build_client(row, proxy_url="http://10.0.0.1:8080")
    assert isinstance(cli, OpenAIClient)
    # 私有字段；测试里直接读受控
    assert cli._proxy_url == "http://10.0.0.1:8080"


def test_build_client_passes_proxy_to_anthropic() -> None:
    from app.services.llm_client import AnthropicClient

    row = LLMProvider(
        id=1,
        name="x",
        provider="anthropic",
        api_key_enc=None,
        base_url=None,
        default_model="m",
        api_format="anthropic_messages",
        created_at=datetime.now(UTC),
    )
    cli = build_client(row, proxy_url="socks5://127.0.0.1:1080")
    assert isinstance(cli, AnthropicClient)
    assert cli._proxy_url == "socks5://127.0.0.1:1080"


# ════════════════════════════════════════════════════════════
# api_format 路由：build_client 按 api_format 选 client 类型
# ════════════════════════════════════════════════════════════


def test_build_client_api_format_chat_completions() -> None:
    """api_format=chat_completions → OpenAIClient（POST /chat/completions）。"""
    from app.services.llm_client import OpenAIClient

    row = LLMProvider(
        id=1,
        name="x",
        provider="openai",
        api_key_enc=None,
        base_url="https://api.example.com/v1",
        default_model="gpt-4o",
        api_format="chat_completions",
        created_at=datetime.now(UTC),
    )
    cli = build_client(row)
    assert isinstance(cli, OpenAIClient)


def test_build_client_api_format_responses() -> None:
    """api_format=responses → ResponsesClient（POST /responses）。"""
    from app.services.llm_client import ResponsesClient

    row = LLMProvider(
        id=1,
        name="x",
        provider="openai",
        api_key_enc=None,
        base_url="https://anyrouter.top/v1",
        default_model="gpt-5.5",
        api_format="responses",
        created_at=datetime.now(UTC),
    )
    cli = build_client(row)
    assert isinstance(cli, ResponsesClient)


def test_build_client_api_format_anthropic_messages() -> None:
    from app.services.llm_client import AnthropicClient

    row = LLMProvider(
        id=1,
        name="x",
        provider="anthropic",
        api_key_enc=None,
        base_url=None,
        default_model="claude-haiku-4-5",
        api_format="anthropic_messages",
        created_at=datetime.now(UTC),
    )
    cli = build_client(row)
    assert isinstance(cli, AnthropicClient)


def test_build_client_legacy_no_api_format_falls_back_by_provider() -> None:
    """老数据没 api_format 字段时按 provider 厂商兜底。"""
    from app.services.llm_client import AnthropicClient, OpenAIClient

    # openai → chat_completions
    row1 = LLMProvider(
        id=1,
        name="x",
        provider="openai",
        api_key_enc=None,
        base_url=None,
        default_model="gpt-4o",
        # api_format 不设
        created_at=datetime.now(UTC),
    )
    assert isinstance(build_client(row1), OpenAIClient)

    # anthropic → anthropic_messages
    row2 = LLMProvider(
        id=1,
        name="x",
        provider="anthropic",
        api_key_enc=None,
        base_url=None,
        default_model="claude-haiku-4-5",
        created_at=datetime.now(UTC),
    )
    assert isinstance(build_client(row2), AnthropicClient)


# ════════════════════════════════════════════════════════════
# ResponsesClient：协议解析（兼容 output_text / output[] 两种形态）
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_responses_client_parses_output_text_top_level() -> None:
    """部分实现直接给顶层 ``output_text`` 字段。"""
    from app.services.llm_client import ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url="https://api.example.com/v1", model="gpt-5.5")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "gpt-5.5-2025",
                "output_text": "hello",
                "usage": {"input_tokens": 5, "output_tokens": 1},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.complete("sys", "user")
    assert result.text == "hello"
    assert result.model == "gpt-5.5-2025"
    assert result.input_tokens == 5
    assert result.output_tokens == 1


@pytest.mark.asyncio
async def test_responses_client_parses_sse_completed_response() -> None:
    """Codex/CLIProxyAPI 类反代可能无视 stream=false，仍返回 Responses SSE。"""
    from app.services.llm_client import ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url="https://codex.example.com/v1", model="gpt-5.5")

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/event-stream; charset=utf-8"}
        text = (
            "event: response.created\n"
            'data: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}\n'
            "\n"
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"model":"gpt-5.5-codex","status":"completed",'
            '"output_text":"sse ok","usage":{"input_tokens":7,"output_tokens":2}}}\n'
            "\n"
        )

        @staticmethod
        def json():
            raise json.JSONDecodeError("Expecting value", "", 0)

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.complete("sys", "user")

    assert result.text == "sse ok"
    assert result.model == "gpt-5.5-codex"
    assert result.input_tokens == 7
    assert result.output_tokens == 2


@pytest.mark.asyncio
async def test_responses_client_parses_sse_text_delta_without_completed_body() -> None:
    """半兼容反代如果只给文本增量，也应折叠成 output_text。"""
    from app.services.llm_client import ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url="https://codex.example.com/v1", model="gpt-5.5")

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}
        text = (
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"hello"}\n'
            "\n"
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":" world"}\n'
            "\n"
            "event: response.output_text.done\n"
            'data: {"type":"response.output_text.done","text":"hello world"}\n'
            "\n"
        )

        @staticmethod
        def json():
            raise json.JSONDecodeError("Expecting value", "", 0)

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.complete("sys", "user")

    assert result.text == "hello world"
    assert result.model == "gpt-5.5"
    assert result.input_tokens == 0
    assert result.output_tokens == 0


@pytest.mark.asyncio
async def test_responses_client_keeps_sse_delta_when_completed_body_has_no_text() -> None:
    """Codex 反代可能把正文只放在 delta，completed body 只带状态和 usage。"""
    from app.services.llm_client import ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url="https://codex.example.com/v1", model="gpt-5.5")

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}
        text = (
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"hello"}\n'
            "\n"
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":" world"}\n'
            "\n"
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"model":"gpt-5.5-codex",'
            '"status":"completed","usage":{"input_tokens":7,"output_tokens":2}}}\n'
            "\n"
        )

        @staticmethod
        def json():
            raise json.JSONDecodeError("Expecting value", "", 0)

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.complete("sys", "user")

    assert result.text == "hello world"
    assert result.model == "gpt-5.5-codex"
    assert result.input_tokens == 7
    assert result.output_tokens == 2


@pytest.mark.asyncio
async def test_responses_client_never_drops_max_output_tokens() -> None:
    """输出上限是成本边界，兼容反代拒绝时必须失败，不能静默删掉。"""
    from app.services.llm_client import LLMError, ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url="https://api.example.com/v1", model="gpt-5.4")

    class _BadResp:
        status_code = 400
        text = '{"detail":"Unsupported parameter: max_output_tokens"}'

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_BadResp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        with pytest.raises(LLMError):
            await cli.complete("sys", "user", max_tokens=9)

    first_body = fake.post.await_args_list[0].kwargs["json"]
    assert first_body["max_output_tokens"] == 9
    assert fake.post.await_count == 1


@pytest.mark.asyncio
async def test_responses_client_can_strip_non_safety_compat_parameter() -> None:
    """非安全边界的 temperature 可针对半兼容 Responses 端点降级。"""
    from app.services.llm_client import ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url="https://codex.example.com/v1", model="gpt-5.5")

    class _BadTemperatureResp:
        status_code = 400
        text = '{"detail":"Unsupported parameter: temperature"}'

    class _OkResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "gpt-5.5",
                "output_text": "ok",
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(side_effect=[_BadTemperatureResp(), _OkResp()])
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.complete("sys", "user", max_tokens=9, temperature=0.7)

    assert result.text == "ok"
    first_body = fake.post.await_args_list[0].kwargs["json"]
    second_body = fake.post.await_args_list[1].kwargs["json"]
    assert first_body["max_output_tokens"] == 9
    assert first_body["temperature"] == 0.7
    assert first_body["stream"] is False
    assert second_body["max_output_tokens"] == 9
    assert "temperature" not in second_body
    assert second_body["stream"] is False


@pytest.mark.asyncio
async def test_responses_client_parses_output_array_form() -> None:
    """``output=[{type:message, content:[{type:output_text, text:"..."}]}]`` 形态。"""
    from app.services.llm_client import ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url=None, model="gpt-5.5")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "part1"},
                            {"type": "output_text", "text": " part2"},
                        ],
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.complete("sys", "user")
    assert result.text == "part1 part2"
    assert result.input_tokens == 10
    assert result.output_tokens == 4


@pytest.mark.asyncio
async def test_responses_client_generate_image_uses_image_generation_tool() -> None:
    """原生生图：Responses 应下发 image_generation 工具并解析 result 图片。"""
    from app.services.llm_client import ResponsesClient

    img_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 128).decode("ascii")
    cli = ResponsesClient(api_key="sk", base_url=None, model="gpt-5.5")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "gpt-5.5",
                "output": [
                    {"type": "image_generation_call", "result": img_b64},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                ],
                "usage": {"input_tokens": 8, "output_tokens": 2},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.generate_image("sys", "画猫")

    body = fake.post.call_args.kwargs["json"]
    assert body["tools"] == [{"type": "image_generation", "action": "generate"}]
    assert body["tool_choice"] == {"type": "image_generation"}
    assert body["instructions"] == "sys"
    assert result.image_data == [f"data:image/png;base64,{img_b64}"]
    assert result.text == "done"
    assert result.input_tokens == 8
    assert result.output_tokens == 2


@pytest.mark.asyncio
async def test_responses_image_generation_keeps_max_output_tokens() -> None:
    """Responses 生图也不能为兼容反代删除输出上限。"""
    from app.services.llm_client import LLMError, ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url=None, model="gpt-5.4")

    class _BadResp:
        status_code = 400
        text = '{"error":"Unsupported parameter: max_output_tokens"}'

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_BadResp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        with pytest.raises(LLMError):
            await cli.generate_image("sys", "画猫", max_tokens=12)

    first_body = fake.post.await_args_list[0].kwargs["json"]
    assert first_body["max_output_tokens"] == 12
    assert fake.post.await_count == 1


@pytest.mark.asyncio
async def test_openai_client_generate_image_uses_images_api() -> None:
    """OpenAI-compatible 原生生图：chat 协议 Provider 走 /images/generations。"""
    img_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 128).decode("ascii")
    cli = OpenAIClient(api_key="sk", base_url="https://api.example.com/v1", model="gpt-image-2")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"b64_json": img_b64}], "usage": {"input_tokens": 7}}

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.generate_image("sys", "画猫")

    assert fake.post.call_args.args[0] == "https://api.example.com/v1/images/generations"
    body = fake.post.call_args.kwargs["json"]
    assert body["model"] == "gpt-image-2"
    assert "用户需求：画猫" in body["prompt"]
    assert result.image_data == [f"data:image/png;base64,{img_b64}"]
    assert result.input_tokens == 7


@pytest.mark.asyncio
async def test_responses_client_web_search_body_and_sources() -> None:
    """开启 web_search 时应下发工具，并把来源提取到 LLMResult.sources。"""
    from app.services.llm_client import ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url=None, model="gpt-5.5")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "gpt-5.5",
                "output_text": "searched",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "searched",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/a",
                                        "title": "A",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "type": "web_search_call",
                        "action": {
                            "sources": [
                                {"url": "https://example.com/b", "title": "B"}
                            ]
                        },
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await cli.complete(
            "sys",
            "user",
            web_search=True,
            web_search_context_size="high",
        )
    body = fake.post.await_args.kwargs["json"]
    assert body["tools"] == [{"type": "web_search", "search_context_size": "high"}]
    assert body["include"] == ["web_search_call.action.sources"]
    assert result.sources == [
        {"url": "https://example.com/a", "title": "A"},
        {"url": "https://example.com/b", "title": "B"},
    ]


@pytest.mark.asyncio
async def test_responses_client_proxy_passed_to_httpx() -> None:
    """ResponsesClient 也要把 proxy 透传给 httpx（与 OpenAIClient 一致）。"""
    from app.services.llm_client import ResponsesClient

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"model": "x", "output_text": "ok", "usage": {}}

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch(
        "app.services.llm_client.httpx.AsyncClient", return_value=fake
    ) as mock_cls:
        cli = ResponsesClient(
            api_key="sk", base_url=None, model="x",
            proxy_url="socks5://127.0.0.1:1080",
        )
        await cli.complete("s", "u")
    assert mock_cls.call_args.kwargs.get("proxy") == "socks5://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_responses_client_4xx_redacts_key() -> None:
    """Responses 4xx 错误消息不能含 api_key。"""
    from app.services.llm_client import LLMError, ResponsesClient

    cli = ResponsesClient(api_key="sk-secret-Y", base_url=None, model="x")

    class _Resp:
        status_code = 404
        text = "model gpt-5.5 not found; key sk-secret-Y rejected"

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        with pytest.raises(LLMError) as exc:
            await cli.complete("s", "u")
    assert "sk-secret-Y" not in str(exc.value)
    assert "404" in str(exc.value)


@pytest.mark.asyncio
async def test_responses_client_520_includes_cf_hint() -> None:
    """Cloudflare 520 → 错误消息里要带"反代/上游"提示，让用户知道不是自己代码错。"""
    from app.services.llm_client import LLMError, ResponsesClient

    cli = ResponsesClient(api_key="sk", base_url=None, model="x")

    class _Resp:
        status_code = 520
        text = "Web server is returning an unknown error"

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        with pytest.raises(LLMError) as exc:
            await cli.complete("s", "u")
    msg = str(exc.value)
    assert "520" in msg
    # 有人话提示
    assert "反代" in msg or "上游" in msg or "Cloudflare" in msg


@pytest.mark.asyncio
async def test_responses_client_non_json_error_includes_response_summary() -> None:
    """非 JSON 响应要带状态码、content-type 和脱敏 body 摘要，便于排查反代返回。"""
    from app.services.llm_client import LLMError, ResponsesClient

    cli = ResponsesClient(api_key="sk-secret-Z", base_url=None, model="x")

    class _Resp:
        status_code = 200
        text = "<html>bad gateway sk-secret-Z</html>"
        headers = {"content-type": "text/html; charset=utf-8"}

        @staticmethod
        def json():
            raise json.JSONDecodeError("Expecting value", "", 0)

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        with pytest.raises(LLMError) as exc:
            await cli.complete("s", "u")
    msg = str(exc.value)
    assert "status=200" in msg
    assert "text/html" in msg
    assert "body=<html>bad gateway" in msg
    assert "sk-secret-Z" not in msg


@pytest.mark.asyncio
async def test_openai_client_404_hints_model_name() -> None:
    """404 → 提示 model 名 / 端点；用户最常因 model 名错命中这条。"""
    from app.services.llm_client import LLMError, OpenAIClient

    cli = OpenAIClient(api_key="sk", base_url="https://anyrouter.top/v1", model="gpt-5.5")

    class _Resp:
        status_code = 404
        text = '{"error":"当前 API 不支持所选模型 gpt-5.5"}'

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_Resp())
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        with pytest.raises(LLMError) as exc:
            await cli.complete("s", "u")
    msg = str(exc.value)
    assert "404" in msg
    assert "model" in msg.lower() or "Fetch" in msg


# ════════════════════════════════════════════════════════════
# worker._safe_exception_text：错误消息脱敏（去路径/token）
# ════════════════════════════════════════════════════════════


def test_safe_exception_text_strips_unix_path() -> None:
    """ImportError 之类的异常 message 含 ``/Users/...`` 路径时要被替换为 <path>。"""
    from app.worker.command import _safe_exception_text

    e = ImportError(
        "cannot import name 'X' from 'app.db.models.command' "
        "(/Users/anoyou/Desktop/telebot/backend/app/db/models/command.py)"
    )
    msg = _safe_exception_text(e)
    assert "/Users/anoyou" not in msg
    assert "<path>" in msg
    # 主要错误信息仍保留
    assert "ImportError" in msg
    assert "cannot import" in msg


def test_safe_exception_text_strips_windows_path() -> None:
    from app.worker.command import _safe_exception_text

    e = FileNotFoundError("could not open C:\\Users\\bob\\app\\foo.py")
    msg = _safe_exception_text(e)
    assert "C:\\" not in msg
    assert "<path>" in msg


def test_safe_exception_text_redacts_sk_key() -> None:
    from app.worker.command import _safe_exception_text

    e = RuntimeError("auth failed: token sk-veryverysecret-XYZ rejected")
    msg = _safe_exception_text(e)
    assert "sk-veryverysecret-XYZ" not in msg
    assert "<redacted>" in msg


def test_humanize_llm_error_redacts_path_and_token() -> None:
    from app.worker.command import _humanize_llm_error

    e = RuntimeError(
        "OpenAI 接口返回 401: key sk-veryverysecret-XYZ failed at /Users/me/app/config.py"
    )
    msg = _humanize_llm_error(e)
    assert "sk-veryverysecret" not in msg
    assert "/Users/me" not in msg
    assert "API Key" in msg or "鉴权失败" in msg


def test_safe_exception_text_truncates() -> None:
    from app.worker.command import _safe_exception_text

    e = RuntimeError("x" * 5000)
    msg = _safe_exception_text(e, max_len=200)
    assert len(msg) <= 201  # 200 + "…"


# ════════════════════════════════════════════════════════════
# 9) ProviderModel schema
# ════════════════════════════════════════════════════════════


def test_provider_model_schema_strips_id() -> None:
    """模型 ID 前后空格自动 strip；空 id 拒绝。"""
    from app.schemas.command import ProviderModel

    m = ProviderModel(id="  gpt-5.5  ", enabled=True, custom=False)
    assert m.id == "gpt-5.5"

    with pytest.raises(ValueError):
        ProviderModel(id="   ", enabled=True, custom=False)


def test_provider_create_accepts_models_list() -> None:
    """LLMProviderCreate 应接受 models 列表，类型校验通过。"""
    from app.schemas.command import LLMProviderCreate, ProviderModel

    p = LLMProviderCreate(
        name="x",
        provider="openai",
        default_model="gpt-4o",
        models=[
            ProviderModel(id="gpt-4o", enabled=True, custom=False),
            ProviderModel(id="gpt-4o-mini", enabled=False, custom=False),
        ],
    )
    assert len(p.models) == 2
    assert p.models[0].enabled is True
    assert p.models[1].enabled is False


def test_provider_create_default_models_empty() -> None:
    """models 字段不传时默认空 list。"""
    from app.schemas.command import LLMProviderCreate

    p = LLMProviderCreate(
        name="x", provider="openai", default_model="gpt-4o"
    )
    assert p.models == []


def test_ai_command_image_codex_allows_missing_provider() -> None:
    """image + codex_image 后端可以不选择 LLM Provider。"""
    from app.schemas.command import CommandTemplateCreate

    tpl = CommandTemplateCreate(
        name="image",
        type="ai",
        config={"mode": "image", "image_backend": "codex_image"},
    )

    assert tpl.config["mode"] == "image"
    assert tpl.config["image_backend"] == "codex_image"


def test_ai_command_video_plugin_allows_missing_provider() -> None:
    """video 先走独立插件后端，不强制绑定 LLM Provider。"""
    from app.schemas.command import CommandTemplateCreate

    tpl = CommandTemplateCreate(
        name="video",
        type="ai",
        config={"mode": "video", "video_plugin_key": "video_bridge"},
    )

    assert tpl.config["mode"] == "video"
    assert tpl.config["video_backend"] == "plugin"
    assert tpl.config["video_plugin_key"] == "video_bridge"


def test_ai_command_search_forces_web_search() -> None:
    """search 模式保存时强制开启 web_search。"""
    from app.schemas.command import CommandTemplateCreate

    tpl = CommandTemplateCreate(
        name="search",
        type="ai",
        config={"mode": "search", "provider_id": 1, "web_search": False},
    )

    assert tpl.config["mode"] == "search"
    assert tpl.config["web_search"] is True


# ════════════════════════════════════════════════════════════
# 10) test-model endpoint：成功 / 失败路径
# ════════════════════════════════════════════════════════════


def _capture_llm_usage(monkeypatch):
    from app.services import llm_runtime, llm_usage_service

    emitted = []

    async def _emit(record):
        emitted.append(record)

    monkeypatch.setattr(llm_usage_service, "ensure_llm_usage_callback_registered", lambda: None)
    monkeypatch.setattr(llm_runtime, "_emit_usage", _emit)
    return emitted


@pytest.mark.asyncio
async def test_test_model_endpoint_success(monkeypatch) -> None:
    """test-model 成功路径：返 ok=True + 延时 + preview。"""
    from app.api import commands as cmds_api
    from app.schemas.command import TestModelRequest
    from app.services.llm_client import LLMResult

    fake_db = AsyncMock()
    # get_provider_row 返一个真 LLMProvider 实例
    row = LLMProvider(
        id=1,
        name="x",
        provider="openai",
        api_key_enc=None,
        base_url="https://api.example.com/v1",
        default_model="gpt-4o",
        created_at=datetime.now(UTC),
    )

    async def _get_provider_row(_db, _pid):
        return row

    monkeypatch.setattr(cmds_api.command_service, "get_provider_row", _get_provider_row)
    monkeypatch.setattr(cmds_api, "_resolve_proxy_url", AsyncMock(return_value=None))

    fake_client = AsyncMock()
    fake_client.complete = AsyncMock(
        return_value=LLMResult(text="pong", model="gpt-4o-2025", input_tokens=1, output_tokens=1)
    )
    # commands.py:test_model 用 ``from ..services.llm_client import build_client`` (function-level)，
    # 每次调用都会重新 import；所以 patch llm_client.build_client 即可
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "build_client", lambda *a, **k: fake_client)
    emitted = _capture_llm_usage(monkeypatch)

    out = await cmds_api.test_model(
        pid=1, payload=TestModelRequest(model="gpt-4o"), db=fake_db, user=AsyncMock()
    )
    assert out.ok is True
    assert out.model == "gpt-4o-2025"
    assert out.preview == "pong"
    assert out.latency_ms >= 0
    assert len(emitted) == 1
    assert emitted[0].source == "diagnostic:test-model"
    assert emitted[0].success is True
    assert emitted[0].input_tokens == 1
    assert emitted[0].output_tokens == 1


@pytest.mark.asyncio
async def test_test_model_endpoint_llm_error(monkeypatch) -> None:
    """test-model 失败路径：LLMError 被捕获，返 ok=False + error。"""
    from app.api import commands as cmds_api
    from app.schemas.command import TestModelRequest
    from app.services import llm_client
    from app.services.llm_client import LLMError

    row = LLMProvider(
        id=1,
        name="x",
        provider="openai",
        api_key_enc=None,
        base_url="https://api.example.com/v1",
        default_model="gpt-4o",
        created_at=datetime.now(UTC),
    )

    async def _get_provider_row(_db, _pid):
        return row

    monkeypatch.setattr(cmds_api.command_service, "get_provider_row", _get_provider_row)
    monkeypatch.setattr(cmds_api, "_resolve_proxy_url", AsyncMock(return_value=None))

    fake_cli = AsyncMock()
    fake_cli.complete = AsyncMock(side_effect=LLMError("OpenAI 接口返回 404: Model not found"))
    monkeypatch.setattr(llm_client, "build_client", lambda *a, **k: fake_cli)
    emitted = _capture_llm_usage(monkeypatch)

    out = await cmds_api.test_model(
        pid=1,
        payload=TestModelRequest(model="bogus"),
        db=AsyncMock(),
        user=AsyncMock(),
    )
    assert out.ok is False
    assert out.error and "404" in out.error
    assert out.latency_ms >= 0
    assert len(emitted) == 1
    assert emitted[0].source == "diagnostic:test-model"
    assert emitted[0].success is False
    assert emitted[0].error_type == "llmerror"


@pytest.mark.asyncio
async def test_chat_test_models_endpoint_success(monkeypatch) -> None:
    """chat-test-models 走真实聊天入参：自定义测试语 + 历史 + token/timeout。"""
    from app.api import commands as cmds_api
    from app.services import llm_client
    from app.services.llm_client import LLMResult

    row = LLMProvider(
        id=1,
        name="AnyGPT",
        provider="openai",
        api_key_enc=None,
        base_url="https://api.example.com/v1",
        default_model="gpt-4o",
        created_at=datetime.now(UTC),
    )

    async def _get_provider_row(_db, _pid):
        return row

    captured: dict[str, object] = {}

    class _FakeClient:
        async def complete(self, system, user, **kwargs):
            captured["system"] = system
            captured["user"] = user
            captured["kwargs"] = kwargs
            return LLMResult(
                text="我在想晚饭吃什么。",
                model="model-a-real",
                input_tokens=11,
                output_tokens=7,
            )

    def _build_client(provider, **kwargs):
        captured["provider"] = provider
        captured["build_kwargs"] = kwargs
        return _FakeClient()

    monkeypatch.setattr(cmds_api.command_service, "get_provider_row", _get_provider_row)
    monkeypatch.setattr(cmds_api, "_resolve_proxy_url", AsyncMock(return_value="socks5://127.0.0.1:1080"))
    monkeypatch.setattr(llm_client, "build_client", _build_client)
    emitted = _capture_llm_usage(monkeypatch)

    payload = ChatTestModelsRequest(
        models=["model-a"],
        message="我好无聊，你在想啥？",
        history=[
            {"role": "user", "content": "上一句"},
            {"role": "assistant", "content": "上一答"},
        ],
        system_prompt="你叫阿光。",
        max_tokens=1234,
        timeout_seconds=77,
    )
    out = await cmds_api.chat_test_models(
        pid=1,
        payload=payload,
        db=AsyncMock(),
        _user=AsyncMock(),
    )

    assert out.provider_id == 1
    assert out.provider_name == "AnyGPT"
    assert len(out.results) == 1
    result = out.results[0]
    assert result.ok is True
    assert result.requested_model == "model-a"
    assert result.model == "model-a-real"
    assert result.response == "我在想晚饭吃什么。"
    assert result.preview == "我在想晚饭吃什么。"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert captured["system"] == "你叫阿光。"
    assert "上一句" in str(captured["user"])
    assert "上一答" in str(captured["user"])
    assert "用户刚刚说：" in str(captured["user"])
    assert "我好无聊，你在想啥？" in str(captured["user"])
    assert captured["build_kwargs"] == {
        "override_model": "model-a",
        "proxy_url": "socks5://127.0.0.1:1080",
    }
    assert captured["kwargs"] == {"max_tokens": 1234, "timeout_seconds": 77}
    assert len(emitted) == 1
    assert emitted[0].source == "diagnostic:chat-test"
    assert emitted[0].success is True
    assert emitted[0].input_tokens == 11
    assert emitted[0].output_tokens == 7


@pytest.mark.asyncio
async def test_chat_test_models_endpoint_empty_response(monkeypatch) -> None:
    """上游完成但空文本时，前端需要拿到明确的空返回状态。"""
    from app.api import commands as cmds_api
    from app.services import llm_client
    from app.services.llm_client import LLMResult

    row = LLMProvider(
        id=1,
        name="AnyGPT",
        provider="openai",
        api_key_enc=None,
        base_url="https://api.example.com/v1",
        default_model="gpt-4o",
        created_at=datetime.now(UTC),
    )

    async def _get_provider_row(_db, _pid):
        return row

    class _FakeClient:
        async def complete(self, *_args, **_kwargs):
            return LLMResult(text="  ", model="model-empty", input_tokens=3, output_tokens=0)

    monkeypatch.setattr(cmds_api.command_service, "get_provider_row", _get_provider_row)
    monkeypatch.setattr(cmds_api, "_resolve_proxy_url", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_client, "build_client", lambda *a, **k: _FakeClient())

    out = await cmds_api.chat_test_models(
        pid=1,
        payload=ChatTestModelsRequest(models=["model-empty"], message="说句话"),
        db=AsyncMock(),
        _user=AsyncMock(),
    )

    result = out.results[0]
    assert result.ok is False
    assert result.empty_response is True
    assert result.response is None
    assert result.error == "上游请求已完成，但返回文本为空。"


@pytest.mark.asyncio
async def test_chat_test_models_endpoint_llm_error(monkeypatch) -> None:
    """LLMError 按单模型失败返回，不应拖垮同批其它模型。"""
    from app.api import commands as cmds_api
    from app.services import llm_client
    from app.services.llm_client import LLMError, LLMResult

    row = LLMProvider(
        id=1,
        name="AnyGPT",
        provider="openai",
        api_key_enc=None,
        base_url="https://api.example.com/v1",
        default_model="gpt-4o",
        created_at=datetime.now(UTC),
    )

    async def _get_provider_row(_db, _pid):
        return row

    class _FakeClient:
        def __init__(self, model: str):
            self.model = model

        async def complete(self, *_args, **_kwargs):
            if self.model == "bad-model":
                raise LLMError("OpenAI 接口返回 404: Model not found")
            return LLMResult(text="好的", model=self.model, input_tokens=1, output_tokens=1)

    def _build_client(_provider, **kwargs):
        return _FakeClient(str(kwargs.get("override_model")))

    monkeypatch.setattr(cmds_api.command_service, "get_provider_row", _get_provider_row)
    monkeypatch.setattr(cmds_api, "_resolve_proxy_url", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_client, "build_client", _build_client)

    out = await cmds_api.chat_test_models(
        pid=1,
        payload=ChatTestModelsRequest(models=["ok-model", "bad-model"], message="测一下"),
        db=AsyncMock(),
        _user=AsyncMock(),
    )

    by_model = {item.requested_model: item for item in out.results}
    assert by_model["ok-model"].ok is True
    assert by_model["bad-model"].ok is False
    assert "404" in (by_model["bad-model"].error or "")


# ════════════════════════════════════════════════════════════
# 4) worker 模板派发
# ════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_ctx():
    """每个测试都从干净的 worker ctx 起步。"""
    wcmd.set_command_context(
        wcmd.CommandContext(account_id=1, templates={}, providers={})
    )
    yield
    wcmd.set_command_context(
        wcmd.CommandContext(account_id=1, templates={}, providers={})
    )


@pytest.mark.asyncio
async def test_run_template_reply_text_substitutes_args() -> None:
    """reply_text 模板的 {args} 占位符应被命令参数替换。"""
    tpl = {
        "id": 1,
        "name": "hi",
        "type": "reply_text",
        "config": {"text": "hello {args}"},
        "description": None,
    }
    client = AsyncMock()
    event = AsyncMock()
    await wcmd._run_template(client, event, ["world", "again"], tpl, account_id=1)
    event.edit.assert_called_once_with("hello world again")


@pytest.mark.asyncio
async def test_run_template_reply_text_empty() -> None:
    """text 为空时回退一个占位文本，避免 TG edit 失败。"""
    tpl = {"name": "x", "type": "reply_text", "config": {"text": ""}}
    client = AsyncMock()
    event = AsyncMock()
    await wcmd._run_template(client, event, [], tpl, account_id=1)
    args = event.edit.call_args[0][0]
    assert args  # 非空


@pytest.mark.asyncio
async def test_run_template_forward_to_no_replied() -> None:
    """forward_to 在用户没回复消息时给出友好提示。"""
    tpl = {
        "name": "f",
        "type": "forward_to",
        "config": {"target_chat_id": -1001},
    }
    client = AsyncMock()
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    await wcmd._run_template(client, event, [], tpl, account_id=1)
    args = event.edit.call_args[0][0]
    assert "请回复" in args


@pytest.mark.asyncio
async def test_run_template_forward_to_invalid_target() -> None:
    """forward_to.target_chat_id 不是数字 → friendly 错误。"""
    tpl = {
        "name": "f",
        "type": "forward_to",
        "config": {"target_chat_id": "not-a-number"},
    }
    client = AsyncMock()
    event = AsyncMock()
    # 模拟有被回复消息（但 chat_id 不合法）
    replied = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=replied)
    await wcmd._run_template(client, event, [], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "target_chat_id" in msg


@pytest.mark.asyncio
async def test_run_template_forward_to_success() -> None:
    """forward_to 正常路径：调 replied.forward_to，再 edit ✓ 提示。"""
    tpl = {
        "name": "f",
        "type": "forward_to",
        "config": {"target_chat_id": -1001234567890},
    }
    client = AsyncMock()
    event = AsyncMock()
    replied = AsyncMock()
    replied.forward_to = AsyncMock(return_value=None)
    event.get_reply_message = AsyncMock(return_value=replied)
    await wcmd._run_template(client, event, [], tpl, account_id=1)
    replied.forward_to.assert_awaited_once_with(-1001234567890)
    final_msg = event.edit.call_args[0][0]
    assert "✓" in final_msg


@pytest.mark.asyncio
async def test_run_template_forward_to_copy_media_sends_file() -> None:
    """copy_media 应复制被回复消息的媒体，适合贴纸/图片/文件复读。"""
    tpl = {
        "name": "f",
        "type": "forward_to",
        "config": {"target_chat_id": -1001234567890, "mode": "copy_media"},
    }
    client = AsyncMock()
    event = AsyncMock()
    event.client = AsyncMock()
    replied = AsyncMock()
    replied.media = object()
    replied.text = ""
    event.get_reply_message = AsyncMock(return_value=replied)

    await wcmd._run_template(client, event, [], tpl, account_id=1)

    event.client.send_file.assert_awaited_once_with(-1001234567890, replied.media)
    replied.forward_to.assert_not_called()
    assert "✓" in event.edit.call_args[0][0]


@pytest.mark.asyncio
async def test_run_template_forward_to_copy_media_falls_back_to_text() -> None:
    """copy_media 遇到纯文本消息时退回普通 send_message。"""
    tpl = {
        "name": "f",
        "type": "forward_to",
        "config": {"target_chat_id": 42, "mode": "copy_media"},
    }
    client = AsyncMock()
    event = AsyncMock()
    event.client = AsyncMock()
    replied = AsyncMock()
    replied.media = None
    replied.document = None
    replied.photo = None
    replied.text = "hello"
    event.get_reply_message = AsyncMock(return_value=replied)

    await wcmd._run_template(client, event, [], tpl, account_id=1)

    event.client.send_message.assert_awaited_once_with(42, "hello")
    event.client.send_file.assert_not_called()


@pytest.mark.asyncio
async def test_run_template_forward_to_delete_immediately_skips_success_edit() -> None:
    """delete_immediately=True 时，不再编辑成功提示，直接删命令消息。"""
    import asyncio as _aio

    tpl = {
        "name": "f",
        "type": "forward_to",
        "config": {"target_chat_id": 4242, "delete_immediately": True},
    }
    client = AsyncMock()
    event = AsyncMock()
    event.chat_id = 4242
    replied = AsyncMock()
    replied.forward_to = AsyncMock(return_value=None)
    event.get_reply_message = AsyncMock(return_value=replied)
    event.delete = AsyncMock(return_value=None)

    await wcmd._run_template(client, event, [], tpl, account_id=1)
    await _aio.sleep(0)

    event.edit.assert_not_called()
    event.delete.assert_awaited()


@pytest.mark.asyncio
async def test_run_template_forward_to_default_target_uses_event_chat() -> None:
    """target_chat_id 缺省 / 空串 → 用触发消息的 chat_id 作为转发目标。"""
    for cfg in ({}, {"target_chat_id": ""}, {"target_chat_id": None}):
        tpl = {"name": "f", "type": "forward_to", "config": cfg}
        client = AsyncMock()
        event = AsyncMock()
        event.chat_id = 4242
        replied = AsyncMock()
        replied.forward_to = AsyncMock(return_value=None)
        event.get_reply_message = AsyncMock(return_value=replied)
        await wcmd._run_template(client, event, [], tpl, account_id=1)
        replied.forward_to.assert_awaited_once_with(4242)


@pytest.mark.asyncio
async def test_run_template_forward_to_delete_after_schedules_delete() -> None:
    """delete_after>0 时应在 sleep 后调用 event.delete()。"""
    import asyncio as _aio

    tpl = {
        "name": "f",
        "type": "forward_to",
        "config": {"target_chat_id": 4242, "delete_after": 1},
    }
    client = AsyncMock()
    event = AsyncMock()
    event.chat_id = 4242
    replied = AsyncMock()
    replied.forward_to = AsyncMock(return_value=None)
    event.get_reply_message = AsyncMock(return_value=replied)
    event.delete = AsyncMock(return_value=None)
    await wcmd._run_template(client, event, [], tpl, account_id=1)
    # 调度的删除任务尚未跑；让出控制权 + 等到 sleep 完成
    await _aio.sleep(1.2)
    event.delete.assert_awaited()


@pytest.mark.asyncio
async def test_run_template_unknown_type() -> None:
    """未知 type → friendly 错误。"""
    tpl = {"name": "x", "type": "nonsense", "config": {}}
    client = AsyncMock()
    event = AsyncMock()
    await wcmd._run_template(client, event, [], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "未知模板类型" in msg


@pytest.mark.asyncio
async def test_run_ai_missing_provider_id() -> None:
    """AI 命令缺 provider_id 时给提示而不是抛异常。"""
    tpl = {"name": "ai", "type": "ai", "config": {}}
    client = AsyncMock()
    event = AsyncMock()
    await wcmd._run_ai(client, event, ["问题"], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "provider_id" in msg


@pytest.mark.asyncio
async def test_run_ai_provider_not_loaded() -> None:
    """provider_id 在 ctx.providers 中不存在 → 友好错误。"""
    wcmd.set_command_context(
        wcmd.CommandContext(account_id=1, templates={}, providers={})
    )
    tpl = {
        "name": "ai",
        "type": "ai",
        "config": {"provider_id": 999},
    }
    client = AsyncMock()
    event = AsyncMock()
    await wcmd._run_ai(client, event, ["q"], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "999" in msg


@pytest.mark.asyncio
async def test_run_ai_rejects_image_on_non_vision_provider() -> None:
    """被回复消息含图但 provider 是纯文本（modality=text）时，必须**拒答**而非让模型瞎编。

    这是反幻觉守卫的关键测试——之前会把 "📷 [图片]" 占位符当文字塞进 prompt，
    模型对着不存在的图随便编一个答案（用户截图里 "这是 Jump 第 8 期封面" 就是这种）。"""
    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                7: {
                    "id": 7,
                    "name": "text-only-provider",
                    "provider": "openai",
                    "api_key_enc": None,
                    "base_url": None,
                    "default_model": "gpt-text",
                    "modality": "text",  # ← 关键：纯文本模型
                    "tags": ["chat"],
                    "cost_tier": 1,
                    "notes": None,
                    "proxy_url": None,
                    "models": [],
                }
            },
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 7, "routing_mode": "fixed"}}
    # 模拟被回复消息含图
    replied = AsyncMock()
    replied.text = ""
    replied.message = ""
    replied.photo = object()  # 非 None 即视为含图
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=replied)
    event.message = AsyncMock()
    event.message.photo = None  # 命令消息自己没图（self-photo 路径不该误判）
    client = AsyncMock()
    await wcmd._run_ai(client, event, ["这图里是什么"], tpl, account_id=1)
    final_msg = event.edit.call_args[0][0]
    assert "✗" in final_msg
    assert "vision" in final_msg or "识图" in final_msg
    # 关键：必须 NOT 调用 LLM——download_media 也不能被调
    replied.download_media.assert_not_called()


@pytest.mark.asyncio
async def test_run_ai_downloads_image_for_vision_provider(monkeypatch) -> None:
    """provider modality=vision 时应下载图片字节并以 ``images=[bytes]`` 传给 LLM。"""
    from app.services import llm_invoke as service_ai_runtime
    from app.services.llm_client import LLMResult
    from app.services.llm_dto import LLMProviderDTO

    invoke_mock = AsyncMock(
        return_value=(
            LLMResult(text="一张猫的图", model="mimo-v2.5", input_tokens=1, output_tokens=1),
            LLMProviderDTO(id=9, name="mimo-cn", provider="openai", default_model="mimo-v2.5"),
            False,
        )
    )
    monkeypatch.setattr(service_ai_runtime, "invoke", invoke_mock)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                9: {
                    "id": 9,
                    "name": "mimo-cn",
                    "provider": "openai",
                    "api_key_enc": None,
                    "base_url": "https://api.example.com/v1",
                    "default_model": "mimo-v2.5",
                    "modality": "vision",  # ← 关键
                    "tags": ["vision"],
                    "cost_tier": 2,
                    "notes": None,
                    "proxy_url": None,
                    "models": [],
                }
            },
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 9, "routing_mode": "fixed"}}
    replied = AsyncMock()
    replied.text = ""
    replied.message = ""
    replied.photo = object()
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 200
    replied.download_media = AsyncMock(return_value=fake_jpeg)
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=replied)
    event.message = AsyncMock()
    event.message.photo = None  # 命令消息自己没图，仅 replied 有
    client = AsyncMock()
    await wcmd._run_ai(client, event, ["这图里是什么"], tpl, account_id=1)

    # 校验图片字节确实被传给 LLM
    assert invoke_mock.await_args.kwargs["images"] == [fake_jpeg], "图片字节应原样传给 LLM"
    # 反幻觉规则注入
    assert "严格规则" in invoke_mock.await_args.args[2]
    # 占位符 "📷 [图片]" 不应继续出现在 user prompt（图片字节已单独发，避免双重提示）
    assert "[图片]" not in invoke_mock.await_args.args[3]


@pytest.mark.asyncio
async def test_run_ai_downloads_self_photo_caption_mode(monkeypatch) -> None:
    """命令消息**自身**含图（caption 触发模式：用户发 "图 + ,ai 这是什么"）也要下载并发给 vision。

    回归这一条是因为之前只看 ``replied.photo``，自己发图时反而被
    反幻觉守卫误伤——明明带了图却被告知"未收到图像数据"。"""
    from app.services import llm_invoke as service_ai_runtime
    from app.services.llm_client import LLMResult
    from app.services.llm_dto import LLMProviderDTO

    invoke_mock = AsyncMock(
        return_value=(
            LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1),
            LLMProviderDTO(id=9, name="v", provider="openai", default_model="m"),
            False,
        )
    )
    monkeypatch.setattr(service_ai_runtime, "invoke", invoke_mock)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                9: {
                    "id": 9, "name": "v", "provider": "openai", "api_key_enc": None,
                    "base_url": None, "default_model": "m", "modality": "vision",
                    "tags": [], "cost_tier": 2, "notes": None, "proxy_url": None, "models": [],
                }
            },
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 9, "routing_mode": "fixed"}}
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    self_msg = AsyncMock()
    self_msg.photo = object()
    self_msg.download_media = AsyncMock(return_value=fake_png)
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)  # ← 没回复任何消息
    event.message = self_msg
    client = AsyncMock()
    await wcmd._run_ai(client, event, ["这是什么"], tpl, account_id=1)

    assert invoke_mock.await_args.kwargs["images"] == [fake_png]
    self_msg.download_media.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_ai_self_photo_routes_to_vision_in_auto_mode(monkeypatch) -> None:
    """auto 模式下命令消息自带图也要触发视觉路由（pick_provider 看到 has_photo=True）。"""
    from app.services import llm_invoke as service_ai_runtime
    from app.services import llm_router as _lr
    from app.services.llm_client import LLMResult
    from app.services.llm_dto import LLMProviderDTO

    captured_router: dict[str, object] = {}

    async def _fake_pick(user_q, replied_text, has_photo, providers, **kw):
        captured_router["has_photo"] = has_photo

        class _D:
            provider_id = 9
            reason = "vision"

        return _D()

    monkeypatch.setattr(_lr, "pick_provider", _fake_pick)

    invoke_mock = AsyncMock(
        return_value=(
            LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1),
            LLMProviderDTO(id=9, name="v", provider="openai", default_model="m"),
            False,
        )
    )
    monkeypatch.setattr(service_ai_runtime, "invoke", invoke_mock)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                9: {
                    "id": 9, "name": "v", "provider": "openai", "api_key_enc": None,
                    "base_url": None, "default_model": "m", "modality": "vision",
                    "tags": [], "cost_tier": 2, "notes": None, "proxy_url": None, "models": [],
                }
            },
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 9, "routing_mode": "auto"}}
    self_msg = AsyncMock()
    self_msg.photo = object()
    self_msg.download_media = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    client = AsyncMock()
    await wcmd._run_ai(client, event, ["q"], tpl, account_id=1)
    # 路由器应收到 has_photo=True（带图自发也算视觉请求）
    assert captured_router["has_photo"] is True


@pytest.mark.asyncio
async def test_run_ai_rejects_oversized_image(monkeypatch) -> None:
    """图片体积超 4MB 时拒掉，避免 token 烧爆。"""
    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                9: {
                    "id": 9, "name": "v", "provider": "openai", "api_key_enc": None,
                    "base_url": None, "default_model": "m", "modality": "vision",
                    "tags": [], "cost_tier": 2, "notes": None, "proxy_url": None, "models": [],
                }
            },
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 9, "routing_mode": "fixed"}}
    replied = AsyncMock()
    replied.text = ""
    replied.message = ""
    replied.photo = object()
    replied.download_media = AsyncMock(return_value=b"\x00" * (5 * 1024 * 1024))
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=replied)
    event.message = AsyncMock()
    event.message.photo = None
    client = AsyncMock()
    await wcmd._run_ai(client, event, [], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "✗" in msg
    assert "MB" in msg or "体积" in msg


# ════════════════════════════════════════════════════════════
# 5) help 命令融合内置 + 模板
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_builtin_help_lists_templates() -> None:
    """,help 输出应同时列出内置命令和已启用的模板命令。"""
    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={
                "hi": {
                    "id": 1,
                    "name": "hi",
                    "type": "reply_text",
                    "config": {"text": "hello"},
                    "description": "say hi",
                }
            },
            providers={},
        )
    )
    client = AsyncMock()
    event = AsyncMock()
    await wcmd._BUILTIN["help"].handler(client, event, [], 1)
    args = event.edit.call_args[0][0]
    # 内置命令仍存在
    assert "ping" in args
    # 模板命令也出现
    assert "hi" in args
    assert "[reply_text]" in args

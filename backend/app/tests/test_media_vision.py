"""媒体抽取层 + 视觉/STT 多路径回归测试。

只测 ``app.worker.media`` 的纯逻辑函数 + ``_run_ai`` 在各种媒体形态下的分支。
不出网，不依赖 telethon。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import llm_client as _lc
from app.services.llm_client import LLMResult
from app.worker import command as wcmd
from app.worker import media as _media


@pytest.fixture(autouse=True)
def _allow_llm_budget(monkeypatch):
    """媒体单测隔离预算后端；预算故障行为由预算服务单测覆盖。"""
    from app.services import llm_account_budget

    monkeypatch.setattr(
        llm_account_budget,
        "acquire",
        AsyncMock(return_value=llm_account_budget.LLMAccountBudgetTicket(1, 1, 512)),
    )
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock(return_value=None))

# ── 静态 helpers ──────────────────────────────────────────────


def _msg(**kw):
    """构造一条假的 telethon Message——必须显式声明每个字段，
    避免 AsyncMock 自动生成属性导致 ``getattr(msg, 'sticker', None)`` 误判非 None。"""
    return SimpleNamespace(
        id=kw.pop("id", 1),
        chat_id=kw.pop("chat_id", 100),
        text=kw.pop("text", ""),
        message=kw.pop("message", ""),
        photo=kw.pop("photo", None),
        document=kw.pop("document", None),
        sticker=kw.pop("sticker", None),
        voice=kw.pop("voice", None),
        audio=kw.pop("audio", None),
        video=kw.pop("video", None),
        video_note=kw.pop("video_note", None),
        gif=kw.pop("gif", None),
        geo=kw.pop("geo", None),
        contact=kw.pop("contact", None),
        poll=kw.pop("poll", None),
        file=kw.pop("file", None),
        media=kw.pop("media", None),
        grouped_id=kw.pop("grouped_id", None),
        download_media=kw.pop("download_media", AsyncMock(return_value=b"data")),
        **kw,
    )


# ════════════════════════════════════════════════════════════
# 1) message_has_image / message_has_audio
# ════════════════════════════════════════════════════════════


def test_message_has_image_photo() -> None:
    assert _media.message_has_image(_msg(photo=object())) is True


def test_message_has_image_image_document() -> None:
    """按文件发送的未压缩图（document with image/jpeg mime）应被识别为图。"""
    doc = SimpleNamespace(mime_type="image/jpeg")
    assert _media.message_has_image(_msg(document=doc)) is True


def test_message_has_image_static_sticker() -> None:
    """静态 webp 贴纸算图（vision 模型能读）。"""
    doc = SimpleNamespace(mime_type="image/webp")
    sticker = object()
    assert _media.message_has_image(_msg(document=doc, sticker=sticker)) is True


def test_message_has_image_animated_sticker_skipped() -> None:
    """动画贴纸（lottie .tgs / video webm）vision 模型读不了，不算图。"""
    doc = SimpleNamespace(mime_type="application/x-tgsticker")
    sticker = object()
    assert _media.message_has_image(_msg(document=doc, sticker=sticker)) is False
    doc2 = SimpleNamespace(mime_type="video/webm")
    assert _media.message_has_image(_msg(document=doc2, sticker=sticker)) is False


def test_message_has_image_skips_video_voice() -> None:
    assert _media.message_has_image(_msg(video=object())) is False
    assert _media.message_has_image(_msg(voice=object())) is False
    assert _media.message_has_image(_msg(geo=object())) is False
    assert _media.message_has_image(None) is False


def test_message_has_image_skips_web_preview() -> None:
    """URL 网页预览缩略图不算用户主动发的图。

    Telegram 为 URL 生成预览时，telethon 的 msg.photo 会返回 web_preview.photo，
    但这是自动生成的链接预览图，不应该触发 vision 路径。
    """
    # 构造一个类名为 MessageMediaWebPage 的 media 对象
    class MessageMediaWebPage:
        pass
    web_media = MessageMediaWebPage()
    # 即使 msg.photo 不为空（因为 telethon 会返回 web_preview.photo），
    # 也不应该被识别为"含图"
    assert _media.message_has_image(_msg(photo=object(), media=web_media)) is False
    # 没有 web preview 时，photo 正常识别
    assert _media.message_has_image(_msg(photo=object(), media=None)) is True
    assert _media.message_has_image(_msg(photo=object())) is True


def test_message_has_image_uses_file_mime_fallback() -> None:
    """document 没 mime_type 但 ``msg.file.mime_type`` 给了——也认。"""
    f = SimpleNamespace(mime_type="image/png")
    # 注意：sticker=None 否则会被当贴纸路径（非 webp 拒）
    assert _media.message_has_image(_msg(file=f)) is True


def test_message_has_audio() -> None:
    assert _media.message_has_audio(_msg(voice=object())) is True
    assert _media.message_has_audio(_msg(audio=object())) is True
    doc = SimpleNamespace(mime_type="audio/ogg")
    assert _media.message_has_audio(_msg(document=doc)) is True
    assert _media.message_has_audio(_msg(photo=object())) is False
    assert _media.message_has_audio(None) is False


# ════════════════════════════════════════════════════════════
# 2) collect_image_sources（含 album 拉伴随消息）
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_collect_image_sources_dual_replied_and_self() -> None:
    """同时 replied 和 self 都含图——两个都收。"""
    replied = _msg(id=10, photo=object())
    self_msg = _msg(id=20, photo=object())
    out = await _media.collect_image_sources(client=None, replied=replied, self_msg=self_msg)
    ids = [m.id for m in out]
    assert 10 in ids and 20 in ids


@pytest.mark.asyncio
async def test_collect_image_sources_album_pulls_siblings() -> None:
    """grouped_id 非空时拉同组 sibling，全部图都进 list。"""
    main = _msg(id=10, photo=object(), grouped_id=99)
    sib_a = _msg(id=11, photo=object(), grouped_id=99)
    sib_b = _msg(id=12, photo=object(), grouped_id=99)
    other = _msg(id=13, photo=object(), grouped_id=88)  # 不同组——不收

    fake_client = AsyncMock()
    fake_client.get_messages = AsyncMock(return_value=[sib_a, sib_b, other])

    out = await _media.collect_image_sources(
        client=fake_client, replied=main, self_msg=None
    )
    ids = sorted(m.id for m in out)
    assert ids == [10, 11, 12]


@pytest.mark.asyncio
async def test_collect_image_sources_capped_at_max() -> None:
    """超过 ``MAX_IMAGES_PER_REQUEST`` 时截断，不让 token 烧爆。"""
    main = _msg(id=10, photo=object(), grouped_id=99)
    sibs = [_msg(id=11 + i, photo=object(), grouped_id=99) for i in range(20)]
    fake_client = AsyncMock()
    fake_client.get_messages = AsyncMock(return_value=sibs)
    out = await _media.collect_image_sources(client=fake_client, replied=main, self_msg=None)
    assert len(out) == _media.MAX_IMAGES_PER_REQUEST


@pytest.mark.asyncio
async def test_collect_image_sources_dedupes_by_id() -> None:
    """同一相册被同时命中两次（极端 race）也只收一次。"""
    main = _msg(id=10, photo=object(), grouped_id=99)
    same = _msg(id=10, photo=object(), grouped_id=99)  # 同 id
    fake_client = AsyncMock()
    fake_client.get_messages = AsyncMock(return_value=[same])
    out = await _media.collect_image_sources(client=fake_client, replied=main, self_msg=None)
    assert len(out) == 1


# ════════════════════════════════════════════════════════════
# 3) download_image_bytes：FileReferenceExpired 重试
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_download_image_retries_on_file_reference_expired() -> None:
    """FileReferenceExpiredError 时会重新拉一次原 message 再下载。"""

    class FileReferenceExpiredError(Exception):
        pass

    # 第一次下载抛 FileRef 错；refetch 后第二次成功
    fresh_msg = _msg(
        id=10, chat_id=100, photo=object(),
        download_media=AsyncMock(return_value=b"refetched-bytes"),
    )
    fake_client = AsyncMock()
    fake_client.get_messages = AsyncMock(return_value=fresh_msg)

    bad = _msg(
        id=10,
        chat_id=100,
        photo=object(),
        download_media=AsyncMock(side_effect=FileReferenceExpiredError("expired")),
    )
    out = await _media.download_image_bytes(fake_client, bad)
    assert out == b"refetched-bytes"
    fake_client.get_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_image_does_not_retry_on_other_errors() -> None:
    """非 FileRef 类异常**不**重试——直接抛给上层。"""
    bad = _msg(
        id=10, chat_id=100, photo=object(),
        download_media=AsyncMock(side_effect=ConnectionError("boom")),
    )
    fake_client = AsyncMock()
    with pytest.raises(ConnectionError):
        await _media.download_image_bytes(fake_client, bad)
    fake_client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_download_image_rejects_empty_and_oversized() -> None:
    bad_empty = _msg(download_media=AsyncMock(return_value=b""))
    with pytest.raises(ValueError, match="为空"):
        await _media.download_image_bytes(None, bad_empty)
    bad_big = _msg(
        download_media=AsyncMock(return_value=b"\x00" * (_media.MAX_IMAGE_BYTES + 1))
    )
    with pytest.raises(ValueError, match="体积超过"):
        await _media.download_image_bytes(None, bad_big)


# ════════════════════════════════════════════════════════════
# 4) _run_ai：image-as-document / album / sticker / dual-source
# ════════════════════════════════════════════════════════════


def _setup_vision_provider() -> None:
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


def _ai_tpl():
    return {"name": "ai", "type": "ai", "config": {"provider_id": 9, "routing_mode": "fixed"}}


@pytest.mark.asyncio
async def test_run_ai_image_as_document_path(monkeypatch) -> None:
    """按文件发送的图（document mime=image/png）也走视觉路径。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            captured["images"] = images
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())

    _setup_vision_provider()
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    doc = SimpleNamespace(mime_type="image/png")
    self_msg = _msg(
        id=20, photo=None, document=doc,
        download_media=AsyncMock(return_value=fake_png),
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    client = AsyncMock()
    await wcmd._run_ai(client, event, ["q"], _ai_tpl(), account_id=1)
    assert captured["images"] == [fake_png]


@pytest.mark.asyncio
async def test_run_ai_album_sends_all_photos(monkeypatch) -> None:
    """相册（grouped_id 共 3 张）应把整组都发给 vision。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            captured["images"] = images
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())
    _setup_vision_provider()

    bytes_a = b"\xff\xd8\xff\xe0a"
    bytes_b = b"\xff\xd8\xff\xe0b"
    bytes_c = b"\xff\xd8\xff\xe0c"
    main = _msg(
        id=10, photo=object(), grouped_id=99,
        download_media=AsyncMock(return_value=bytes_a),
    )
    sib_b = _msg(
        id=11, photo=object(), grouped_id=99,
        download_media=AsyncMock(return_value=bytes_b),
    )
    sib_c = _msg(
        id=12, photo=object(), grouped_id=99,
        download_media=AsyncMock(return_value=bytes_c),
    )
    fake_client = AsyncMock()
    fake_client.get_messages = AsyncMock(return_value=[sib_b, sib_c])

    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=main)
    event.message = _msg(id=99)  # 自己消息无图
    await wcmd._run_ai(fake_client, event, ["q"], _ai_tpl(), account_id=1)

    images = captured["images"]
    assert isinstance(images, list)
    assert bytes_a in images and bytes_b in images and bytes_c in images
    assert len(images) == 3


@pytest.mark.asyncio
async def test_run_ai_dual_source_replied_and_self(monkeypatch) -> None:
    """同时 replied 含图 + 自己消息也含图——两张都送给 vision。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            captured["images"] = images
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())
    _setup_vision_provider()

    rb = b"\xff\xd8\xff\xe0R"
    sb = b"\xff\xd8\xff\xe0S"
    replied = _msg(
        id=10, photo=object(),
        download_media=AsyncMock(return_value=rb),
    )
    self_msg = _msg(
        id=20, photo=object(),
        download_media=AsyncMock(return_value=sb),
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=replied)
    event.message = self_msg
    await wcmd._run_ai(AsyncMock(), event, ["q"], _ai_tpl(), account_id=1)
    assert captured["images"] == [rb, sb]


@pytest.mark.asyncio
async def test_run_ai_static_sticker_treated_as_image(monkeypatch) -> None:
    """静态 webp 贴纸走视觉路径。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            captured["images"] = images
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())
    _setup_vision_provider()

    sticker_bytes = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100
    doc = SimpleNamespace(mime_type="image/webp")
    sticker = object()
    self_msg = _msg(
        id=20, document=doc, sticker=sticker,
        download_media=AsyncMock(return_value=sticker_bytes),
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    await wcmd._run_ai(AsyncMock(), event, ["啥"], _ai_tpl(), account_id=1)
    assert captured["images"] == [sticker_bytes]


@pytest.mark.asyncio
async def test_run_ai_animated_sticker_falls_through_to_text(monkeypatch) -> None:
    """动画贴纸（.tgs）vision 读不了，应当走纯文本路径——配合反幻觉规则模型会拒答。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            captured["images"] = images
            return LLMResult(text="无法识别", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())
    _setup_vision_provider()
    doc = SimpleNamespace(mime_type="application/x-tgsticker")
    sticker = object()
    self_msg = _msg(id=20, document=doc, sticker=sticker)
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    await wcmd._run_ai(AsyncMock(), event, ["q"], _ai_tpl(), account_id=1)
    assert captured["images"] is None  # 没有图字节传给模型


# ════════════════════════════════════════════════════════════
# 5) send_new + self caption photo 守卫
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_ai_send_new_with_self_photo_falls_back_to_edit(monkeypatch) -> None:
    """命令消息自带图 + send_mode=send_new 应降级为 edit，避免删图。"""

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            return LLMResult(text="caption answer", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())
    _setup_vision_provider()

    self_msg = _msg(
        id=20, photo=object(),
        download_media=AsyncMock(return_value=b"\xff\xd8\xff\xe0\x00"),
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    client = AsyncMock()
    tpl = {
        "name": "ai", "type": "ai",
        "config": {"provider_id": 9, "routing_mode": "fixed", "send_mode": "send_new"},
    }
    await wcmd._run_ai(client, event, ["q"], tpl, account_id=1)
    # 关键：不应调 client.send_message（send_new 路径）；也不应 event.delete
    client.send_message.assert_not_called()
    event.delete.assert_not_called()
    # 而是 edit 了原命令消息
    event.edit.assert_called()


# ════════════════════════════════════════════════════════════
# 6) STT：modality=audio 时 Whisper 转写
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_ai_audio_transcribe_then_complete(monkeypatch) -> None:
    """有语音 + provider modality=audio 时：先 transcribe，再把转写文本送 complete。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def transcribe(self, audio, model):
            captured["stt_audio"] = audio
            captured["stt_model"] = model
            return "你好世界"

        async def complete(self, system, user, max_tokens=512, images=None):
            captured["complete_user"] = user
            return LLMResult(text="收到", model="gpt", input_tokens=2, output_tokens=2)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                9: {
                    "id": 9, "name": "stt", "provider": "openai", "api_key_enc": None,
                    "base_url": None, "default_model": "gpt", "modality": "audio",
                    "tags": [], "cost_tier": 2, "notes": None, "proxy_url": None, "models": [],
                }
            },
        )
    )
    audio_bytes = b"OggS" + b"\x00" * 100
    self_msg = _msg(
        id=20, voice=object(),
        download_media=AsyncMock(return_value=audio_bytes),
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 9, "routing_mode": "fixed"}}
    await wcmd._run_ai(AsyncMock(), event, [], tpl, account_id=1)
    assert captured["stt_audio"] == audio_bytes
    assert captured["stt_model"] == "whisper-1"  # 默认值
    # 转写文本应进入 complete 的 user prompt
    assert "你好世界" in str(captured["complete_user"])


@pytest.mark.asyncio
async def test_run_ai_audio_rejects_text_only_provider() -> None:
    """有语音但 provider modality=text → 拒答而非瞎答。"""
    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                7: {
                    "id": 7, "name": "t", "provider": "openai", "api_key_enc": None,
                    "base_url": None, "default_model": "m", "modality": "text",
                    "tags": [], "cost_tier": 1, "notes": None, "proxy_url": None, "models": [],
                }
            },
        )
    )
    self_msg = _msg(id=20, voice=object())
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 7, "routing_mode": "fixed"}}
    await wcmd._run_ai(AsyncMock(), event, [], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "✗" in msg
    assert "audio" in msg or "转写" in msg


@pytest.mark.asyncio
async def test_run_ai_audio_uses_custom_transcribe_model(monkeypatch) -> None:
    """``cfg.transcribe_model`` 给了就覆盖默认 ``whisper-1``。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def transcribe(self, audio, model):
            captured["stt_model"] = model
            return "ok"

        async def complete(self, system, user, max_tokens=512, images=None):
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())
    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                9: {
                    "id": 9, "name": "stt", "provider": "openai", "api_key_enc": None,
                    "base_url": None, "default_model": "m", "modality": "audio",
                    "tags": [], "cost_tier": 2, "notes": None, "proxy_url": None, "models": [],
                }
            },
        )
    )
    self_msg = _msg(
        id=20, voice=object(),
        download_media=AsyncMock(return_value=b"OggS" + b"\x00" * 50),
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    tpl = {
        "name": "ai", "type": "ai",
        "config": {
            "provider_id": 9, "routing_mode": "fixed",
            "transcribe_model": "whisper-large-v3",
        },
    }
    await wcmd._run_ai(AsyncMock(), event, [], tpl, account_id=1)
    assert captured["stt_model"] == "whisper-large-v3"


# ════════════════════════════════════════════════════════════
# 7) OpenAIClient.transcribe（Whisper 协议 body 形态）
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_helper_records_usage_and_budget(monkeypatch) -> None:
    """STT helper 必须先预扣账号预算，再写 source=stt 的 usage。"""
    from app.services import llm_account_budget, llm_invoke, llm_runtime
    from app.services.llm_dto import LLMProviderDTO

    provider = LLMProviderDTO(
        id=9,
        name="stt",
        provider="openai",
        default_model="gpt",
        cost_tier=3,
    )
    captured: dict[str, object] = {}
    emitted = []
    ticket = llm_account_budget.LLMAccountBudgetTicket(
        account_id=1,
        provider_id=9,
        estimated_tokens=64,
        backend="test",
        limited=True,
    )

    async def _acquire(account_id, provider_dto, estimated_tokens):
        captured["account_id"] = account_id
        captured["provider_id"] = provider_dto.id
        captured["estimated_tokens"] = estimated_tokens
        return ticket

    async def _settle(settle_ticket, *, actual_tokens, actual_provider, success):
        captured["settle_ticket"] = settle_ticket
        captured["actual_tokens"] = actual_tokens
        captured["actual_provider"] = actual_provider
        captured["success"] = success

    async def _emit(record):
        emitted.append(record)

    class _FakeClient:
        async def transcribe(self, audio, model):
            captured["audio"] = audio
            captured["model"] = model
            return "你好世界"

    monkeypatch.setattr(llm_account_budget, "acquire", _acquire)
    monkeypatch.setattr(llm_account_budget, "settle", _settle)
    monkeypatch.setattr(llm_runtime, "_emit_usage", _emit)

    text = await llm_invoke.transcribe(
        provider,
        b"OggS" + b"\x00" * 20,
        model="whisper-1",
        account_id=1,
        triggered_by_account_id=22,
        source="stt",
        client_factory=lambda *_args, **_kwargs: _FakeClient(),
    )

    assert text == "你好世界"
    assert captured["account_id"] == 1
    assert captured["provider_id"] == 9
    assert captured["estimated_tokens"] == 64
    assert captured["success"] is True
    assert captured["actual_provider"] is provider
    assert int(captured["actual_tokens"]) >= 65
    assert len(emitted) == 1
    assert emitted[0].source == "stt"
    assert emitted[0].account_id == 1
    assert emitted[0].triggered_by_account_id == 22
    assert emitted[0].provider_id == 9
    assert emitted[0].success is True


@pytest.mark.asyncio
async def test_openai_client_transcribe_posts_multipart() -> None:
    """OpenAIClient.transcribe 必须发到 ``/audio/transcriptions``，
    用 multipart 上传 ``file=<bytes>`` + ``model=<id>``。"""
    from app.services.llm_client import OpenAIClient

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "hello world"}

    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_FakeResp())

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        cli = OpenAIClient(
            api_key="sk", base_url="https://api.example.com/v1", model="gpt"
        )
        out = await cli.transcribe(b"OggS\x00\x00", model="whisper-1")

    assert out == "hello world"
    args, kwargs = fake.post.call_args
    assert args[0].endswith("/audio/transcriptions"), f"端点不对: {args[0]}"
    assert kwargs["data"] == {"model": "whisper-1", "response_format": "json"}
    # files 是 dict[name, (filename, bytes, mime)]
    assert "file" in kwargs["files"]
    _, payload, _ = kwargs["files"]["file"]
    assert payload == b"OggS\x00\x00"


@pytest.mark.asyncio
async def test_anthropic_client_transcribe_raises() -> None:
    """Anthropic 没 STT 端点——transcribe 应明确抛 NotImplementedError。"""
    from app.services.llm_client import AnthropicClient

    cli = AnthropicClient(api_key="sk", base_url=None, model="claude")
    with pytest.raises(NotImplementedError):
        await cli.transcribe(b"OggS", model="whisper-1")

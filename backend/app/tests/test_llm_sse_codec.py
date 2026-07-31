from __future__ import annotations

import pytest

from app.services.llm_codecs.sse import iter_sse_events, parse_sse_text


class _ChunkedResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def test_parse_sse_text_supports_multiline_data_and_final_block() -> None:
    events = parse_sse_text(
        "event: response.output_text.delta\r\n"
        'data: {"type":"response.output_text.delta",\r\n'
        'data: "delta":"你"}'
    )

    assert len(events) == 1
    assert events[0].event == "response.output_text.delta"
    assert events[0].data == '{"type":"response.output_text.delta",\n"delta":"你"}'


def test_parse_sse_text_accepts_cr_only_event_boundaries() -> None:
    events = parse_sse_text("event: one\rdata: first\r\rdata: second")

    assert [(event.event, event.data) for event in events] == [
        ("one", "first"),
        ("message", "second"),
    ]


@pytest.mark.asyncio
async def test_iter_sse_events_is_utf8_safe_across_chunks() -> None:
    raw = 'data: {"delta":"你好"}\n\ndata: [DONE]'.encode()
    split = raw.index("你".encode()) + 1

    events = [
        event
        async for event in iter_sse_events(
            _ChunkedResponse([raw[:split], raw[split : split + 1], raw[split + 1 :]])
        )
    ]

    assert [event.data for event in events] == ['{"delta":"你好"}', "[DONE]"]


@pytest.mark.asyncio
async def test_iter_sse_events_rejects_oversized_unterminated_event() -> None:
    with pytest.raises(ValueError, match="单个 SSE 事件"):
        _ = [
            event
            async for event in iter_sse_events(
                _ChunkedResponse([b"data: " + b"x" * 32]),
                event_limit_bytes=16,
            )
        ]

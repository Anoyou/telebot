"""Bounded, transport-safe Server-Sent Events parsing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SSEEvent:
    """One complete SSE event block."""

    event: str = "message"
    data: str = ""


def _next_delimiter(buffer: bytearray) -> tuple[int, int] | None:
    candidates = [
        (position, len(delimiter))
        for delimiter in (
            b"\r\n\r\n",
            b"\r\n\n",
            b"\n\r\n",
            b"\n\n",
            b"\r\r",
        )
        if (position := buffer.find(delimiter)) >= 0
    ]
    return min(candidates, default=None)


def _field_value(line: str, field: str) -> str | None:
    prefix = f"{field}:"
    if not line.startswith(prefix):
        return None
    value = line[len(prefix) :]
    return value[1:] if value.startswith(" ") else value


def _parse_block(block: bytes) -> SSEEvent | None:
    try:
        text = block.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SSE 事件包含无效 UTF-8") from exc

    event_name = "message"
    data_lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line or line.startswith(":"):
            continue
        event_value = _field_value(line, "event")
        if event_value is not None:
            event_name = event_value.strip() or "message"
            continue
        data_value = _field_value(line, "data")
        if data_value is not None:
            data_lines.append(data_value)
    if not data_lines:
        return None
    return SSEEvent(event=event_name, data="\n".join(data_lines))


def parse_sse_text(text: str) -> list[SSEEvent]:
    """Parse a complete SSE response, including a final unterminated block."""

    buffer = bytearray(str(text or "").encode("utf-8"))
    events: list[SSEEvent] = []
    while delimiter := _next_delimiter(buffer):
        position, length = delimiter
        block = bytes(buffer[:position])
        del buffer[: position + length]
        event = _parse_block(block)
        if event is not None:
            events.append(event)
    if buffer:
        event = _parse_block(bytes(buffer))
        if event is not None:
            events.append(event)
    return events


async def iter_sse_events(
    response: Any,
    *,
    event_limit_bytes: int = 1_048_576,
    total_limit_bytes: int = 8 * 1_048_576,
) -> AsyncIterator[SSEEvent]:
    """Yield complete SSE blocks across arbitrary byte and UTF-8 chunking."""

    buffer = bytearray()
    total = 0
    async for chunk in response.aiter_bytes():
        if not isinstance(chunk, (bytes, bytearray)):
            raise ValueError("SSE 上游返回了无效字节流")
        total += len(chunk)
        if total > total_limit_bytes:
            raise ValueError("SSE 响应超过传输大小限制")
        buffer.extend(chunk)
        while delimiter := _next_delimiter(buffer):
            position, length = delimiter
            if position > event_limit_bytes:
                raise ValueError("单个 SSE 事件超过大小限制")
            block = bytes(buffer[:position])
            del buffer[: position + length]
            event = _parse_block(block)
            if event is not None:
                yield event
        if len(buffer) > event_limit_bytes:
            raise ValueError("单个 SSE 事件超过大小限制")
    if buffer:
        event = _parse_block(bytes(buffer))
        if event is not None:
            yield event

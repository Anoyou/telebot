from __future__ import annotations

import base64
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm_protocol import ImageContent
from app.services.system_agent.media import (
    decode_data_url,
    extract_image_contents,
    image_urls_from_text,
    materialize_attachments,
    normalize_attachment,
)


def _png_data_url(data: bytes = b"\x89PNG\r\n\x1a\n") -> str:
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def test_data_url_requires_complete_valid_image_payload() -> None:
    data_url = _png_data_url()
    data, mime = decode_data_url(data_url)
    assert data == b"\x89PNG\r\n\x1a\n"
    assert mime == "image/png"

    with pytest.raises(ValueError, match="格式无效|base64"):
        decode_data_url("data:image/png;base64,AAAA!")


def test_private_image_urls_are_rejected_before_fetch() -> None:
    with pytest.raises(ValueError, match="内网"):
        normalize_attachment({"source": "remote_url", "url": "http://127.0.0.1/a.png"})


@pytest.mark.parametrize(
    ("url", "message"),
    (
        ("https://user:pass@example.com/a.png", "用户名或密码"),
        (f"https://example.com/{'a' * 2_048}.png", "2048"),
    ),
)
def test_unsafe_image_url_forms_are_rejected(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_attachment({"source": "remote_url", "url": url})


def test_unresolvable_image_urls_fail_closed() -> None:
    with (
        patch("app.services.system_agent.media.socket.getaddrinfo", side_effect=socket.gaierror),
        pytest.raises(ValueError, match="无法解析"),
    ):
        normalize_attachment(
            {"source": "remote_url", "url": "https://missing.example.com/a.png"}
        )


def test_image_url_detection_is_limited_to_explicit_image_paths() -> None:
    text = (
        "看 https://cdn.example.test/a.PNG?size=2 和 "
        "https://pbs.twimg.com/media/AbCdEfG?format=jpg&name=large ，"
        "忽略 https://example.test/page"
    )
    assert image_urls_from_text(text) == [
        "https://cdn.example.test/a.PNG?size=2",
        "https://pbs.twimg.com/media/AbCdEfG?format=jpg&name=large",
    ]


def test_tool_image_extraction_preserves_long_and_malformed_strings() -> None:
    ordinary = "A" * 12_000
    malformed = "data:image/png;base64,AAAA!"
    assert extract_image_contents(ordinary) == ()
    assert extract_image_contents(malformed) == ()
    assert extract_image_contents({"type": "image", "source": {"type": "url", "url": "https://example.test/a.png"}}) == (
        ImageContent(url="https://example.test/a.png"),
    )


@pytest.mark.asyncio
async def test_remote_image_is_materialized_and_keeps_public_url() -> None:
    response = httpx.Response(
        200,
        content=b"\x89PNG\r\n\x1a\n",
        headers={"content-type": "image/png"},
        request=httpx.Request("GET", "https://93.184.216.34:443/a.png"),
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    stream_context = AsyncMock()
    stream_context.__aenter__.return_value = response
    stream_context.__aexit__.return_value = None
    client.stream = MagicMock(return_value=stream_context)
    public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    with (
        patch("app.services.system_agent.media.socket.getaddrinfo", return_value=public_dns),
        patch("app.services.system_agent.media.httpx.AsyncClient", return_value=client),
    ):
        normalized, images = await materialize_attachments(
            [{"source": "remote_url", "url": "https://images.example.com/a.png"}]
        )

    assert normalized == [{"kind": "image", "source": "remote_url", "mime_type": "image/png", "url": "https://images.example.com/a.png"}]
    assert images == [ImageContent(data=b"\x89PNG\r\n\x1a\n", mime_type="image/png")]
    client.stream.assert_called_once_with(
        "GET",
        "https://93.184.216.34:443/a.png",
        headers={"Accept": "image/*", "Host": "images.example.com"},
        extensions={"sni_hostname": "images.example.com"},
    )


@pytest.mark.asyncio
async def test_remote_image_download_errors_are_normalized() -> None:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.stream = MagicMock(
        side_effect=httpx.ConnectError("offline", request=httpx.Request("GET", "https://93.184.216.34:443/a.png"))
    )
    public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    with (
        patch("app.services.system_agent.media.socket.getaddrinfo", return_value=public_dns),
        patch("app.services.system_agent.media.httpx.AsyncClient", return_value=client),
        pytest.raises(ValueError, match="下载失败"),
    ):
        await materialize_attachments(
            [{"source": "remote_url", "url": "https://images.example.com/a.png"}]
        )

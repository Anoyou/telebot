"""System Agent 图片附件的校验、抓取与协议中立转换。"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..llm_protocol import ImageContent

MAX_IMAGE_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 6 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_DATA_URL_RE = re.compile(
    r"^data:(image/(?:jpeg|png|webp|gif));base64,([A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s<>\"]+\.(?:jpe?g|png|webp|gif)(?:[?#][^\s<>\"]*)?",
    re.IGNORECASE,
)


def _validate_public_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("图片 URL 必须使用 http 或 https")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("图片 URL 不允许指向本机或内网地址")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))}
    except (OSError, ValueError):
        raise ValueError("图片 URL 域名无法解析") from None
    if not addresses:
        raise ValueError("图片 URL 域名无法解析")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("图片 URL 不允许指向本机或内网地址")
    return candidate


def decode_data_url(value: str) -> tuple[bytes, str]:
    match = _DATA_URL_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("图片 data URL 格式无效")
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("图片 data URL 的 base64 内容无效") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片大小必须在 1 字节到 6 MiB 之间")
    return data, match.group(1).lower()


def normalize_attachment(value: Mapping[str, Any]) -> dict[str, Any]:
    source = str(value.get("source") or "").strip().lower()
    if source not in {"data_url", "remote_url"}:
        raise ValueError("图片附件 source 必须是 data_url 或 remote_url")
    result: dict[str, Any] = {"kind": "image", "source": source}
    name = str(value.get("name") or "").strip()
    if name:
        result["name"] = name[:128]
    if source == "data_url":
        raw = str(value.get("data_url") or "").strip()
        data, mime = decode_data_url(raw)
        result.update({"mime_type": mime, "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"})
    else:
        url = _validate_public_url(str(value.get("url") or ""))
        result.update({"mime_type": str(value.get("mime_type") or "").lower() or None, "url": url})
    return result


async def materialize_attachments(values: list[Mapping[str, Any]] | None) -> tuple[list[dict[str, Any]], list[ImageContent]]:
    """返回可持久化的规范附件和发给模型的内存图片。"""
    normalized: list[dict[str, Any]] = []
    images: list[ImageContent] = []
    for raw in (values or [])[:MAX_IMAGE_ATTACHMENTS]:
        item = normalize_attachment(raw)
        if item["source"] == "data_url":
            data, mime = decode_data_url(str(item["data_url"]))
            images.append(ImageContent(data=data, mime_type=mime))
        else:
            url = str(item["url"])
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12.0, connect=4.0),
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream("GET", url, headers={"Accept": "image/*"}) as response:
                        if response.status_code >= 400:
                            raise ValueError(f"图片 URL 返回 HTTP {response.status_code}")
                        mime = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
                        if mime not in ALLOWED_IMAGE_MIMES:
                            raise ValueError("图片 URL 的 Content-Type 不是支持的图片格式")
                        declared_length = response.headers.get("content-length")
                        if declared_length and declared_length.isdigit() and int(declared_length) > MAX_IMAGE_BYTES:
                            raise ValueError("图片 URL 返回内容超过 6 MiB")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_IMAGE_BYTES:
                                raise ValueError("图片 URL 返回内容超过 6 MiB")
            except httpx.HTTPError as exc:
                raise ValueError("图片 URL 下载失败") from exc
            item["mime_type"] = mime
            images.append(ImageContent(data=bytes(body), mime_type=mime))
        normalized.append(item)
    return normalized, images


def image_urls_from_text(text: str) -> list[str]:
    found: list[str] = []
    for value in _IMAGE_URL_RE.findall(text or ""):
        clean = value.rstrip(".,;:!?)]}")
        if clean and clean not in found:
            found.append(clean)
    return found[:MAX_IMAGE_ATTACHMENTS]


def image_content_to_public(image: ImageContent) -> dict[str, str]:
    if image.url:
        return {"kind": "image", "source": "remote_url", "url": image.url}
    mime = image.mime_type or "image/png"
    data = base64.b64encode(image.data or b"").decode("ascii")
    return {"kind": "image", "source": "data_url", "mime_type": mime, "data_url": f"data:{mime};base64,{data}"}


def extract_image_contents(value: Any, *, _depth: int = 0) -> tuple[ImageContent, ...]:
    """从工具结果提取明确图片块与合法 data URL，普通长字符串保持文本。"""
    if _depth > 24:
        return ()
    if isinstance(value, ImageContent):
        return (value,)
    if isinstance(value, str):
        try:
            data, mime = decode_data_url(value)
        except ValueError:
            return ()
        return (ImageContent(data=data, mime_type=mime),)
    if isinstance(value, Mapping):
        item_type = str(value.get("type") or value.get("kind") or "").lower()
        if item_type in {"image", "input_image", "image_url", "output_image", "image_generation"}:
            source = value.get("source")
            if source == "data_url":
                target = value.get("data_url")
                if isinstance(target, str):
                    try:
                        data, mime = decode_data_url(target)
                    except ValueError:
                        return ()
                    return (ImageContent(data=data, mime_type=mime),)
            if source == "remote_url":
                target = value.get("url")
                if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
                    return (ImageContent(url=target.strip()),)
            if isinstance(source, Mapping) and str(source.get("type") or "").lower() == "url":
                source_url = source.get("url")
                if isinstance(source_url, str) and source_url.lower().startswith(("http://", "https://")):
                    return (ImageContent(url=source_url.strip()),)
            if isinstance(source, Mapping) and str(source.get("type") or "") == "base64":
                raw = source.get("data")
                mime = str(source.get("media_type") or "").lower()
                if isinstance(raw, str) and mime in ALLOWED_IMAGE_MIMES:
                    try:
                        data = base64.b64decode(raw, validate=True)
                    except (ValueError, binascii.Error):
                        data = b""
                    if data and len(data) <= MAX_IMAGE_BYTES:
                        return (ImageContent(data=data, mime_type=mime),)
            target = value.get("url") or value.get("image_url")
            if isinstance(target, Mapping):
                target = target.get("url")
            if isinstance(target, str):
                try:
                    data, mime = decode_data_url(target)
                    return (ImageContent(data=data, mime_type=mime),)
                except ValueError:
                    if target.lower().startswith(("http://", "https://")):
                        return (ImageContent(url=target.strip()),)
        found: list[ImageContent] = []
        for item in value.values():
            found.extend(extract_image_contents(item, _depth=_depth + 1))
        return tuple(found)
    if isinstance(value, (list, tuple)):
        found: list[ImageContent] = []
        for item in value:
            found.extend(extract_image_contents(item, _depth=_depth + 1))
        return tuple(found)
    return ()


__all__ = [
    "MAX_IMAGE_ATTACHMENTS",
    "MAX_IMAGE_BYTES",
    "image_content_to_public",
    "extract_image_contents",
    "image_urls_from_text",
    "materialize_attachments",
    "normalize_attachment",
]

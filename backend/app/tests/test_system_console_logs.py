from __future__ import annotations

from app.services.system_console_logs import read_system_console_logs


async def test_read_system_console_logs_filters_noise_keyword_and_redacts() -> None:
    def fetcher(service: str, tail: int):
        return {
            "ok": True,
            "source": "docker_compose",
            "services": [service],
            "tail": tail,
            "lines": [
                'web | INFO:httpx:HTTP Request: POST https://example.test "HTTP/1.1 200 OK"',
                "web | ERROR Provider token=abc12345 failed",
                "web | ERROR unrelated",
            ],
        }

    result = await read_system_console_logs("web", 100, "Provider", fetcher=fetcher)

    assert result["ok"] is True
    assert result["source"] == "docker_compose"
    assert result["lines"] == ["web | ERROR Provider token=*** failed"]


async def test_read_system_console_logs_rejects_unknown_service() -> None:
    try:
        await read_system_console_logs("docker", 100, fetcher=lambda *_args: {})
    except ValueError as exc:
        assert str(exc) == "不支持的系统日志服务"
    else:
        raise AssertionError("unknown service should be rejected")


async def test_read_system_console_logs_redacts_updater_error() -> None:
    result = await read_system_console_logs(
        "web",
        100,
        fetcher=lambda *_args: {"ok": False, "error": "authorization=Bearer secret-token"},
    )

    assert result["error"] == "authorization=Bearer ***"

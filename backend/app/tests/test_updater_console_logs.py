from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_updater_module():
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "deploy" / "updater" / "server.py"
    spec = importlib.util.spec_from_file_location("telepilot_updater_server_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_console_logs_use_host_compose_project_name(monkeypatch) -> None:
    updater = _load_updater_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/TelePilot"))

    def fake_run(args, *, timeout=60, env=None):  # noqa: ANN001
        commands.append(args)
        return "telepilot-web-1 | ready\ntelepilot-frontend-1 | ok", "", 0

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._tail_console_logs("web", 20, None)

    assert result["ok"] is True
    assert result["project"] == "telepilot"
    assert commands[0][:4] == ["docker", "compose", "-p", "telepilot"]
    assert commands[0][4:7] == ["logs", "--no-color", "--timestamps"]
    assert commands[0][-1] == "web"
    assert result["lines"] == [
        "telepilot-web-1 | ready",
        "telepilot-frontend-1 | ok",
    ]


def test_console_logs_respects_explicit_compose_project_name(monkeypatch) -> None:
    updater = _load_updater_module()
    commands: list[list[str]] = []

    monkeypatch.setenv("TELEPILOT_COMPOSE_PROJECT_NAME", "custom_stack")
    monkeypatch.setattr(updater, "_run", lambda args, **_kw: (commands.append(args) or ("", "", 0)))

    result = updater._tail_console_logs("all", 20, None)

    assert result["ok"] is True
    assert result["project"] == "custom_stack"
    assert commands[0][:4] == ["docker", "compose", "-p", "custom_stack"]


def test_console_logs_returns_partial_lines_when_compose_times_out(monkeypatch) -> None:
    updater = _load_updater_module()

    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/TelePilot"))
    monkeypatch.setattr(
        updater,
        "_run",
        lambda _args, **_kw: (
            "telepilot-web-1 | booting\ntelepilot-web-1 | ready",
            "command timed out",
            124,
        ),
    )

    result = updater._tail_console_logs("web", 20, None)

    assert result["ok"] is True
    assert result["project"] == "telepilot"
    assert result["lines"] == [
        "telepilot-web-1 | booting",
        "telepilot-web-1 | ready",
    ]
    assert "超时" in result["error"]

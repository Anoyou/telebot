"""Installed package metadata must expose the public TelePilot SDK namespace."""

from importlib.metadata import distribution


def test_installed_distribution_includes_public_sdk_namespace() -> None:
    top_level = distribution("telepilot-backend").read_text("top_level.txt") or ""

    assert "app" in top_level.splitlines()
    assert "telepilot" in top_level.splitlines()


def test_public_sdk_import_is_available() -> None:
    from telepilot import PluginContext, plugin

    assert PluginContext is not None
    assert plugin is not None

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "frontend" / "index.html"
NGINX_CONF = REPO_ROOT / "frontend" / "nginx.conf"
CHANGELOG_MENU = REPO_ROOT / "frontend" / "src" / "components" / "layout" / "ChangelogMenu.tsx"
VITE_CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"


def test_ios_pwa_chrome_is_painted_before_body_first_frame() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    body_index = html.index("<body>")

    style_match = re.search(
        r'<style id="telepilot-chrome-bootstrap">(?P<content>[\s\S]*?)</style>',
        html,
    )
    assert style_match is not None
    assert style_match.start() < body_index
    assert "body" in style_match.group("content")
    assert "var(--telepilot-app-chrome" in style_match.group("content")

    script_match = re.search(
        r'<script data-telepilot-chrome-bootstrap>(?P<content>[\s\S]*?)</script>',
        html,
    )
    assert script_match is not None
    assert script_match.start() < body_index
    script = script_match.group("content")
    assert 'style.setProperty("--telepilot-app-chrome", chrome)' in script
    assert 'meta[name="color-scheme"]' in script


def test_ios_pwa_chrome_bootstrap_matches_csp_and_status_bar_mode() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    nginx = NGINX_CONF.read_text(encoding="utf-8")

    assert re.search(
        r'<meta name="apple-mobile-web-app-status-bar-style" content="default"\s*/>',
        html,
    )

    script_match = re.search(
        r'<script data-telepilot-chrome-bootstrap>(?P<content>[\s\S]*?)</script>',
        html,
    )
    assert script_match is not None
    digest = base64.b64encode(
        hashlib.sha256(script_match.group("content").encode()).digest()
    ).decode()
    assert f"sha256-{digest}" in nginx


def test_frontend_nginx_disables_routine_access_log_noise() -> None:
    nginx = NGINX_CONF.read_text(encoding="utf-8")

    assert "access_log off;" in nginx


def test_changelog_is_a_lazy_static_asset_instead_of_a_javascript_string() -> None:
    component = CHANGELOG_MENU.read_text(encoding="utf-8")
    vite = VITE_CONFIG.read_text(encoding="utf-8")

    assert 'CHANGELOG.md?url"' in component
    assert 'CHANGELOG.md?raw"' not in component
    assert "fetch(changelogUrl" in component
    assert "js,css,md,ico" in vite

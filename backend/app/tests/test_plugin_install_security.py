"""插件安装安全回归测试（P0#6 硬项）。

覆盖四类风险：
1) 先验签后执行（未签名/伪签名时，不触发 parse_zip/manifest 执行）
2) 路径穿越（zip member 含 .. / 绝对路径）
3) 超大 zip 拒绝
4) 伪签名拒绝（SIGNATURE_FAILED）
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.db.models.plugin import PluginInstall
from app.services import plugin_install_service as pis


class _FakeDB:
    """最小 DB stub，仅满足 install_zip 的 get/add/flush。"""

    def __init__(self) -> None:
        self.rows: dict[str, PluginInstall] = {}

    async def get(self, model, pk):  # noqa: ANN001
        if model is PluginInstall:
            return self.rows.get(pk)
        return None

    def add(self, obj) -> None:  # noqa: ANN001
        if isinstance(obj, PluginInstall):
            self.rows[obj.key] = obj

    async def flush(self) -> None:
        return None


def _make_zip(*, key: str = "demo", version: str = "0.1.0", extra_members: list[tuple[str, bytes]] | None = None) -> bytes:
    manifest_py = (
        "from app.worker.plugins.manifest import Manifest\n"
        f"MANIFEST = Manifest(key={key!r}, display_name='Demo', version={version!r})\n"
    ).encode()
    init_py = (
        b"from .plugin import DemoPlugin\n"
        b"from .manifest import MANIFEST\n"
        b"PLUGIN_CLASS = DemoPlugin\n"
    )
    plugin_py = (
        "from app.worker.plugins.base import Plugin, register\n"
        "@register\n"
        "class DemoPlugin(Plugin):\n"
        f"    key = {key!r}\n"
        "    display_name = 'Demo'\n"
    ).encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.py", manifest_py)
        zf.writestr("__init__.py", init_py)
        zf.writestr("plugin.py", plugin_py)
        if extra_members:
            for name, data in extra_members:
                zf.writestr(name, data)
    return buf.getvalue()


def _ed25519_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pub_pem


@pytest.mark.asyncio
async def test_install_zip_rejects_unsigned_before_parse(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pis.settings, "plugins_installed_dir", str(tmp_path / "installed"))
    _, pub = _ed25519_keypair()
    monkeypatch.setattr(pis.settings, "plugin_pubkey", pub)

    called = False

    def _parse_zip_should_not_run(_zip_bytes: bytes):  # noqa: ANN001
        nonlocal called
        called = True
        raise AssertionError("parse_zip 不应被调用")

    monkeypatch.setattr(pis, "parse_zip", _parse_zip_should_not_run)

    db = _FakeDB()
    payload = _make_zip(key="unsigned")
    with pytest.raises(pis.SignatureFailed) as ex:
        await pis.install_zip(db, zip_bytes=payload, signature=None)

    assert ex.value.code == "SIGNATURE_FAILED"
    assert called is False
    assert not (tmp_path / "installed").exists()


@pytest.mark.asyncio
async def test_install_zip_rejects_forged_signature_before_parse(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pis.settings, "plugins_installed_dir", str(tmp_path / "installed"))
    _, pub = _ed25519_keypair()
    monkeypatch.setattr(pis.settings, "plugin_pubkey", pub)

    called = False

    def _parse_zip_should_not_run(_zip_bytes: bytes):  # noqa: ANN001
        nonlocal called
        called = True
        raise AssertionError("parse_zip 不应被调用")

    monkeypatch.setattr(pis, "parse_zip", _parse_zip_should_not_run)

    db = _FakeDB()
    payload = _make_zip(key="forged")
    forged_sig = b"\x00" * 64
    with pytest.raises(pis.SignatureFailed) as ex:
        await pis.install_zip(db, zip_bytes=payload, signature=forged_sig)

    assert ex.value.code == "SIGNATURE_FAILED"
    assert called is False
    assert not (tmp_path / "installed").exists()


@pytest.mark.asyncio
async def test_install_zip_unsigned_rejected_before_parse_when_switch_off(monkeypatch, tmp_path) -> None:
    """未配置公钥但关闭未签名兼容开关时，未签名包也必须在 parse_zip 之前被拒。"""
    monkeypatch.setattr(pis.settings, "plugins_installed_dir", str(tmp_path / "installed"))
    monkeypatch.setattr(pis.settings, "plugin_pubkey", "")
    monkeypatch.setattr(pis.settings, "plugin_allow_legacy_unsigned_plugins", False)

    called = False

    def _parse_zip_should_not_run(_zip_bytes: bytes):  # noqa: ANN001
        nonlocal called
        called = True
        raise AssertionError("parse_zip 不应被调用")

    monkeypatch.setattr(pis, "parse_zip", _parse_zip_should_not_run)

    db = _FakeDB()
    payload = _make_zip(key="unsigned_switch_off")
    with pytest.raises(pis.SignatureFailed) as ex:
        await pis.install_zip(db, zip_bytes=payload, signature=None)

    assert ex.value.code == "SIGNATURE_FAILED"
    assert called is False
    assert not (tmp_path / "installed").exists()


def test_parse_zip_rejects_path_traversal() -> None:
    payload = _make_zip(extra_members=[("../escape.txt", b"x")])
    with pytest.raises(pis.InvalidZipStructure) as ex:
        pis.parse_zip(payload)
    assert ex.value.code in ("ZIP_PATH_TRAVERSAL", "ZIP_ABS_PATH")


def test_parse_zip_rejects_oversize(monkeypatch) -> None:
    monkeypatch.setattr(pis.settings, "plugin_zip_max_bytes", 50)
    payload = _make_zip(key="too_big")
    with pytest.raises(pis.ZipTooLarge):
        pis.parse_zip(payload)

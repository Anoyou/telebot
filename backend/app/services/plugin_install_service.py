"""第三方插件 zip 安装 / 卸载 / 启停服务（阶段 B）。

主要职责：
- 校验 zip 完整性（含 ``manifest.py`` / ``__init__.py`` / ``plugin.py``）
- 解析 manifest.py 拿到 ``MANIFEST`` 实例（不会 import 到 app 命名空间，全在临时目录隔离）
- 可选 Ed25519 签名校验：``settings.plugin_pubkey`` 配置公钥 + 上传 ``.sig`` 文件
- 把临时目录原子地搬到 ``settings.plugins_installed_dir/<key>/``
- 在 ``installed_plugin`` 表写一行（已存在则视为升级，写库覆盖）
- ``set_enabled`` / ``uninstall`` 工具函数

安全约束：
- zip 体积上限 ``settings.plugin_zip_max_bytes``
- 拒绝路径穿越（绝对路径、含 ``..`` 的成员）
- 拒绝与 builtin feature key 冲突
- 解压后任意单个成员失败都视作整体失败（解压前先校验完所有 names）
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.feature import BUILTIN_FEATURES, AccountFeature, Feature
from ..db.models.plugin import (
    PLUGIN_SOURCE_OFFICIAL,
    PLUGIN_SOURCE_ZIP,
    PLUGIN_TRUST_COMMUNITY,
    PLUGIN_TRUST_VERIFIED,
    InstalledPlugin,
)
from ..db.models.plugin_global_config import PluginGlobalConfig
from ..settings import settings
from ..worker.plugins.manifest import Manifest
from .remote_plugin_service import (
    lint_plugin_metadata_files,
    upsert_installed_plugin,
)

log = logging.getLogger(__name__)
# ``official`` 仅保留给旧安装记录的启停/卸载兼容；新推荐入口写入 repo。
_PACKAGE_MANAGED_SOURCES = (PLUGIN_SOURCE_ZIP, PLUGIN_SOURCE_OFFICIAL)


# ─────────────────────────────────────────────────────
# 错误类型
# ─────────────────────────────────────────────────────
class PluginInstallError(Exception):
    """插件安装期间所有可恢复错误的基类。``code`` 用于 API 层映射 HTTP 状态。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ZipTooLarge(PluginInstallError):
    pass


class InvalidZipStructure(PluginInstallError):
    pass


class ManifestError(PluginInstallError):
    pass


class KeyConflict(PluginInstallError):
    pass


class SignatureFailed(PluginInstallError):
    pass


# ─────────────────────────────────────────────────────
# zip 解析结果
# ─────────────────────────────────────────────────────
@dataclass
class ParsedPlugin:
    """临时目录里待落盘的插件包元数据。"""

    manifest: Manifest
    # 解压后的临时目录（成功安装后会被 move 走；失败时调用方负责清理）
    extract_dir: Path


# ─────────────────────────────────────────────────────
# 公共：解析 zip
# ─────────────────────────────────────────────────────
REQUIRED_FILES = ("manifest.py", "__init__.py", "plugin.py")


def parse_zip(zip_bytes: bytes) -> ParsedPlugin:
    """解析上传的 zip，返回 ``ParsedPlugin``（含临时解压目录与 Manifest）。

    临时解压目录的 owner 是调用方：成功安装后 ``install_zip`` 会把它 move 到正式位置；
    失败时本函数已经在 except 分支里清理；调用方仅在 ``install_zip`` 之外使用 ParsedPlugin
    时需要自己 ``shutil.rmtree(parsed.extract_dir)``。
    """
    if len(zip_bytes) > settings.plugin_zip_max_bytes:
        raise ZipTooLarge(
            "ZIP_TOO_LARGE",
            f"zip 体积超出 {settings.plugin_zip_max_bytes // 1024 // 1024} MiB 上限",
        )

    # 解压到一个唯一的临时目录（在系统临时目录下，install 成功后再搬到正式位置）
    extract_dir = Path(tempfile.mkdtemp(prefix="telepilot-plugin-"))
    try:
        from io import BytesIO

        try:
            with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
                # 1) 路径穿越校验：拒绝绝对路径与含 ".." 的成员
                _validate_zip_members(zf)
                zf.extractall(extract_dir)
        except zipfile.BadZipFile as exc:
            raise InvalidZipStructure("BAD_ZIP", f"zip 不可读: {exc}") from exc

        # 2) 自动展开"单顶层目录"约定：不少打包器会以 ``mypkg/...`` 形式
        #    打包，于是真正内容在 ``extract_dir/mypkg``。如解压根只有一个子目录、
        #    且根目录没有 manifest.py，则把它当真正的根。
        root = _resolve_real_root(extract_dir)

        # 3) 必含文件检查
        for required in REQUIRED_FILES:
            if not (root / required).is_file():
                raise InvalidZipStructure(
                    "MISSING_REQUIRED_FILE",
                    f"zip 必须包含 {required}（找不到 {root / required}）",
                )

        # 4) 加载 manifest.py 拿 Manifest
        manifest = _load_manifest_from_path(root / "manifest.py")
        if not isinstance(manifest, Manifest):
            raise ManifestError(
                "BAD_MANIFEST",
                f"manifest.py 顶层 MANIFEST 必须是 Manifest 实例，得到 {type(manifest).__name__}",
            )
        if not manifest.key or "/" in manifest.key or "\\" in manifest.key:
            raise ManifestError(
                "BAD_MANIFEST_KEY",
                f"manifest.key 非法: {manifest.key!r}（不能为空且不允许斜杠）",
            )

        # 5) 与 builtin 冲突
        if manifest.key in BUILTIN_FEATURES:
            raise KeyConflict(
                "KEY_CONFLICTS_BUILTIN",
                f"key {manifest.key!r} 与内置插件冲突，请改 manifest.key",
            )

        # 如果根目录被展开过，把内容浮到 extract_dir 顶层（统一接口）
        if root != extract_dir:
            _flatten_into(root, extract_dir)

        return ParsedPlugin(manifest=manifest, extract_dir=extract_dir)
    except Exception:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise


def _validate_zip_members(zf: zipfile.ZipFile) -> None:
    """禁止绝对路径 / `..` 段，防止 zip slip。"""
    for name in zf.namelist():
        # 拒绝绝对路径
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise InvalidZipStructure(
                "ZIP_ABS_PATH",
                f"zip 不允许绝对路径成员: {name!r}",
            )
        parts = Path(name).parts
        if any(p == ".." for p in parts):
            raise InvalidZipStructure(
                "ZIP_PATH_TRAVERSAL",
                f"zip 不允许 .. 路径穿越: {name!r}",
            )


def _resolve_real_root(extract_dir: Path) -> Path:
    """对"打包者把内容包了一层目录"的情况自动展开。

    判定：解压根直接含 manifest.py → 就是根；否则若根下只有一个子目录且该子目录有
    manifest.py → 把它当真正的根。其余情况返回原 ``extract_dir``，让 ``parse_zip``
    后续的 ``MISSING_REQUIRED_FILE`` 报错来兜。
    """
    if (extract_dir / "manifest.py").is_file():
        return extract_dir
    children = [p for p in extract_dir.iterdir() if not p.name.startswith("__")]
    if len(children) == 1 and children[0].is_dir() and (children[0] / "manifest.py").is_file():
        return children[0]
    return extract_dir


def _flatten_into(src: Path, dst: Path) -> None:
    """把 ``src`` 内的所有内容平移到 ``dst`` 目录顶层；操作完后删掉 ``src`` 自身。

    用于 ``_resolve_real_root`` 找到的"嵌一层"目录展开到统一布局。
    """
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            # 父级（extract_dir）原本就有同名条目 → 不太可能，保险起见跳过
            continue
        shutil.move(str(item), str(target))
    src.rmdir()


def _load_manifest_from_path(manifest_py: Path) -> Manifest:
    """用 importlib spec 单独加载一个 manifest.py（不会污染 app 命名空间）。"""
    spec_name = f"_telepilot_pending_manifest_{manifest_py.parent.name}_{id(manifest_py)}"
    spec = importlib.util.spec_from_file_location(spec_name, manifest_py)
    if spec is None or spec.loader is None:
        raise ManifestError("MANIFEST_LOAD_FAIL", f"无法加载 {manifest_py}")
    mod = importlib.util.module_from_spec(spec)
    try:
        # sys.modules 注册一份，避免 manifest.py 内部 `from .xxx` 时 KeyError；
        # 加载完立刻 pop 掉防止常驻
        sys.modules[spec_name] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(spec_name, None)
    except Exception as exc:  # noqa: BLE001
        raise ManifestError("MANIFEST_EXEC_FAIL", f"manifest.py 执行失败: {exc}") from exc
    manifest = getattr(mod, "MANIFEST", None)
    if manifest is None:
        raise ManifestError(
            "MANIFEST_MISSING_CONST",
            "manifest.py 必须导出顶层常量 MANIFEST: Manifest",
        )
    return manifest


# ─────────────────────────────────────────────────────
# 签名校验（Ed25519）
# ─────────────────────────────────────────────────────
def verify_signature(
    payload: bytes,
    signature: bytes | None,
    pubkey_pem: str | None,
) -> bool | None:
    """校验 detached 签名。返回三态：

    - ``True``：签名 + 公钥都在，且校验通过
    - ``False``：签名 + 公钥都在，但校验失败
    - ``None``：缺签名或缺公钥，跳过校验（前端展示"未签名"提示）
    """
    if not signature or not pubkey_pem:
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except Exception:  # noqa: BLE001
        log.warning("cryptography 未安装，跳过签名校验")
        return None
    try:
        key = load_pem_public_key(pubkey_pem.encode("utf-8"))
    except Exception:  # noqa: BLE001
        log.warning("plugin_pubkey 解析失败，跳过签名校验")
        return None
    try:
        # 直接 .verify(signature, payload)，对 Ed25519 / RSA-PKCS1 都通用？
        # 实际上 RSA 的 verify 签名不一样，这里只兼容 Ed25519，其他 key 类型走 hash + verify。
        # 为了简洁我们仅声明支持 Ed25519：
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if not isinstance(key, Ed25519PublicKey):
            log.warning(
                "plugin_pubkey 不是 Ed25519 公钥（type=%s），跳过校验",
                type(key).__name__,
            )
            return None
        key.verify(signature, payload)
        return True
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001
        log.exception("签名校验抛出未知异常")
        return False


# ─────────────────────────────────────────────────────
# 安装 / 升级 / 卸载 / 启停
# ─────────────────────────────────────────────────────
async def install_zip(
    db: AsyncSession,
    *,
    zip_bytes: bytes,
    signature: bytes | None = None,
    source: str = PLUGIN_SOURCE_ZIP,
) -> InstalledPlugin:
    """完整的 zip 安装流程：解析 → 验签 → 落盘 → 写表。

    存在同名 ``key`` 时视作"升级"：写库 UPDATE 同时覆盖目录；
    保留旧的 ``enabled`` 状态，但若新签名失败强制 enabled=False（管理员需手动启用）。
    """
    sig_ok = verify_signature(zip_bytes, signature, settings.plugin_pubkey or None)
    # 安全要求：任何未签名/验签失败的包都不进入解压与 manifest 执行阶段。
    if sig_ok is not True:
        raise SignatureFailed(
            "SIGNATURE_FAILED",
            "插件签名缺失或校验失败，已拒绝安装",
        )

    parsed = parse_zip(zip_bytes)
    try:
        # 路径计算
        installed_root = settings.plugins_installed_path
        installed_root.mkdir(parents=True, exist_ok=True)
        final_dir = (installed_root / parsed.manifest.key).resolve()
        # 防御性：再校验 final_dir 一定在 installed_root 之下（避免 manifest.key 含路径符）
        if installed_root not in final_dir.parents and final_dir != installed_root:
            raise KeyConflict(
                "BAD_KEY_PATH",
                f"非法 manifest.key 派生路径: {parsed.manifest.key!r}",
            )

        # 旧记录（升级情况）
        existing = await db.get(InstalledPlugin, parsed.manifest.key)
        was_enabled = bool(existing.enabled) if existing is not None else False

        # 删除旧目录后把临时目录搬过去
        if final_dir.exists():
            shutil.rmtree(final_dir)
        shutil.move(str(parsed.extract_dir), str(final_dir))
        # parsed.extract_dir 已被 move 走，不必再 rmtree

        # 计算最终 enabled：sig_ok=False 时强制 false
        final_enabled = was_enabled and (sig_ok is not False)

        manifest_json = parsed.manifest.to_dict()
        lint_warnings = lint_plugin_metadata_files(final_dir)
        row = await upsert_installed_plugin(
            db,
            key=parsed.manifest.key,
            source=source,
            source_url=None,
            installed_path=str(final_dir),
            version=parsed.manifest.version,
            manifest_json=manifest_json,
            enabled=final_enabled,
            signature_ok=sig_ok,
            trust_tier=PLUGIN_TRUST_VERIFIED if sig_ok is True else PLUGIN_TRUST_COMMUNITY,
            source_label="ZIP",
            last_install_error=None,
            lint_warnings=lint_warnings,
        )
        await db.flush()
        return row
    except Exception:
        # 任何失败都清理临时目录（如果还在）
        if parsed.extract_dir.exists():
            shutil.rmtree(parsed.extract_dir, ignore_errors=True)
        raise


async def uninstall(db: AsyncSession, key: str) -> bool:
    """卸载指定 key：删表行 + 删目录 + 清理功能矩阵残留。"""
    row = await db.get(InstalledPlugin, key)
    if row is None or row.source not in _PACKAGE_MANAGED_SOURCES:
        return False
    target = Path(row.installed_path or settings.plugins_installed_path / key)

    afs = (
        await db.execute(
            select(AccountFeature).where(AccountFeature.feature_key == key)
        )
    ).scalars().all()
    for af in afs:
        await db.delete(af)

    global_config = await db.get(PluginGlobalConfig, key)
    if global_config is not None:
        await db.delete(global_config)

    feat = await db.get(Feature, key)
    if feat is not None and not bool(feat.is_builtin):
        await db.delete(feat)

    await db.delete(row)
    await db.flush()
    # 删目录失败不阻塞 DB 提交（但写日志方便排查）
    try:
        if target.exists():
            shutil.rmtree(target)
    except Exception:  # noqa: BLE001
        log.exception("卸载插件 %s 时删除目录失败 %s", key, target)
    return True


async def set_enabled(db: AsyncSession, key: str, enabled: bool) -> InstalledPlugin:
    """设置 enabled 标志；调用方负责后续向 worker 广播 reload_config。"""
    row = await db.get(InstalledPlugin, key)
    if row is None or row.source not in _PACKAGE_MANAGED_SOURCES:
        raise PluginInstallError("PLUGIN_NOT_FOUND", f"插件不存在: {key}")
    if enabled and row.signature_ok is False:
        # 签名失败时不允许直接 enable；前端要显式"我知道风险"再调，会先把 signature_ok 置 None
        raise SignatureFailed(
            "SIGNATURE_FAILED",
            "签名校验失败，禁止启用；管理员可先重新上传带正确签名的 zip",
        )
    row.enabled = bool(enabled)
    await db.flush()
    return row


async def list_installed(db: AsyncSession) -> list[InstalledPlugin]:
    """列出 zip 与旧 official 安装包，按 key 字典序。"""
    rows = (
        await db.execute(
            select(InstalledPlugin)
            .where(InstalledPlugin.source.in_(_PACKAGE_MANAGED_SOURCES))
            .order_by(InstalledPlugin.key)
        )
    ).scalars().all()
    return list(rows)


# ─────────────────────────────────────────────────────
# 内部 BytesIO 包装：保留兼容（早期实现遗留，目前直接走 io.BytesIO）
# ─────────────────────────────────────────────────────


__all__ = [
    "InvalidZipStructure",
    "KeyConflict",
    "ManifestError",
    "ParsedPlugin",
    "PluginInstallError",
    "SignatureFailed",
    "ZipTooLarge",
    "install_zip",
    "list_installed",
    "parse_zip",
    "set_enabled",
    "uninstall",
    "verify_signature",
]

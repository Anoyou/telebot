"""插件仓库（plugin_repo）服务：浏览 Git 仓库内可装插件 + 选择性安装。

与 ``remote_plugin_service`` 的分工：
- 本服务管理“仓库列表”（plugin_repo 表）：CRUD + 列出仓库内插件 + 触发安装
- ``remote_plugin_service`` 才是真正的“已安装插件”落地逻辑（写 installed_plugin 表 +
  落盘 plugins/installed/<name>/ + 注册 Feature + 热加载）

安装路径分两种：
1) 仓库根目录就是单个插件（含 plugin.json）→ 直接复用
   ``remote_plugin_service.install(repo_url)`` 走 git clone 流程
2) 仓库下多个子目录、每个子目录一个插件 → 不能简单 git clone 整个仓库到
   ``plugins/installed/<plugin>/``（会把无关插件也带进去）；本服务从本地缓存
   的对应子目录复制到 ``plugins/installed/<plugin>/``，写 installed_plugin 行 +
   注册 Feature + 触发热加载，整套流程与 ``remote_plugin_service.install`` 一致

安全设计：
- 同样禁止执行 ``manifest.py``，只静态解析 ``plugin.json``
- ``url`` 走与 ``remote_plugin_service._validate_source_url`` 同套校验
- 缓存目录隔离：每个 URL 对应一个 sha256 摘要目录，避免文件名冲突 / 路径穿越
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt_str, encrypt_str
from ..db.models.account import Account
from ..db.models.feature import FEATURE_STATE_DISABLED, AccountFeature, Feature
from ..db.models.plugin import (
    PLUGIN_SOURCE_LOCAL,
    PLUGIN_SOURCE_REPO,
    PLUGIN_TRUST_COMMUNITY,
    PLUGIN_TRUST_LOCAL,
    InstalledPlugin,
)
from ..db.models.plugin_repo import PluginRepo
from ..schemas.plugin_repo import (
    PluginRepoBulkUpdateItem,
    PluginRepoBulkUpdateResult,
    PluginRepoPlugin,
)
from ..settings import settings
from .remote_plugin_service import (
    DuplicatePluginName,
    InvalidPluginMetadata,
    PluginMetadata,
    RemotePluginError,
    RemotePluginView,
    _derive_name_from_url,
    _feature_manifest_from_meta,
    _manifest_json_from_remote_meta,
    _merge_feature_manifest_preserving_global_config,
    _plugin_dir,
    _read_plugin_metadata,
    _remote_info_from_manifest,
    _run_git,
    _validate_runtime_plugin_shape,
    _validate_source_url,
    _with_remote_info,
    lint_plugin_metadata_files,
    normalize_git_source_url,
    remote_plugin_view_from_installed,
    upsert_installed_plugin,
)

log = logging.getLogger(__name__)

PLUGIN_REPO_AUTH_NONE = "none"
PLUGIN_REPO_AUTH_GITHUB_TOKEN = "github_token"
DEFAULT_OFFICIAL_PLUGIN_REPO_URL = "https://github.com/Anoyou/telebot-plugins"


@dataclass(frozen=True, slots=True)
class _OfficialPluginSource:
    plugin_dir: Path
    meta: PluginMetadata
    source_url: str
    remote: bool = False


async def git_env_for_source_url(db: AsyncSession, source_url: str) -> dict[str, str] | None:
    """返回已保存插件仓库 URL 对应的 git 临时凭证环境。"""
    if not source_url or source_url.startswith("local://"):
        return None
    row = (
        await db.execute(select(PluginRepo).where(PluginRepo.url == source_url.strip()))
    ).scalar_one_or_none()
    if row is None:
        return None
    return _github_token_env(row.url, _repo_credential(row))


# ─────────────────────────────────────────────────────
# 错误类型
# ─────────────────────────────────────────────────────
class PluginRepoError(Exception):
    """plugin_repo 操作基类异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PluginRepoNotFound(PluginRepoError):
    """根据 id 查不到 plugin_repo 行。"""


class DuplicatePluginRepo(PluginRepoError):
    """同 URL 已经保存过。"""


class PluginNotInRepo(PluginRepoError):
    """仓库内找不到指定名字的插件。"""


class InvalidPluginRepoCredential(PluginRepoError):
    """插件仓库凭证不合法。"""


# ─────────────────────────────────────────────────────
# 缓存目录管理
# ─────────────────────────────────────────────────────
def _cache_root() -> Path:
    """所有仓库克隆的缓存根目录。"""
    root = settings.resolve_project_path(settings.plugin_repos_cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _local_import_root() -> Path:
    """本地调试插件目录：开发者把插件目录放到这里后可在 Web 一键导入。"""
    root = settings.resolve_project_path("./plugins/local_imports")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _official_plugin_root() -> Path:
    """历史随包推荐插件目录。

    当前 TelePilot 本体不再从这里分发普通插件；推荐入口只读取
    ``settings.official_plugin_repo_url`` 指向的插件库。保留函数名是为了兼容
    旧测试和迁移代码里对该路径的 monkeypatch。
    """

    return Path(__file__).resolve().parents[1] / "worker" / "plugins" / "official"


def _official_plugin_repo_url() -> str:
    return str(getattr(settings, "official_plugin_repo_url", "") or DEFAULT_OFFICIAL_PLUGIN_REPO_URL).strip()


async def _official_remote_plugin_root(*, force_refresh: bool = False) -> Path:
    return await _ensure_repo_cached(_official_plugin_repo_url(), force_refresh=force_refresh)


def _cache_dir_for(url: str) -> Path:
    """单个仓库 URL 对应的缓存目录。

    用 sha256(url) 当目录名，既避免文件系统非法字符也防止路径穿越攻击。
    """
    digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]
    return _cache_root() / digest


def _normalize_repo_auth_type(auth_type: str | None, token: str | None = None) -> str:
    raw = str(auth_type or "").strip().lower()
    if raw in {"", "public"}:
        return PLUGIN_REPO_AUTH_GITHUB_TOKEN if token and token.strip() else PLUGIN_REPO_AUTH_NONE
    if raw == PLUGIN_REPO_AUTH_NONE:
        return PLUGIN_REPO_AUTH_NONE
    if raw in {"github", PLUGIN_REPO_AUTH_GITHUB_TOKEN, "token", "pat"}:
        return PLUGIN_REPO_AUTH_GITHUB_TOKEN
    raise InvalidPluginRepoCredential(
        "BAD_REPO_CREDENTIAL",
        "插件仓库凭证类型仅支持 github_token 或 none",
    )


def _validate_github_token(token: str) -> str:
    value = token.strip()
    if not value:
        raise InvalidPluginRepoCredential(
            "BAD_REPO_CREDENTIAL",
            "GitHub Token 不能为空；如不需要私有仓库凭证请清除凭证。",
        )
    if any(ch.isspace() for ch in value):
        raise InvalidPluginRepoCredential(
            "BAD_REPO_CREDENTIAL",
            "GitHub Token 不能包含空白字符。",
        )
    if len(value) < 8:
        raise InvalidPluginRepoCredential(
            "BAD_REPO_CREDENTIAL",
            "GitHub Token 长度过短。",
        )
    return value


def _repo_credential(row: PluginRepo) -> str | None:
    token_enc = getattr(row, "credential_enc", None)
    if not token_enc:
        return None
    try:
        return decrypt_str(str(token_enc))
    except ValueError as exc:
        raise InvalidPluginRepoCredential(
            "REPO_CREDENTIAL_DECRYPT_FAILED",
            "插件仓库凭证无法解密，通常是 MASTER_KEY 已变更。请恢复原 MASTER_KEY，或重新保存该仓库凭证。",
        ) from exc


def _github_token_env(url: str, token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise InvalidPluginRepoCredential(
            "BAD_REPO_CREDENTIAL_TARGET",
            "GitHub Token 仅支持 https://github.com/... 插件仓库。SSH 私有仓库请使用服务器侧 SSH key。",
        )
    # 不把 token 拼进 URL，避免污染 DB、缓存 key、git remote 和错误日志。
    basic = b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


async def _ensure_repo_cached(
    url: str,
    *,
    force_refresh: bool = False,
    token: str | None = None,
) -> Path:
    """确保仓库已克隆到本地缓存；返回缓存目录路径。

    - 首次：浅克隆目标分支，插件仓库只需要当前版本文件
    - 已存在：只刷新 origin 的目标引用并 reset，避免每次扫全量远端引用
    - 失败：清理半完成的克隆目录，向上抛 ``GitOperationFailed``
    """
    _validate_source_url(url)
    source = normalize_git_source_url(url)
    git_env = _github_token_env(source.clone_url, token)
    target = _cache_dir_for(url)

    if not target.exists() or force_refresh and target.exists() and not (target / ".git").exists():
        # 第一种：根本不存在 → clone
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            args = ["clone", "--depth", "1"]
            if source.ref:
                args.extend(["--branch", source.ref, "--single-branch"])
            args.extend([source.clone_url, str(target)])
            await _run_git(*args, timeout=90.0, env=git_env)
        except Exception:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise
        return target

    # 缓存命中：普通列表读取允许用旧副本兜底；强制刷新必须暴露失败，避免 UI 误报成功。
    try:
        if source.ref:
            await _run_git(
                "fetch",
                "--depth",
                "1",
                "--prune",
                "origin",
                f"+refs/heads/{source.ref}:refs/remotes/origin/{source.ref}",
                cwd=target,
                timeout=30.0,
                env=git_env,
            )
            remote_ref = f"refs/remotes/origin/{source.ref}"
            await _run_git("rev-parse", "--verify", remote_ref, cwd=target, timeout=10.0)
            await _run_git("reset", "--hard", remote_ref, cwd=target, timeout=30.0)
        else:
            await _run_git(
                "fetch",
                "--depth",
                "1",
                "--prune",
                "origin",
                cwd=target,
                timeout=30.0,
                env=git_env,
            )
            # 用 origin 的默认分支做硬重置；--ff-only 在分支变更时会失败，硬重置更鲁棒
            head = (await _run_git(
                "symbolic-ref", "refs/remotes/origin/HEAD", cwd=target, timeout=10.0,
            )).strip() or "refs/remotes/origin/HEAD"
            # symbolic-ref 输出形如 "refs/remotes/origin/main"
            await _run_git("reset", "--hard", head, cwd=target, timeout=30.0)
    except Exception:  # noqa: BLE001
        if force_refresh:
            raise
        log.warning("刷新仓库缓存失败，继续使用旧副本: %s", url, exc_info=True)
    return target


# ─────────────────────────────────────────────────────
# 浏览仓库：找出可装插件清单
# ─────────────────────────────────────────────────────
def _name_from_url(url: str) -> str:
    """从 URL 派生默认显示名（与 remote_plugin_service 中的派生一致）。"""
    try:
        return _derive_name_from_url(url)
    except RemotePluginError:
        return url


def _scan_plugins(repo_dir: Path) -> list[tuple[str, Path]]:
    """扫描仓库目录，找出所有插件子目录。

    返回 ``[(默认名, 绝对路径), ...]``：
    - 仓库根有 plugin.json → 整个仓库是一个插件，默认名 = 目录名
    - 否则扫描一级子目录，凡含 plugin.json 的都算一个插件，默认名 = 子目录名

    只识别含 ``plugin.json`` 的目录；没有 plugin.json 的目录无法解析元数据、
    也无法走安全安装流程，故不在列表中暴露。
    """
    results: list[tuple[str, Path]] = []
    if (repo_dir / "plugin.json").is_file():
        results.append((repo_dir.name, repo_dir))
        return results

    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        # 跳过 .git / 隐藏目录
        if child.name.startswith("."):
            continue
        if (child / "plugin.json").is_file():
            results.append((child.name, child))
    return results


def _version_tuple(raw: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", str(raw or ""))[:3])


async def list_plugins_in_repo(
    db: AsyncSession,
    repo_id: int,
    *,
    force_refresh: bool = False,
) -> list[PluginRepoPlugin]:
    """列出 ``plugin_repo[id]`` 仓库内所有可装插件。

    步骤：
      1. 取仓库行
      2. ``_ensure_repo_cached`` 拉到最新副本
      3. 扫描根 / 一级子目录里的 plugin.json
      4. 用 ``_read_plugin_metadata`` 静态解析元数据
      5. 与 ``installed_plugin.key`` 做差集，标记 ``installed`` 字段
    """
    row = await _get_repo(db, repo_id)

    repo_dir = await _ensure_repo_cached(
        row.url,
        force_refresh=force_refresh,
        token=_repo_credential(row),
    )
    raw = _scan_plugins(repo_dir)

    installed_rows = (
        await db.execute(select(InstalledPlugin.key, InstalledPlugin.version))
    ).all()
    installed_versions = {str(name): str(version or "") for name, version in installed_rows}

    out: list[PluginRepoPlugin] = []
    for default_name, plugin_dir in raw:
        try:
            meta = _read_plugin_metadata(plugin_dir, fallback_name=default_name)
        except InvalidPluginMetadata:
            log.warning("跳过仓库 %s 内非法插件目录: %s", row.url, plugin_dir)
            continue
        subdir = (
            ""
            if plugin_dir == repo_dir
            else str(plugin_dir.relative_to(repo_dir))
        )
        installed_version = installed_versions.get(meta.name)
        out.append(
            PluginRepoPlugin(
                name=meta.name,
                display_name=meta.display_name or meta.name,
                description=meta.description,
                usage=meta.usage,
                author=meta.author,
                version=meta.version,
                installed=installed_version is not None,
                installed_version=installed_version,
                update_available=(
                    installed_version is not None
                    and _version_tuple(meta.version) > _version_tuple(installed_version)
                ),
                event_subscriptions=[item for item in meta.event_subscriptions if isinstance(item, dict)],
                capabilities=dict(meta.capabilities) if isinstance(meta.capabilities, dict) else {},
                permissions=list(meta.permissions or []),
                tags=list(meta.tags or []),
                subdir=subdir,
            )
        )
    # 已装的排到后面，便于用户先看到可装的
    out.sort(key=lambda p: (p.installed, p.name))
    return out


# ─────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────
async def list_repos(db: AsyncSession) -> list[PluginRepo]:
    """按 ``added_at`` 倒序列出所有仓库。"""
    rows = (
        await db.execute(select(PluginRepo).order_by(PluginRepo.added_at.desc()))
    ).scalars().all()
    return list(rows)


async def _get_repo(db: AsyncSession, repo_id: int) -> PluginRepo:
    row = (
        await db.execute(select(PluginRepo).where(PluginRepo.id == repo_id))
    ).scalar_one_or_none()
    if row is None:
        raise PluginRepoNotFound("REPO_NOT_FOUND", f"仓库不存在: id={repo_id}")
    return row


async def get_repo(db: AsyncSession, repo_id: int) -> PluginRepo:
    """对外的 get；不存在抛 ``PluginRepoNotFound``。"""
    return await _get_repo(db, repo_id)


async def create_repo(
    db: AsyncSession,
    url: str,
    *,
    name: str | None = None,
    description: str | None = None,
    auth_type: str | None = None,
    credential: str | None = None,
) -> PluginRepo:
    """新增仓库行。

    - URL 必须通过 ``_validate_source_url``
    - URL 全局唯一；重复时抛 ``DuplicatePluginRepo``
    - ``name`` 为空时从 URL 派生
    """
    _validate_source_url(url)
    url = url.strip()

    existing = (
        await db.execute(select(PluginRepo).where(PluginRepo.url == url))
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicatePluginRepo("REPO_EXISTS", f"仓库已保存: {url}")

    normalized_auth_type = _normalize_repo_auth_type(auth_type, credential)
    credential_enc: str | None = None
    if normalized_auth_type == PLUGIN_REPO_AUTH_GITHUB_TOKEN:
        token = _validate_github_token(credential or "")
        _github_token_env(url, token)
        credential_enc = encrypt_str(token)

    row = PluginRepo(
        url=url,
        name=(name or "").strip() or _name_from_url(url),
        description=(description or "").strip(),
        auth_type=normalized_auth_type,
        credential_enc=credential_enc,
    )
    db.add(row)
    await db.flush()
    return row


async def update_repo_credential(
    db: AsyncSession,
    repo_id: int,
    *,
    auth_type: str | None,
    token: str | None,
) -> PluginRepo:
    """更新或清除插件仓库凭证。"""
    row = await _get_repo(db, repo_id)
    if not (token or "").strip() or str(auth_type or "").strip().lower() in {
        PLUGIN_REPO_AUTH_NONE,
        "public",
    }:
        row.auth_type = PLUGIN_REPO_AUTH_NONE
        row.credential_enc = None
        await db.flush()
        return row
    normalized_auth_type = _normalize_repo_auth_type(auth_type, token)
    if normalized_auth_type == PLUGIN_REPO_AUTH_NONE:
        row.auth_type = PLUGIN_REPO_AUTH_NONE
        row.credential_enc = None
    else:
        value = _validate_github_token(token or "")
        _github_token_env(row.url, value)
        row.auth_type = normalized_auth_type
        row.credential_enc = encrypt_str(value)
    await db.flush()
    return row


async def delete_repo(db: AsyncSession, repo_id: int) -> bool:
    """删除仓库行；同时清理本地缓存目录。

    ``False`` 表示行不存在。**不**联动卸载已安装的插件——仓库只是“目录”，
    用户卸载具体插件应走插件管理页面（remote_plugin DELETE 接口）。
    """
    row = (
        await db.execute(select(PluginRepo).where(PluginRepo.id == repo_id))
    ).scalar_one_or_none()
    if row is None:
        return False

    cache = _cache_dir_for(row.url)
    await db.delete(row)
    await db.flush()

    # DB 已提交逻辑由调用方负责；这里只清理缓存目录
    try:
        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
    except Exception:  # noqa: BLE001
        log.exception("清理仓库缓存目录失败: %s", cache)
    return True


# ─────────────────────────────────────────────────────
# 选择性安装
# ─────────────────────────────────────────────────────
async def install_plugin_from_repo(
    db: AsyncSession,
    repo_id: int,
    plugin_name: str,
    *,
    default_enabled: bool = False,
) -> RemotePluginView:
    """从仓库中安装指定名字的插件。

    步骤：
      1. 取仓库行 → 确保缓存最新（git clone / pull）
      2. 在缓存里定位含 plugin.json 的插件目录
      3. 校验目标安装目录 ``plugins/installed/<plugin_name>/`` 不存在且 DB 无同名行
      4. ``copytree`` 把插件目录拷过去（不含 .git）
      5. 写 ``installed_plugin`` 行（source_url 用仓库 URL，便于追溯）
      6. 注册到 ``feature`` 表 + 按 ``default_enabled`` 批量启用账号
      7. 触发 worker 热加载
    """
    row = await _get_repo(db, repo_id)
    repo_dir = await _ensure_repo_cached(row.url, token=_repo_credential(row))

    # 扫描定位插件子目录
    raw = _scan_plugins(repo_dir)
    target_dir: Path | None = None
    for default_name, plugin_dir in raw:
        try:
            meta = _read_plugin_metadata(plugin_dir, fallback_name=default_name)
        except InvalidPluginMetadata:
            continue
        if meta.name == plugin_name:
            target_dir = plugin_dir
            break
    if target_dir is None:
        raise PluginNotInRepo(
            "PLUGIN_NOT_IN_REPO",
            f"仓库 {row.url!r} 内未找到插件 {plugin_name!r}",
        )

    # 重读元数据（已校验通过；再读一次以拿到完整字段）
    meta = _read_plugin_metadata(target_dir, fallback_name=plugin_name)
    _validate_runtime_plugin_shape(target_dir, meta)
    final_name = meta.name

    # 统一从缓存副本复制。公开仓库与私有仓库行为一致；私有仓库不会触发二次无凭证 clone。
    install_path = _plugin_dir(final_name)
    staging = install_path.parent / f"{install_path.name}.installing"

    # 重名检查：DB 行 + 目录都不能存在
    existing = await db.get(InstalledPlugin, final_name)
    if existing is not None:
        raise DuplicatePluginName(
            "PLUGIN_EXISTS", f"插件 {final_name!r} 已安装"
        )
    if install_path.exists():
        raise DuplicatePluginName(
            "DIR_EXISTS",
            f"目录已存在但 DB 无记录: {install_path}（请先手动清理）",
        )
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    install_path.parent.mkdir(parents=True, exist_ok=True)
    renamed = False
    try:
        shutil.copytree(
            target_dir,
            staging,
            ignore=shutil.ignore_patterns(".git", ".gitignore", "__pycache__"),
        )
        staged_meta = _read_plugin_metadata(staging, fallback_name=final_name)
        _validate_runtime_plugin_shape(staging, staged_meta)
        lint_warnings = lint_plugin_metadata_files(staging)
        staging.rename(install_path)
        renamed = True
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise PluginRepoError(
            "COPY_FAILED", f"复制插件目录失败: {exc}"
        ) from exc

    try:
        final_enabled = bool(default_enabled)
        manifest_json = _with_remote_info(
            _manifest_json_from_remote_meta(meta),
            default_enabled=default_enabled,
            latest_version=meta.version,
            update_available=False,
        )

        feat = (
            await db.execute(select(Feature).where(Feature.key == final_name))
        ).scalar_one_or_none()
        if feat is None:
            db.add(
                Feature(
                    key=final_name,
                    display_name=meta.display_name or final_name,
                    is_builtin=False,
                    version=meta.version,
                    manifest=_feature_manifest_from_meta(meta),
                )
            )
        else:
            feat.display_name = meta.display_name or final_name
            feat.version = meta.version
            feat.is_builtin = False
            feat.manifest = _feature_manifest_from_meta(meta)

        await db.flush()
        installed_row = await upsert_installed_plugin(
            db,
            key=final_name,
            source=PLUGIN_SOURCE_REPO,
            source_url=row.url,
            installed_path=str(install_path),
            version=meta.version,
            manifest_json=manifest_json,
            enabled=final_enabled,
            signature_ok=None,
            trust_tier=PLUGIN_TRUST_COMMUNITY,
            source_label="Plugin Repo",
            last_install_error=None,
            lint_warnings=lint_warnings,
        )
        await db.flush()

        if default_enabled:
            aids = (await db.execute(select(Account.id))).scalars().all()
            for aid in aids:
                af = (
                    await db.execute(
                        select(AccountFeature).where(
                            AccountFeature.account_id == int(aid),
                            AccountFeature.feature_key == final_name,
                        )
                    )
                ).scalar_one_or_none()
                if af is None:
                    db.add(
                        AccountFeature(
                            account_id=int(aid),
                            feature_key=final_name,
                            enabled=True,
                            state=FEATURE_STATE_DISABLED,
                        )
                    )
            await db.flush()
    except Exception:
        # 写库 / 元数据失败 → 回滚已复制的目录
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if renamed and install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)
        raise

    return remote_plugin_view_from_installed(installed_row)


async def _replace_installed_plugin_from_repo_dir(
    db: AsyncSession,
    *,
    repo_url: str,
    plugin_dir: Path,
    meta: PluginMetadata,
    installed: InstalledPlugin,
) -> RemotePluginView:
    """用仓库缓存中的插件目录覆盖已安装插件，并保留用户启停状态。"""

    final_name = meta.name
    install_path = Path(installed.installed_path or _plugin_dir(final_name))
    staging = install_path.parent / f"{install_path.name}.installing"
    backup = install_path.parent / f"{install_path.name}.bak-update"

    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    install_path.parent.mkdir(parents=True, exist_ok=True)
    swapped = False
    try:
        shutil.copytree(
            plugin_dir,
            staging,
            ignore=shutil.ignore_patterns(".git", ".gitignore", "__pycache__"),
        )
        staged_meta = _read_plugin_metadata(staging, fallback_name=final_name)
        if staged_meta.name != final_name:
            raise PluginRepoError(
                "PLUGIN_NAME_MISMATCH",
                f"仓库中插件名为 {staged_meta.name!r}，预期 {final_name!r}",
            )
        _validate_runtime_plugin_shape(staging, staged_meta)
        lint_warnings = lint_plugin_metadata_files(staging)

        if install_path.exists():
            install_path.rename(backup)
        staging.rename(install_path)
        swapped = True
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if swapped and install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)
        if backup.exists() and not install_path.exists():
            backup.rename(install_path)
        if isinstance(exc, PluginRepoError):
            raise
        raise PluginRepoError("COPY_FAILED", f"复制插件目录失败: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists():
            if swapped:
                shutil.rmtree(backup, ignore_errors=True)
            elif not install_path.exists():
                backup.rename(install_path)

    old_enabled = bool(installed.enabled)
    old_default_enabled = remote_plugin_view_from_installed(installed).default_enabled

    feat = (
        await db.execute(select(Feature).where(Feature.key == final_name))
    ).scalar_one_or_none()
    if feat is None:
        db.add(
            Feature(
                key=final_name,
                display_name=meta.display_name or final_name,
                is_builtin=False,
                version=meta.version,
                manifest=_feature_manifest_from_meta(meta),
            )
        )
    else:
        feat.display_name = meta.display_name or final_name
        feat.version = meta.version
        feat.is_builtin = False
        feat.manifest = _merge_feature_manifest_preserving_global_config(feat.manifest, meta)

    manifest_json = _with_remote_info(
        _manifest_json_from_remote_meta(meta),
        default_enabled=old_default_enabled,
        latest_version=meta.version,
        update_available=False,
    )
    row = await upsert_installed_plugin(
        db,
        key=final_name,
        source=PLUGIN_SOURCE_REPO,
        source_url=repo_url,
        installed_path=str(install_path),
        version=meta.version,
        manifest_json=manifest_json,
        enabled=old_enabled,
        signature_ok=None,
        trust_tier=PLUGIN_TRUST_COMMUNITY,
        source_label="Plugin Repo",
        last_install_error=None,
        lint_warnings=lint_warnings,
    )
    await db.flush()
    return remote_plugin_view_from_installed(row)


def _installed_plugin_metadata_needs_refresh(installed: InstalledPlugin, meta: PluginMetadata) -> bool:
    """同版本插件也可能残留旧 manifest；批量更新时需要顺手自愈。"""

    manifest = dict(installed.manifest_json or {})
    manifest_version = str(manifest.get("version") or "")
    if manifest_version and manifest_version != str(meta.version or ""):
        return True

    remote_info = _remote_info_from_manifest(manifest)
    if bool(remote_info.get("update_available", False)):
        return True
    if str(remote_info.get("latest_version") or "") not in {"", str(meta.version or "")}:
        return True
    return False


async def update_installed_plugins_from_repo(
    db: AsyncSession,
    repo_id: int,
) -> PluginRepoBulkUpdateResult:
    """把仓库中版本更高的已安装插件批量升级到该仓库版本。"""

    row = await _get_repo(db, repo_id)
    repo_dir = await _ensure_repo_cached(
        row.url,
        force_refresh=True,
        token=_repo_credential(row),
    )
    result = PluginRepoBulkUpdateResult(repo_id=row.id, repo_name=row.name)

    for default_name, plugin_dir in _scan_plugins(repo_dir):
        try:
            meta = _read_plugin_metadata(plugin_dir, fallback_name=default_name)
        except InvalidPluginMetadata as exc:
            result.failed += 1
            result.items.append(
                PluginRepoBulkUpdateItem(
                    name=default_name,
                    status="failed",
                    message=exc.message,
                )
            )
            continue

        installed = await db.get(InstalledPlugin, meta.name)
        if installed is None:
            continue
        result.checked += 1
        old_version = str(installed.version or "")
        if _version_tuple(meta.version) <= _version_tuple(installed.version):
            if _version_tuple(meta.version) == _version_tuple(installed.version) and _installed_plugin_metadata_needs_refresh(installed, meta):
                try:
                    updated = await _replace_installed_plugin_from_repo_dir(
                        db,
                        repo_url=row.url,
                        plugin_dir=plugin_dir,
                        meta=meta,
                        installed=installed,
                    )
                except PluginRepoError as exc:
                    result.failed += 1
                    result.items.append(
                        PluginRepoBulkUpdateItem(
                            name=meta.name,
                            display_name=meta.display_name or meta.name,
                            from_version=old_version,
                            to_version=meta.version,
                            status="failed",
                            message=exc.message,
                        )
                    )
                    continue

                result.updated += 1
                result.items.append(
                    PluginRepoBulkUpdateItem(
                        name=updated.name,
                        display_name=updated.display_name or updated.name,
                        from_version=old_version,
                        to_version=updated.version,
                        status="updated",
                        message="已同步元数据",
                    )
                )
                continue

            result.skipped += 1
            result.items.append(
                PluginRepoBulkUpdateItem(
                    name=meta.name,
                    display_name=meta.display_name or meta.name,
                    from_version=old_version,
                    to_version=meta.version,
                    status="skipped",
                    message="本地已是该仓库中的最新版本",
                )
            )
            continue

        result.update_available += 1
        try:
            updated = await _replace_installed_plugin_from_repo_dir(
                db,
                repo_url=row.url,
                plugin_dir=plugin_dir,
                meta=meta,
                installed=installed,
            )
        except PluginRepoError as exc:
            result.failed += 1
            result.items.append(
                PluginRepoBulkUpdateItem(
                    name=meta.name,
                    display_name=meta.display_name or meta.name,
                    from_version=old_version,
                    to_version=meta.version,
                    status="failed",
                    message=exc.message,
                )
            )
            continue

        result.updated += 1
        result.items.append(
            PluginRepoBulkUpdateItem(
                name=updated.name,
                display_name=updated.display_name or updated.name,
                from_version=old_version,
                to_version=updated.version,
                status="updated",
                message="已更新",
            )
        )

    return result


def list_local_import_candidates() -> list[PluginRepoPlugin]:
    """列出 ``plugins/local_imports`` 下可导入的本地插件目录。"""
    root = _local_import_root()
    out: list[PluginRepoPlugin] = []
    for default_name, plugin_dir in _scan_plugins(root):
        try:
            meta = _read_plugin_metadata(plugin_dir, fallback_name=default_name)
        except InvalidPluginMetadata:
            log.warning("跳过本地非法插件目录: %s", plugin_dir)
            continue
        out.append(
            PluginRepoPlugin(
                name=meta.name,
                display_name=meta.display_name or meta.name,
                description=meta.description,
                usage=meta.usage,
                author=meta.author,
                version=meta.version,
                installed=False,
                event_subscriptions=[item for item in meta.event_subscriptions if isinstance(item, dict)],
                capabilities=dict(meta.capabilities) if isinstance(meta.capabilities, dict) else {},
                permissions=list(meta.permissions or []),
                tags=list(meta.tags or []),
                subdir=str(plugin_dir.relative_to(root)),
            )
        )
    out.sort(key=lambda p: p.name)
    return out


def _official_plugin_sort_key(item: PluginRepoPlugin) -> tuple[int, str]:
    recommended = {"auto_reply", "autorepeat"}
    return (0 if item.name in recommended else 1, item.name)


def _plugin_meta_has_official_tag(meta: PluginMetadata) -> bool:
    return "official" in {str(tag or "").strip().lower() for tag in (meta.tags or [])}


def _iter_local_official_sources() -> list[_OfficialPluginSource]:
    """不再从 TelePilot 随包目录提供推荐插件。"""

    return []


async def _iter_remote_official_sources(*, force_refresh: bool = False) -> list[_OfficialPluginSource]:
    root = await _official_remote_plugin_root(force_refresh=force_refresh)
    out: list[_OfficialPluginSource] = []
    for default_name, plugin_dir in _scan_plugins(root):
        try:
            meta = _read_plugin_metadata(plugin_dir, fallback_name=default_name)
        except InvalidPluginMetadata:
            log.warning("跳过远程官方非法插件目录: %s", plugin_dir)
            continue
        if not _plugin_meta_has_official_tag(meta):
            continue
        out.append(
            _OfficialPluginSource(
                plugin_dir=plugin_dir,
                meta=meta,
                source_url=_official_plugin_repo_url(),
                remote=True,
            )
        )
    return out


async def _find_official_plugin_source(plugin_name: str) -> _OfficialPluginSource | None:
    for source in _iter_local_official_sources():
        if source.meta.name == plugin_name:
            return source
    for source in await _iter_remote_official_sources():
        if source.meta.name == plugin_name:
            return source
    return None


def _manifest_json_for_official_source(source: _OfficialPluginSource) -> dict[str, Any]:
    if source.remote:
        data = _manifest_json_from_remote_meta(source.meta)
        data["source_url"] = source.source_url
        return data
    manifest_obj = None
    try:
        from ..feature_registry import _load_manifest_file  # noqa: PLC0415

        manifest_obj = _load_manifest_file(source.plugin_dir / "manifest.py")
    except Exception:  # noqa: BLE001
        manifest_obj = None
    return _manifest_json_from_manifest_object(manifest_obj, source.meta)


async def list_official_plugins(db: AsyncSession) -> list[PluginRepoPlugin]:
    """列出推荐插件库中带 official/recommended 标签的插件，并标记安装状态."""

    installed_rows = (
        await db.execute(select(InstalledPlugin.key, InstalledPlugin.version))
    ).all()
    installed_versions = {str(name): str(version or "") for name, version in installed_rows}
    out: list[PluginRepoPlugin] = []
    seen: set[str] = set()
    sources = _iter_local_official_sources()
    sources.extend(await _iter_remote_official_sources())
    for source in sources:
        meta = source.meta
        if meta.name in seen:
            continue
        seen.add(meta.name)
        installed_version = installed_versions.get(meta.name)
        out.append(
            PluginRepoPlugin(
                name=meta.name,
                display_name=meta.display_name or meta.name,
                description=meta.description,
                usage=meta.usage,
                author=meta.author,
                version=meta.version,
                installed=installed_version is not None,
                installed_version=installed_version,
                update_available=(
                    installed_version is not None
                    and _version_tuple(meta.version) > _version_tuple(installed_version)
                ),
                event_subscriptions=[item for item in meta.event_subscriptions if isinstance(item, dict)],
                capabilities=dict(meta.capabilities) if isinstance(meta.capabilities, dict) else {},
                permissions=list(meta.permissions or []),
                tags=list(meta.tags or []),
                subdir=str(source.plugin_dir.name),
            )
        )
    out.sort(key=_official_plugin_sort_key)
    return out


async def install_local_plugin(
    db: AsyncSession,
    plugin_name: str,
    *,
    default_enabled: bool = False,
) -> RemotePluginView:
    """从 ``plugins/local_imports`` 导入指定本地插件。"""
    root = _local_import_root()
    target_dir: Path | None = None
    for default_name, plugin_dir in _scan_plugins(root):
        try:
            meta = _read_plugin_metadata(plugin_dir, fallback_name=default_name)
        except InvalidPluginMetadata:
            continue
        if meta.name == plugin_name:
            target_dir = plugin_dir
            break
    if target_dir is None:
        raise PluginNotInRepo("PLUGIN_NOT_FOUND_LOCAL", f"本地目录里未找到插件: {plugin_name}")

    meta = _read_plugin_metadata(target_dir, fallback_name=plugin_name)
    _validate_runtime_plugin_shape(target_dir, meta)
    final_name = meta.name
    install_path = _plugin_dir(final_name)
    staging = install_path.parent / f"{install_path.name}.installing"

    existing = await db.get(InstalledPlugin, final_name)
    if existing is not None:
        raise DuplicatePluginName("PLUGIN_EXISTS", f"插件 {final_name!r} 已安装")
    if install_path.exists():
        raise DuplicatePluginName("DIR_EXISTS", f"目录已存在但 DB 无记录: {install_path}")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    install_path.parent.mkdir(parents=True, exist_ok=True)
    renamed = False
    try:
        shutil.copytree(
            target_dir,
            staging,
            ignore=shutil.ignore_patterns(".git", ".gitignore", "__pycache__"),
        )
        staged_meta = _read_plugin_metadata(staging, fallback_name=final_name)
        _validate_runtime_plugin_shape(staging, staged_meta)
        lint_warnings = lint_plugin_metadata_files(staging)
        staging.rename(install_path)
        renamed = True
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise PluginRepoError("COPY_FAILED", f"复制本地插件目录失败: {exc}") from exc

    try:
        final_enabled = bool(default_enabled)
        manifest_json = _with_remote_info(
            _manifest_json_from_remote_meta(meta),
            default_enabled=default_enabled,
            latest_version=meta.version,
            update_available=False,
        )

        feat = (
            await db.execute(select(Feature).where(Feature.key == final_name))
        ).scalar_one_or_none()
        if feat is None:
            db.add(
                Feature(
                    key=final_name,
                    display_name=meta.display_name or final_name,
                    is_builtin=False,
                    version=meta.version,
                    manifest=_feature_manifest_from_meta(meta),
                )
            )
        else:
            feat.display_name = meta.display_name or final_name
            feat.version = meta.version
            feat.is_builtin = False
            feat.manifest = _feature_manifest_from_meta(meta)

        await db.flush()
        installed_row = await upsert_installed_plugin(
            db,
            key=final_name,
            source=PLUGIN_SOURCE_LOCAL,
            source_url=f"local://local_imports/{final_name}",
            installed_path=str(install_path),
            version=meta.version,
            manifest_json=manifest_json,
            enabled=final_enabled,
            signature_ok=None,
            trust_tier=PLUGIN_TRUST_LOCAL,
            source_label="Local",
            last_install_error=None,
            lint_warnings=lint_warnings,
        )
        await db.flush()

        if default_enabled:
            aids = (await db.execute(select(Account.id))).scalars().all()
            for aid in aids:
                af = (
                    await db.execute(
                        select(AccountFeature).where(
                            AccountFeature.account_id == int(aid),
                            AccountFeature.feature_key == final_name,
                        )
                    )
                ).scalar_one_or_none()
                if af is None:
                    db.add(
                        AccountFeature(
                            account_id=int(aid),
                            feature_key=final_name,
                            enabled=True,
                            state=FEATURE_STATE_DISABLED,
                        )
                    )
        await db.flush()
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if renamed and install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)
        raise

    return remote_plugin_view_from_installed(installed_row)


def _manifest_json_from_manifest_object(
    manifest: object | None,
    fallback: PluginMetadata,
) -> dict[str, Any]:
    """用官方 ``manifest.py`` 补齐插件运行元数据。

    官方插件随 TelePilot 代码发布，允许安装阶段读取本地 manifest 对象；
    远程插件仍只依赖静态 ``plugin.json``，不执行 Python。
    """

    to_dict = getattr(manifest, "to_dict", None)
    if callable(to_dict):
        data = dict(to_dict())
    else:
        data = _manifest_json_from_remote_meta(fallback)
    data["name"] = str(data.get("key") or data.get("name") or fallback.name)
    data.setdefault("display_name", fallback.display_name or fallback.name)
    data.setdefault("description", fallback.description)
    data.setdefault("author", fallback.author or "TelePilot Official")
    data.setdefault("version", fallback.version)
    data.setdefault("entry", fallback.entry)
    if fallback.tags and not data.get("tags"):
        data["tags"] = list(fallback.tags)
    return data


def _feature_manifest_from_manifest_json(manifest_json: dict[str, Any]) -> dict[str, Any] | None:
    manifest: dict[str, Any] = {}
    cfg_schema = manifest_json.get("config_schema")
    if isinstance(cfg_schema, dict):
        manifest["config_schema"] = cfg_schema
    config_actions = manifest_json.get("config_actions")
    if isinstance(config_actions, list):
        manifest["config_actions"] = [item for item in config_actions if isinstance(item, dict)]
    usage = str(manifest_json.get("usage") or "").strip()
    if usage:
        manifest["usage"] = usage
    category = str(manifest_json.get("category") or "").strip()
    if category:
        manifest["category"] = category
    interaction_profile = str(manifest_json.get("interaction_profile") or "").strip()
    if interaction_profile:
        manifest["interaction_profile"] = interaction_profile
    entries = manifest_json.get("interaction_entries")
    if isinstance(entries, list):
        manifest["interaction_entries"] = [item for item in entries if isinstance(item, dict)]
    subscriptions = manifest_json.get("event_subscriptions")
    if isinstance(subscriptions, list):
        manifest["event_subscriptions"] = [item for item in subscriptions if isinstance(item, dict)]
    capabilities = manifest_json.get("capabilities")
    if isinstance(capabilities, dict):
        manifest["capabilities"] = dict(capabilities)
    if manifest_json.get("x-experimental") or manifest_json.get("experimental"):
        manifest["x-experimental"] = True
    permissions = manifest_json.get("permissions")
    if isinstance(permissions, list):
        manifest["permissions"] = list(permissions)
    for version_key in ("min_telepilot_version", "min_telebot_version"):
        version_value = str(manifest_json.get(version_key) or "").strip()
        if version_value:
            manifest[version_key] = version_value
    return manifest or None


async def install_official_plugin(
    db: AsyncSession,
    plugin_name: str,
    *,
    default_enabled: bool = False,
) -> RemotePluginView:
    """从推荐插件库导入指定插件。

    推荐入口只是插件库的快捷安装入口；安装记录仍按普通 repo 插件落库，
    不把插件标记为 TelePilot 本体内置或随包插件。
    """

    source = await _find_official_plugin_source(plugin_name)
    if source is None:
        raise PluginNotInRepo("PLUGIN_NOT_FOUND_OFFICIAL", f"推荐插件源里未找到插件: {plugin_name}")

    _validate_runtime_plugin_shape(source.plugin_dir, source.meta)
    final_name = source.meta.name
    install_path = _plugin_dir(final_name)
    staging = install_path.parent / f"{install_path.name}.installing"
    backup = install_path.parent / f"{install_path.name}.bak-official"

    existing = await db.get(InstalledPlugin, final_name)
    updating_existing = existing is not None and _version_tuple(source.meta.version) > _version_tuple(existing.version)
    if existing is not None and not updating_existing:
        raise DuplicatePluginName("PLUGIN_EXISTS", f"插件 {final_name!r} 已安装")
    if install_path.exists() and not updating_existing:
        raise DuplicatePluginName("DIR_EXISTS", f"目录已存在但 DB 无记录: {install_path}")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    install_path.parent.mkdir(parents=True, exist_ok=True)
    renamed = False
    backed_up = False
    try:
        shutil.copytree(
            source.plugin_dir,
            staging,
            ignore=shutil.ignore_patterns(".git", ".gitignore", "__pycache__"),
        )
        staged_meta = _read_plugin_metadata(staging, fallback_name=final_name)
        _validate_runtime_plugin_shape(staging, staged_meta)
        manifest_json = _manifest_json_for_official_source(
            _OfficialPluginSource(
                plugin_dir=staging,
                meta=staged_meta,
                source_url=source.source_url,
                remote=source.remote,
            )
        )
        lint_warnings = lint_plugin_metadata_files(staging)
        if updating_existing and install_path.exists():
            install_path.rename(backup)
            backed_up = True
        staging.rename(install_path)
        renamed = True
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backed_up and backup.exists() and not install_path.exists():
            backup.rename(install_path)
        raise PluginRepoError("COPY_FAILED", f"复制插件库插件目录失败: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    try:
        final_enabled = bool(existing.enabled) if existing is not None else bool(default_enabled)
        feature_manifest = _feature_manifest_from_manifest_json(manifest_json)
        feat = (
            await db.execute(select(Feature).where(Feature.key == final_name))
        ).scalar_one_or_none()
        if feat is None:
            db.add(
                Feature(
                    key=final_name,
                    display_name=str(manifest_json.get("display_name") or final_name),
                    is_builtin=False,
                    version=str(manifest_json.get("version") or staged_meta.version),
                    manifest=feature_manifest,
                )
            )
        else:
            feat.display_name = str(manifest_json.get("display_name") or final_name)
            feat.version = str(manifest_json.get("version") or staged_meta.version)
            feat.is_builtin = False
            feat.manifest = feature_manifest

        await db.flush()
        installed_row = await upsert_installed_plugin(
            db,
            key=final_name,
            source=PLUGIN_SOURCE_REPO,
            source_url=source.source_url,
            installed_path=str(install_path),
            version=str(manifest_json.get("version") or staged_meta.version),
            manifest_json=manifest_json,
            enabled=final_enabled,
            signature_ok=None,
            trust_tier=PLUGIN_TRUST_COMMUNITY,
            source_label="Plugin Repo",
            last_install_error=None,
            lint_warnings=lint_warnings,
        )
        await db.flush()

        if default_enabled:
            aids = (await db.execute(select(Account.id))).scalars().all()
            for aid in aids:
                af = (
                    await db.execute(
                        select(AccountFeature).where(
                            AccountFeature.account_id == int(aid),
                            AccountFeature.feature_key == final_name,
                        )
                    )
                ).scalar_one_or_none()
                if af is None:
                    db.add(
                        AccountFeature(
                            account_id=int(aid),
                            feature_key=final_name,
                            enabled=True,
                            state=FEATURE_STATE_DISABLED,
                        )
                    )
        await db.flush()
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if updating_existing and backed_up and backup.exists():
            if install_path.exists():
                shutil.rmtree(install_path, ignore_errors=True)
            backup.rename(install_path)
        elif renamed and install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)
        raise
    finally:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

    return remote_plugin_view_from_installed(installed_row)


__all__ = [
    "DuplicatePluginRepo",
    "PluginNotInRepo",
    "PluginRepoError",
    "PluginRepoNotFound",
    "create_repo",
    "delete_repo",
    "get_repo",
    "install_official_plugin",
    "install_plugin_from_repo",
    "install_local_plugin",
    "list_official_plugins",
    "list_plugins_in_repo",
    "list_local_import_candidates",
    "list_repos",
    "update_installed_plugins_from_repo",
]

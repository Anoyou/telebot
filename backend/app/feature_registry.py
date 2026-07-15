"""内置 feature 注册表：动态扫描 + 惰性缓存。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# builtin 插件目录：backend/app/worker/plugins/builtin/
_BUILTIN_PLUGIN_DIR: Path = Path(__file__).parent / "worker" / "plugins" / "builtin"
# 非核心可选插件不会从 builtin registry seed；即使旧镜像/增量部署残留了目录，
# 也必须继续走官方/远程 installed 插件安装链路。
_NON_CORE_BUILTIN_COMPAT_KEYS: frozenset[str] = frozenset(
    {
        "auto_reply",
        "autorepeat",
        "chatgpt_image",
        "codex_image",
        "game24",
        "math10",
    }
)


def _load_manifest_file(path: Path) -> Any | None:
    """直接加载单个 manifest.py，避免导入插件实现代码。"""
    if not path.exists():
        return None
    try:
        namespace: dict[str, Any] = {}
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        exec(code, namespace)  # noqa: S102
        return namespace.get("MANIFEST")
    except Exception:  # noqa: BLE001
        log.warning("加载 builtin manifest 失败: %s", path, exc_info=True)
        return None


def scan_builtin_manifest_objects() -> dict[str, Any]:
    """扫描 builtin 目录，返回 {plugin_key: MANIFEST}。"""
    result: dict[str, Any] = {}
    if not _BUILTIN_PLUGIN_DIR.exists():
        log.warning("builtin 插件目录不存在: %s", _BUILTIN_PLUGIN_DIR)
        return result

    for sub in sorted(_BUILTIN_PLUGIN_DIR.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        manifest_file = sub / "manifest.py"
        if not manifest_file.exists():
            continue
        if sub.name in _NON_CORE_BUILTIN_COMPAT_KEYS:
            continue
        m = _load_manifest_file(manifest_file)
        if m is None:
            log.warning("builtin 插件 %s 的 manifest.py 没有 MANIFEST 对象，跳过", sub.name)
            continue
        key: str = getattr(m, "key", sub.name)
        result[key] = m
    return result


class LazyBuiltinFeatures(dict):
    """惰性填充、可刷新的内置功能字典。"""

    _loaded: bool = False
    _manifest_cache: dict[str, Any] = {}

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.refresh()

    def refresh(self) -> None:
        manifests = scan_builtin_manifest_objects()
        self.clear()
        self.update(
            {
                key: str(getattr(manifest, "display_name", key))
                for key, manifest in manifests.items()
            }
        )
        self._manifest_cache = manifests
        self._loaded = True
        log.debug("BUILTIN_FEATURES 已刷新: %s", list(self.keys()))

    def manifest_for(self, key: str) -> Any | None:
        self._ensure_loaded()
        return self._manifest_cache.get(key)

    def __contains__(self, item: object) -> bool:
        self._ensure_loaded()
        return super().__contains__(item)

    def __iter__(self):
        self._ensure_loaded()
        return super().__iter__()

    def __len__(self) -> int:
        self._ensure_loaded()
        return super().__len__()

    def keys(self):
        self._ensure_loaded()
        return super().keys()

    def values(self):
        self._ensure_loaded()
        return super().values()

    def items(self):
        self._ensure_loaded()
        return super().items()

    def get(self, key, default=None):
        self._ensure_loaded()
        return super().get(key, default)

    def __getitem__(self, key):
        self._ensure_loaded()
        return super().__getitem__(key)


BUILTIN_FEATURES: LazyBuiltinFeatures = LazyBuiltinFeatures()

#!/usr/bin/env bash
# 将公开文档同步到稳定的运行时目录。该目录按文件更新，不替换目录 inode，
# 因此已运行容器的只读 bind mount 能立即看到 Git fast-forward 后的新内容。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_lib.sh"

CONTENT_DIR_RAW="${TELEPILOT_RUNTIME_CONTENT_DIR:-$ROOT_DIR/.run/runtime-content}"
CONTENT_DIR="$(python3 - "$ROOT_DIR" "$CONTENT_DIR_RAW" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
raw = Path(sys.argv[2]).expanduser()
unresolved = raw if raw.is_absolute() else root / raw
cursor = unresolved
while True:
    if cursor.is_symlink():
        raise SystemExit("运行时内容目录路径不得包含符号链接")
    if cursor == root or cursor == cursor.parent:
        break
    cursor = cursor.parent
candidate = unresolved.resolve(strict=False)
safe_root = (root / ".run").resolve(strict=False)
if candidate == safe_root or safe_root not in candidate.parents:
    raise SystemExit("运行时内容目录必须位于仓库 .run/ 下")
if candidate in {root, root / "docs"}:
    raise SystemExit("运行时内容目录与仓库源码目录冲突")
print(candidate)
PY
)" || die "TELEPILOT_RUNTIME_CONTENT_DIR 不在安全边界内"
DOCS_TARGET="$CONTENT_DIR/docs"
DOC_FILES=(
  PLUGIN-AI.md
  PLUGIN-API-REFERENCE.md
  PLUGIN-CHEATSHEET.md
  PLUGIN-DEV-GUIDE.md
  PLUGIN-DEVTOOLS.md
  PLUGIN-HTTP.md
  PLUGIN-OVERVIEW.md
  PLUGIN-QUICKSTART.md
  PLUGIN-REMOTE.md
  PLUGIN-RULES.md
  PLUGIN-SAFETY.md
  PLUGIN-WEBHOOK-QUICKSTART.md
  PLATFORM-CAPABILITIES.md
  SECURITY-OPS.md
)

# updater 可能以 root 身份运行；拒绝已有 symlink，避免把清理/覆盖跟随到
# .run 之外。源文件也必须在开始修改目标目录前全部验证完成。
[[ ! -L "$CONTENT_DIR" && ! -L "$DOCS_TARGET" ]] \
  || die "运行时内容目录不得是符号链接"
for filename in "${DOC_FILES[@]}"; do
  [[ -f "$ROOT_DIR/docs/$filename" && ! -L "$ROOT_DIR/docs/$filename" ]] \
    || die "运行时文档缺失或不是普通文件：docs/$filename"
done
[[ -f "$ROOT_DIR/CHANGELOG.md" && ! -L "$ROOT_DIR/CHANGELOG.md" ]] \
  || die "CHANGELOG.md 缺失或不是普通文件"

mkdir -p "$DOCS_TARGET"
for target in "$CONTENT_DIR/CHANGELOG.md" "$CONTENT_DIR/REVISION"; do
  [[ ! -L "$target" ]] || die "运行时目标不得是符号链接：$target"
done

# 运行时目录只公开 Markdown；先清理已从仓库删除的旧文档，再复制当前版本。
find "$DOCS_TARGET" -mindepth 1 -maxdepth 1 -type f -name '*.md' -delete
for filename in "${DOC_FILES[@]}"; do
  source="$ROOT_DIR/docs/$filename"
  install -m 0644 "$source" "$DOCS_TARGET/$filename"
done
install -m 0644 "$ROOT_DIR/CHANGELOG.md" "$CONTENT_DIR/CHANGELOG.md"
git -C "$ROOT_DIR" rev-parse HEAD > "$CONTENT_DIR/REVISION"
chmod 0644 "$CONTENT_DIR/REVISION"

ok "运行时文档已同步到 $CONTENT_DIR"

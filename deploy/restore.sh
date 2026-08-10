#!/usr/bin/env bash
# 恢复 PostgreSQL 与持久化业务卷。
# 用法：deploy/restore.sh db.sql [sessions.tgz|-] [plugins-installed.tgz] [plugin-repos.tgz]
set -euo pipefail

if [[ $# -lt 1 || $# -gt 4 ]]; then
  echo "用法：$0 <db-dump.sql> [sessions.tgz|-] [plugins-installed.tgz] [plugin-repos.tgz]" >&2
  exit 2
fi

DB_DUMP="$1"
SESSIONS_ARCHIVE="${2:-}"
PLUGINS_ARCHIVE="${3:-}"
REPOS_ARCHIVE="${4:-}"
[[ "$SESSIONS_ARCHIVE" == "-" ]] && SESSIONS_ARCHIVE=""
[[ "$PLUGINS_ARCHIVE" == "-" ]] && PLUGINS_ARCHIVE=""
[[ "$REPOS_ARCHIVE" == "-" ]] && REPOS_ARCHIVE=""
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
# 恢复旧部署备份时允许 Compose 解析；本脚本不会启动 updater。
export UPDATER_TOKEN="${UPDATER_TOKEN:-restore-only-compose-parse-token-000000000000000}"
PG_USER="${POSTGRES_USER:-telebot}"
PG_DB="${POSTGRES_DB:-telebot}"
[[ "$PG_DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "POSTGRES_DB 含非法字符" >&2; exit 1; }

[[ -s "$DB_DUMP" ]] || { echo "找不到或文件为空：$DB_DUMP" >&2; exit 1; }
for file in "$SESSIONS_ARCHIVE" "$PLUGINS_ARCHIVE" "$REPOS_ARCHIVE"; do
  [[ -z "$file" || -s "$file" ]] || { echo "找不到或文件为空：$file" >&2; exit 1; }
done
command -v python3 >/dev/null 2>&1 || {
  echo "恢复需要 python3（官方安装脚本已包含），当前环境未找到" >&2
  exit 1
}

DB_DIR="$(cd "$(dirname "$DB_DUMP")" && pwd)"
DB_BASE="$(basename "$DB_DUMP")"
STAMP="${DB_BASE#db-}"
STAMP="${STAMP%.sql}"
CHECKSUM_FILE="$DB_DIR/checksums-$STAMP.sha256"
if [[ -f "$CHECKSUM_FILE" ]]; then
  echo "校验备份文件 SHA-256..."
  verify_checksum() {
    local file="$1"
    [[ -n "$file" ]] || return 0
    python3 - "$CHECKSUM_FILE" "$file" <<'PY'
from __future__ import annotations

import hashlib
import os
import re
import sys

checksum_path, target_path = sys.argv[1:3]
target_name = os.path.basename(target_path)
if any(ord(char) < 32 or ord(char) == 127 for char in target_name):
    print(f"备份文件名包含控制字符，拒绝恢复：{target_name!r}", file=sys.stderr)
    raise SystemExit(1)

matches: list[str] = []
with open(checksum_path, encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.rstrip("\r\n")
        match = re.fullmatch(r"([0-9A-Fa-f]{64})[ \t]+[*]?(.+)", line)
        if match and match.group(2) == target_name:
            matches.append(match.group(1).lower())

if not matches:
    print(f"校验文件缺少条目：{target_name}", file=sys.stderr)
    raise SystemExit(1)
if len(matches) != 1:
    print(f"校验文件包含重复条目：{target_name}", file=sys.stderr)
    raise SystemExit(1)

digest = hashlib.sha256()
with open(target_path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != matches[0]:
    print(f"SHA-256 校验失败：{target_name}", file=sys.stderr)
    raise SystemExit(1)
PY
  }
  verify_checksum "$DB_DUMP"
  verify_checksum "$SESSIONS_ARCHIVE"
  verify_checksum "$PLUGINS_ARCHIVE"
  verify_checksum "$REPOS_ARCHIVE"
fi

validate_archive() {
  local archive="$1"
  [[ -n "$archive" ]] || return 0
  python3 - "$archive" <<'PY'
from __future__ import annotations

import sys
import tarfile
from pathlib import PurePosixPath

archive = sys.argv[1]
seen: set[str] = set()
has_member = False
try:
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            has_member = True
            name = member.name
            if any(ord(char) < 32 or ord(char) == 127 for char in name):
                print(f"归档路径包含控制字符，拒绝恢复：{name!r}", file=sys.stderr)
                raise SystemExit(1)
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                print(f"归档包含越界路径，拒绝恢复：{name}", file=sys.stderr)
                raise SystemExit(1)
            normalized = str(path)
            if normalized not in {"", "."}:
                if normalized in seen:
                    print(f"归档包含重复路径，拒绝恢复：{name}", file=sys.stderr)
                    raise SystemExit(1)
                seen.add(normalized)
            if not (member.isfile() or member.isdir()):
                print(f"归档包含链接或特殊文件，拒绝恢复：{name}", file=sys.stderr)
                raise SystemExit(1)
    if not has_member:
        print(f"归档不包含任何成员，拒绝恢复：{archive}", file=sys.stderr)
        raise SystemExit(1)
except (tarfile.TarError, OSError, EOFError):
    print(f"归档无法读取或已损坏：{archive}", file=sys.stderr)
    raise SystemExit(1)
PY
}

validate_archive "$SESSIONS_ARCHIVE"
validate_archive "$PLUGINS_ARCHIVE"
validate_archive "$REPOS_ARCHIVE"

WEB_CONTAINER="$(docker compose ps -q web)"
[[ -n "$WEB_CONTAINER" ]] || { echo "找不到 web 容器，请先启动生产栈" >&2; exit 1; }

volume_source() {
  local destination="$1"
  docker inspect "$WEB_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "'"$destination"'"}}{{.Source}}{{end}}{{end}}'
}

restore_volume() {
  local archive="$1" destination="$2" source archive_dir archive_name strip_components
  [[ -n "$archive" ]] || return 0
  source="$(volume_source "$destination")"
  [[ -n "$source" ]] || { echo "web 容器缺少挂载：$destination" >&2; exit 1; }
  archive_dir="$(cd "$(dirname "$archive")" && pwd)"
  archive_name="$(basename "$archive")"
  strip_components=0
  # 旧版 sessions 归档包含顶层 sessions/，新版归档直接保存卷内容。
  if [[ "$destination" == "/app/sessions" ]]; then
    strip_components="$(python3 - "$archive" <<'PY'
from __future__ import annotations

import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1], "r:gz") as bundle:
    paths = [
        PurePosixPath(member.name)
        for member in bundle
        if str(PurePosixPath(member.name)) not in {"", "."}
    ]
print(1 if paths and all(path.parts and path.parts[0] == "sessions" for path in paths) else 0)
PY
)"
  fi
  docker run --rm -v "$source":/data -v "$archive_dir":/backup:ro alpine \
    sh -eu -c '
      archive=$1
      strip_components=$2
      stage=$(mktemp -d /data/.telepilot-restore-stage.XXXXXX)
      previous=$(mktemp -d /data/.telepilot-restore-previous.XXXXXX)
      phase=staged
      move_children() {
        source_dir=$1
        target_dir=$2
        skip_one=${3:-}
        skip_two=${4:-}
        failed=0
        for path in "$source_dir"/* "$source_dir"/.[!.]* "$source_dir"/..?*; do
          [ -e "$path" ] || [ -L "$path" ] || continue
          [ "$path" = "$skip_one" ] && continue
          [ "$path" = "$skip_two" ] && continue
          mv "$path" "$target_dir/" || failed=1
        done
        return "$failed"
      }
      remove_children() {
        source_dir=$1
        skip_one=${2:-}
        skip_two=${3:-}
        failed=0
        for path in "$source_dir"/* "$source_dir"/.[!.]* "$source_dir"/..?*; do
          [ -e "$path" ] || [ -L "$path" ] || continue
          [ "$path" = "$skip_one" ] && continue
          [ "$path" = "$skip_two" ] && continue
          rm -rf "$path" || failed=1
        done
        return "$failed"
      }
      rollback() {
        status=$1
        trap - EXIT INT TERM
        rollback_failed=0
        if [ "$phase" = move_old ]; then
          # 旧数据只移动了一部分：保留尚未移动的条目，仅把 previous 中的条目放回。
          move_children "$previous" /data || rollback_failed=1
        elif [ "$phase" = move_new ]; then
          # 旧数据已完整保存：先移除部分新数据，再恢复全部旧数据。
          remove_children /data "$stage" "$previous" || rollback_failed=1
          move_children "$previous" /data || rollback_failed=1
        fi
        if [ "$rollback_failed" -eq 0 ]; then
          rm -rf "$stage" "$previous"
        else
          echo "卷回滚未完整完成；已保留 $stage 与 $previous 供人工恢复" >&2
        fi
        exit "$status"
      }
      on_exit() {
        rollback "$?"
      }
      on_int() {
        rollback 130
      }
      on_term() {
        rollback 143
      }
      trap on_exit EXIT
      trap on_int INT
      trap on_term TERM
      if [ "$strip_components" -eq 1 ]; then
        tar xzf "/backup/$archive" -C "$stage" --strip-components=1
      else
        tar xzf "/backup/$archive" -C "$stage"
      fi
      phase=move_old
      move_children /data "$previous" "$stage" "$previous"
      phase=move_new
      move_children "$stage" /data
      phase=done
      rm -rf "$stage" "$previous"
      trap - EXIT INT TERM
    ' sh "$archive_name" "$strip_components"
}

targets="数据库"
[[ -z "$SESSIONS_ARCHIVE" ]] || targets="${targets}、历史 sessions 卷"
[[ -z "$PLUGINS_ARCHIVE" ]] || targets="${targets}、已安装插件"
[[ -z "$REPOS_ARCHIVE" ]] || targets="${targets}、插件仓库缓存"
read -r -p "确认覆盖恢复${targets}？此操作不可撤销。(yes/N) " ans
[[ "$ans" == "yes" ]] || { echo "已取消"; exit 0; }

WEB_STOPPED=0
RESTORE_STARTED=0
DATA_RESTORE_DONE=0
on_exit() {
  local status=$?
  local restart_ok=1
  if [[ "$WEB_STOPPED" -eq 1 ]]; then
    docker compose start web >/dev/null 2>&1 || restart_ok=0
  fi
  if [[ "$status" -ne 0 && "$RESTORE_STARTED" -eq 1 ]]; then
    if [[ "$DATA_RESTORE_DONE" -eq 1 ]]; then
      if [[ "$restart_ok" -eq 1 ]]; then
        echo "数据恢复已完成，但首次启动 Web 失败；已重试启动成功，请检查健康状态。" >&2
      else
        echo "数据恢复已完成，但 Web 启动失败；请手动启动并检查健康状态。" >&2
      fi
    else
      echo "恢复未完成，数据库或部分卷可能已变更；已尝试重启 Web，请检查状态并从原备份重试。" >&2
    fi
  fi
  exit "$status"
}
trap on_exit EXIT
docker compose stop web
WEB_STOPPED=1
RESTORE_STARTED=1

docker compose exec -T postgres psql -U "$PG_USER" -d postgres \
  -c "DROP DATABASE IF EXISTS \"$PG_DB\";" -c "CREATE DATABASE \"$PG_DB\";"
docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" < "$DB_DUMP"

restore_volume "$SESSIONS_ARCHIVE" /app/sessions
restore_volume "$PLUGINS_ARCHIVE" /app/plugins/installed
restore_volume "$REPOS_ARCHIVE" /app/data/plugin_repos

DATA_RESTORE_DONE=1
docker compose start web >/dev/null
WEB_STOPPED=0
trap - EXIT
echo "恢复完成。请核对 web 健康状态、账号登录状态与插件列表。"

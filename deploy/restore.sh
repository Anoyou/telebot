#!/usr/bin/env bash
# 恢复 PostgreSQL 与持久化业务卷。
# 用法：deploy/restore.sh db.sql sessions.tgz [plugins-installed.tgz] [plugin-repos.tgz]
set -euo pipefail

if [[ $# -lt 2 || $# -gt 4 ]]; then
  echo "用法：$0 <db-dump.sql> <sessions.tgz> [plugins-installed.tgz] [plugin-repos.tgz]" >&2
  exit 2
fi

DB_DUMP="$1"
SESSIONS_ARCHIVE="$2"
PLUGINS_ARCHIVE="${3:-}"
REPOS_ARCHIVE="${4:-}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
# 恢复旧部署备份时允许 Compose 解析；本脚本不会启动 updater。
export UPDATER_TOKEN="${UPDATER_TOKEN:-restore-only-compose-parse-token-000000000000000}"
PG_USER="${POSTGRES_USER:-telebot}"
PG_DB="${POSTGRES_DB:-telebot}"
[[ "$PG_DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "POSTGRES_DB 含非法字符" >&2; exit 1; }

for file in "$DB_DUMP" "$SESSIONS_ARCHIVE"; do
  [[ -s "$file" ]] || { echo "找不到或文件为空：$file" >&2; exit 1; }
done
for file in "$PLUGINS_ARCHIVE" "$REPOS_ARCHIVE"; do
  [[ -z "$file" || -s "$file" ]] || { echo "找不到或文件为空：$file" >&2; exit 1; }
done
tar tzf "$SESSIONS_ARCHIVE" >/dev/null
[[ -z "$PLUGINS_ARCHIVE" ]] || tar tzf "$PLUGINS_ARCHIVE" >/dev/null
[[ -z "$REPOS_ARCHIVE" ]] || tar tzf "$REPOS_ARCHIVE" >/dev/null

DB_DIR="$(cd "$(dirname "$DB_DUMP")" && pwd)"
DB_BASE="$(basename "$DB_DUMP")"
STAMP="${DB_BASE#db-}"
STAMP="${STAMP%.sql}"
CHECKSUM_FILE="$DB_DIR/checksums-$STAMP.sha256"
if [[ -f "$CHECKSUM_FILE" ]]; then
  echo "校验备份文件 SHA-256..."
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$DB_DIR" && sha256sum -c "$(basename "$CHECKSUM_FILE")")
  else
    (cd "$DB_DIR" && shasum -a 256 -c "$(basename "$CHECKSUM_FILE")")
  fi
fi

WEB_CONTAINER="$(docker compose ps -q web)"
[[ -n "$WEB_CONTAINER" ]] || { echo "找不到 web 容器，请先启动生产栈" >&2; exit 1; }

volume_source() {
  local destination="$1"
  docker inspect "$WEB_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "'"$destination"'"}}{{.Source}}{{end}}{{end}}'
}

restore_volume() {
  local archive="$1" destination="$2" source archive_dir archive_name first_entry strip_args=()
  [[ -n "$archive" ]] || return 0
  source="$(volume_source "$destination")"
  [[ -n "$source" ]] || { echo "web 容器缺少挂载：$destination" >&2; exit 1; }
  archive_dir="$(cd "$(dirname "$archive")" && pwd)"
  archive_name="$(basename "$archive")"
  first_entry="$(tar tzf "$archive" | sed -n '1p')"
  # 旧版 sessions 归档包含顶层 sessions/，新版归档直接保存卷内容。
  if [[ "$destination" == "/app/sessions" && "$first_entry" == sessions/* ]]; then
    strip_args=(--strip-components=1)
  fi
  docker run --rm -v "$source":/data -v "$archive_dir":/backup:ro alpine \
    sh -eu -c 'archive=$1; shift; find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +; tar xzf "/backup/$archive" -C /data "$@"' \
    sh "$archive_name" "${strip_args[@]}"
}

targets="数据库与 sessions"
[[ -z "$PLUGINS_ARCHIVE" ]] || targets="$targets、已安装插件"
[[ -z "$REPOS_ARCHIVE" ]] || targets="$targets、插件仓库缓存"
read -r -p "确认覆盖恢复${targets}？此操作不可撤销。(yes/N) " ans
[[ "$ans" == "yes" ]] || { echo "已取消"; exit 0; }

restart_web() { docker compose start web >/dev/null 2>&1 || true; }
trap restart_web EXIT
docker compose stop web

docker compose exec -T postgres psql -U "$PG_USER" -d postgres \
  -c "DROP DATABASE IF EXISTS \"$PG_DB\";" -c "CREATE DATABASE \"$PG_DB\";"
docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" < "$DB_DUMP"

restore_volume "$SESSIONS_ARCHIVE" /app/sessions
restore_volume "$PLUGINS_ARCHIVE" /app/plugins/installed
restore_volume "$REPOS_ARCHIVE" /app/data/plugin_repos

restart_web
trap - EXIT
echo "恢复完成。请核对 web 健康状态、账号登录状态与插件列表。"

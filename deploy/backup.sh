#!/usr/bin/env bash
# 生产数据备份：PostgreSQL + sessions + 已安装插件 + 插件仓库缓存。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
# 兼容从旧版升级：新版 Compose 强制要求 UPDATER_TOKEN，但备份只会查询/执行
# 已有 postgres、web 容器，不会启动 updater。真正 token 仍由 prod-up 写回 .env。
export UPDATER_TOKEN="${UPDATER_TOKEN:-backup-only-compose-parse-token-0000000000000000}"

TS="$(date +%Y%m%d-%H%M%S)"
DIR="${BACKUP_DIR:-/var/backups/telebot}"
PG_USER="${POSTGRES_USER:-telebot}"
PG_DB="${POSTGRES_DB:-telebot}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

command -v docker >/dev/null 2>&1 || { echo "缺少 docker" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "缺少 Docker Compose v2" >&2; exit 1; }
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"

WEB_CONTAINER="$(docker compose ps -q web)"
[[ -n "$WEB_CONTAINER" ]] || { echo "找不到 web 容器，请先启动生产栈" >&2; exit 1; }
WEB_WAS_RUNNING="$(docker inspect "$WEB_CONTAINER" --format '{{.State.Running}}' 2>/dev/null || echo false)"

resume_web() {
  if [[ "${TELEPILOT_BACKUP_QUIESCE:-0}" == "1" && "$WEB_WAS_RUNNING" == "true" ]]; then
    docker compose start web >/dev/null || true
  fi
}
trap resume_web EXIT

volume_source() {
  local destination="$1"
  docker inspect "$WEB_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "'"$destination"'"}}{{.Source}}{{end}}{{end}}'
}

archive_volume() {
  local label="$1" destination="$2" source archive
  source="$(volume_source "$destination")"
  [[ -n "$source" ]] || { echo "web 容器缺少挂载：$destination" >&2; exit 1; }
  archive="$DIR/$label-$TS.tgz"
  docker run --rm -v "$source":/data:ro -v "$DIR":/backup alpine \
    tar czf "/backup/$(basename "$archive")" -C /data .
  [[ -s "$archive" ]] || { echo "备份归档为空：$archive" >&2; exit 1; }
  tar tzf "$archive" >/dev/null
  printf '%s\n' "$archive"
}

echo "[$(date)] 开始备份到 $DIR"
if [[ "${TELEPILOT_BACKUP_QUIESCE:-0}" == "1" && "$WEB_WAS_RUNNING" == "true" ]]; then
  echo "[$(date)] 暂停 web 写入，创建数据库与插件文件一致性备份"
  docker compose stop -t 30 web >/dev/null
fi
DB_DUMP="$DIR/db-$TS.sql"
docker compose exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" --no-owner > "$DB_DUMP"
[[ -s "$DB_DUMP" ]] || { echo "数据库备份为空：$DB_DUMP" >&2; exit 1; }

SESSIONS_ARCHIVE="$(archive_volume sessions /app/sessions)"
PLUGINS_ARCHIVE="$(archive_volume plugins-installed /app/plugins/installed)"
REPOS_ARCHIVE="$(archive_volume plugin-repos /app/data/plugin_repos)"

CHECKSUM_FILE="$DIR/checksums-$TS.sha256"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DIR" && sha256sum "$(basename "$DB_DUMP")" "$(basename "$SESSIONS_ARCHIVE")" \
    "$(basename "$PLUGINS_ARCHIVE")" "$(basename "$REPOS_ARCHIVE")") > "$CHECKSUM_FILE"
else
  (cd "$DIR" && shasum -a 256 "$(basename "$DB_DUMP")" "$(basename "$SESSIONS_ARCHIVE")" \
    "$(basename "$PLUGINS_ARCHIVE")" "$(basename "$REPOS_ARCHIVE")") > "$CHECKSUM_FILE"
fi

find "$DIR" -type f \( -name 'db-*.sql' -o -name 'sessions-*.tgz' \
  -o -name 'plugins-installed-*.tgz' -o -name 'plugin-repos-*.tgz' \
  -o -name 'checksums-*.sha256' \) -mtime "+$RETENTION_DAYS" -delete || true

echo "[$(date)] 备份完成："
printf '  - %s\n' "$DB_DUMP" "$SESSIONS_ARCHIVE" "$PLUGINS_ARCHIVE" "$REPOS_ARCHIVE" "$CHECKSUM_FILE"
echo "注意：MASTER_KEY 与 UPDATER_TOKEN 请通过 deploy/backup-keys.sh 分开加密备份。"

#!/usr/bin/env bash
# 生产数据备份：PostgreSQL + sessions + 已安装插件 + 插件仓库缓存。
set -euo pipefail
# 数据库、Telegram session 与插件配置都可能包含密钥；禁止继承宽松 umask。
umask 077

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
chmod 700 "$DIR"
DIR="$(cd "$DIR" && pwd)"
# 自动收紧旧版本留下的宽权限备份；等价的一次性修复无需另跑命令。
find "$DIR" -maxdepth 1 -type f \( -name 'db-*.sql' -o -name 'sessions-*.tgz' \
  -o -name 'plugins-installed-*.tgz' -o -name 'plugin-repos-*.tgz' \
  -o -name 'checksums-*.sha256' \) -exec chmod 600 {} + 2>/dev/null || true

WEB_CONTAINER="$(docker compose ps -q web)"
if [[ -z "$WEB_CONTAINER" ]]; then
  # updater 在 /workspace 内运行时，Compose 会误推断项目名为 workspace。
  # 从现有 web 容器标签恢复宿主项目名，并写回 .env 供本次更新后续命令复用。
  HOST_PROJECT_DIR="${TELEPILOT_HOST_PROJECT_DIR:-$ROOT_DIR}"
  CANDIDATES="$(docker ps -a \
    --filter label=com.docker.compose.service=web \
    --format '{{.ID}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.project"}}')"
  SELECTED="$(printf '%s\n' "$CANDIDATES" | awk -F'|' -v host="$HOST_PROJECT_DIR" '$2 == host { print; exit }')"
  if [[ -z "$SELECTED" && -n "$CANDIDATES" && "$(printf '%s\n' "$CANDIDATES" | sed '/^$/d' | wc -l | tr -d ' ')" == "1" ]]; then
    SELECTED="$CANDIDATES"
  fi
  if [[ -n "$SELECTED" ]]; then
    WEB_CONTAINER="${SELECTED%%|*}"
    COMPOSE_PROJECT_NAME="${SELECTED##*|}"
    [[ "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
      echo "识别到无效的 Compose 项目名：$COMPOSE_PROJECT_NAME" >&2
      exit 1
    }
    export COMPOSE_PROJECT_NAME
    if [[ -n "$COMPOSE_PROJECT_NAME" ]] && ! grep -qE '^COMPOSE_PROJECT_NAME=' .env 2>/dev/null; then
      printf '\nCOMPOSE_PROJECT_NAME=%s\n' "$COMPOSE_PROJECT_NAME" >> .env
      chmod 600 .env 2>/dev/null || true
      echo "已从运行中容器识别 Compose 项目：$COMPOSE_PROJECT_NAME"
    fi
  fi
fi
[[ -n "$WEB_CONTAINER" ]] || { echo "找不到 web 容器，请确认生产栈已启动且 TELEPILOT_HOST_PROJECT_DIR 指向当前项目" >&2; exit 1; }
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
  chmod 600 "$archive"
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
chmod 600 "$DB_DUMP"

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
chmod 600 "$CHECKSUM_FILE"

find "$DIR" -type f \( -name 'db-*.sql' -o -name 'sessions-*.tgz' \
  -o -name 'plugins-installed-*.tgz' -o -name 'plugin-repos-*.tgz' \
  -o -name 'checksums-*.sha256' \) -mtime "+$RETENTION_DAYS" -delete || true

echo "[$(date)] 备份完成："
printf '  - %s\n' "$DB_DUMP" "$SESSIONS_ARCHIVE" "$PLUGINS_ARCHIVE" "$REPOS_ARCHIVE" "$CHECKSUM_FILE"
echo "注意：MASTER_KEY 与 UPDATER_TOKEN 请通过 deploy/backup-keys.sh 分开加密备份。"

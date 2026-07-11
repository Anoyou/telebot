#!/usr/bin/env bash
# Production incremental updater.
#
# Goal:
#   - Pull only fast-forward updates.
#   - Classify changed files before applying.
#   - Rebuild only the Docker Compose services that need new code.
#   - Fall back to full prod-up whenever the change is risky or ambiguous.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_lib.sh"
cd "$ROOT_DIR"
export TELEPILOT_HOST_PROJECT_DIR="${TELEPILOT_HOST_PROJECT_DIR:-$ROOT_DIR}"

REMOTE="${TELEPILOT_UPDATE_REMOTE:-origin}"
BRANCH="${TELEPILOT_UPDATE_BRANCH:-main}"
DRY_RUN=0
FORCE_FULL=0
OLD_COMMIT=""
HEAD_ALREADY_UPDATED=0
HANDOFF_SCHEDULED=0
RUNNING_UPDATER_TOKEN="${UPDATER_TOKEN:-}"

usage() {
  cat <<EOF
用法：scripts/prod-update.sh [--dry-run] [--full]

  --dry-run   只检查远程更新和分类，不拉取、不重建
  --full      强制走完整 make prod-up 路径

可通过环境变量覆盖远程分支：
  TELEPILOT_UPDATE_REMOTE=origin
  TELEPILOT_UPDATE_BRANCH=main
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --full)
      FORCE_FULL=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
  shift
done

on_error() {
  err "增量更新失败"
  if [[ -n "$OLD_COMMIT" ]]; then
    warn "当前更新前 commit：$OLD_COMMIT"
    warn "如需回滚代码，请人工确认后执行：git checkout $OLD_COMMIT && make prod-up"
  fi
}
trap on_error ERR

need_cmd git "Git 仓库更新"
need_cmd docker "Docker Compose 生产部署"
docker info >/dev/null 2>&1 || die "docker 守护进程未启动"
docker compose version >/dev/null 2>&1 || die "缺 docker compose v2 插件"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "当前目录不是 Git 工作树"
PENDING_FILE="$(git rev-parse --git-path telepilot-deploy-pending)"

REMOTE_REF="refs/remotes/${REMOTE}/${BRANCH}"

log "拉取远程索引 ${REMOTE}/${BRANCH}"
git fetch "$REMOTE" "${BRANCH}:${REMOTE_REF}" >/dev/null

CURRENT_COMMIT="$(git rev-parse HEAD)"
TARGET_COMMIT="$(git rev-parse "$REMOTE_REF")"
OLD_COMMIT="$CURRENT_COMMIT"

if [[ "$CURRENT_COMMIT" == "$TARGET_COMMIT" ]]; then
  if [[ -f "$PENDING_FILE" ]]; then
    read -r pending_old pending_target < "$PENDING_FILE" || true
    if [[ -n "${pending_old:-}" && "${pending_target:-}" == "$TARGET_COMMIT" ]] \
      && git cat-file -e "${pending_old}^{commit}" 2>/dev/null; then
      warn "检测到 commit 已拉取但部署未完成，继续重试 ${pending_old:0:12}..${TARGET_COMMIT:0:12}"
      CURRENT_COMMIT="$pending_old"
      OLD_COMMIT="$pending_old"
      HEAD_ALREADY_UPDATED=1
    else
      warn "忽略与当前 HEAD 不匹配的旧部署 pending 标记"
      rm -f "$PENDING_FILE"
    fi
  fi
  if (( HEAD_ALREADY_UPDATED == 0 )); then
    ok "当前已是最新 commit：${TARGET_COMMIT:0:12}"
    exit 0
  fi
fi

mapfile -t CHANGED_FILES < <(git diff --name-only "$CURRENT_COMMIT..$TARGET_COMMIT")

NEEDS_BACKEND=0
NEEDS_FRONTEND=0
NEEDS_FULL=0
REQUIRES_BACKUP=0
DOCS_ONLY=1

mark_backend() {
  NEEDS_BACKEND=1
  DOCS_ONLY=0
}

mark_frontend() {
  NEEDS_FRONTEND=1
  DOCS_ONLY=0
}

mark_full() {
  NEEDS_FULL=1
  DOCS_ONLY=0
}

classify_file() {
  local file="$1"
  case "$file" in
    docker-compose.yml|docker-compose.dev.yml|Makefile)
      mark_full
      ;;
    .dockerignore|backend/.dockerignore|frontend/.dockerignore)
      mark_full
      ;;
    backend/Dockerfile|frontend/Dockerfile)
      mark_full
      ;;
    backend/pyproject.toml|frontend/package.json|frontend/pnpm-lock.yaml|frontend/.npmrc)
      mark_full
      ;;
    scripts/_lib.sh|scripts/prod-up.sh|scripts/install-server.sh|scripts/prod-update.sh|scripts/bootstrap.sh)
      mark_full
      ;;
    deploy/*|.github/*)
      mark_full
      ;;
    backend/alembic/versions/*)
      mark_backend
      REQUIRES_BACKUP=1
      ;;
    backend/*|plugins/*)
      mark_backend
      ;;
    frontend/*|CHANGELOG.md|docs/PLUGIN-DEV-GUIDE.md)
      mark_frontend
      ;;
    README.md|CONTRIBUTING.md|LICENSE|AGENTS.md|docs/*|examples/*)
      ;;
    *)
      mark_full
      ;;
  esac
}

for file in "${CHANGED_FILES[@]}"; do
  classify_file "$file"
done

if (( FORCE_FULL == 1 )); then
  NEEDS_FULL=1
  DOCS_ONLY=0
fi

log "更新范围预览"
printf '  当前：%s\n' "${CURRENT_COMMIT:0:12}"
printf '  目标：%s\n' "${TARGET_COMMIT:0:12}"
printf '  文件：%d 个\n' "${#CHANGED_FILES[@]}"
for file in "${CHANGED_FILES[@]:0:30}"; do
  printf '    - %s\n' "$file"
done
if (( ${#CHANGED_FILES[@]} > 30 )); then
  printf '    ... 还有 %d 个文件\n' "$(( ${#CHANGED_FILES[@]} - 30 ))"
fi

if (( NEEDS_FULL == 1 )); then
  warn "分类结果：完整生产更新"
elif (( DOCS_ONLY == 1 )); then
  ok "分类结果：仅文档/说明变更，无需重建服务"
else
  components=()
  (( NEEDS_BACKEND == 1 )) && components+=("web")
  (( NEEDS_FRONTEND == 1 )) && components+=("frontend")
  ok "分类结果：增量更新 ${components[*]}"
fi

if (( REQUIRES_BACKUP == 1 )); then
  warn "本次包含数据库迁移；应用代码回滚不能撤销已执行的数据库变更。"
fi

if (( DRY_RUN == 1 )); then
  ok "dry-run 完成，未拉取代码、未重建服务"
  exit 0
fi

if [[ -n "$(git status --porcelain)" ]]; then
  die "工作区存在未提交改动，拒绝自动更新。请先提交、stash 或清理后重试。"
fi

if (( HEAD_ALREADY_UPDATED == 0 )); then
  log "执行 fast-forward 更新"
  git pull --ff-only "$REMOTE" "$BRANCH"
  NEW_COMMIT="$(git rev-parse HEAD)"
  printf '%s %s\n' "$OLD_COMMIT" "$NEW_COMMIT" > "$PENDING_FILE"
  ok "代码已更新到 ${NEW_COMMIT:0:12}"
else
  NEW_COMMIT="$(git rev-parse HEAD)"
fi

# 新版 compose 在解析任何服务前都要求 UPDATER_TOKEN。先补齐并与 JWT
# 解耦，保证从旧部署升级时备份和后续 compose 命令都能执行。
ensure_updater_token_env .env
PERSISTED_UPDATER_TOKEN="$(grep -E '^UPDATER_TOKEN=' .env | head -n1 | cut -d= -f2- | tr -d ' "')"

if (( REQUIRES_BACKUP == 1 )); then
  if [[ "${TELEPILOT_MIGRATION_BACKUP_CONFIRMED:-0}" == "1" ]]; then
    warn "已由操作者确认存在可恢复备份，跳过自动备份。"
  else
    log "迁移前创建数据库与持久化卷备份"
    TELEPILOT_BACKUP_QUIESCE=1 "$ROOT_DIR/deploy/backup.sh"
    ok "迁移前备份已完成；尚未启动新版容器或执行迁移。"
  fi
fi

frontend_url() {
  local raw
  raw="$(grep -E '^WEB_PORT_PUBLISH=' .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d ' "' || true)"
  raw="${raw:-80}"
  if [[ "$raw" == *:* ]]; then
    local host="${raw%:*}"
    local port="${raw##*:}"
    [[ "$host" == "0.0.0.0" || "$host" == "::" ]] && host="127.0.0.1"
    printf 'http://%s:%s' "$host" "$port"
  else
    printf 'http://localhost:%s' "$raw"
  fi
}

if (( NEEDS_FULL == 1 )); then
  if [[ "${TELEPILOT_SKIP_UPDATER_RECREATE:-0}" == "1" ]]; then
    warn "当前由内部 updater 执行完整更新，业务服务完成后由临时 handoff 容器重建 updater。"
    log "构建 + 启动业务容器（postgres / redis / web / frontend）"
    if [[ -n "$RUNNING_UPDATER_TOKEN" && "$RUNNING_UPDATER_TOKEN" != "$PERSISTED_UPDATER_TOKEN" ]]; then
      # 过渡阶段先让新 web 与仍在运行的旧 updater 使用同一 token；handoff
      # 随后会用 .env 中的新 token 一起重建两者。
      UPDATER_TOKEN="$RUNNING_UPDATER_TOKEN" docker compose up -d --build postgres redis web frontend
    else
      docker compose up -d --build postgres redis web frontend
    fi
    wait_compose_healthy docker-compose.yml postgres 60 || {
      docker compose logs --tail=80 postgres >&2
      exit 1
    }
    wait_compose_healthy docker-compose.yml redis 30 || {
      docker compose logs --tail=80 redis >&2
      exit 1
    }
    wait_compose_healthy docker-compose.yml web 120 || {
      docker compose logs --tail=80 web >&2
      exit 1
    }
    wait_compose_healthy docker-compose.yml frontend 60 || {
      docker compose logs --tail=80 frontend >&2
      exit 1
    }
    wait_http "$(frontend_url)" 30 "前端" || {
      docker compose logs --tail=80 frontend >&2
      exit 1
    }
    log "构建新版 updater 并安排 token/镜像原子切换"
    docker compose build updater
    env -u UPDATER_TOKEN docker compose run -d --rm --no-deps --entrypoint sh updater -c \
      'sleep 3; env -u UPDATER_TOKEN docker compose up -d --no-deps --force-recreate updater web && rm -f "$(git rev-parse --git-path telepilot-deploy-pending)"' \
      >/dev/null
    HANDOFF_SCHEDULED=1
    ok "完整业务更新完成；updater/web handoff 已安排"
  else
    log "执行完整生产更新"
    "$SCRIPT_DIR/prod-up.sh"
  fi
elif (( DOCS_ONLY == 1 )); then
  ok "无需重建服务，更新完成"
else
  services=()
  (( NEEDS_BACKEND == 1 )) && services+=("web")
  (( NEEDS_FRONTEND == 1 )) && services+=("frontend")

  log "增量重建服务：${services[*]}"
  docker compose up -d --build --no-deps "${services[@]}"

  if (( NEEDS_BACKEND == 1 )); then
    wait_compose_healthy docker-compose.yml web 120 || {
      docker compose logs --tail=80 web >&2
      exit 1
    }
  fi

  if (( NEEDS_FRONTEND == 1 )); then
    wait_compose_healthy docker-compose.yml frontend 60 || {
      docker compose logs --tail=80 frontend >&2
      exit 1
    }
    wait_http "$(frontend_url)" 30 "前端" || {
      docker compose logs --tail=80 frontend >&2
      exit 1
    }
  fi

  ok "增量更新完成"
fi

if (( HANDOFF_SCHEDULED == 0 )); then
  rm -f "$PENDING_FILE"
fi

echo
ok "TelePilot 已更新到 ${NEW_COMMIT:0:12}"

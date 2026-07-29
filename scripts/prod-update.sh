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
[[ "$TELEPILOT_HOST_PROJECT_DIR" == /* ]] \
  || die "TELEPILOT_HOST_PROJECT_DIR 必须是宿主机绝对路径，拒绝从容器内解析相对挂载目录"

REMOTE="${TELEPILOT_UPDATE_REMOTE:-origin}"
BRANCH="${TELEPILOT_UPDATE_BRANCH:-main}"
DRY_RUN=0
FORCE_FULL=0
OLD_COMMIT=""
HEAD_ALREADY_UPDATED=0
HANDOFF_SCHEDULED=0
PATCH_CONTAINER=""
WEB_SYNC_OLD_IMAGE_ID=""
WEB_SYNC_IMAGE_REF=""
WEB_SYNC_ACTIVE=0
RUNNING_UPDATER_TOKEN="${UPDATER_TOKEN:-}"
PROGRESS_PREFIX="@@TELEPILOT_PROGRESS@@"

emit_progress() {
  local percent="$1" phase="$2" detail="${3:-}"
  printf '%s%s|%s|%s\n' "$PROGRESS_PREFIX" "$percent" "$phase" "$detail"
}

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
  if [[ -n "$PATCH_CONTAINER" ]]; then
    docker rm -f "$PATCH_CONTAINER" >/dev/null 2>&1 || true
    PATCH_CONTAINER=""
  fi
  if (( WEB_SYNC_ACTIVE == 1 )); then
    rollback_web_runtime_image || true
  fi
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

emit_progress 4 "检查远端" "读取 ${REMOTE}/${BRANCH}"
if [[ "${TELEPILOT_UPDATE_PREFETCHED:-0}" == "1" ]] && git cat-file -e "${REMOTE_REF}^{commit}" 2>/dev/null; then
  ok "复用 updater 已拉取的远程索引 ${REMOTE}/${BRANCH}"
else
  log "拉取远程索引 ${REMOTE}/${BRANCH}"
  git fetch "$REMOTE" "${BRANCH}:${REMOTE_REF}" >/dev/null
fi

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

need_cmd python3 "服务级更新计划"
PLAN_JSON="$(python3 backend/app/util/update_plan.py \
  --root "$ROOT_DIR" --old "$CURRENT_COMMIT" --new "$TARGET_COMMIT")" || {
  die "无法生成服务级更新计划"
}
emit_progress 12 "生成计划" "计算需要切换的服务"

plan_value() {
  local key="$1"
  PLAN_JSON="$PLAN_JSON" PLAN_KEY="$key" python3 - <<'PY'
import json
import os
import sys

value = json.loads(os.environ["PLAN_JSON"]).get(os.environ["PLAN_KEY"])
if isinstance(value, list):
    if value:
        sys.stdout.write("\n".join(str(item) for item in value) + "\n")
elif isinstance(value, bool):
    print("1" if value else "0")
elif value is not None:
    print(value)
PY
}

mapfile -t CHANGED_FILES < <(plan_value changed_files)
mapfile -t PLAN_COMPONENTS < <(plan_value components)
mapfile -t PLAN_SERVICES < <(plan_value services)
mapfile -t PLAN_FILE_SYNC_SERVICES < <(plan_value file_sync_services)
mapfile -t PLAN_REBUILD_SERVICES < <(plan_value rebuild_services)
NEEDS_BACKEND=0
NEEDS_FRONTEND=0
NEEDS_UPDATER=0
NEEDS_BACKEND_SYNC=0
NEEDS_BACKEND_REBUILD=0
NEEDS_FRONTEND_REBUILD=0
NEEDS_UPDATER_REBUILD=0
NEEDS_FULL="$(plan_value requires_full_update)"
REQUIRES_BACKUP="$(plan_value requires_backup)"
REQUIRES_MIGRATION="$(plan_value requires_migration)"
DOCS_ONLY=0
for service in "${PLAN_SERVICES[@]}"; do
  [[ "$service" == "web" ]] && NEEDS_BACKEND=1
  [[ "$service" == "frontend" ]] && NEEDS_FRONTEND=1
  [[ "$service" == "updater" ]] && NEEDS_UPDATER=1
done
for service in "${PLAN_FILE_SYNC_SERVICES[@]}"; do
  [[ "$service" == "web" ]] && NEEDS_BACKEND_SYNC=1
done
for service in "${PLAN_REBUILD_SERVICES[@]}"; do
  [[ "$service" == "web" ]] && NEEDS_BACKEND_REBUILD=1
  [[ "$service" == "frontend" ]] && NEEDS_FRONTEND_REBUILD=1
  [[ "$service" == "updater" ]] && NEEDS_UPDATER_REBUILD=1
done
# 兼容旧更新计划：新脚本若读取到尚未提供动作字段的 JSON，保守地按原方式重建。
if (( ${#PLAN_SERVICES[@]} > 0 && ${#PLAN_FILE_SYNC_SERVICES[@]} == 0 && ${#PLAN_REBUILD_SERVICES[@]} == 0 )); then
  NEEDS_BACKEND_REBUILD=$NEEDS_BACKEND
  NEEDS_FRONTEND_REBUILD=$NEEDS_FRONTEND
  NEEDS_UPDATER_REBUILD=$NEEDS_UPDATER
fi
if [[ "$(plan_value components)" == "docs_only" ]]; then
  DOCS_ONLY=1
fi

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
  ok "分类结果：服务级增量更新 ${PLAN_SERVICES[*]}"
  if (( ${#PLAN_FILE_SYNC_SERVICES[@]} > 0 )); then
    log "文件同步后重启：${PLAN_FILE_SYNC_SERVICES[*]}"
  fi
  if (( ${#PLAN_REBUILD_SERVICES[@]} > 0 )); then
    log "需要镜像构建：${PLAN_REBUILD_SERVICES[*]}"
  fi
  if (( ${#PLAN_COMPONENTS[@]} > 0 )); then
    log "影响组件：${PLAN_COMPONENTS[*]}"
  fi
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
  emit_progress 18 "拉取代码" "Fast-forward 到目标 commit"
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
  emit_progress 24 "备份数据" "迁移前创建恢复点"
  if [[ "${TELEPILOT_MIGRATION_BACKUP_CONFIRMED:-0}" == "1" ]]; then
    warn "已由操作者确认存在可恢复备份，跳过自动备份。"
  else
    log "迁移前创建数据库与持久化卷备份"
    TELEPILOT_BACKUP_QUIESCE=1 "$ROOT_DIR/deploy/backup.sh"
    ok "迁移前备份已完成；尚未启动新版容器或执行迁移。"
  fi
fi

compose_project_name() {
  local web_id project
  web_id="$(docker compose ps -q web 2>/dev/null || true)"
  if [[ -n "$web_id" ]]; then
    project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$web_id" 2>/dev/null || true)"
  fi
  if [[ -z "${project:-}" ]]; then
    project="$(basename "$ROOT_DIR" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  fi
  printf '%s' "${project:-telepilot}"
}

rollback_web_runtime_image() {
  if (( WEB_SYNC_ACTIVE == 0 )); then
    return 0
  fi
  warn "web 文件同步未完成，恢复更新前镜像"
  if ! docker image tag "$WEB_SYNC_OLD_IMAGE_ID" "$WEB_SYNC_IMAGE_REF"; then
    err "无法恢复旧 web 镜像标签：$WEB_SYNC_IMAGE_REF"
    return 1
  fi
  if ! docker compose up -d --no-deps --force-recreate web; then
    err "旧 web 镜像已恢复，但容器重建失败"
    return 1
  fi
  WEB_SYNC_ACTIVE=0
  if ! wait_compose_healthy docker-compose.yml web 120; then
    docker compose logs --tail=80 web >&2 || true
    err "恢复后的 web 未通过健康检查"
    return 1
  fi
  warn "web 已回到更新前镜像；仓库保留在目标 commit，后续可直接重试"
}

sync_web_runtime_image() {
  local web_id image_ref old_image_id image_cmd image_entrypoint project stage new_image_id
  web_id="$(docker compose ps -q web 2>/dev/null || true)"
  [[ -n "$web_id" ]] || {
    err "web 容器不存在，不能执行文件同步快速更新"
    return 1
  }
  image_ref="$(docker inspect --format '{{.Config.Image}}' "$web_id")"
  old_image_id="$(docker inspect --format '{{.Image}}' "$web_id")"
  if [[ -z "$image_ref" || "$image_ref" == sha256:* || "$image_ref" == *@* ]]; then
    err "web 当前镜像没有可安全覆盖的本地标签，拒绝文件同步：${image_ref:-unknown}"
    return 1
  fi
  docker image inspect "$image_ref" >/dev/null
  image_cmd="$(docker image inspect --format '{{json .Config.Cmd}}' "$image_ref")"
  image_entrypoint="$(docker image inspect --format '{{json .Config.Entrypoint}}' "$image_ref")"
  [[ "$image_cmd" == "null" ]] && image_cmd="[]"
  if [[ "$image_entrypoint" != "null" && "$image_entrypoint" != "[]" ]]; then
    err "web 基础镜像设置了自定义 ENTRYPOINT，无法安全生成文件补丁镜像"
    return 1
  fi

  project="$(compose_project_name)"
  PATCH_CONTAINER="${project}-web-patch-${NEW_COMMIT:0:12}-$$"
  stage="/tmp/telepilot-runtime-${NEW_COMMIT:0:12}"
  docker create --name "$PATCH_CONTAINER" "$image_ref" \
    python -c 'import time; time.sleep(600)' >/dev/null
  docker start "$PATCH_CONTAINER" >/dev/null
  docker exec "$PATCH_CONTAINER" mkdir -p "$stage"

  # 只从目标 commit 归档受控的运行时目录，既覆盖新增/修改文件，也通过整目录
  # 替换清除已删除文件；不会把服务器工作区中的 ignored/untracked 文件带进镜像。
  git archive "$NEW_COMMIT" \
    backend/app backend/alembic backend/alembic.ini backend/pyproject.toml CHANGELOG.md \
    frontend/src frontend/package.json frontend/tsconfig.json \
    frontend/tsconfig.app.json frontend/vite.config.ts \
    | docker cp - "$PATCH_CONTAINER:$stage"
  docker exec "$PATCH_CONTAINER" python -m compileall -q "$stage/backend/app"
  docker exec "$PATCH_CONTAINER" sh -c '
    set -eu
    stage="$1"
    rm -rf /app/app /app/alembic /app/source/frontend
    rm -f /app/alembic.ini /app/pyproject.toml /app/CHANGELOG.md /app/.telepilot-runtime-commit
    mkdir -p /app/source
    mv "$stage/backend/app" /app/app
    mv "$stage/backend/alembic" /app/alembic
    mv "$stage/backend/alembic.ini" /app/alembic.ini
    mv "$stage/backend/pyproject.toml" /app/pyproject.toml
    mv "$stage/CHANGELOG.md" /app/CHANGELOG.md
    mv "$stage/frontend" /app/source/frontend
  ' sh "$stage"
  printf '%s\n' "$NEW_COMMIT" \
    | docker exec -i "$PATCH_CONTAINER" sh -c 'cat > /app/.telepilot-runtime-commit'

  WEB_SYNC_OLD_IMAGE_ID="$old_image_id"
  WEB_SYNC_IMAGE_REF="$image_ref"
  new_image_id="$(docker commit \
    --message "TelePilot 文件同步 ${OLD_COMMIT:0:12} -> ${NEW_COMMIT:0:12}" \
    --change "CMD $image_cmd" \
    "$PATCH_CONTAINER" "$image_ref")"
  WEB_SYNC_ACTIVE=1
  docker rm -f "$PATCH_CONTAINER" >/dev/null
  PATCH_CONTAINER=""
  log "web 运行文件已生成补丁镜像：${new_image_id:0:19}"
  if ! docker compose up -d --no-deps --force-recreate web; then
    rollback_web_runtime_image || true
    return 1
  fi
}

schedule_updater_handoff() {
  local project handoff_log
  project="$(compose_project_name)"
  handoff_log="$(git rev-parse --git-path telepilot-updater-handoff.log)"
  rm -f "$handoff_log"
  docker compose build updater
  env -u UPDATER_TOKEN docker compose run -d --rm --no-deps \
    -e COMPOSE_PROJECT_NAME="$project" \
    -e TELEPILOT_HOST_PROJECT_DIR="$TELEPILOT_HOST_PROJECT_DIR" \
    --entrypoint sh updater -c \
    'sleep 3; log="$(git rev-parse --git-path telepilot-updater-handoff.log)"; if env -u UPDATER_TOKEN docker compose up -d --no-deps --force-recreate updater >"$log" 2>&1; then printf "handoff succeeded\n" >>"$log"; rm -f "$(git rev-parse --git-path telepilot-deploy-pending)"; else rc=$?; printf "handoff failed: exit %s\n" "$rc" >>"$log"; exit "$rc"; fi' \
    >/dev/null
  HANDOFF_SCHEDULED=1
}

if (( NEEDS_FULL == 1 )); then
  if [[ "${TELEPILOT_SKIP_UPDATER_RECREATE:-0}" == "1" ]]; then
    warn "当前由内部 updater 执行完整更新，业务服务完成后由临时 handoff 容器重建 updater。"
    log "构建 + 启动业务容器（仅显式指定 postgres / redis / web / frontend）"
    emit_progress 30 "构建镜像" "构建并切换业务服务"
    if [[ -n "$RUNNING_UPDATER_TOKEN" && "$RUNNING_UPDATER_TOKEN" != "$PERSISTED_UPDATER_TOKEN" ]]; then
      # 过渡阶段先让新 web 与仍在运行的旧 updater 使用同一 token；handoff
      # 随后会用 .env 中的新 token 一起重建两者。
      UPDATER_TOKEN="$RUNNING_UPDATER_TOKEN" docker compose up -d --build --no-deps postgres redis web frontend
    else
      docker compose up -d --build --no-deps postgres redis web frontend
    fi
    emit_progress 78 "健康检查" "等待业务服务 ready"
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
    emit_progress 92 "服务就绪" "业务服务已通过健康检查"
    log "构建新版 updater 并安排 token/镜像原子切换"
    emit_progress 96 "更新更新器" "安排 updater handoff"
    schedule_updater_handoff
    ok "完整业务更新完成；updater handoff 已安排"
  else
    log "执行完整生产更新"
    "$SCRIPT_DIR/prod-up.sh"
  fi
elif (( DOCS_ONLY == 1 )); then
  ok "无需重建服务，更新完成"
else
  rebuild_services=()
  (( NEEDS_BACKEND_REBUILD == 1 )) && rebuild_services+=("web")
  (( NEEDS_FRONTEND_REBUILD == 1 )) && rebuild_services+=("frontend")

  if (( ${#rebuild_services[@]} > 0 )); then
    if (( NEEDS_FRONTEND_REBUILD == 1 && NEEDS_BACKEND_REBUILD == 0 )); then
      emit_progress 30 "编译前端" "编译并切换 frontend"
    else
      emit_progress 30 "构建镜像" "增量重建 ${rebuild_services[*]}"
    fi
    log "增量重建服务：${rebuild_services[*]}"
    # WP-U2：确认按 classify_changed_files 裁剪后的服务列表重建，不整栈 up
    log "更新计划服务集合：${PLAN_SERVICES[*]:-none}"
    docker compose up -d --build --no-deps "${rebuild_services[@]}"
  fi

  if (( NEEDS_BACKEND_SYNC == 1 )); then
    emit_progress 60 "同步运行文件" "覆盖目标 commit 的后端与诊断源码并重启 web"
    log "文件级同步服务：web（无需执行 docker build）"
    sync_web_runtime_image
  fi

  emit_progress 78 "健康检查" "等待新容器 ready"

  # WP-U2：web / frontend 同时变更时并行等待健康，缩短串行阻塞
  health_pids=()
  health_names=()
  if (( NEEDS_BACKEND == 1 )); then
    (
      wait_compose_healthy docker-compose.yml web 120 || {
        docker compose logs --tail=80 web >&2
        exit 1
      }
    ) &
    health_pids+=("$!")
    health_names+=("web")
  fi
  if (( NEEDS_FRONTEND == 1 )); then
    (
      wait_compose_healthy docker-compose.yml frontend 60 || {
        docker compose logs --tail=80 frontend >&2
        exit 1
      }
    ) &
    health_pids+=("$!")
    health_names+=("frontend")
  fi
  health_failed=0
  for i in "${!health_pids[@]}"; do
    if ! wait "${health_pids[$i]}"; then
      err "健康检查失败：${health_names[$i]}"
      health_failed=1
    fi
  done
  if (( health_failed != 0 )); then
    rollback_web_runtime_image || true
    false
  fi

  emit_progress 92 "服务就绪" "受影响服务已通过健康检查"

  if (( NEEDS_UPDATER == 1 )); then
    emit_progress 96 "更新更新器" "安排 updater handoff"
    log "构建新版 updater 并安排独立 handoff"
    schedule_updater_handoff
  fi

  ok "增量更新完成"
fi

WEB_SYNC_ACTIVE=0
if (( HANDOFF_SCHEDULED == 0 )); then
  rm -f "$PENDING_FILE"
fi

echo
emit_progress 100 "更新完成" "所有计划步骤已完成"
ok "TelePilot 已更新到 ${NEW_COMMIT:0:12}"

#!/usr/bin/env bash
# Production incremental updater.
#
# Goal:
#   - Pull only fast-forward updates.
#   - Classify changed files before applying.
#   - Rebuild only the Docker Compose services that need new code.
#   - Fall back to full prod-up whenever the change is risky or ambiguous.

set -euo pipefail

ORIGINAL_ARGS=("$@")
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
NEEDS_UPDATER_HANDOFF=0
PATCH_CONTAINER=""
WEB_SYNC_OLD_IMAGE_REF=""
WEB_SYNC_IMAGE_REF=""
WEB_SYNC_ACTIVE=0
REQUIRES_MIGRATION=0
RUNNING_UPDATER_TOKEN="${UPDATER_TOKEN:-}"
TOKEN_ROTATION_REQUIRED=0
TOKEN_ROTATION_HOST_RECREATE=0
PROGRESS_PREFIX="@@TELEPILOT_PROGRESS@@"
TARGET_IMAGE_COMMIT=""
TARGET_WEB_IMAGE=""
TARGET_FRONTEND_IMAGE=""
TARGET_UPDATER_IMAGE=""
NEEDS_PLUGIN_SYNC=0
VERIFIED_IMAGE_REF=""
PLUGIN_SYNC_ACTIVE=0
PLUGIN_ROLLBACK_STAGE=""
SWITCHED_SERVICES=()
SWITCHED_OLD_IMAGES=()
SWITCHED_ENV_KEYS=()
SWITCHED_TIMEOUTS=()

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

if [[ "${TELEPILOT_SKIP_UPDATER_RECREATE:-0}" == "1" ]] \
  && [[ ! "${TELEPILOT_UPDATE_JOB_ID:-}" =~ ^[0-9a-f]{12}$ ]]; then
  die "内部 updater 更新缺少合法任务 ID，拒绝在无法可靠收尾的状态下开始"
fi

rollback_switched_services() {
  local index service old_image env_key timeout
  for (( index=${#SWITCHED_SERVICES[@]} - 1; index >= 0; index-- )); do
    service="${SWITCHED_SERVICES[$index]}"
    old_image="${SWITCHED_OLD_IMAGES[$index]}"
    env_key="${SWITCHED_ENV_KEYS[$index]}"
    timeout="${SWITCHED_TIMEOUTS[$index]}"
    if (( REQUIRES_MIGRATION == 1 )) && [[ "$service" == "web" ]]; then
      warn "本次已进入数据库迁移边界，不自动把 web 回退到旧代码；请按备份恢复流程处理"
      continue
    fi
    warn "恢复 $service 更新前镜像：$old_image"
    if ! env "$env_key=$old_image" \
      docker compose up -d --no-build --no-deps --force-recreate "$service"; then
      err "$service 旧镜像重建失败"
      continue
    fi
    wait_compose_healthy docker-compose.yml "$service" "$timeout" \
      || err "$service 回滚后仍未恢复健康"
    set_env_value .env "$env_key" "$old_image" || true
  done
  SWITCHED_SERVICES=()
  SWITCHED_OLD_IMAGES=()
  SWITCHED_ENV_KEYS=()
  SWITCHED_TIMEOUTS=()
}

rollback_tracked_plugins() {
  local path target old_list new_list old_tree
  (( PLUGIN_SYNC_ACTIVE == 1 )) || return 0
  old_list="$PLUGIN_ROLLBACK_STAGE/old-files"
  new_list="$PLUGIN_ROLLBACK_STAGE/new-files"
  old_tree="$PLUGIN_ROLLBACK_STAGE/old-tree"
  warn "恢复更新前的 Git 跟踪插件文件"
  while IFS= read -r path; do
    [[ "$path" == plugins/installed/* && "$path" != *".."* ]] || continue
    if ! grep -Fxq "$path" "$old_list"; then
      target="/app/$path"
      docker compose exec -T web python -c \
        'from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)' \
        "$target" || true
    fi
  done < "$new_list"
  while IFS= read -r path; do
    [[ "$path" == plugins/installed/* && "$path" != *".."* ]] || continue
    target="/app/$path"
    docker compose exec -T web python -c \
      'from pathlib import Path; import sys; Path(sys.argv[1]).parent.mkdir(parents=True, exist_ok=True)' \
      "$target" || true
    docker compose cp "$old_tree/$path" "web:$target" || true
  done < "$old_list"
  docker compose restart web >/dev/null 2>&1 || true
  wait_compose_healthy docker-compose.yml web 120 || true
  rm -rf "$PLUGIN_ROLLBACK_STAGE"
  PLUGIN_SYNC_ACTIVE=0
  PLUGIN_ROLLBACK_STAGE=""
}

on_error() {
  if [[ -n "$PATCH_CONTAINER" ]]; then
    docker rm -f "$PATCH_CONTAINER" >/dev/null 2>&1 || true
    PATCH_CONTAINER=""
  fi
  if (( WEB_SYNC_ACTIVE == 1 )); then
    rollback_web_runtime_image || true
  fi
  rollback_tracked_plugins || true
  rollback_switched_services || true
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
git remote | grep -Fxq -- "$REMOTE" \
  || die "未知 Git 远程：$REMOTE"
git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 \
  || die "非法 Git 分支名：$BRANCH"
PENDING_FILE="$(git rev-parse --git-path telepilot-deploy-pending)"

REMOTE_REF="refs/remotes/${REMOTE}/${BRANCH}"

emit_progress 4 "检查远端" "读取 ${REMOTE}/${BRANCH}"
if [[ "${TELEPILOT_UPDATE_PREFETCHED:-0}" == "1" ]] && git cat-file -e "${REMOTE_REF}^{commit}" 2>/dev/null; then
  ok "复用 updater 已拉取的远程索引 ${REMOTE}/${BRANCH}"
else
  log "拉取远程索引 ${REMOTE}/${BRANCH}"
  git fetch "$REMOTE" "${BRANCH}:${REMOTE_REF}" >/dev/null
fi

CHECKED_OUT_COMMIT="$(git rev-parse HEAD)"
CURRENT_COMMIT="$CHECKED_OUT_COMMIT"
TARGET_COMMIT="$(git rev-parse "$REMOTE_REF")"
OLD_COMMIT="$CURRENT_COMMIT"

if [[ -f "$PENDING_FILE" ]]; then
  read -r pending_old pending_target < "$PENDING_FILE" || true
  if [[ -n "${pending_old:-}" && "${pending_target:-}" == "$CHECKED_OUT_COMMIT" ]] \
    && git cat-file -e "${pending_old}^{commit}" 2>/dev/null \
    && git cat-file -e "${pending_target}^{commit}" 2>/dev/null \
    && git merge-base --is-ancestor "$pending_target" "$TARGET_COMMIT"; then
    warn "检测到尚未完成的部署，累计更新 ${pending_old:0:12}..${TARGET_COMMIT:0:12}"
    CURRENT_COMMIT="$pending_old"
    OLD_COMMIT="$pending_old"
    if [[ "$CHECKED_OUT_COMMIT" == "$TARGET_COMMIT" ]]; then
      HEAD_ALREADY_UPDATED=1
    fi
  elif [[ "$CHECKED_OUT_COMMIT" == "$TARGET_COMMIT" ]]; then
    warn "忽略与当前更新链不匹配的旧部署 pending 标记"
    rm -f "$PENDING_FILE"
  fi
fi

if [[ "$CHECKED_OUT_COMMIT" == "$TARGET_COMMIT" ]]; then
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
mapfile -t PLAN_REASONS < <(plan_value reasons)
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
for file in "${CHANGED_FILES[@]}"; do
  [[ "$file" == plugins/installed/* ]] && NEEDS_PLUGIN_SYNC=1
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
    log "需要预构建镜像：${PLAN_REBUILD_SERVICES[*]}"
  fi
  if (( ${#PLAN_COMPONENTS[@]} > 0 )); then
    log "影响组件：${PLAN_COMPONENTS[*]}"
  fi
fi
for reason in "${PLAN_REASONS[@]}"; do
  warn "分类依据：$reason"
done

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

image_checkpoint_commit() {
  git log -1 --format=%H "$TARGET_COMMIT" -- . \
    ':(exclude)docs/**' \
    ':(exclude,glob)**/*.md' \
    ':(exclude,glob)**/*.rst' \
    ':(exclude,glob)**/*.txt' \
    ':(exclude)LICENSE'
}

prepare_target_images() {
  local prefix="${TELEPILOT_IMAGE_PREFIX:-ghcr.io/anoyou/telepilot}"
  TARGET_IMAGE_COMMIT="$(image_checkpoint_commit)"
  [[ -n "$TARGET_IMAGE_COMMIT" ]] || die "无法确定目标提交对应的镜像检查点"
  TARGET_WEB_IMAGE="${prefix}-web:sha-${TARGET_IMAGE_COMMIT}"
  TARGET_FRONTEND_IMAGE="${prefix}-frontend:sha-${TARGET_IMAGE_COMMIT}"
  TARGET_UPDATER_IMAGE="${prefix}-updater:sha-${TARGET_IMAGE_COMMIT}"
}

pull_verified_image() {
  local service="$1" image_ref="$2" revision="$3" image_revision image_source digest_ref
  local expected_source="${TELEPILOT_IMAGE_SOURCE:-https://github.com/Anoyou/Telebot}"
  log "预拉取 $service 镜像：$image_ref"
  docker pull "$image_ref" >/dev/null || die \
    "目标 $service 镜像不存在或构建未成功（commit ${revision:0:12}）；请检查 https://github.com/Anoyou/Telebot/actions 后重试"
  image_revision="$(docker image inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "$image_ref" 2>/dev/null || true)"
  [[ "$image_revision" == "$revision" ]] \
    || die "$service 镜像 revision 校验失败：期望 $revision，实际 ${image_revision:-missing}"
  image_source="$(docker image inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.source" }}' \
    "$image_ref" 2>/dev/null || true)"
  [[ "$image_source" == "$expected_source" ]] \
    || die "$service 镜像 source 校验失败：${image_source:-missing}"
  digest_ref="$(docker image inspect --format '{{ index .RepoDigests 0 }}' "$image_ref" 2>/dev/null || true)"
  [[ "$digest_ref" == *@sha256:* ]] || die "$service 镜像缺少可固定的 registry digest"
  verify_image_attestation "$digest_ref" "$revision" \
    || die "$service 镜像缺少受信 GitHub Actions 构建来源证明"
  VERIFIED_IMAGE_REF="$digest_ref"
}

record_previous_deployment() {
  local state_file web_id frontend_id updater_id web_image frontend_image updater_image
  state_file="$(git rev-parse --git-path telepilot-deploy-previous.json)"
  web_id="$(docker compose ps -q web 2>/dev/null || true)"
  frontend_id="$(docker compose ps -q frontend 2>/dev/null || true)"
  updater_id="$(docker compose ps -q updater 2>/dev/null || true)"
  web_image="$(docker inspect --format '{{.Config.Image}}' "$web_id" 2>/dev/null || true)"
  frontend_image="$(docker inspect --format '{{.Config.Image}}' "$frontend_id" 2>/dev/null || true)"
  updater_image="$(docker inspect --format '{{.Config.Image}}' "$updater_id" 2>/dev/null || true)"
  python3 - "$state_file" "$CURRENT_COMMIT" "$web_image" "$frontend_image" "$updater_image" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = {
    "commit": sys.argv[2],
    "images": {
        "web": sys.argv[3] or None,
        "frontend": sys.argv[4] or None,
        "updater": sys.argv[5] or None,
    },
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

if (( DOCS_ONLY == 0 && HEAD_ALREADY_UPDATED == 0 )); then
  record_previous_deployment
fi

if (( HEAD_ALREADY_UPDATED == 0 )); then
  pending_tmp="${PENDING_FILE}.tmp.$$"
  printf '%s %s\n' "$OLD_COMMIT" "$TARGET_COMMIT" > "$pending_tmp"
  mv "$pending_tmp" "$PENDING_FILE"
  emit_progress 18 "拉取代码" "Fast-forward 到目标 commit"
  log "执行 fast-forward 更新"
  git pull --ff-only "$REMOTE" "$BRANCH"
  NEW_COMMIT="$(git rev-parse HEAD)"
  [[ "$NEW_COMMIT" == "$TARGET_COMMIT" ]] \
    || die "fast-forward 后 HEAD 与已校验目标不一致"
  ok "代码已更新到 ${NEW_COMMIT:0:12}"
else
  NEW_COMMIT="$(git rev-parse HEAD)"
fi

# 更新脚本和它在启动时 source 的 _lib.sh 也可能属于本次更新。首次 fast-forward
# 后必须立即重新执行目标版本脚本，让新的兼容、自举和安全逻辑在预拉取镜像前接管。
# pending 标记会让第二次执行继续使用 OLD_COMMIT..TARGET_COMMIT 生成完整计划，
# 且 HEAD_ALREADY_UPDATED=1 会阻止再次 pull/re-exec。
if (( HEAD_ALREADY_UPDATED == 0 )); then
  emit_progress 20 "接管更新" "重新加载目标版本更新逻辑"
  log "重新加载目标 commit 的更新逻辑"
  exec bash scripts/prod-update.sh "${ORIGINAL_ARGS[@]}"
fi

# 目标版本更新逻辑接管后再确认所有必需镜像。镜像缺失或验签失败时，运行中的
# 服务仍保持旧镜像；pending 标记保留，镜像就绪或环境修复后可直接在线重试。
if (( NEEDS_FULL == 1 || NEEDS_BACKEND_REBUILD == 1 || NEEDS_FRONTEND_REBUILD == 1 || NEEDS_UPDATER_REBUILD == 1 )); then
  prepare_target_images
  if (( NEEDS_FULL == 1 || NEEDS_BACKEND_REBUILD == 1 )); then
    pull_verified_image web "$TARGET_WEB_IMAGE" "$TARGET_IMAGE_COMMIT"
    TARGET_WEB_IMAGE="$VERIFIED_IMAGE_REF"
  fi
  if (( NEEDS_FULL == 1 || NEEDS_FRONTEND_REBUILD == 1 )); then
    pull_verified_image frontend "$TARGET_FRONTEND_IMAGE" "$TARGET_IMAGE_COMMIT"
    TARGET_FRONTEND_IMAGE="$VERIFIED_IMAGE_REF"
  fi
  if (( NEEDS_FULL == 1 || NEEDS_UPDATER_REBUILD == 1 )); then
    pull_verified_image updater "$TARGET_UPDATER_IMAGE" "$TARGET_IMAGE_COMMIT"
    TARGET_UPDATER_IMAGE="$VERIFIED_IMAGE_REF"
  fi
fi

# 新版 compose 在解析任何服务前都要求 UPDATER_TOKEN。先补齐并与 JWT
# 解耦，保证从旧部署升级时备份和后续 compose 命令都能执行。
if [[ -z "$RUNNING_UPDATER_TOKEN" ]]; then
  running_updater_id="$(docker compose ps -q updater 2>/dev/null || true)"
  if [[ -n "$running_updater_id" ]]; then
    RUNNING_UPDATER_TOKEN="$(
      docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$running_updater_id" 2>/dev/null \
        | awk -F= '$1 == "UPDATER_TOKEN" {sub(/^[^=]*=/, ""); print; exit}'
    )"
  fi
fi
ensure_updater_token_env .env
PERSISTED_UPDATER_TOKEN="$(grep -E '^UPDATER_TOKEN=' .env | head -n1 | cut -d= -f2- | tr -d ' "')"
if [[ -n "$RUNNING_UPDATER_TOKEN" && "$RUNNING_UPDATER_TOKEN" != "$PERSISTED_UPDATER_TOKEN" ]]; then
  TOKEN_ROTATION_REQUIRED=1
  # 在最终协同 recreate 之前，所有 Compose 操作继续显式使用运行中旧 token，
  # 避免只重建 web 或失败回滚时提前与旧 updater 失联。最终切换用 env -u
  # 一次性让 web/updater 读取 .env 中的新 token。
  export UPDATER_TOKEN="$RUNNING_UPDATER_TOKEN"
  if [[ "${TELEPILOT_SKIP_UPDATER_RECREATE:-0}" == "1" ]]; then
    if [[ -z "$TARGET_UPDATER_IMAGE" ]]; then
      current_updater_id="$(docker compose ps -q updater 2>/dev/null || true)"
      [[ -n "$current_updater_id" ]] || die "token 轮换需要运行中的 updater 容器"
      TARGET_UPDATER_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$current_updater_id")"
      [[ -n "$TARGET_UPDATER_IMAGE" ]] || die "无法读取 token 轮换使用的 updater 镜像"
    fi
    NEEDS_UPDATER_HANDOFF=1
  else
    TOKEN_ROTATION_HOST_RECREATE=1
  fi
fi

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
  if (( REQUIRES_MIGRATION == 1 )); then
    warn "数据库迁移可能已经执行，不自动把 web 回退到旧代码；保留新镜像等待人工恢复"
    WEB_SYNC_ACTIVE=0
    return 0
  fi
  warn "web 文件同步未完成，恢复更新前镜像"
  if ! env TELEPILOT_WEB_IMAGE="$WEB_SYNC_OLD_IMAGE_REF" \
    docker compose up -d --no-build --no-deps --force-recreate web; then
    err "旧 web 镜像已恢复，但容器重建失败"
    return 1
  fi
  WEB_SYNC_ACTIVE=0
  if ! wait_compose_healthy docker-compose.yml web 120; then
    docker compose logs --tail=80 web >&2 || true
    err "恢复后的 web 未通过健康检查"
    return 1
  fi
  set_env_value .env TELEPILOT_WEB_IMAGE "$WEB_SYNC_OLD_IMAGE_REF" || true
  warn "web 已回到更新前镜像；仓库保留在目标 commit，后续可直接重试"
}

sync_web_runtime_image() {
  local web_id image_ref image_cmd image_entrypoint project stage new_image_id runtime_ref
  web_id="$(docker compose ps -q web 2>/dev/null || true)"
  [[ -n "$web_id" ]] || {
    err "web 容器不存在，不能执行文件同步快速更新"
    return 1
  }
  image_ref="$(docker inspect --format '{{.Config.Image}}' "$web_id")"
  if [[ -z "$image_ref" || "$image_ref" == sha256:* ]]; then
    err "web 当前镜像引用不可用于安全回滚：${image_ref:-unknown}"
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

  WEB_SYNC_OLD_IMAGE_REF="$image_ref"
  runtime_ref="telepilot-web-runtime:${NEW_COMMIT}"
  WEB_SYNC_IMAGE_REF="$runtime_ref"
  new_image_id="$(docker commit \
    --message "TelePilot 文件同步 ${OLD_COMMIT:0:12} -> ${NEW_COMMIT:0:12}" \
    --change "CMD $image_cmd" \
    "$PATCH_CONTAINER" "$runtime_ref")"
  WEB_SYNC_ACTIVE=1
  docker rm -f "$PATCH_CONTAINER" >/dev/null
  PATCH_CONTAINER=""
  log "web 运行文件已生成补丁镜像：${new_image_id:0:19}"
  if ! env TELEPILOT_WEB_IMAGE="$runtime_ref" \
    docker compose up -d --no-build --no-deps --force-recreate web; then
    rollback_web_runtime_image || true
    return 1
  fi
}

switch_prebuilt_service() {
  local service="$1" target_image="$2" env_key="$3" timeout="$4"
  local container_id old_image rollback_safe=1
  container_id="$(docker compose ps -q "$service" 2>/dev/null || true)"
  [[ -n "$container_id" ]] || {
    err "$service 容器不存在，不能执行预构建镜像切换"
    return 1
  }
  old_image="$(docker inspect --format '{{.Config.Image}}' "$container_id")"
  [[ -n "$old_image" ]] || {
    err "无法读取 $service 当前镜像引用"
    return 1
  }
  if (( REQUIRES_MIGRATION == 1 )) && [[ "$service" == "web" ]]; then
    rollback_safe=0
  fi

  log "切换 $service：$old_image -> $target_image"
  if ! env "$env_key=$target_image" \
    docker compose up -d --no-build --no-deps --force-recreate "$service"; then
    if (( rollback_safe == 1 )); then
      env "$env_key=$old_image" \
        docker compose up -d --no-build --no-deps --force-recreate "$service" >/dev/null 2>&1 || true
      err "$service 镜像切换失败，已尝试恢复旧镜像"
    else
      err "$service 镜像切换失败；迁移边界内禁止自动恢复旧代码"
    fi
    return 1
  fi
  if ! wait_compose_healthy docker-compose.yml "$service" "$timeout"; then
    docker compose logs --tail=80 "$service" >&2 || true
    if (( rollback_safe == 1 )); then
      warn "$service 新镜像未通过健康检查，恢复 $old_image"
      env "$env_key=$old_image" \
        docker compose up -d --no-build --no-deps --force-recreate "$service"
      wait_compose_healthy docker-compose.yml "$service" "$timeout" \
        || err "$service 回滚后仍未恢复健康"
      err "$service 新镜像健康检查失败，已恢复旧镜像"
    else
      err "$service 新镜像健康检查失败；schema 可能已升级，保留新代码等待人工恢复"
    fi
    return 1
  fi
  SWITCHED_SERVICES+=("$service")
  SWITCHED_OLD_IMAGES+=("$old_image")
  SWITCHED_ENV_KEYS+=("$env_key")
  SWITCHED_TIMEOUTS+=("$timeout")
}

persist_switched_services() {
  local index
  for (( index=0; index < ${#SWITCHED_SERVICES[@]}; index++ )); do
    case "${SWITCHED_ENV_KEYS[$index]}" in
      TELEPILOT_WEB_IMAGE)
        set_env_value .env TELEPILOT_WEB_IMAGE "$TARGET_WEB_IMAGE"
        ;;
      TELEPILOT_FRONTEND_IMAGE)
        set_env_value .env TELEPILOT_FRONTEND_IMAGE "$TARGET_FRONTEND_IMAGE"
        ;;
      TELEPILOT_UPDATER_IMAGE)
        set_env_value .env TELEPILOT_UPDATER_IMAGE "$TARGET_UPDATER_IMAGE"
        ;;
    esac
  done
}

sync_tracked_plugins() {
  local stage old_list new_list old_tree path target
  stage="$(mktemp -d "${TMPDIR:-/tmp}/telepilot-plugins.XXXXXX")"
  old_list="$stage/old-files"
  new_list="$stage/new-files"
  old_tree="$stage/old-tree"
  mkdir -p "$old_tree"
  git ls-tree -r --name-only "$OLD_COMMIT" -- plugins/installed > "$old_list"
  git ls-tree -r --name-only "$NEW_COMMIT" -- plugins/installed > "$new_list"
  if [[ -s "$old_list" ]]; then
    git archive "$OLD_COMMIT" plugins/installed | tar -x -C "$old_tree"
  fi
  if [[ -s "$new_list" ]]; then
    git archive "$NEW_COMMIT" plugins/installed | tar -x -C "$stage"
  fi
  PLUGIN_ROLLBACK_STAGE="$stage"
  PLUGIN_SYNC_ACTIVE=1

  while IFS= read -r path; do
    [[ "$path" == plugins/installed/* && "$path" != *".."* ]] || continue
    if ! grep -Fxq "$path" "$new_list"; then
      target="/app/$path"
      docker compose exec -T web python -c \
        'from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)' \
        "$target"
    fi
  done < "$old_list"

  while IFS= read -r path; do
    [[ "$path" == plugins/installed/* && "$path" != *".."* ]] || continue
    target="/app/$path"
    docker compose exec -T web python -c \
      'from pathlib import Path; import sys; Path(sys.argv[1]).parent.mkdir(parents=True, exist_ok=True)' \
      "$target"
    docker compose cp "$stage/$path" "web:$target"
  done < "$new_list"
  ok "Git 跟踪插件已同步到持久卷；运行期配置与其它已安装插件保持不动"
}

schedule_updater_handoff() {
  local target_image="$1" project handoff_log handoff_active handoff_active_tmp
  local updater_id old_image job_id web_id handoff_web_image
  job_id="${TELEPILOT_UPDATE_JOB_ID:-}"
  [[ "$job_id" =~ ^[0-9a-f]{12}$ ]] || {
    err "内部 updater handoff 缺少合法任务 ID"
    return 1
  }
  project="$(compose_project_name)"
  handoff_log="$(git rev-parse --git-path telepilot-updater-handoff.log)"
  handoff_active="$(git rev-parse --git-path telepilot-updater-handoff-active)"
  rm -f "$handoff_log"
  updater_id="$(docker compose ps -q updater 2>/dev/null || true)"
  [[ -n "$updater_id" ]] || {
    err "updater 容器不存在，无法安排自更新 handoff"
    return 1
  }
  old_image="$(docker inspect --format '{{.Config.Image}}' "$updater_id")"
  [[ -n "$old_image" ]] || {
    err "无法读取 updater 当前镜像引用"
    return 1
  }
  web_id="$(docker compose ps -q web 2>/dev/null || true)"
  [[ -n "$web_id" ]] || {
    err "web 容器不存在，无法完成 updater token 交接"
    return 1
  }
  handoff_web_image="$(docker inspect --format '{{.Config.Image}}' "$web_id")"
  [[ -n "$handoff_web_image" ]] || {
    err "无法读取 handoff 时的 web 镜像引用"
    return 1
  }

  handoff_active_tmp="${handoff_active}.tmp.$$"
  printf '%s %s\n' "$job_id" "$(date +%s)" > "$handoff_active_tmp"
  mv "$handoff_active_tmp" "$handoff_active"

  if ! env -u UPDATER_TOKEN TELEPILOT_UPDATER_IMAGE="$target_image" \
    docker compose run -d --rm --no-deps \
    -e COMPOSE_PROJECT_NAME="$project" \
    -e TELEPILOT_HOST_PROJECT_DIR="$TELEPILOT_HOST_PROJECT_DIR" \
    -e TELEPILOT_TARGET_UPDATER_IMAGE="$target_image" \
    -e TELEPILOT_OLD_UPDATER_IMAGE="$old_image" \
    -e TELEPILOT_UPDATE_JOB_ID="$job_id" \
    -e TELEPILOT_TARGET_COMMIT="$NEW_COMMIT" \
    -e TELEPILOT_HANDOFF_WEB_IMAGE="$handoff_web_image" \
    -e TELEPILOT_TOKEN_ROTATION_REQUIRED="$TOKEN_ROTATION_REQUIRED" \
    --entrypoint bash updater -lc \
    'set -euo pipefail
     sleep 8
     source scripts/_lib.sh
     log_file="$(git rev-parse --git-path telepilot-updater-handoff.log)"
     pending_file="$(git rev-parse --git-path telepilot-deploy-pending)"
     active_file="$(git rev-parse --git-path telepilot-updater-handoff-active)"
     finalize() {
       python3 scripts/finalize-update-job.py \
         --root . \
         --job-id "$TELEPILOT_UPDATE_JOB_ID" \
         --status "$1" \
         --detail "$2" \
         --commit "$TELEPILOT_TARGET_COMMIT" >>"$log_file" 2>&1
     }
     rollback() {
       printf "handoff rollback -> %s\n" "$TELEPILOT_OLD_UPDATER_IMAGE" >>"$log_file"
       env -u UPDATER_TOKEN TELEPILOT_UPDATER_IMAGE="$TELEPILOT_OLD_UPDATER_IMAGE" \
         docker compose up -d --no-build --no-deps --force-recreate updater >>"$log_file" 2>&1 || true
       wait_compose_healthy docker-compose.yml updater 60 >>"$log_file" 2>&1 || true
       if [[ "$TELEPILOT_TOKEN_ROTATION_REQUIRED" == "1" ]]; then
         env -u UPDATER_TOKEN TELEPILOT_WEB_IMAGE="$TELEPILOT_HANDOFF_WEB_IMAGE" \
           docker compose up -d --no-build --no-deps --force-recreate web >>"$log_file" 2>&1 || true
         wait_compose_healthy docker-compose.yml web 120 >>"$log_file" 2>&1 || true
       fi
       finalize failed "updater handoff 失败，已尝试恢复旧镜像" || true
       rm -f "$active_file"
     }
     if ! env -u UPDATER_TOKEN TELEPILOT_UPDATER_IMAGE="$TELEPILOT_TARGET_UPDATER_IMAGE" \
       docker compose up -d --no-build --no-deps --force-recreate updater >"$log_file" 2>&1; then
       rollback
       exit 1
     fi
     if ! wait_compose_healthy docker-compose.yml updater 60 >>"$log_file" 2>&1; then
       rollback
       exit 1
     fi
     if [[ "$TELEPILOT_TOKEN_ROTATION_REQUIRED" == "1" ]]; then
       if ! env -u UPDATER_TOKEN TELEPILOT_WEB_IMAGE="$TELEPILOT_HANDOFF_WEB_IMAGE" \
         docker compose up -d --no-build --no-deps --force-recreate web >>"$log_file" 2>&1; then
         rollback
         exit 1
       fi
       if ! wait_compose_healthy docker-compose.yml web 120 >>"$log_file" 2>&1; then
         rollback
         exit 1
       fi
     fi
     if ! set_env_value .env TELEPILOT_UPDATER_IMAGE "$TELEPILOT_TARGET_UPDATER_IMAGE"; then
       rollback
       exit 1
     fi
     printf "handoff succeeded\n" >>"$log_file"
     finalized=0
     for _ in 1 2 3; do
       if finalize succeeded "所有计划步骤与 updater handoff 已完成"; then
         finalized=1
         break
       fi
       sleep 1
     done
     if [[ "$finalized" != "1" ]]; then
       printf "handoff job finalize failed; pending preserved\n" >>"$log_file"
       exit 1
     fi
     rm -f "$pending_file"
     rm -f "$active_file"' \
    >/dev/null; then
    rm -f "$handoff_active"
    err "无法启动独立 updater handoff 容器"
    return 1
  fi
  HANDOFF_SCHEDULED=1
}

if (( NEEDS_FULL == 1 )); then
  if [[ "${TELEPILOT_SKIP_UPDATER_RECREATE:-0}" == "1" ]]; then
    warn "当前由内部 updater 执行完整更新，业务服务完成后由临时 handoff 容器重建 updater。"
    log "切换预构建业务镜像（仅显式指定 postgres / redis / web / frontend）"
    emit_progress 30 "切换镜像" "切换预构建业务服务"
    docker compose up -d --no-build --no-deps postgres redis
    wait_compose_healthy docker-compose.yml postgres 60 || {
      docker compose logs --tail=80 postgres >&2
      exit 1
    }
    wait_compose_healthy docker-compose.yml redis 30 || {
      docker compose logs --tail=80 redis >&2
      exit 1
    }
    if [[ -n "$RUNNING_UPDATER_TOKEN" && "$RUNNING_UPDATER_TOKEN" != "$PERSISTED_UPDATER_TOKEN" ]]; then
      # 过渡阶段先让新 web 与仍在运行的旧 updater 使用同一 token；handoff
      # 随后会用 .env 中的新 token 一起切换两者。
      export UPDATER_TOKEN="$RUNNING_UPDATER_TOKEN"
    fi
    switch_prebuilt_service web "$TARGET_WEB_IMAGE" TELEPILOT_WEB_IMAGE 120
    switch_prebuilt_service frontend "$TARGET_FRONTEND_IMAGE" TELEPILOT_FRONTEND_IMAGE 60
    emit_progress 78 "健康检查" "业务服务已完成镜像切换"
    emit_progress 92 "服务就绪" "业务服务已通过健康检查"
    log "业务服务已完成，待运行时内容同步后再交接 updater"
    NEEDS_UPDATER_HANDOFF=1
    ok "完整业务更新完成；updater handoff 待安排"
  else
    log "从宿主机执行完整服务级镜像切换"
    docker compose up -d --no-build --no-deps postgres redis
    wait_compose_healthy docker-compose.yml postgres 60
    wait_compose_healthy docker-compose.yml redis 30
    switch_prebuilt_service web "$TARGET_WEB_IMAGE" TELEPILOT_WEB_IMAGE 120
    switch_prebuilt_service frontend "$TARGET_FRONTEND_IMAGE" TELEPILOT_FRONTEND_IMAGE 60
    switch_prebuilt_service updater "$TARGET_UPDATER_IMAGE" TELEPILOT_UPDATER_IMAGE 60
  fi
elif (( DOCS_ONLY == 1 )); then
  ok "无需重建服务，更新完成"
else
  if (( NEEDS_BACKEND_REBUILD == 1 || NEEDS_FRONTEND_REBUILD == 1 )); then
    emit_progress 30 "切换镜像" "拉取结果已校验，切换受影响服务"
    log "更新计划服务集合：${PLAN_SERVICES[*]:-none}"
  fi
  if (( NEEDS_BACKEND_REBUILD == 1 )); then
    switch_prebuilt_service web "$TARGET_WEB_IMAGE" TELEPILOT_WEB_IMAGE 120
  fi
  if (( NEEDS_FRONTEND_REBUILD == 1 )); then
    switch_prebuilt_service frontend "$TARGET_FRONTEND_IMAGE" TELEPILOT_FRONTEND_IMAGE 60
  fi

  if (( NEEDS_PLUGIN_SYNC == 1 )); then
    emit_progress 52 "同步插件" "只覆盖 Git 跟踪的插件文件"
    sync_tracked_plugins
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
    if [[ "${TELEPILOT_SKIP_UPDATER_RECREATE:-0}" == "1" ]]; then
      log "由独立 handoff 容器接管 updater 自更新"
      NEEDS_UPDATER_HANDOFF=1
    else
      log "从宿主机直接切换 updater 预构建镜像"
      switch_prebuilt_service updater "$TARGET_UPDATER_IMAGE" TELEPILOT_UPDATER_IMAGE 60
    fi
  fi

  ok "增量更新完成"
fi

# 文档和 CHANGELOG 只同步公开文件，不重建或重启任何服务。放在业务服务
# 健康检查之后，避免失败部署把运行时说明提前切到未成功上线的 commit。
"$SCRIPT_DIR/sync-runtime-content.sh"

if [[ -n "$WEB_SYNC_IMAGE_REF" ]]; then
  set_env_value .env TELEPILOT_WEB_IMAGE "$WEB_SYNC_IMAGE_REF"
fi
persist_switched_services

if (( TOKEN_ROTATION_HOST_RECREATE == 1 )); then
  token_web_id="$(docker compose ps -q web 2>/dev/null || true)"
  token_updater_id="$(docker compose ps -q updater 2>/dev/null || true)"
  [[ -n "$token_web_id" && -n "$token_updater_id" ]] \
    || die "token 轮换需要运行中的 web 与 updater 容器"
  token_web_image="$(docker inspect --format '{{.Config.Image}}' "$token_web_id")"
  token_updater_image="$(docker inspect --format '{{.Config.Image}}' "$token_updater_id")"
  env -u UPDATER_TOKEN \
    TELEPILOT_WEB_IMAGE="$token_web_image" \
    TELEPILOT_UPDATER_IMAGE="$token_updater_image" \
    docker compose up -d --no-build --no-deps --force-recreate updater web
  wait_compose_healthy docker-compose.yml updater 60
  wait_compose_healthy docker-compose.yml web 120
fi

if (( NEEDS_UPDATER_HANDOFF == 1 )); then
  emit_progress 96 "更新更新器" "启动独立 updater handoff"
  schedule_updater_handoff "$TARGET_UPDATER_IMAGE"
fi

if (( PLUGIN_SYNC_ACTIVE == 1 )); then
  rm -rf "$PLUGIN_ROLLBACK_STAGE"
  PLUGIN_SYNC_ACTIVE=0
  PLUGIN_ROLLBACK_STAGE=""
fi

if (( HANDOFF_SCHEDULED == 1 )); then
  emit_progress 98 "等待交接" "新 updater 将在健康后收尾当前任务"
  ok "业务服务与运行时内容已就绪，等待独立 updater handoff 给出最终结果"
  while :; do
    sleep 30
  done
fi

WEB_SYNC_ACTIVE=0
rm -f "$PENDING_FILE"

echo
emit_progress 100 "更新完成" "所有计划步骤已完成"
ok "TelePilot 已更新到 ${NEW_COMMIT:0:12}"

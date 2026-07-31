#!/usr/bin/env bash
# 一键生产部署：纯 docker compose
#   - 检查 .env（密钥必须就位）
#   - 默认拉取并启动全部 5 个容器（postgres / redis / web / updater / frontend）
#   - 等待 web 健康检查通过
#   - 打印访问地址
#
# 这是"生产/演示"模式：所有东西容器化，不依赖宿主机的 venv / pnpm。
# 默认对外暴露 80 端口（可在 .env 设 WEB_PORT_PUBLISH=8080 等）。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_lib.sh"
cd "$ROOT_DIR"
export TELEPILOT_HOST_PROJECT_DIR="${TELEPILOT_HOST_PROJECT_DIR:-$ROOT_DIR}"
[[ "$TELEPILOT_HOST_PROJECT_DIR" == /* ]] \
  || die "TELEPILOT_HOST_PROJECT_DIR 必须是宿主机绝对路径，拒绝解析相对挂载目录"

SOURCE_BUILD="${TELEPILOT_SOURCE_BUILD:-0}"

usage() {
  cat <<'EOF'
用法：scripts/prod-up.sh [--source-build]

默认从 GHCR 拉取预构建镜像，不在服务器编译。
  --source-build  维护者救援模式：使用当前工作树在本机重新构建应用镜像
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-build)
      SOURCE_BUILD=1
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

# ── 1. 依赖与 .env ────────────────────────────────────────
need_cmd docker "macOS Docker Desktop / Linux docker.io"
docker info >/dev/null 2>&1 || die "docker 守护进程未启动"
docker compose version >/dev/null 2>&1 || die "缺 docker compose v2 插件"

if [[ ! -f .env ]]; then
  warn ".env 不存在，自动调 bootstrap.sh 生成（需要 python3.12）"
  "$SCRIPT_DIR/bootstrap.sh"
fi

# 强制 .env 权限 600（生产环境绝不能让其它本机用户读到）
chmod 600 .env 2>/dev/null || true

# 校验密钥已替换
if grep -qE '^(MASTER_KEY|JWT_SECRET)=(changeme.*)?$' .env; then
  err ".env 中 MASTER_KEY / JWT_SECRET 还是占位值！"
  echo "  自动重新生成（不会动其它字段）..."
  if command -v python3.12 >/dev/null 2>&1; then
    PY=python3.12
  elif [[ -x backend/.venv/bin/python ]]; then
    PY=backend/.venv/bin/python
  else
    die "需要 python3.12 或已存在的 backend/.venv 来生成密钥"
  fi
  MK="$($PY -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  JS="$($PY -c 'import secrets; print(secrets.token_urlsafe(64))')"
  $PY - "$MK" "$JS" <<'PY'
import sys, pathlib, re
mk, js = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
text = p.read_text()
text = re.sub(r'^MASTER_KEY=(changeme.*)?$', f'MASTER_KEY={mk}', text, flags=re.MULTILINE)
text = re.sub(r'^JWT_SECRET=(changeme.*)?$', f'JWT_SECRET={js}', text, flags=re.MULTILINE)
p.write_text(text)
PY
  ok ".env 密钥已生成"
fi

# updater 拥有宿主机 Docker socket 控制能力，必须使用独立于 Web JWT 的随机密钥。
ensure_updater_token_env .env

# 非占位但格式无效的 MASTER_KEY 不能自动替换，否则已有密文会永久不可解。
_secret_py="$(command -v python3.12 || true)"
[[ -n "$_secret_py" ]] || _secret_py="$(command -v python3 || true)"
[[ -n "$_secret_py" ]] || [[ ! -x backend/.venv/bin/python ]] || _secret_py=backend/.venv/bin/python
[[ -n "$_secret_py" ]] || die "需要 Python 校验生产密钥"
"$_secret_py" - .env <<'PY'
import base64
import pathlib
import sys

values = {}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
try:
    master_key = values.get("MASTER_KEY", "").encode("ascii")
    if len(base64.urlsafe_b64decode(master_key)) != 32:
        raise ValueError("解码后长度不是 32 字节")
except Exception as exc:
    raise SystemExit(f"MASTER_KEY 不是合法 Fernet key：{exc}") from None
if len(values.get("JWT_SECRET", "")) < 32:
    raise SystemExit("JWT_SECRET 长度不足 32 字符")
if values.get("UPDATER_TOKEN") == values.get("JWT_SECRET"):
    raise SystemExit("UPDATER_TOKEN 不得与 JWT_SECRET 复用")
PY

# 校验 Postgres 密码不是默认弱密码（仅生产强制）
_pg_pwd="$(grep -E '^POSTGRES_PASSWORD=' .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' "' || true)"
_pg_user="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' "' || true)"
_pg_user="${_pg_user:-telebot}"
_pg_db="$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' "' || true)"
_pg_db="${_pg_db:-telebot}"
_db_url="$(grep -E '^DATABASE_URL=' .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' "' || true)"

# 弱密码黑名单（小写比较）
_weak_list=("telepilot" "telebot" "postgres" "password" "changeme" "admin" "123456" "root" "")
_pwd_lc="$(printf '%s' "$_pg_pwd" | tr '[:upper:]' '[:lower:]')"
for w in "${_weak_list[@]}"; do
  if [[ "$_pwd_lc" == "$w" ]]; then
    err "POSTGRES_PASSWORD 是弱密码 / 默认值（'$_pg_pwd'），生产环境拒绝启动！"
    echo "  请在 .env 中设置一个强密码，例如："
    echo "    POSTGRES_PASSWORD=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || echo "$(date +%s)$RANDOM")"
    echo "  并同步更新 DATABASE_URL（用户名/密码段必须一致）。"
    die "拒绝以默认/弱密码启动生产 Postgres"
  fi
done

# 校验 DATABASE_URL 与 POSTGRES_USER/PASSWORD/DB 一致（避免 web 连不上）
#
# 走 Python urlparse 拆解，避免 substring 在密码含 @ : / + URL-encode 时假阴。
# 校验 user / pwd / host / port / dbname 五段：
#   - user / dbname 直接比对
#   - pwd 用 urlencode 后再比（密码含特殊字符时合法）
#   - host 必须是 'postgres'（compose 内部服务名），port 5432
if [[ -n "$_db_url" ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    _PY=python3.12
  elif [[ -x backend/.venv/bin/python ]]; then
    _PY=backend/.venv/bin/python
  else
    _PY=python3
  fi
  if ! "$_PY" - "$_db_url" "$_pg_user" "$_pg_pwd" "${_pg_db:-telebot}" <<'PY'
import sys
from urllib.parse import urlparse, unquote, quote
url, want_user, want_pwd, want_db = sys.argv[1:5]
# asyncpg DSN 形如 postgresql+asyncpg://user:pwd@host:port/db
u = urlparse(url)
got_user = unquote(u.username or "")
got_pwd  = unquote(u.password or "")
got_host = u.hostname or ""
got_port = u.port or 5432
got_db   = (u.path or "").lstrip("/")
problems = []
if got_user != want_user:
    problems.append(f"user 不一致: DSN={got_user!r} vs POSTGRES_USER={want_user!r}")
if got_pwd != want_pwd:
    problems.append(f"password 不一致 (urldecode 后)；密码含特殊字符时记得 urlencode 写到 DSN")
if got_host not in ("postgres", "localhost", "127.0.0.1"):
    problems.append(f"host={got_host!r}，compose 部署应是 'postgres'")
if int(got_port) != 5432:
    problems.append(f"port={got_port}，期望 5432")
if got_db != want_db:
    problems.append(f"db 不一致: DSN={got_db!r} vs POSTGRES_DB={want_db!r}")
if problems:
    print("DATABASE_URL 校验失败：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print(f"  pwd urlencode 形式: {quote(want_pwd, safe='')}", file=sys.stderr)
    sys.exit(1)
PY
  then
    die "请修正 .env 中 DATABASE_URL 段位"
  fi
fi

ok "Postgres 凭据校验通过（用户=$_pg_user）"

# ── 1.5 自适应内存档位：tiny / small / large ────────────────────
# 仅在 .env 没有 ``MEMORY_TIER=`` 时注入；用户后续可任意修改或改 manual 禁用。
auto_tune_env .env

# ── 2. 拉取并启动 ────────────────────────────────────────
# 先创建 bind mount 目录，内容在所有服务健康后再发布。
mkdir -p "$ROOT_DIR/.run/runtime-content/docs"
if (( SOURCE_BUILD == 1 )); then
  warn "使用维护者救援模式：服务器将本地构建应用镜像"
  docker compose up -d --build
else
  log "解析并校验 TelePilot 预构建镜像集合"
  mapfile -t app_images < <(
    docker compose config --format json | python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
for name in ("web", "frontend", "updater"):
    print(services[name]["image"])
'
  )
  (( ${#app_images[@]} == 3 )) || die "无法解析 web/frontend/updater 镜像引用"
  expected_revision="$(
    git log -1 --format=%H HEAD -- . \
      ':(exclude)docs/**' \
      ':(exclude,glob)**/*.md' \
      ':(exclude,glob)**/*.rst' \
      ':(exclude,glob)**/*.txt' \
      ':(exclude)LICENSE'
  )"
  [[ -n "$expected_revision" ]] || die "无法计算当前 checkout 的镜像检查点"
  expected_source="${TELEPILOT_IMAGE_SOURCE:-https://github.com/Anoyou/Telebot}"
  resolved_images=()
  revisions=()
  for index in 0 1 2; do
    image_ref="${app_images[$index]}"
    docker pull "$image_ref" >/dev/null \
      || die "预构建镜像拉取失败：$image_ref"
    image_revision="$(docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "$image_ref" 2>/dev/null || true)"
    image_source="$(docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.source" }}' \
      "$image_ref" 2>/dev/null || true)"
    digest_ref="$(docker image inspect --format '{{ index .RepoDigests 0 }}' "$image_ref" 2>/dev/null || true)"
    [[ "$image_revision" == "$expected_revision" ]] \
      || die "镜像 revision 与当前 checkout 不一致：$image_ref"
    [[ "$image_source" == "$expected_source" ]] \
      || die "镜像 source 不受信：$image_ref"
    [[ "$digest_ref" == *@sha256:* ]] \
      || die "镜像缺少 registry digest：$image_ref"
    verify_image_attestation "$digest_ref" "$image_revision" \
      || die "镜像缺少受信 GitHub Actions 构建来源证明：$image_ref"
    revisions+=("$image_revision")
    resolved_images+=("$digest_ref")
  done
  [[ "${revisions[0]}" == "${revisions[1]}" && "${revisions[1]}" == "${revisions[2]}" ]] \
    || die "三项应用镜像不是同一 commit，拒绝混装"
  TELEPILOT_WEB_IMAGE="${resolved_images[0]}"
  TELEPILOT_FRONTEND_IMAGE="${resolved_images[1]}"
  TELEPILOT_UPDATER_IMAGE="${resolved_images[2]}"
  export TELEPILOT_WEB_IMAGE TELEPILOT_FRONTEND_IMAGE TELEPILOT_UPDATER_IMAGE
  log "启动生产容器（服务器不执行编译）"
  docker compose up -d --no-build
fi

# ── 3. 等待健康 ──────────────────────────────────────────
log "等待 web 容器健康"
wait_compose_healthy docker-compose.yml postgres 60 || die "postgres 不健康"
wait_compose_healthy docker-compose.yml redis 30    || die "redis 不健康"

# web 容器内 alembic upgrade 跑完才会健康，所以这里等更久
i=0
while (( i < 120 )); do
  state="$(docker compose ps --format json web 2>/dev/null \
            | python3 -c "import sys,json;
data = sys.stdin.read().strip()
if data.startswith('['):
    arr = json.loads(data)
else:
    arr = [json.loads(l) for l in data.splitlines() if l.strip()]
print((arr[0].get('Health') or arr[0].get('State') or 'unknown') if arr else 'missing')" 2>/dev/null)"
  if [[ "$state" == "healthy" ]]; then break; fi
  sleep 1
  i=$((i + 1))
done
if [[ "$state" != "healthy" ]]; then
  err "web 在 120s 内未健康，最近日志："
  docker compose logs --tail=60 web >&2
  exit 1
fi
ok "web 容器健康"
wait_compose_healthy docker-compose.yml updater 60 || die "updater 不健康"
wait_compose_healthy docker-compose.yml frontend 60 || die "frontend 不健康"

# 所有服务成功后再切换公开文档与 revision，避免失败启动提前发布新状态。
"$SCRIPT_DIR/sync-runtime-content.sh"

PUBLISH_PORT="$(grep -E '^WEB_PORT_PUBLISH=' .env 2>/dev/null | cut -d= -f2 | tr -d ' "' || true)"
PUBLISH_PORT="${PUBLISH_PORT:-80}"

echo
ok "生产栈已就绪 ${C_GRN}🎉${C_RST}"
echo
printf '  访问  ${C_BLU}http://localhost:%s${C_RST}\n' "$PUBLISH_PORT"
echo
dim "  实时日志：docker compose logs -f"
dim "  停止栈：  make prod-down"
dim "  完整重建：make prod-down && make prod-up"

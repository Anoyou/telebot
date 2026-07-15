#!/usr/bin/env bash
# 服务器开箱部署入口：
#   curl -fsSL https://raw.githubusercontent.com/Anoyou/telebot/main/scripts/install-server.sh | bash
#
# 目标：SSH 到一台干净的 Debian / Ubuntu VPS 后，一条命令完成：
#   - 安装基础依赖与 Docker Compose v2
#   - 拉取 TelePilot 仓库
#   - 生成生产可用 .env（强随机 MASTER_KEY / JWT_SECRET / UPDATER_TOKEN / POSTGRES_PASSWORD）
#   - 调用 make prod-up 启动 postgres / redis / web / frontend
#
# 可选环境变量：
#   TELEPILOT_REPO=https://github.com/Anoyou/telebot.git
#   TELEPILOT_BRANCH=main
#   TELEPILOT_DIR=/opt/telepilot
#   WEB_PORT_PUBLISH=80          # 支持 8080 或 127.0.0.1:8080；端口占用时自动递增
#   COOKIE_SECURE=false

set -euo pipefail

if [[ -t 1 ]]; then
  C_RED=$'\033[0;31m'; C_GRN=$'\033[0;32m'; C_YEL=$'\033[0;33m'
  C_BLU=$'\033[0;34m'; C_DIM=$'\033[2m';   C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YEL=''; C_BLU=''; C_DIM=''; C_RST=''
fi

log()  { printf '%b\n' "${C_BLU}▸${C_RST} $*"; }
ok()   { printf '%b\n' "${C_GRN}✓${C_RST} $*"; }
warn() { printf '%b\n' "${C_YEL}!${C_RST} $*" >&2; }
err()  { printf '%b\n' "${C_RED}✗${C_RST} $*" >&2; }
die()  { err "$*"; exit 1; }
dim()  { printf '%b\n' "${C_DIM}$*${C_RST}"; }

TELEPILOT_REPO="${TELEPILOT_REPO:-https://github.com/Anoyou/telebot.git}"
TELEPILOT_BRANCH="${TELEPILOT_BRANCH:-}"
TELEPILOT_DIR="${TELEPILOT_DIR:-/opt/telepilot}"
WEB_PORT_PUBLISH="${WEB_PORT_PUBLISH:-80}"
COOKIE_SECURE="${COOKIE_SECURE:-false}"

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  command -v sudo >/dev/null 2>&1 || die "当前不是 root，且系统没有 sudo；请用 root 用户执行，或先安装 sudo。"
  SUDO=(sudo)
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

apt_install() {
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

ensure_base_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    log "安装基础依赖"
    "${SUDO[@]}" apt-get update
    apt_install ca-certificates curl git gnupg make python3
    ok "基础依赖就绪"
    return 0
  fi

  need_cmd curl
  need_cmd git
  need_cmd make
  need_cmd python3
  warn "当前系统不是 apt 系发行版，跳过自动安装基础依赖。"
}

docker_repo_id() {
  local id="" id_like="" ubuntu_codename="" version_codename=""
  # shellcheck disable=SC1091
  . /etc/os-release
  id="${ID:-}"
  id_like="${ID_LIKE:-}"
  ubuntu_codename="${UBUNTU_CODENAME:-}"
  version_codename="${VERSION_CODENAME:-}"

  case "$id" in
    ubuntu|debian)
      printf '%s:%s\n' "$id" "$version_codename"
      ;;
    *)
      if [[ "$id_like" == *ubuntu* && -n "$ubuntu_codename" ]]; then
        printf 'ubuntu:%s\n' "$ubuntu_codename"
      elif [[ "$id_like" == *debian* && -n "$version_codename" ]]; then
        printf 'debian:%s\n' "$version_codename"
      else
        return 1
      fi
      ;;
  esac
}

install_docker_from_official_repo() {
  command -v apt-get >/dev/null 2>&1 || return 1

  local repo_meta repo_id codename arch
  repo_meta="$(docker_repo_id)" || return 1
  repo_id="${repo_meta%%:*}"
  codename="${repo_meta#*:}"
  [[ -n "$repo_id" && -n "$codename" ]] || return 1

  log "安装 Docker Engine 与 Docker Compose v2"
  "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL "https://download.docker.com/linux/${repo_id}/gpg" \
      | "${SUDO[@]}" gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.gpg
  fi

  arch="$(dpkg --print-architecture)"
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/%s %s stable\n' \
    "$arch" "$repo_id" "$codename" \
    | "${SUDO[@]}" tee /etc/apt/sources.list.d/docker.list >/dev/null

  "${SUDO[@]}" apt-get update
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_docker_from_distro_repo() {
  command -v apt-get >/dev/null 2>&1 || return 1

  warn "Docker 官方源安装失败，尝试发行版仓库里的 docker.io。"
  "${SUDO[@]}" apt-get update
  apt_install docker.io
  apt_install docker-compose-plugin || apt_install docker-compose-v2
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker Compose v2 已存在"
  else
    install_docker_from_official_repo || install_docker_from_distro_repo || die "无法自动安装 Docker Compose v2，请手动安装后重试。"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    "${SUDO[@]}" systemctl enable --now docker >/dev/null 2>&1 || true
  else
    "${SUDO[@]}" service docker start >/dev/null 2>&1 || true
  fi

  if docker info >/dev/null 2>&1; then
    DOCKER_WITH_SUDO=false
  elif [[ "${#SUDO[@]}" -gt 0 ]] && "${SUDO[@]}" docker info >/dev/null 2>&1; then
    DOCKER_WITH_SUDO=true
    warn "当前用户暂时不能直接访问 Docker，本次会用 sudo 启动；重新登录后通常可直接使用 docker。"
  else
    die "Docker 守护进程未就绪。"
  fi

  if [[ "$DOCKER_WITH_SUDO" == false ]]; then
    docker compose version >/dev/null 2>&1 || die "缺少 Docker Compose v2。"
  else
    "${SUDO[@]}" docker compose version >/dev/null 2>&1 || die "缺少 Docker Compose v2。"
  fi
  ok "Docker 就绪"
}

prepare_install_dir() {
  local parent
  parent="$(dirname "$TELEPILOT_DIR")"
  if [[ ! -d "$parent" ]]; then
    "${SUDO[@]}" mkdir -p "$parent"
  fi

  if [[ ! -e "$TELEPILOT_DIR" ]]; then
    "${SUDO[@]}" mkdir -p "$TELEPILOT_DIR"
    if [[ "${#SUDO[@]}" -gt 0 ]]; then
      "${SUDO[@]}" chown "$(id -u):$(id -g)" "$TELEPILOT_DIR"
    fi
  fi
}

sync_repo() {
  prepare_install_dir

  if [[ -d "$TELEPILOT_DIR/.git" ]]; then
    log "更新已有仓库：$TELEPILOT_DIR"
    git -C "$TELEPILOT_DIR" fetch --prune
    if [[ -n "$TELEPILOT_BRANCH" ]]; then
      git -C "$TELEPILOT_DIR" checkout "$TELEPILOT_BRANCH"
      git -C "$TELEPILOT_DIR" pull --ff-only origin "$TELEPILOT_BRANCH"
    else
      git -C "$TELEPILOT_DIR" pull --ff-only
    fi
    ok "仓库已更新"
    return 0
  fi

  if find "$TELEPILOT_DIR" -mindepth 1 -maxdepth 1 | read -r _; then
    die "$TELEPILOT_DIR 已存在但不是 Git 仓库；请换 TELEPILOT_DIR 或手动清理。"
  fi

  log "拉取 TelePilot 仓库"
  if [[ -n "$TELEPILOT_BRANCH" ]]; then
    git clone --depth 1 --branch "$TELEPILOT_BRANCH" "$TELEPILOT_REPO" "$TELEPILOT_DIR"
  else
    git clone --depth 1 "$TELEPILOT_REPO" "$TELEPILOT_DIR"
  fi
  ok "仓库已拉取到 $TELEPILOT_DIR"
}

random_fernet_key() {
  python3 - <<'PY'
import base64
import os
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
}

random_token() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(64))
PY
}

random_password() {
  python3 - <<'PY'
import secrets
import string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(32)))
PY
}

split_publish_host_port() {
  local publish="$1"
  if [[ "$publish" == *":"* ]]; then
    printf '%s\n' "${publish%:*}" "${publish##*:}"
  else
    printf '%s\n' "" "$publish"
  fi
}

join_publish_host_port() {
  local host="$1" port="$2"
  if [[ -n "$host" ]]; then
    printf '%s:%s\n' "$host" "$port"
  else
    printf '%s\n' "$port"
  fi
}

is_publish_port_free() {
  local publish="$1"
  python3 - "$publish" <<'PY'
import errno
import socket
import sys

raw = sys.argv[1].strip()
host = ""
port_text = raw
if ":" in raw:
    host, port_text = raw.rsplit(":", 1)
try:
    port = int(port_text)
except ValueError:
    sys.exit(2)
if not 1 <= port <= 65535:
    sys.exit(2)

bind_host = host or "0.0.0.0"
try:
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_host, port))
except OSError as exc:
    if exc.errno == errno.EACCES:
        probe_host = "127.0.0.1" if bind_host in ("", "0.0.0.0") else bind_host
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            if probe.connect_ex((probe_host, port)) != 0:
                sys.exit(0)
    sys.exit(1)
sys.exit(0)
PY
}

pick_publish_port() {
  local host port start candidate
  mapfile -t _publish_parts < <(split_publish_host_port "$WEB_PORT_PUBLISH")
  host="${_publish_parts[0]}"
  port="${_publish_parts[1]}"
  [[ "$port" =~ ^[0-9]+$ ]] || die "WEB_PORT_PUBLISH 端口无效：$WEB_PORT_PUBLISH"
  (( port >= 1 && port <= 65535 )) || die "WEB_PORT_PUBLISH 端口范围必须是 1-65535：$WEB_PORT_PUBLISH"

  candidate="$(join_publish_host_port "$host" "$port")"
  if is_publish_port_free "$candidate"; then
    WEB_PORT_PUBLISH="$candidate"
    return 0
  fi

  warn "端口 $candidate 已被占用，正在自动寻找可用端口。"
  if [[ "$port" == "80" ]]; then
    start=8080
  else
    start=$((port + 1))
  fi

  for (( port = start; port <= 65535; port++ )); do
    candidate="$(join_publish_host_port "$host" "$port")"
    if is_publish_port_free "$candidate"; then
      WEB_PORT_PUBLISH="$candidate"
      ok "已改用可用端口：$WEB_PORT_PUBLISH"
      return 0
    fi
  done

  die "未找到可用端口，请手动设置 WEB_PORT_PUBLISH 后重试。"
}

create_env() {
  cd "$TELEPILOT_DIR"
  if [[ -f .env ]]; then
    ok ".env 已存在，保持现有配置"
    chmod 600 .env 2>/dev/null || true
    return 0
  fi

  log "生成生产 .env"
  cp .env.example .env

  local master_key jwt_secret updater_token pg_password pg_password_url
  master_key="$(random_fernet_key)"
  jwt_secret="$(random_token)"
  updater_token="$(random_token)"
  pg_password="$(random_password)"
  pg_password_url="$(
    python3 - "$pg_password" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=""))
PY
  )"

  python3 - "$master_key" "$jwt_secret" "$updater_token" "$pg_password" "$pg_password_url" "$WEB_PORT_PUBLISH" "$COOKIE_SECURE" <<'PY'
import pathlib
import re
import sys

master_key, jwt_secret, updater_token, pg_password, pg_password_url, web_port, cookie_secure = sys.argv[1:8]
p = pathlib.Path(".env")
text = p.read_text()

def put(key: str, value: str) -> None:
    global text
    pattern = rf"^{re.escape(key)}=.*$"
    line = f"{key}={value}"
    if re.search(pattern, text, flags=re.MULTILINE):
        text = re.sub(pattern, line, text, flags=re.MULTILINE)
    else:
        text += f"\n{line}\n"

put("MASTER_KEY", master_key)
put("JWT_SECRET", jwt_secret)
put("UPDATER_TOKEN", updater_token)
put("POSTGRES_PASSWORD", pg_password)
put("DATABASE_URL", f"postgresql+asyncpg://telebot:{pg_password_url}@postgres:5432/telebot")
put("COOKIE_SECURE", cookie_secure.lower())
put("WEB_PORT_PUBLISH", web_port)
p.write_text(text)
PY

  chmod 600 .env 2>/dev/null || true
  ok ".env 已生成（含强随机密钥与数据库密码）"
}

run_prod_up() {
  cd "$TELEPILOT_DIR"
  log "启动 TelePilot 生产栈"
  if [[ "${DOCKER_WITH_SUDO:-false}" == true ]]; then
    "${SUDO[@]}" make prod-up
  else
    make prod-up
  fi
}

print_done() {
  local host_ip port_suffix
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  port_suffix=":${WEB_PORT_PUBLISH}"
  if [[ "$WEB_PORT_PUBLISH" == "80" ]]; then
    port_suffix=""
  fi

  echo
  ok "TelePilot 已部署完成"
  printf '  项目目录：%s\n' "$TELEPILOT_DIR"
  printf '  本机访问：http://localhost%s\n' "$port_suffix"
  if [[ -n "$host_ip" ]]; then
    printf '  服务器访问：http://%s%s\n' "$host_ip" "$port_suffix"
  fi
  echo
  dim "常用命令："
  dim "  cd $TELEPILOT_DIR && make prod-up      # 更新/重建并启动"
  dim "  cd $TELEPILOT_DIR && make prod-down    # 停止"
  dim "  cd $TELEPILOT_DIR && docker compose logs -f"
}

ensure_base_packages
ensure_docker
sync_repo
pick_publish_port
create_env
run_prod_up
print_done

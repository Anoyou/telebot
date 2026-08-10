#!/usr/bin/env bash
# 通用日志/工具函数：scripts/* 共享。
# 用 source 引入：source "$(dirname "$0")/_lib.sh"

set -o pipefail

# 颜色（仅在 TTY 启用，避免污染日志）
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

# 解析仓库根目录（_lib.sh 位于 scripts/ 下）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RUN_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/logs"
BACKEND_PID="$RUN_DIR/backend.pid"
FRONTEND_PID="$RUN_DIR/frontend.pid"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

ensure_dirs() {
  mkdir -p "$RUN_DIR" "$LOG_DIR"
}

is_alive() {
  # 用法：is_alive <pidfile> ；存在且进程存活返 0
  local pf="$1"
  [[ -f "$pf" ]] || return 1
  local pid
  pid="$(cat "$pf" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

stop_pid() {
  # 用法：stop_pid <pidfile> <name>
  local pf="$1" name="$2"
  if ! [[ -f "$pf" ]]; then
    return 0
  fi
  local pid
  pid="$(cat "$pf" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    log "停止 $name (pid=$pid)"
    kill "$pid" 2>/dev/null || true
    # 优雅关闭最多等 8 秒
    for _ in 1 2 3 4 5 6 7 8; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      warn "$name 未在 8 秒内退出，强杀"
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pf"
}

# 等待 HTTP 端点就绪：wait_http <url> <max_seconds> <name>
wait_http() {
  local url="$1" max="${2:-30}" name="${3:-service}"
  local i=0
  while (( i < max )); do
    if curl -fsS -m 2 "$url" >/dev/null 2>&1; then
      ok "$name 就绪 ($url)"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  err "$name 未在 ${max}s 内就绪 ($url)"
  return 1
}

# 等待 docker compose 服务 healthy：wait_compose_healthy <compose_file> <service> <max_seconds>
wait_compose_healthy() {
  local cf="$1" svc="$2" max="${3:-60}"
  local i=0
  while (( i < max )); do
    local state
    state="$(docker compose -f "$cf" ps --format json "$svc" 2>/dev/null \
              | python3 -c "import sys,json
try:
  data = sys.stdin.read().strip()
  if not data:
    print('missing'); sys.exit()
  # 兼容旧版（一行一对象）和新版（数组）
  if data.startswith('['):
    arr = json.loads(data)
  else:
    arr = [json.loads(l) for l in data.splitlines() if l.strip()]
  if not arr:
    print('missing'); sys.exit()
  print(arr[0].get('Health') or arr[0].get('State') or 'unknown')
except Exception:
  print('error')
" 2>/dev/null)"
    if [[ "$state" == "healthy" || "$state" == "running" ]]; then
      ok "compose:$svc → $state"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  err "compose:$svc 在 ${max}s 内未达健康状态"
  return 1
}

# 通用：检查命令是否存在
need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1（$2）"
}

# 兼容 0.87.0-beta.1 之前创建的 updater 容器：在线更新会在旧容器内
# 执行新 checkout 的脚本，因此不能依赖目标 updater 镜像已经包含 gh。
ensure_github_cli() {
  if command -v gh >/dev/null 2>&1 \
    && gh attestation verify --help >/dev/null 2>&1; then
    return 0
  fi

  if command -v apk >/dev/null 2>&1 && [[ "$(id -u)" -eq 0 ]]; then
    log "旧 updater 环境缺少 GitHub CLI，正在安装 attestation 验证能力"
    apk add --no-cache github-cli >/dev/null \
      || die "无法在 updater 容器内安装 GitHub CLI（github-cli）"
  fi

  command -v gh >/dev/null 2>&1 \
    && gh attestation verify --help >/dev/null 2>&1 \
    || die "缺少支持 attestation 的 GitHub CLI（gh）；请重新安装或更新 updater"
}

# 校验 GHCR 镜像由本仓库受信 GitHub Actions workflow 为指定 commit 构建。
# OCI source/revision 标签只做一致性检查；Sigstore/SLSA attestation 才提供
# 不可由普通 package 写入者伪造的构建者身份与源码绑定。
verify_image_attestation() {
  local digest_ref="$1" revision="$2" cli_token
  local repository="${TELEPILOT_ATTESTATION_REPO:-Anoyou/Telebot}"
  local signer="${TELEPILOT_ATTESTATION_SIGNER:-github.com/Anoyou/Telebot/.github/workflows/publish-images.yml}"
  ensure_github_cli
  # `--bundle-from-oci` 不访问 GitHub API，但 gh 仍会在命令启动时要求存在
  # token 才能构造 HTTP client。生产 updater 不应要求人工 gh auth login；
  # 有真实 token 时沿用，否则仅提供初始化占位值。若 gh 未来改为访问 API，
  # 占位值会认证失败并保持 fail-closed。
  cli_token="${GH_TOKEN:-${GITHUB_TOKEN:-telepilot-oci-attestation-verification}}"
  GH_TOKEN="$cli_token" GH_PROMPT_DISABLED=1 \
    gh attestation verify "oci://$digest_ref" \
    --repo "$repository" \
    --signer-workflow "$signer" \
    --source-digest "$revision" \
    --deny-self-hosted-runners \
    --bundle-from-oci \
    >/dev/null
}

# 确保 updater 使用独立随机 token。参数为 env 文件路径，默认 .env。
# 缺失、占位、过短或与 JWT_SECRET 相同时都会替换；不会改动其它密钥。
ensure_updater_token_env() {
  local env_file="${1:-.env}" py current jwt
  [[ -f "$env_file" ]] || die "找不到环境文件：$env_file"
  current="$(grep -E '^UPDATER_TOKEN=' "$env_file" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' "' || true)"
  jwt="$(grep -E '^JWT_SECRET=' "$env_file" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' "' || true)"
  if [[ -n "$current" && "$current" != changeme-* && ${#current} -ge 32 && "$current" != "$jwt" ]]; then
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    py=python3
  elif [[ -x "$ROOT_DIR/backend/.venv/bin/python" ]]; then
    py="$ROOT_DIR/backend/.venv/bin/python"
  else
    die "需要 Python 生成独立 UPDATER_TOKEN"
  fi
  "$py" - "$env_file" <<'PY'
import pathlib
import re
import secrets
import sys

p = pathlib.Path(sys.argv[1])
text = p.read_text()
line = f"UPDATER_TOKEN={secrets.token_urlsafe(64)}"
if re.search(r"^UPDATER_TOKEN=.*$", text, flags=re.MULTILINE):
    text = re.sub(r"^UPDATER_TOKEN=.*$", line, text, flags=re.MULTILINE)
else:
    text = text.rstrip() + "\n" + line + "\n"
p.write_text(text)
PY
  chmod 600 "$env_file" 2>/dev/null || true
  ok "$env_file 已生成独立 UPDATER_TOKEN"
}

# 精确写入一个 .env 键；不会重新格式化或打印其它配置（其中可能含敏感值）。
set_env_value() {
  local env_file="$1" key="$2" value="$3" py
  if command -v python3 >/dev/null 2>&1; then
    py=python3
  elif [[ -x "$ROOT_DIR/backend/.venv/bin/python" ]]; then
    py="$ROOT_DIR/backend/.venv/bin/python"
  else
    die "需要 Python 更新部署镜像引用"
  fi
  "$py" - "$env_file" "$key" "$value" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
text = path.read_text(encoding="utf-8")
line = f"{key}={value}"
pattern = rf"^{re.escape(key)}=.*$"
if re.search(pattern, text, flags=re.MULTILINE):
    text = re.sub(pattern, lambda _: line, text, flags=re.MULTILINE)
else:
    text = text.rstrip() + "\n" + line + "\n"
path.write_text(text, encoding="utf-8")
PY
  chmod 600 "$env_file" 2>/dev/null || true
}

# ════════════════════════════════════════════════════════════
# 自适应内存档位：根据 Docker 可用 RAM 选 tiny / small / large。
# 用法：
#   tier="$(detect_memory_tier)"   # echo: tiny|small|large
#   auto_tune_env .env             # 仅当 .env 中没有 MEMORY_TIER 时注入一段
#
# 用户可在 .env 写 ``MEMORY_TIER=manual``（或任意非 tiny/small/large 的值）
# 来禁用自动注入；之后所有 *_MEM_LIMIT / DB_POOL_SIZE 等都不再被脚本覆盖。
# ════════════════════════════════════════════════════════════

detect_memory_tier() {
  local mem_kb=0
  if command -v docker >/dev/null 2>&1; then
    # macOS + OrbStack / Docker Desktop 场景下，sysctl 读到的是 Mac 宿主机内存，
    # 不是 Docker VM 的内存上限；docker info 的 MemTotal 更贴近 compose 实际可用值。
    local docker_mem_bytes
    docker_mem_bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
    if [[ "$docker_mem_bytes" =~ ^[0-9]+$ ]] && (( docker_mem_bytes > 0 )); then
      mem_kb=$(( docker_mem_bytes / 1024 ))
    fi
  fi
  if (( mem_kb <= 0 )) && [[ -r /proc/meminfo ]]; then
    mem_kb="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
  elif (( mem_kb <= 0 )) && command -v sysctl >/dev/null 2>&1; then
    # macOS：sysctl 输出字节，转 kB
    local b
    b="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    mem_kb=$(( b / 1024 ))
  fi
  if (( mem_kb <= 0 )); then
    # 拿不到内存信息时按 small（中档）兜底，行为最接近原默认
    echo "small"
    return 0
  fi
  # 1.2 GiB ≈ 1258291 kB；2.5 GiB ≈ 2621440 kB
  if (( mem_kb <= 1258291 )); then
    echo "tiny"
  elif (( mem_kb <= 2621440 )); then
    echo "small"
  else
    echo "large"
  fi
}

# 把档位翻译成具体配置块；写到 stdout（被调用方追加到 .env）
_memory_tier_block() {
  local tier="$1"
  case "$tier" in
    tiny)
      cat <<'EOF'
# ── 自适应内存档位：tiny（≤ 1.2 GiB 宿主机）────────────────────
# 删除该 ## auto-tuned 块，或手动改 MEMORY_TIER=manual 即可禁用自动覆盖。
MEMORY_TIER=tiny
WEB_MEM_LIMIT=320m
POSTGRES_MEM_LIMIT=160m
REDIS_MEM_LIMIT=48m
FRONTEND_MEM_LIMIT=24m
DB_POOL_SIZE=2
DB_MAX_OVERFLOW=0
REDIS_MAX_CONNECTIONS=8
POSTGRES_SHARED_BUFFERS=32MB
POSTGRES_EFFECTIVE_CACHE_SIZE=96MB
POSTGRES_MAX_CONNECTIONS=30
POSTGRES_WORK_MEM=1MB
POSTGRES_MAINTENANCE_WORK_MEM=16MB
REDIS_MAXMEMORY=24mb
EOF
      ;;
    small)
      cat <<'EOF'
# ── 自适应内存档位：small（1.2 - 2.5 GiB）─────────────────────
# 删除该 ## auto-tuned 块，或手动改 MEMORY_TIER=manual 即可禁用自动覆盖。
MEMORY_TIER=small
WEB_MEM_LIMIT=512m
POSTGRES_MEM_LIMIT=256m
REDIS_MEM_LIMIT=96m
FRONTEND_MEM_LIMIT=32m
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=1
REDIS_MAX_CONNECTIONS=12
POSTGRES_SHARED_BUFFERS=64MB
POSTGRES_EFFECTIVE_CACHE_SIZE=192MB
POSTGRES_MAX_CONNECTIONS=40
REDIS_MAXMEMORY=64mb
EOF
      ;;
    large)
      cat <<'EOF'
# ── 自适应内存档位：large（> 2.5 GiB）─────────────────────────
# 删除该 ## auto-tuned 块，或手动改 MEMORY_TIER=manual 即可禁用自动覆盖。
MEMORY_TIER=large
WEB_MEM_LIMIT=1024m
POSTGRES_MEM_LIMIT=512m
REDIS_MEM_LIMIT=192m
FRONTEND_MEM_LIMIT=64m
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=2
REDIS_MAX_CONNECTIONS=16
POSTGRES_SHARED_BUFFERS=128MB
POSTGRES_EFFECTIVE_CACHE_SIZE=384MB
POSTGRES_MAX_CONNECTIONS=60
REDIS_MAXMEMORY=128mb
EOF
      ;;
  esac
}

auto_tune_env() {
  local env_file="${1:-.env}"
  [[ -f "$env_file" ]] || { warn "auto_tune_env：$env_file 不存在，跳过"; return 0; }
  # 已显式设置过非空值——保持不动（开发自定义、回滚都靠它）。
  # 空值（``MEMORY_TIER=``）视为「未设置」，继续走自适应注入。
  # 用 ``tail -n1`` 保证拿到 dotenv 实际生效的「最后一次定义」（多行同 key 时
  # python-dotenv / pydantic-settings 都按最后一次值走）。
  local cur=""
  if grep -qE '^MEMORY_TIER=' "$env_file" 2>/dev/null; then
    cur="$(grep -E '^MEMORY_TIER=' "$env_file" | tail -n1 | cut -d= -f2- | tr -d ' "')"
  fi
  if [[ -n "$cur" ]]; then
    dim "MEMORY_TIER 已存在（=${cur}）→ 跳过自适应注入"
    return 0
  fi
  local tier
  tier="$(detect_memory_tier)"
  log "检测到宿主机内存档位：${C_GRN}${tier}${C_RST}（自动写入 $env_file 末尾）"
  {
    printf '\n## auto-tuned BEGIN — 由 scripts/_lib.sh 自适应注入；改 MEMORY_TIER=manual 禁用\n'
    _memory_tier_block "$tier"
    printf '## auto-tuned END\n'
  } >> "$env_file"
  ok "已写入 ${tier} 档位的 mem_limit / DB / Redis 默认值"
}

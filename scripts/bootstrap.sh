#!/usr/bin/env bash
# 一次性环境初始化（幂等）：
#   - 检查依赖（python3.12 / docker / pnpm）
#   - 生成 .env（自动填 MASTER_KEY / JWT_SECRET / UPDATER_TOKEN）
#   - 创建 backend/.venv 并装 pip 依赖
#   - 装前端 pnpm 依赖
# 多次执行无副作用——已就绪的部分会跳过。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_lib.sh"

cd "$ROOT_DIR"

# ── 1. 依赖检查 ────────────────────────────────────────────────
log "检查环境依赖"

PYTHON_BIN=""
for cand in python3.12 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [[ "$ver" == "3.12" || "$ver" == "3.13" ]]; then
      PYTHON_BIN="$cand"
      break
    fi
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  err "未找到 Python 3.12 / 3.13"
  echo "  macOS 安装：brew install python@3.12"
  echo "  Linux：apt/dnf 装 python3.12 或 pyenv install 3.12"
  exit 1
fi
ok "Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

need_cmd docker "macOS: 装 Docker Desktop；Linux: 装 docker.io + docker compose 插件"
if ! docker info >/dev/null 2>&1; then
  die "docker 守护进程没启动（Mac 上请打开 Docker Desktop）"
fi
ok "Docker: $(docker --version)"

if ! docker compose version >/dev/null 2>&1; then
  die "缺少 docker compose 插件（v2）"
fi
ok "Docker Compose: $(docker compose version --short 2>/dev/null || docker compose version | head -1)"

need_cmd pnpm "安装：corepack enable && corepack prepare pnpm@latest --activate；或 npm i -g pnpm"
ok "pnpm: $(pnpm --version)"

need_cmd curl "macOS 自带；Linux: apt/dnf install curl"
ok "curl: present"

# ── 2. .env 生成 ──────────────────────────────────────────────
if [[ ! -f .env ]]; then
  log "首次创建 .env"
  cp .env.example .env
  MASTER_KEY="$("$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || true)"
  JWT_SECRET="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(64))' 2>/dev/null || true)"
  UPDATER_TOKEN="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(64))' 2>/dev/null || true)"
  if [[ -z "$MASTER_KEY" || -z "$JWT_SECRET" ]]; then
    warn ".env 已创建，但密钥生成失败（cryptography 未装）；先继续装依赖，稍后会自动重试"
  else
    # 用 python 替换避免 sed 在 macOS / GNU 间差异
    "$PYTHON_BIN" - "$MASTER_KEY" "$JWT_SECRET" "$UPDATER_TOKEN" <<'PY'
import sys, pathlib, re
mk, js, updater = sys.argv[1:4]
p = pathlib.Path(".env")
text = p.read_text()
text = re.sub(r'^MASTER_KEY=.*$', f'MASTER_KEY={mk}', text, flags=re.MULTILINE)
text = re.sub(r'^JWT_SECRET=.*$', f'JWT_SECRET={js}', text, flags=re.MULTILINE)
text = re.sub(r'^UPDATER_TOKEN=.*$', f'UPDATER_TOKEN={updater}', text, flags=re.MULTILINE)
p.write_text(text)
PY
    ok ".env 已生成（MASTER_KEY / JWT_SECRET / UPDATER_TOKEN 自动填好；其余沿用默认）"
  fi
else
  # 已有 .env，但仍可能含占位 changeme- → 自动替换
  if grep -q '^MASTER_KEY=changeme-' .env || grep -q '^JWT_SECRET=changeme-' .env; then
    log "检测到 .env 内仍是占位密钥，自动替换"
    MASTER_KEY="$("$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || true)"
    JWT_SECRET="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(64))' 2>/dev/null || true)"
    if [[ -n "$MASTER_KEY" && -n "$JWT_SECRET" ]]; then
      "$PYTHON_BIN" - "$MASTER_KEY" "$JWT_SECRET" <<'PY'
import sys, pathlib, re
mk, js = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
text = p.read_text()
text = re.sub(r'^MASTER_KEY=changeme-.*$', f'MASTER_KEY={mk}', text, flags=re.MULTILINE)
text = re.sub(r'^JWT_SECRET=changeme-.*$', f'JWT_SECRET={js}', text, flags=re.MULTILINE)
p.write_text(text)
PY
      ok ".env 占位密钥已替换"
    fi
  else
    ok ".env 已存在（保持不变）"
  fi
fi

if ! grep -qE '^UPDATER_TOKEN=[^[:space:]]+$' .env 2>/dev/null || grep -q '^UPDATER_TOKEN=changeme-' .env 2>/dev/null; then
  UPDATER_TOKEN="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(64))')"
  "$PYTHON_BIN" - "$UPDATER_TOKEN" <<'PY'
import pathlib
import re
import sys

p = pathlib.Path(".env")
text = p.read_text()
line = f"UPDATER_TOKEN={sys.argv[1]}"
if re.search(r"^UPDATER_TOKEN=.*$", text, flags=re.MULTILINE):
    text = re.sub(r"^UPDATER_TOKEN=.*$", line, text, flags=re.MULTILINE)
else:
    text = text.rstrip() + "\n" + line + "\n"
p.write_text(text)
PY
  ok ".env 已补齐独立 UPDATER_TOKEN"
fi

# ── 3. backend venv ───────────────────────────────────────────
if [[ ! -d backend/.venv ]]; then
  log "创建 backend/.venv 并装依赖（首次较慢，3-5 分钟）"
  cd backend
  "$PYTHON_BIN" -m venv .venv
  # shellcheck disable=SC1091
  . .venv/bin/activate
  pip install -q -U pip wheel
  if ! pip install -q -e ".[dev]"; then
    warn "pip install 失败：尝试不带 cryptg 兜底（性能略低）"
    pip install -q -e ".[dev]" --no-deps || true
    pip install -q fastapi 'uvicorn[standard]' python-multipart \
      'sqlalchemy[asyncio]' asyncpg alembic aiosqlite \
      pydantic pydantic-settings redis \
      telethon 'python-socks[asyncio]' \
      cryptography argon2-cffi pyotp pyjwt \
      httpx python-json-logger anyio tenacity \
      pytest pytest-asyncio pytest-cov ruff
  fi
  deactivate
  cd "$ROOT_DIR"
  ok "backend 依赖安装完成"
else
  # 检查是否能 import 关键包；不能则补一次 install
  if ! backend/.venv/bin/python -c "import fastapi, telethon, sqlalchemy, redis" 2>/dev/null; then
    log "backend/.venv 存在但关键包不全，补装"
    # shellcheck disable=SC1091
    . backend/.venv/bin/activate
    cd backend && pip install -q -e ".[dev]" && cd "$ROOT_DIR"
    deactivate
  fi
  ok "backend/.venv 已就绪"
fi

# 如果之前因 cryptography 未装导致 .env 没填密钥，这里补一次
if grep -q '^MASTER_KEY=$' .env 2>/dev/null || grep -q '^MASTER_KEY=changeme-' .env 2>/dev/null; then
  MK="$(backend/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  JS="$(backend/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(64))')"
  backend/.venv/bin/python - "$MK" "$JS" <<'PY'
import sys, pathlib, re
mk, js = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
text = p.read_text()
text = re.sub(r'^MASTER_KEY=(changeme-.*)?$', f'MASTER_KEY={mk}', text, flags=re.MULTILINE)
text = re.sub(r'^JWT_SECRET=(changeme-.*)?$', f'JWT_SECRET={js}', text, flags=re.MULTILINE)
p.write_text(text)
PY
  ok ".env 密钥补齐"
fi

# ── 4. frontend deps ─────────────────────────────────────────
if [[ ! -d frontend/node_modules ]]; then
  log "安装前端依赖（pnpm install，首次较慢）"
  cd frontend
  pnpm install --silent
  cd "$ROOT_DIR"
  ok "frontend 依赖安装完成"
else
  ok "frontend/node_modules 已就绪"
fi

ensure_dirs

# ── 4.5 自适应内存档位（仅在 .env 未含 MEMORY_TIER 时注入） ─────────
# 让 ``make up`` 在小机器开发场景也得到合理的 DB pool / Postgres / Redis
# 默认。生产 ``make prod-up`` 也会再调一次（幂等）。
auto_tune_env .env

# ── 5. 收紧 .env 权限（含敏感密钥，必须 600） ───────────────────────
# 即便 umask 默认 022 让 .env 落成 644，这里也强制收紧；
# 多次执行无副作用——chmod 是幂等的。
if [[ -f .env ]]; then
  chmod 600 .env || warn "chmod 600 .env 失败（可能在不支持的 FS 上），请手动收紧权限"
  ok ".env 权限：$(stat -f '%Sp' .env 2>/dev/null || stat -c '%A' .env 2>/dev/null)"
fi

ok "环境初始化完成"
echo
dim "下一步：运行 ${C_GRN}make up${C_RST}${C_DIM} 一键启动开发环境（pg+redis+后端+前端）"

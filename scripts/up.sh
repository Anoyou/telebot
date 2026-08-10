#!/usr/bin/env bash
# 一键开发启动：
#   1. 检查环境（必要时自动调用 bootstrap.sh）
#   2. **强制清理 TelePilot 残留进程**（防止上次留下的孤儿 worker 跑老代码）
#   3. 起 PostgreSQL + Redis（docker compose dev）
#   4. 跑 alembic 迁移
#   5. 后台启动 uvicorn（**默认关闭 --reload**，原因见下方注释）
#   6. 后台启动 vite dev server
#   7. 等待两端就绪后打印访问地址
#
# 进程信息写到 .run/*.pid，输出写到 logs/*.log。
# 用 `make down` 一键停止；`make restart` = down + up。
#
# 为什么默认关闭 uvicorn --reload？
# ────────────────────────────────────────────────────────────
# 本项目用 multiprocessing.spawn 拉起每账号独立 worker 子进程：
#   - --reload 检测到代码变化时只重启 uvicorn 主进程
#   - **不会**重启已经 spawn 出去的 worker 子进程（它们是独立 Python 进程）
#   - 主进程 lifespan finally 来不及优雅关停子进程时，子进程被 init (pid 1)
#     接管成孤儿——下次主进程启动 spawn 一批新 worker，老孤儿仍连着 Redis
#     处理 TG 消息，新老 worker 跑两套不同代码
#
# 这种情况下 --reload 反而给"代码已生效"的假象，调试时很坑。
# 真正的开发循环：改完代码跑 ``make restart`` —— 全杀 + 全新启动，确定性。
# 想强行启用 --reload？设 ``UVICORN_RELOAD=1 make up``（自担风险）。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_lib.sh"
cd "$ROOT_DIR"
ensure_dirs
export TELEPILOT_GATEWAY_BIN="${TELEPILOT_GATEWAY_BIN:-$ROOT_DIR/.run/telepilot-gateway}"
export TELEPILOT_GATEWAY_SOCKET="${TELEPILOT_GATEWAY_SOCKET:-$ROOT_DIR/.run/gateway.sock}"

# ── 1. 幂等 bootstrap（首次会装依赖；后续秒过） ──────────────
"$SCRIPT_DIR/bootstrap.sh"

# ── 2. 强制清理 TelePilot 残留进程 ────────────────────────────
# 避免历史孤儿 worker（上次 uvicorn --reload 留下的、上次 make down 没清干净的）
# 跟新 worker 抢 Redis pubsub 处理 TG 消息，跑两套不同代码导致行为飘忽。
log "清理任何 TelePilot 残留 Python 进程"
kill_orphan_telepilot_workers_quiet() {
  local pids killed=()
  pids="$(pgrep -f 'multiprocessing.spawn' 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    return 0
  fi
  local backend_path="$ROOT_DIR/backend"
  local pid
  for pid in $pids; do
    # 用 lsof 看打开的文件里有没有当前 backend 路径——孤儿的 cwd 被 init
    # 重置成 ``/``，cwd 检测对真正的孤儿失效；但 worker 持有的 .py 文件 fd 会
    # 一直含项目 backend 路径。保留旧 telebot/backend 匹配用于迁移期清理。
    if lsof -p "$pid" 2>/dev/null | grep -Fq "$backend_path" \
      || lsof -p "$pid" 2>/dev/null | grep -Eq "(telebot|Telebot|telepilot|TelePilot)/backend"; then
      kill -9 "$pid" 2>/dev/null || true
      killed+=("$pid")
    fi
  done
  if (( ${#killed[@]} > 0 )); then
    warn "杀掉残留 worker: ${killed[*]}"
  fi
}
kill_orphan_telepilot_workers_quiet
# 兜底：把 8000/5173 端口上任何残留也清了（PID 文件可能已失效）
for port in 8000 5173; do
  pids="$(lsof -nP -iTCP:$port -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    warn "端口 :$port 残留 (pid=$pids)，强杀"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
done

# ── 3. 起本地依赖容器 ──────────────────────────────────────
log "启动 PostgreSQL + Redis"
docker compose -f docker-compose.dev.yml up -d >/dev/null

wait_compose_healthy docker-compose.dev.yml postgres 60 || die "PostgreSQL 未就绪，看 docker logs telebot-postgres"
wait_compose_healthy docker-compose.dev.yml redis 30   || die "Redis 未就绪，看 docker logs telebot-redis"

# 本地 dev compose 把 Postgres/Redis 映射到 15432/16379，避免撞宿主机已有服务。
# .env.example 仍保留 5432/6379，方便生产/容器内默认值；make up 只在当前进程
# 环境里做开发端口映射，不回写 .env，也不影响 prod-up。
if [[ -f .env ]]; then
  DEV_DATABASE_URL="$(grep -E '^DATABASE_URL=' .env | tail -n1 | cut -d= -f2- | tr -d ' "' || true)"
  DEV_REDIS_URL="$(grep -E '^REDIS_URL=' .env | tail -n1 | cut -d= -f2- | tr -d ' "' || true)"
  if [[ -n "$DEV_DATABASE_URL" ]]; then
    DEV_DATABASE_URL="${DEV_DATABASE_URL/@localhost:5432/@localhost:15432}"
    DEV_DATABASE_URL="${DEV_DATABASE_URL/@127.0.0.1:5432/@127.0.0.1:15432}"
    export DATABASE_URL="$DEV_DATABASE_URL"
  fi
  if [[ -n "$DEV_REDIS_URL" ]]; then
    DEV_REDIS_URL="${DEV_REDIS_URL/localhost:6379/localhost:16379}"
    DEV_REDIS_URL="${DEV_REDIS_URL/127.0.0.1:6379/127.0.0.1:16379}"
    export REDIS_URL="$DEV_REDIS_URL"
  fi
fi

# ── 4. 迁移 ────────────────────────────────────────────────
log "执行 alembic 迁移"
( cd backend && . .venv/bin/activate && alembic upgrade head ) \
  >> "$LOG_DIR/migrate.log" 2>&1 \
  || { tail -40 "$LOG_DIR/migrate.log"; die "alembic 失败（见 logs/migrate.log）"; }
ok "迁移完成"

# ── 5. 后端 uvicorn ────────────────────────────────────────
# 默认关闭 --reload（见文件顶部注释）。设 UVICORN_RELOAD=1 强行启用。
RELOAD_FLAG=""
if [[ "${UVICORN_RELOAD:-0}" == "1" ]]; then
  RELOAD_FLAG="--reload"
  warn "uvicorn --reload 启用（experimental——子 worker 不会自动重启，改完代码仍需 make restart）"
fi

if is_alive "$BACKEND_PID"; then
  ok "后端已在运行 (pid=$(cat "$BACKEND_PID"))，跳过"
else
  log "启动后端 uvicorn :8000${RELOAD_FLAG:+ (reload)}"
  (
    cd backend
    . .venv/bin/activate
    nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 $RELOAD_FLAG \
      >> "$BACKEND_LOG" 2>&1 &
    echo $! > "$BACKEND_PID"
  )
  sleep 1
  if ! is_alive "$BACKEND_PID"; then
    err "后端启动失败，最近日志："
    tail -30 "$BACKEND_LOG" >&2
    exit 1
  fi
fi

wait_http "http://localhost:8000/healthz" 30 "后端" || {
  err "后端最近日志："
  tail -40 "$BACKEND_LOG" >&2
  exit 1
}

# ── 6. 前端 vite ──────────────────────────────────────────
if is_alive "$FRONTEND_PID"; then
  ok "前端已在运行 (pid=$(cat "$FRONTEND_PID"))，跳过"
else
  log "启动前端 vite :5173"
  (
    cd frontend
    nohup pnpm dev --host 0.0.0.0 --port 5173 \
      >> "$FRONTEND_LOG" 2>&1 &
    echo $! > "$FRONTEND_PID"
  )
  sleep 2
  if ! is_alive "$FRONTEND_PID"; then
    err "前端启动失败，最近日志："
    tail -30 "$FRONTEND_LOG" >&2
    exit 1
  fi
fi

wait_http "http://localhost:5173/" 30 "前端" || {
  err "前端最近日志："
  tail -40 "$FRONTEND_LOG" >&2
  exit 1
}

# ── 7. 完成 ────────────────────────────────────────────────
echo
ok "全部就绪 ${C_GRN}🎉${C_RST}"
echo
printf '  前端  ${C_BLU}%s${C_RST}\n' "http://localhost:5173"
printf '  后端  ${C_BLU}%s${C_RST}    （${C_DIM}文档：%s${C_RST}）\n' \
  "http://localhost:8000" "http://localhost:8000/docs"
echo
dim "  改完代码？跑 ${C_GRN}make restart${C_RST}${C_DIM}（会确定性地杀光重启，不留孤儿）"
dim "  实时日志：make logs        （Ctrl+C 仅退出 tail，不停服务）"
dim "  状态总览：make status"
dim "  全部停止：make down"

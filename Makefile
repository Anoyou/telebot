# TelePilot — 项目级 Makefile
# 一键命令：
#   make up          一键开发启动（pg + redis + 后端 + 前端）★最常用
#   make down        一键停止
#   make logs        实时跟踪后端 + 前端日志
#   make status      四组件状态总览
#   make prod-up     一键生产部署（纯 docker compose）
#   make prod-update 增量更新生产栈（按变更重建必要服务）
#   make nuke        彻底清理（删数据 + venv + node_modules + .env）
#   make help        全部命令清单

.PHONY: help up down restart logs status nuke bootstrap init-prod-env auth-recovery \
        dev-up dev-down dev-logs install migrate makemigration backend frontend \
        test lint codegen build prod-build prod-up prod-update prod-down backup clean

PYTHON := python3.12
VENV := backend/.venv
ACTIVATE := . $(VENV)/bin/activate
PROD_UPDATE_ARGS ?=

help:
	@echo "════════════ 一键命令（推荐） ════════════"
	@echo "  make up          ★ 一键启动开发环境（首次会自动 bootstrap）"
	@echo "  make down          一键停止开发环境（保留数据）"
	@echo "  make restart       ★ 改完代码后一键重启（down + up；确定性新代码）"
	@echo "  make logs          跟踪后端+前端日志（Ctrl+C 退出 tail）"
	@echo "  make logs be|fe|db 单独看某个组件日志"
	@echo "  make status        四组件状态总览"
	@echo "  make prod-up       一键生产部署（纯 docker compose 4 容器）"
	@echo "  make init-prod-env 生成生产 .env（随机密钥 + 数据库密码）"
	@echo "  make prod-update   增量更新生产栈（按变更重建必要服务）"
	@echo "  make prod-down     停止生产栈"
	@echo "  make auth-recovery 生成 Web 登录一次性恢复码（需本机/服务器执行）"
	@echo "  make nuke          ⚠ 彻底清理（含数据库）"
	@echo ""
	@echo "════════════ 细粒度命令 ════════════"
	@echo "  make bootstrap     仅初始化环境（venv + .env + pnpm install）"
	@echo "  make dev-up        仅启动 pg + redis 容器"
	@echo "  make dev-down      仅停止 pg + redis 容器"
	@echo "  make dev-logs      跟踪 pg + redis 容器日志"
	@echo "  make install       重装后端 + 前端依赖（已有 venv 也会更新）"
	@echo "  make migrate       手动跑 alembic upgrade head"
	@echo "  make makemigration m='describe'   生成新迁移"
	@echo "  make backend       前台跑 uvicorn（不后台、不写 PID）"
	@echo "  make frontend      前台跑 vite"
	@echo "  make test          后端 pytest"
	@echo "  make lint          ruff check"
	@echo "  make codegen       OpenAPI → 前端类型"
	@echo "  make backup        备份脚本（pg_dump + sessions 卷）"
	@echo "  make clean         清 caches / .venv / node_modules（不删数据卷）"

# ════════════════════════════════════════════
# 一键命令（脚本驱动）
# ════════════════════════════════════════════
up:
	@./scripts/up.sh

down:
	@./scripts/down.sh

# 改完代码后必须用这个——只重启 backend uvicorn 不会让 worker 子进程拿新代码
# （它们是 multiprocessing.spawn 出来的独立 Python 进程，跟 uvicorn --reload 无关）。
# restart = down（清光所有 TelePilot 进程，含孤儿 worker）+ up（拉新进程）。
restart:
	@./scripts/down.sh
	@./scripts/up.sh

logs:
	@./scripts/logs.sh $(filter-out $@,$(MAKECMDGOALS))

status:
	@./scripts/status.sh

bootstrap:
	@./scripts/bootstrap.sh

init-prod-env:
	@./scripts/init-prod-env.sh

prod-up:
	@./scripts/prod-up.sh

prod-update:
	@./scripts/prod-update.sh $(PROD_UPDATE_ARGS)

prod-down:
	docker compose down

auth-recovery:
	cd backend && $(ACTIVATE) && python -m app.scripts.auth_recovery

nuke:
	@./scripts/nuke.sh

# 让 `make logs be` 这种"接位置参数"不报错
%:
	@:

# ════════════════════════════════════════════
# 细粒度（保留旧 target 不破坏既有用法）
# ════════════════════════════════════════════
dev-up:
	docker compose -f docker-compose.dev.yml up -d

dev-down:
	docker compose -f docker-compose.dev.yml down

dev-logs:
	docker compose -f docker-compose.dev.yml logs -f --tail=100

install:
	cd backend && $(PYTHON) -m venv .venv && $(ACTIVATE) && pip install -U pip && pip install -e .[dev]
	cd frontend && pnpm install

migrate:
	cd backend && $(ACTIVATE) && alembic upgrade head

makemigration:
	cd backend && $(ACTIVATE) && alembic revision --autogenerate -m "$(m)"

backend:
	cd backend && $(ACTIVATE) && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

frontend:
	cd frontend && pnpm dev

test:
	cd backend && $(ACTIVATE) && pytest -v

lint:
	cd backend && $(ACTIVATE) && ruff check app

codegen:
	cd frontend && pnpm codegen

build:
	docker compose build

prod-build:
	docker compose build

backup:
	./deploy/backup.sh

clean:
	rm -rf backend/.venv backend/.pytest_cache backend/.ruff_cache
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	rm -rf frontend/node_modules frontend/dist frontend/.vite
	rm -rf .run logs

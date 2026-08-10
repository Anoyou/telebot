# syntax=docker/dockerfile:1.7
# 后端镜像：多阶段构建
#   - gateway-builder 阶段：构建内置 telepilot-gateway 静态二进制
#   - builder 阶段：安装编译依赖 + 通过 pyproject.toml 安装 Python 依赖
#   - runtime 阶段：仅保留必要运行库 + 已装好的 site-packages，体积更小
# 暴露端口 8000；健康检查命中 /healthz；启动命令含 alembic 迁移由 docker-compose 注入

FROM golang:1.23-bookworm AS gateway-builder

ARG BUILD_COMMIT=dev
WORKDIR /src/gateway
COPY gateway/go.mod ./go.mod
COPY gateway/ ./
RUN go test ./... && \
    go vet ./... && \
    CGO_ENABLED=0 go build -trimpath \
    -ldflags="-s -w -X github.com/anoyou/telepilot/gateway/internal/version.BuildCommit=${BUILD_COMMIT}" \
    -o /out/telepilot-gateway ./cmd/telepilot-gateway


FROM python:3.12-slim AS builder

WORKDIR /app

# 安装编译期依赖（cryptg/cffi 需要 gcc/libffi-dev）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# 先拷贝依赖清单并安装 runtime 依赖。不要在这一层拷贝 app/，否则任意后端
# 代码变更都会打穿依赖安装缓存，生产增量更新会被迫重装 Telethon / cryptg 等包。
COPY backend/pyproject.toml /app/
RUN --mount=type=cache,target=/root/.cache/pip pip install --upgrade pip
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import tomllib; from pathlib import Path; data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8")); print("\n".join(data["project"]["dependencies"]))' \
    > /tmp/requirements.txt && \
    pip install -r /tmp/requirements.txt

# 业务源码由 runtime 阶段直接复制到 /app。应用没有 console entry point，
# 无需再构建一份重复 wheel 到 site-packages；后端代码变化因此不会打穿依赖层。


FROM python:3.12-slim AS runtime

WORKDIR /app

# runtime 需要 libffi/CA；远程模块库浏览和安装依赖 git，SSH 仓库依赖 openssh-client
RUN apt-get update && apt-get install -y --no-install-recommends \
        libffi8 ca-certificates git openssh-client && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf /usr/local/lib/python3.12/site-packages/pip \
           /usr/local/lib/python3.12/site-packages/pip-*.dist-info

# 从 builder 拷贝已安装的 Python 包与可执行入口（uvicorn/alembic 等都在这里）
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
RUN python -c "import importlib.metadata as m, pathlib, pip; d=list(pathlib.Path('/usr/local/lib/python3.12/site-packages').glob('pip-*.dist-info')); assert len(d) == 1, d; assert m.version('pip') == pip.__version__, (m.version('pip'), pip.__version__)"
COPY --from=gateway-builder /out/telepilot-gateway /usr/local/bin/telepilot-gateway
COPY gateway/LICENSE gateway/THIRD_PARTY_NOTICES.md /usr/share/licenses/telepilot-gateway/

# 拷贝完整后端源码（含 alembic 迁移脚本、app/ 等）及 Agent 读取的发布日志。
COPY backend/ /app
COPY CHANGELOG.md /app/CHANGELOG.md
# System Agent 只读源码诊断使用。仅复制前端源码与构建配置，不复制依赖和产物。
COPY frontend/src /app/source/frontend/src
COPY frontend/package.json frontend/tsconfig.json frontend/tsconfig.app.json frontend/vite.config.ts /app/source/frontend/

# 关闭 pyc 写盘 + 立即刷新 stdout，便于 docker logs 实时查看
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# 健康检查：访问 /healthz；连续失败 5 次则视为不健康
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz').read()" || exit 1

# 默认启动 uvicorn；docker-compose 中会用 sh -c 包一层先跑 alembic upgrade head
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# syntax=docker/dockerfile:1.7
# 后端镜像：多阶段构建
#   - builder 阶段：安装编译依赖 + 通过 pyproject.toml 安装 Python 依赖
#   - runtime 阶段：仅保留必要运行库 + 已装好的 site-packages，体积更小
# 暴露端口 8000；健康检查命中 /healthz；启动命令含 alembic 迁移由 docker-compose 注入

FROM python:3.12-slim AS builder

WORKDIR /app

# 安装编译期依赖（cryptg/cffi 需要 gcc/libffi-dev）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# 先拷贝依赖清单并安装 runtime 依赖。不要在这一层拷贝 app/，否则任意后端
# 代码变更都会打穿依赖安装缓存，生产增量更新会被迫重装 Telethon / cryptg 等包。
COPY pyproject.toml /app/
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
    rm -rf /var/lib/apt/lists/*

# 从 builder 拷贝已安装的 Python 包与可执行入口（uvicorn/alembic 等都在这里）
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 拷贝完整源码（含 alembic 迁移脚本、app/ 等）
COPY . /app

# 关闭 pyc 写盘 + 立即刷新 stdout，便于 docker logs 实时查看
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# 健康检查：访问 /healthz；连续失败 5 次则视为不健康
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz').read()" || exit 1

# 默认启动 uvicorn；docker-compose 中会用 sh -c 包一层先跑 alembic upgrade head
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

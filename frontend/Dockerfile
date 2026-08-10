# syntax=docker/dockerfile:1.7
# 前端镜像：多阶段构建
#   - builder：node22 alpine，corepack 启用 pnpm，依次执行类型检查与 Vite 构建
#   - runtime：nginx alpine，托管 dist 静态资源 + 反代后端
# 暴露 80 端口；通过 docker-compose 中 ${WEB_PORT_PUBLISH:-80} 对外发布

FROM node:22-alpine AS builder
WORKDIR /app

# 启用 corepack，并固定 pnpm 10，避免服务器构建时漂到 pnpm 11 后忽略 onlyBuiltDependencies。
RUN corepack enable && corepack prepare pnpm@10.23.0 --activate

# 先按 lockfile 拉取依赖包。版本号变化只会重跑离线链接，不会重新下载依赖。
COPY frontend/pnpm-lock.yaml frontend/.npmrc frontend/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=telepilot-pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store && pnpm fetch --frozen-lockfile
COPY frontend/package.json ./
RUN --mount=type=cache,id=telepilot-pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store && pnpm install --offline --frozen-lockfile

# 拷贝前端源码。docs 与根更新日志由生产运行时目录提供，不参与 bundle。
COPY frontend/ .
RUN --mount=type=cache,id=telepilot-tsbuild,target=/app/node_modules/.cache/tsbuild \
    pnpm typecheck
# 生产 bundle 的稳定 live set 已超过 600 MiB，完整进程峰值会超过 1 GiB；
# 显式给标准 CI/Buildx builder 留足 V8 余量，避免自动探测给出过小堆上限。
RUN NODE_OPTIONS=--max-old-space-size=1536 pnpm exec vite build


FROM nginx:1.27-alpine AS runtime

# 静态资源
COPY --from=builder /app/dist /usr/share/nginx/html
# 站点配置：替换默认 default.conf
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

# 健康检查：能拿到首页即可
HEALTHCHECK --interval=10s --timeout=3s CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1

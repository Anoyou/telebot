# TelePilot 部署说明

本文档记录部署相关脚本和生产运行方式。更完整的公网 HTTPS 说明见 [docs/DEPLOY-PUBLIC.md](../docs/DEPLOY-PUBLIC.md)。

## 本地开发

仓库根目录直接使用 Makefile：

```bash
make up
make logs
make status
make restart
make down
```

`make up` 会初始化 `.env`、安装本地依赖、启动 PostgreSQL / Redis，并在本机启动后端和前端开发服务。

## 服务器开箱部署

SSH 到 Debian / Ubuntu 服务器后：

```bash
curl -fsSL https://raw.githubusercontent.com/Anoyou/telebot/main/scripts/install-server.sh | bash
```

脚本会安装基础依赖、Docker Compose v2 和支持 attestation 的 GitHub CLI，拉取仓库、生成生产 `.env`，然后从 GHCR 拉取预构建镜像并执行 `make prod-up`。三个应用镜像必须通过 source、revision、digest 与本仓库 SLSA/Sigstore provenance 校验后才会启动；正常服务器部署不编译前端或应用镜像。

公网 HTTPS 场景建议让 Docker 只监听本机端口，再由 Caddy 对外：

```bash
curl -fsSL https://raw.githubusercontent.com/Anoyou/telebot/main/scripts/install-server.sh \
  | env WEB_PORT_PUBLISH=127.0.0.1:8080 COOKIE_SECURE=true bash
```

## 已克隆仓库内生产启动

```bash
cp .env.example .env
# 修改 MASTER_KEY / JWT_SECRET / UPDATER_TOKEN / POSTGRES_PASSWORD / COOKIE_SECURE 等
make prod-up
```

生产栈包含：

- `postgres`：主数据存储
- `redis`：IPC、限速和任务状态；生产默认 AOF everysec + noeviction，磁盘不足时应先扩容或清理，不能依赖逐出关键键
- `web`：FastAPI + worker supervisor
- `updater`：仅 Docker 内网可访问的在线更新执行器
- `frontend`：nginx 静态前端 + 后端反代

常用命令：

```bash
make prod-up
make prod-down
docker compose ps
docker compose logs -f web
```

## 备份与恢复

脚本：

- `deploy/backup.sh`：备份数据库、sessions、已安装插件和插件仓库缓存，并生成 SHA-256 校验文件
- 默认写入 `/var/backups/telepilot`；`BACKUP_RETENTION_COUNT` 默认保留最近 7 个完整备份组，`BACKUP_RETENTION_DAYS` 继续作为最长保留天数。
- `deploy/backup-keys.sh`：备份 `.env` 中关键密钥
- `deploy/restore.sh`：恢复备份

`MASTER_KEY` 必须离线备份，并且不要和数据库备份放在同一个位置。丢失它会导致已加密的 Telegram session、api_id、api_hash、TOTP secret 和 Bot Token 无法解密。

## 升级与回滚

稳定版升级：

```bash
cd /opt/telepilot
./deploy/backup.sh
cp .env "/var/backups/telepilot/env-$(date +%Y%m%d-%H%M).bak"
cp docker-compose.yml "/var/backups/telepilot/docker-compose-$(date +%Y%m%d-%H%M).yml.bak"
TELEPILOT_UPDATE_BRANCH=main make prod-update
```

测试发布候选分支时不要覆盖 `main`，必须显式指定分支：

```bash
cd /opt/telepilot
TELEPILOT_UPDATE_BRANCH=codex/your-release-branch make prod-update
```

### Web 面板自更新

生产栈包含一个仅 Docker 内网可访问的 `updater` 服务。它挂载项目目录和 Docker socket，由已登录的 Web 面板“检查更新”弹窗触发：

`UPDATER_TOKEN` 必须是独立随机密钥，不得与 `JWT_SECRET` 复用；缺失时 updater 会拒绝启动。

- 检查更新：读取当前分支或 `TELEPILOT_UPDATE_BRANCH`，执行 `git fetch`，生成 `web` / `frontend` / `updater` 服务级更新计划，并明确区分“直接文件同步”和“预构建镜像切换”；Compose 变化会比较到具体服务，不因文件本身变化直接升级为全栈更新。
- 应用更新：后台执行 `scripts/prod-update.sh`。文档只同步公开运行时文件；纯后端源码从目标 commit 归档后生成轻量补丁镜像并重启 `web`；前端、依赖、Dockerfile、Compose 与 updater 变化从 GHCR 拉取对应 SHA 的多架构镜像，服务器不编译。普通更新不会重启 PostgreSQL / Redis；迁移或基础设施变化会先备份。无迁移时任一服务失败会恢复整组已切换镜像和 Git 跟踪插件；迁移可能已执行时禁止自动回旧 web，避免旧代码连接新 schema。
- updater 自更新：业务服务完成健康检查后，由独立 handoff 切换 updater 并等待健康；失败时恢复旧 updater 镜像。
- 任务日志：Web 面板轮询 updater job，任务状态同时持久化到 Git 目录；updater 重启后仍可读取结果。

`TELEPILOT_HOST_PROJECT_DIR` 必须是宿主机上的绝对部署路径。宿主机直接运行 Compose 时相对路径虽然可解析，但 updater 在容器内调用宿主 Docker daemon 时会把 `.` 解释为容器工作目录 `/workspace`。更新器会优先从自身 Compose 标签恢复真实宿主路径；无法恢复时拒绝更新，不会继续使用不确定的挂载目录。handoff 结果写入 `.git/telepilot-updater-handoff.log`，更新后仍反复提示部署未完成时，应同时检查该日志与 `.git/telepilot-deploy-pending`。

首次把 `updater` 服务部署到服务器仍需要一次宿主机操作；之后常规补丁不再依赖 SSH 登录。若部署目录不是当前 shell 的工作目录，可显式指定：

```bash
cd /opt/telepilot
TELEPILOT_HOST_PROJECT_DIR=/opt/telepilot make prod-up
```

部署后至少验收：

```bash
git rev-parse HEAD
docker compose ps
PUBLISH_PORT="$(sed -n 's/^WEB_PORT_PUBLISH=//p' .env | tail -n1 | tr -d '"')"
PUBLISH_PORT="${PUBLISH_PORT##*:}"
curl -fsS "http://127.0.0.1:${PUBLISH_PORT:-80}/healthz"
docker compose exec -T web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5).read().decode())"
docker compose logs --tail=100 web
```

生产 Compose 不把 `web:8000` 发布到宿主机。`/healthz` 可经 frontend 的实际发布端口检查；
`/readyz` 当前应在 Web 容器内请求。

仅代码且不含迁移时，更新器会在健康失败时自动回滚。最近一次成功切换前的镜像引用记录在 `.git/telepilot-deploy-previous.json`，可用于人工恢复；不要只改 Git commit 而继续使用新镜像。

人工恢复必须同时恢复该 JSON 中的 commit 与 Web、Frontend、Updater 三张镜像；完整可复制步骤见 [公网部署指南的人工回滚](../docs/DEPLOY-PUBLIC.md#人工回滚)。没有完整旧镜像且确认无迁移时，才使用目标旧 commit 的 `make prod-up PROD_UP_ARGS=--source-build` 救援。

维护者必须从自定义分支验证尚未发布镜像的代码时，显式使用本地构建救援模式：

```bash
cd /opt/telepilot
make prod-up PROD_UP_ARGS=--source-build
```

如果更新执行了数据库迁移，切回旧代码并不能还原 schema，必须从迁移前备份恢复数据库。先确认 `.env` 里的 `MASTER_KEY` 与备份时一致，再执行 `deploy/restore.sh`；恢复脚本还可一并恢复插件卷。

部分 Docker 默认值、数据库默认名和 volume 名仍保留 `telebot` 历史兼容命名，不影响对外产品名 TelePilot。

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

脚本会安装基础依赖和 Docker Compose v2、拉取仓库、生成生产 `.env`，然后执行 `make prod-up`。

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
TELEPILOT_UPDATE_BRANCH=codex/0.33-interaction-framework make prod-update
```

### Web 面板自更新

生产栈包含一个仅 Docker 内网可访问的 `updater` 服务。它挂载项目目录和 Docker socket，由已登录的 Web 面板“检查更新”弹窗触发：

`UPDATER_TOKEN` 必须是独立随机密钥，不得与 `JWT_SECRET` 复用；缺失时 updater 会拒绝启动。

- 检查更新：读取当前分支或 `TELEPILOT_UPDATE_BRANCH`，执行 `git fetch`，按变更文件分类。
- 应用更新：后台执行 `scripts/prod-update.sh`，优先增量重建 `web` / `frontend`；涉及 compose、Dockerfile、依赖、部署脚本等关键文件时自动回退完整更新。
- 任务日志：Web 面板轮询 updater job，显示最近输出；服务重启期间页面可能短暂断开，刷新后可重新检查版本。

首次把 `updater` 服务部署到服务器仍需要一次宿主机操作；之后常规补丁不再依赖 SSH 登录。若部署目录不是当前 shell 的工作目录，可显式指定：

```bash
cd /opt/telepilot
TELEPILOT_HOST_PROJECT_DIR=/opt/telepilot make prod-up
```

部署后至少验收：

```bash
git rev-parse HEAD
docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
docker compose logs --tail=100 web
```

仅代码且不含迁移时可回滚：

```bash
cd /opt/telepilot
git checkout <tag-or-commit>
make prod-up
```

如果更新执行了数据库迁移，切回旧代码并不能还原 schema，必须从迁移前备份恢复数据库。先确认 `.env` 里的 `MASTER_KEY` 与备份时一致，再执行 `deploy/restore.sh`；恢复脚本还可一并恢复插件卷。

部分 Docker 默认值、数据库默认名和 volume 名仍保留 `telebot` 历史兼容命名，不影响对外产品名 TelePilot。

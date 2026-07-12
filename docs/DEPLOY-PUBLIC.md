# 公网部署指南

这篇文档讲的是：**怎么把 TelePilot 放到一台服务器上，并让浏览器可以访问**。

如果你只是自己测试，可以先用 README 里的 `make up` 或一条命令安装，不一定要一开始就配置域名和 HTTPS。

## 先选部署方式

| 方式 | 适合谁 | 说明 |
| --- | --- | --- |
| 一条命令安装 | 大多数 VPS 用户 | 脚本自动安装依赖、生成配置、启动服务 |
| Docker Compose | 想自己控制配置的人 | 稳定、好更新、好备份，也是当前推荐生产方式 |
| 源码混合运行 | 不想全套 Docker 的人 | 后端/前端跑在宿主机，PostgreSQL / Redis 可用 Docker 或已有服务 |
| Caddy / Nginx 反代 | 需要公网 HTTPS 的人 | 在服务跑起来之后，再加域名和证书 |

当前推荐的正式部署路径是：TelePilot 服务由 Docker Compose 启动，公网 HTTPS 由 Caddy 或 Nginx 负责。

仓库里部分默认卷名、数据库名和环境标记仍保留 `telebot` 兼容命名，不影响对外产品名 `TelePilot`。

## 1. 最省心：一条命令安装

SSH 到 Debian / Ubuntu 服务器后执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Anoyou/telebot/main/scripts/install-server.sh | bash
```

脚本会做这些事：

- 安装 Git、Make、Docker 和 Docker Compose v2。
- 拉取 TelePilot 到 `/opt/telepilot`。
- 生成生产用 `.env`。
- 启动数据库、Redis、后端和前端。

如果 80 端口被占用，可以指定别的端口：

```bash
curl -fsSL https://raw.githubusercontent.com/Anoyou/telebot/main/scripts/install-server.sh \
  | env WEB_PORT_PUBLISH=8080 bash
```

启动后，访问：

```text
http://服务器IP:端口
```

如果你要挂域名和 HTTPS，继续看下面的反代配置。

## 2. 推荐公网结构

- 公网入口：`https://telepilot.example.com`
- Caddy：监听服务器 `80/443`，自动申请 TLS
- TelePilot frontend 容器：只发布到本机 `127.0.0.1:8080`
- TelePilot web 容器：仅在 Docker 网络内提供 `web:8000`
- PostgreSQL / Redis / sessions / 远程插件目录：Docker volume 持久化

## 3. 带 HTTPS 的安装方式

如果你已经准备好域名，并打算用 Caddy / Nginx 做 HTTPS，建议让 TelePilot 只监听本机端口：

```bash
curl -fsSL https://raw.githubusercontent.com/Anoyou/telebot/main/scripts/install-server.sh \
  | env WEB_PORT_PUBLISH=127.0.0.1:8080 COOKIE_SECURE=true bash
```

这条命令会安装基础依赖与 Docker Compose v2、拉取仓库到 `/opt/telepilot`、生成生产 `.env`，并执行 `make prod-up` 启动 `postgres` / `redis` / `web` / `frontend`。如果 `WEB_PORT_PUBLISH` 指定的端口已被占用，脚本会保留 host 绑定并自动递增到可用端口，例如从 `127.0.0.1:8080` 改到 `127.0.0.1:8081`。

如果已经克隆仓库，也可以在仓库目录内手动配置：

```bash
cp .env.example .env
# 修改 MASTER_KEY / JWT_SECRET / UPDATER_TOKEN / POSTGRES_PASSWORD / COOKIE_SECURE / WEB_PORT_PUBLISH
make prod-up
```

公网 HTTPS 场景建议在 `.env` 中确认：

```dotenv
COOKIE_SECURE=true
TRUST_FORWARDED_FOR=true
CORS_ORIGINS=https://telepilot.example.com
WEB_PORT_PUBLISH=127.0.0.1:8080
```

`WEB_PORT_PUBLISH=127.0.0.1:8080` 可以避免 nginx 前端容器直接裸露到公网，只让 Caddy 作为唯一外部入口。

## 4. Caddy 配置

安装 Caddy：

```bash
sudo apt update
sudo apt install -y caddy
```

写入 `/etc/caddy/Caddyfile`：

```Caddyfile
telepilot.example.com {
    encode gzip zstd

    reverse_proxy 127.0.0.1:8080 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "same-origin"
    }
}
```

启动或重载：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

也可以用 Nginx、宝塔面板或其它反向代理，只要把公网 HTTPS 请求转发到 `127.0.0.1:8080` 即可。

## 5. 不想全套 Docker 怎么办

可以用源码混合方式：

```bash
make bootstrap
make dev-up
make migrate
```

然后开两个终端：

```bash
# 终端 1
make backend

# 终端 2
make frontend
```

如果你已经有自己的 PostgreSQL / Redis，就在 `.env` 里配置 `DATABASE_URL` / `REDIS_URL`，然后跳过 `make dev-up`。

生产环境仍建议至少把数据库、session、`.env` 做好备份。

## 6. 升级与回滚

升级：

```bash
cd /opt/telepilot
./deploy/backup.sh

# 默认保留最近 7 个完整备份组；可在 .env 中调整
# BACKUP_RETENTION_COUNT=7
cp .env "/var/backups/telepilot/env-$(date +%Y%m%d-%H%M).bak"
cp docker-compose.yml "/var/backups/telepilot/docker-compose-$(date +%Y%m%d-%H%M).yml.bak"
TELEPILOT_UPDATE_BRANCH=main make prod-update
```

`make prod-update` 会先检查远程变更，再生成服务级更新计划：仅后端变化时只切换
`web`，仅前端变化时只切换 `frontend`，updater 在业务服务健康后最后独立切换，纯文档
变化不重启服务。Dockerfile、依赖文件与 Compose 变化也会映射到具体服务；只有
PostgreSQL / Redis 配置、卷结构或无法识别的基础设施变化才进入完整更新。没有 Alembic
迁移时不会创建备份或处理数据库。更新前如果工作区存在未提交改动会拒绝执行，避免
覆盖服务器上的本地修改。

发布候选分支不要覆盖 `main`，用环境变量显式指定：

```bash
cd /opt/telepilot
TELEPILOT_UPDATE_BRANCH=codex/0.33-interaction-framework make prod-update
```

想先看本次会走哪条路径，可以执行：

```bash
cd /opt/telepilot
TELEPILOT_UPDATE_BRANCH=codex/0.33-interaction-framework make prod-update PROD_UPDATE_ARGS=--dry-run
```

### Web 面板自更新

生产栈会启动一个仅 Docker 内网可访问的 `updater` 服务。它挂载项目目录和 Docker socket，由已登录的 Web 后端通过共享 token 发起更新任务，不对公网暴露端口。

由于 updater 能控制宿主机 Docker，`UPDATER_TOKEN` 必须使用独立随机值，不能与 `JWT_SECRET` 复用；缺失时服务会直接拒绝启动。

- 检查更新：读取当前分支或 `TELEPILOT_UPDATE_BRANCH`，执行 `git fetch` 并展示具体受影响服务、数据库迁移和备份要求。
- 应用更新：后台执行 `scripts/prod-update.sh`，使用 `--no-deps` 只切换计划内的 `web` / `frontend` / `updater`；PostgreSQL / Redis 默认保持运行。
- 任务日志：Web 面板轮询持久化的 updater job；updater 自更新时页面可能短暂无法轮询，但新进程启动后可继续读取任务结果。

首次把 `updater` 服务部署到服务器仍需要一次宿主机操作；之后常规补丁不再依赖 SSH 登录。若部署目录不是当前 shell 的工作目录，可显式指定：

```bash
cd /opt/telepilot
TELEPILOT_HOST_PROJECT_DIR=/opt/telepilot make prod-up
```

没有数据库迁移时，可回滚到指定版本：

```bash
cd /opt/telepilot
git checkout <tag-or-commit>
make prod-up
```

`make prod-up` 会重新构建镜像、启动容器，并在 `web` 容器启动时执行 `alembic upgrade head`。包含迁移的更新会在拉取代码前自动运行备份；切回旧 commit 不会撤销 schema，必须用迁移前备份恢复数据库。恢复前确认 `.env` 中的 `MASTER_KEY` 与备份时一致，再按 `deploy/restore.sh` 恢复数据库、sessions 与插件卷。

## 7. 备份

至少备份三类数据：

- PostgreSQL 数据库
- `.env`，尤其 `MASTER_KEY`
- Docker volumes：`sessions`、`plugins_installed`、`plugin_repos`

仓库已有脚本可参考：

- [deploy/backup.sh](../deploy/backup.sh)（输出数据库、三个业务卷归档及 SHA-256 校验文件）
- [deploy/backup-keys.sh](../deploy/backup-keys.sh)
- [deploy/restore.sh](../deploy/restore.sh)

`MASTER_KEY` 必须和数据库备份分开保存。丢失 `MASTER_KEY` 后，已有 Telegram session、api_id、api_hash、TOTP secret 和 Bot Token 都无法解密。

## 8. 验收清单

1. `git rev-parse HEAD` 是本次目标 commit，`grep` 四处版本号一致。
2. `docker compose ps` 中 `postgres` / `redis` / `web` / `frontend` 均为 running 或 healthy。
3. `curl -fsS http://127.0.0.1:8000/healthz` 返回健康结果。
4. `curl -I http://127.0.0.1:8080` 能返回前端响应。
5. `docker compose logs --tail=100 web` 没有迁移、导入、路由或 worker 启动错误。
6. `https://telepilot.example.com` 可打开登录页。
7. 浏览器 Cookie 带 `Secure`，确认 `COOKIE_SECURE=true` 生效。
8. 服务器安全组只对公网开放 `80/tcp` 和 `443/tcp`，不要额外开放 `8000`。
9. 登录后确认概览、日志、交互、插件、设置页可打开。

## 9. 常见问题

Q: HTTPS 证书申请失败怎么办？  
A: 检查域名 A 记录是否指向服务器公网 IP，安全组是否放通 80/443，以及是否有其它服务占用这两个端口。

Q: 登录接口被 CORS 拦截怎么办？  
A: 检查 `.env` 中 `CORS_ORIGINS` 是否和实际访问地址完全一致，包括协议、域名和端口。

Q: PWA 安装后无法保持登录怎么办？
A: 公网 HTTPS 部署必须设置 `COOKIE_SECURE=true`，并通过 `https://` 访问。

Q: 远程插件更新后重建容器，插件文件不见了怎么办？
A: 确认 `docker-compose.yml` 里的 `plugins_installed` 和 `plugin_repos` volume 没有被改成容器临时目录。

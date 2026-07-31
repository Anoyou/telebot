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
- 启动数据库、Redis、后端、内网 Updater 和前端。

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

这条命令会安装基础依赖、Docker Compose v2 与支持 attestation 的 GitHub CLI，拉取仓库到 `/opt/telepilot`、生成生产 `.env`，从 GHCR 拉取预构建的 AMD64/ARM64 应用镜像，并启动 `postgres` / `redis` / `web` / `updater` / `frontend`。启动前会要求三个应用镜像的 OCI source 与当前 checkout 一致、revision 完全相同，再固定到 registry digest，并校验由本仓库 `publish-images.yml` 为该 commit 签发的 SLSA/Sigstore provenance；任一项不成立都会拒绝启动。服务器不运行 `pnpm build` 或正常路径的 Docker build。如果 `WEB_PORT_PUBLISH` 指定的端口已被占用，脚本会保留 host 绑定并自动递增到可用端口，例如从 `127.0.0.1:8080` 改到 `127.0.0.1:8081`。

GHCR 的三个容器包首次发布后需要由仓库管理员在 GitHub Packages 设置中各自改为 Public。公开后服务器可匿名拉取镜像及其 OCI provenance，不需要保存 GitHub Token；如果仍是 Private，`make prod-up` 会在改动现有服务前直接失败。

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
WEBHOOK_ALLOW_QUERY_TOKEN=false
PLUGIN_ALLOW_NEW_UNSIGNED_PLUGINS=false
PLUGIN_ALLOW_LEGACY_UNSIGNED_PLUGINS=true
```

`WEB_PORT_PUBLISH=127.0.0.1:8080` 可以避免 nginx 前端容器直接裸露到公网，只让 Caddy 作为唯一外部入口。
Webhook token 默认只允许通过请求头传递；新 ZIP 插件默认必须签名。历史未签名插件的兼容开关与新安装策略相互独立，不要为了加载旧插件打开新未签名安装入口。

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

`make prod-update` 会先比较当前部署 commit 与目标 commit 的文件差异，再为每个服务生成
具体动作。文档与 CHANGELOG 只同步到运行时目录，刷新页面立即生效，不构建也不重启；
纯后端源码、迁移脚本和 System Agent 只读源码快照会归档目标 commit 的受控目录，在临时
容器内通过 Python 编译校验后生成轻量本地补丁镜像，只重启 `web`，不执行 Docker build；
前端、依赖、Dockerfile、Compose 或 updater 变化会先拉取 GitHub Actions 已构建的不可变
镜像 digest，核对镜像 revision，再切换对应服务。镜像缺失、下载失败或 revision 不符时，
工作区会保留目标 commit 和 pending 标记，但当前服务仍保持旧镜像不动；镜像就绪后可直接
重试。更新脚本自身发生变化时，fast-forward 后会立即重新执行目标版本脚本，再进入镜像
校验和服务切换，避免旧 updater 继续使用启动时加载的过期逻辑。PostgreSQL / Redis 配置、
卷结构或无法识别的基础设施变化才进入完整更新。仅
`backend/pyproject.toml` 版本号变化不会被误判为依赖变化；只有 `project.dependencies`
实际改变才切换 web 镜像。没有 Alembic 迁移或基础设施变化时不会创建备份或处理数据库。

文件同步生成新 web 镜像后才切换容器；Web、Frontend 或 Updater 健康检查失败都会恢复
更新前镜像。成功部署前的 commit 与三项镜像引用保存在
`.git/telepilot-deploy-previous.json`，失败部署保留 pending 标记，修复后可直接重试。更新前
如果工作区存在未提交改动会拒绝执行，避免覆盖服务器上的本地修改。

高频实测统一使用 `Beta`，不要覆盖 `main`：

```bash
cd /opt/telepilot
TELEPILOT_UPDATE_BRANCH=Beta make prod-update
```

想先看本次会走哪条路径，可以执行：

```bash
cd /opt/telepilot
TELEPILOT_UPDATE_BRANCH=Beta make prod-update PROD_UPDATE_ARGS=--dry-run
```

### Web 面板自更新

生产栈会启动一个仅 Docker 内网可访问的 `updater` 服务。它挂载项目目录和 Docker socket，由已登录的 Web 后端通过共享 token 发起更新任务，不对公网暴露端口。

由于 updater 能控制宿主机 Docker，`UPDATER_TOKEN` 必须使用独立随机值，不能与 `JWT_SECRET` 复用；缺失时服务会直接拒绝启动。

- 检查更新：读取当前分支或 `TELEPILOT_UPDATE_BRANCH`，执行 `git fetch` 并展示具体受影响服务、数据库迁移和备份要求。
- 应用更新：后台执行 `scripts/prod-update.sh`；文档只同步文件，纯后端源码生成轻量补丁镜像并重启 `web`，前端和依赖变化只拉取 Actions 预构建镜像；PostgreSQL / Redis 默认保持运行。
- 任务日志：Web 面板轮询持久化的 updater job；updater 自更新时页面可能短暂无法轮询，但新进程启动后可继续读取任务结果。

首次把 `updater` 服务部署到服务器仍需要一次宿主机操作；之后常规补丁不再依赖 SSH 登录。若部署目录不是当前 shell 的工作目录，可显式指定：

```bash
cd /opt/telepilot
TELEPILOT_HOST_PROJECT_DIR=/opt/telepilot make prod-up
```

`v0.87.0-beta.5` 是例外：该版本在拉取目标脚本前就要求运行中的 updater 容器提供
GitHub CLI，因此无法靠尚未加载的新代码自我修复。若面板日志出现
`缺少命令：gh（验证 GHCR 镜像构建来源……）`，或者安装 `gh` 后仍要求
`gh auth login`，需要在宿主机执行一次兼容修复。先从已经 fetch 到本机的目标
Git 提交中导出脚本，查看来源提交、SHA-256 和完整内容，确认后再送入旧 updater
容器：

```bash
cd /opt/telepilot
git fetch origin Beta
REPAIR_COMMIT="$(git rev-parse --verify origin/Beta)"
git show "${REPAIR_COMMIT}:scripts/repair-legacy-updater.sh" \
  > /tmp/telepilot-repair-legacy-updater.sh
printf '修复脚本来源提交：%s\n' "$REPAIR_COMMIT"
sha256sum /tmp/telepilot-repair-legacy-updater.sh
sed -n '1,240p' /tmp/telepilot-repair-legacy-updater.sh
docker compose exec -T -u 0 updater sh \
  < /tmp/telepilot-repair-legacy-updater.sh
```

脚本成功时会打印 `旧 updater 验签环境已修复`、GitHub CLI 版本、包装器路径和
SHA-256。随后回到 Web 面板重试即可，不需要执行 `gh auth login`、配置 PAT 或关闭
验签。该脚本会为旧验签命令补上 `--bundle-from-oci`，让 GitHub CLI 直接从 GHCR
读取 OCI 证明，同时保留仓库、workflow、source commit 和自托管 runner 等原有
限制；任何证明缺失或不匹配仍会让更新失败关闭。

该操作只修改当前 updater 容器，安装 GitHub CLI 和一个可审计的兼容包装器；不会修改
数据库、业务容器或 Git 工作树。目标 updater 镜像接管后会替换旧容器，后续常规版本会
在镜像校验前重新加载目标更新逻辑，可继续直接使用 Web 面板更新，无需重复执行修复。

没有数据库迁移时，可回滚到指定版本：

```bash
cd /opt/telepilot
git checkout <tag-or-commit>
make prod-up
```

`make prod-up` 默认拉取 `.env` 指定的镜像并启动容器，在 `web` 容器启动时执行 `alembic upgrade head`。只有维护者救援时才使用 `make prod-up PROD_UP_ARGS=--source-build` 在服务器本地构建。包含迁移的更新会在拉取代码前自动运行备份；一旦新版 web 可能已经执行迁移，健康失败也不会自动切回旧代码，而会保留 pending 状态要求按恢复流程处理。切回旧 commit 不会撤销 schema，必须用迁移前备份恢复数据库。恢复前确认 `.env` 中的 `MASTER_KEY` 与备份时一致，再按 `deploy/restore.sh` 恢复数据库、sessions 与插件卷。

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

1. `git rev-parse HEAD` 是本次目标 commit，四个源版本文件与 `openapi/telepilot.openapi.json` 的 `info.version` 一致。
2. `docker compose ps` 中 `postgres` / `redis` / `web` / `updater` / `frontend` 均为 running 或 healthy。
3. 运行 `PUBLISH_PORT="$(sed -n 's/^WEB_PORT_PUBLISH=//p' .env | tail -n1 | tr -d '\"')"; PUBLISH_PORT="${PUBLISH_PORT##*:}"; curl -fsS "http://127.0.0.1:${PUBLISH_PORT:-80}/healthz"`，确认 FastAPI 进程存活。生产 Compose 不把 `web:8000` 暴露到宿主机。
4. 在 Web 容器内执行 `docker compose exec -T web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5).read().decode())"`，确认返回 `ok=true`，并确认 DB、Redis、Worker Supervisor、账号 Bot manager 和交互 Bot manager 均正常；生产流量与更新完成判定以此为准。
5. 使用上一步得到的 `PUBLISH_PORT` 执行 `curl -I "http://127.0.0.1:${PUBLISH_PORT:-80}"`，确认前端能够响应。
6. `docker compose logs --tail=100 web` 没有迁移、导入、路由或关键组件反复重试错误。
7. `https://telepilot.example.com` 可打开登录页。
8. 浏览器 Cookie 带 `Secure`，确认 `COOKIE_SECURE=true` 生效。
9. 服务器安全组只对公网开放 `80/tcp` 和 `443/tcp`，不要额外开放 `8000`。
10. 登录后确认概览、日志、交互、插件、设置页可打开。

## 9. 常见问题

Q: HTTPS 证书申请失败怎么办？  
A: 检查域名 A 记录是否指向服务器公网 IP，安全组是否放通 80/443，以及是否有其它服务占用这两个端口。

Q: 登录接口被 CORS 拦截怎么办？  
A: 检查 `.env` 中 `CORS_ORIGINS` 是否和实际访问地址完全一致，包括协议、域名和端口。

Q: PWA 安装后无法保持登录怎么办？
A: 公网 HTTPS 部署必须设置 `COOKIE_SECURE=true`，并通过 `https://` 访问。

Q: `/healthz` 正常但 Compose 仍显示 unhealthy 怎么办？
A: `/healthz` 只检查进程存活。继续请求 `/readyz` 并查看 `checks` 字段；DB、Redis 或三个关键 runtime manager 任一未就绪都会返回 503。检查 `docker compose logs --tail=200 web`，等待自动重试恢复或先修复对应依赖。

Q: 远程插件更新后重建容器，插件文件不见了怎么办？
A: 确认 `docker-compose.yml` 里的 `plugins_installed` 和 `plugin_repos` volume 没有被改成容器临时目录。

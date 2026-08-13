# TelePilot 开发指南

这份文档面向 TelePilot 本体开发。插件作者从 [插件 5 分钟 Quickstart](docs/PLUGIN-QUICKSTART.md) 开始，再按需查看 [插件开发指南](docs/PLUGIN-DEV-GUIDE.md) 和 [插件 API 参考](docs/PLUGIN-API-REFERENCE.md)。

TelePilot 是单租户、自托管项目。Bug 修复、测试、文档和边界清楚的小功能可以直接准备 PR；数据库迁移、新依赖、主入口调整、大型重构和兼容性变化先开 issue 对齐方向。

## 开始前先确认工作区

仓库可能同时存在用户和其他 Agent 的未提交改动。开始工作前运行：

```bash
git rev-parse --show-toplevel
git status --short --branch -uall
```

不要用 `checkout`、`reset`、`clean` 或 stash 处理不属于当前任务的文件。代码、文档、排障、UI、部署和发布任务还要先读 [Agent Playbooks](docs/AGENT-PLAYBOOKS.md)。

## 开发环境

| 依赖 | 当前要求 |
| --- | --- |
| Python | 3.12 或 3.13；Makefile 默认命令名为 `python3.12`，只有 3.13 时用 `make install PYTHON=python3.13` |
| Node.js | 22，与 CI 一致 |
| pnpm | 10.23，版本声明在 `frontend/package.json` |
| Go | 1.23 或更高；仅开发内置 Codex Gateway 时需要 |
| Docker | Docker Desktop、OrbStack 或 Docker Engine |
| Compose | Docker Compose v2 |
| 系统 | macOS 或 Linux；Windows 尚未作为正式开发环境验证 |
| 其它 | curl、make、lsof |

### 一条命令启动

```bash
git clone https://github.com/Anoyou/Telebot telepilot
cd telepilot
make up
```

首次执行 `make up` 会自动调用 `make bootstrap`，完成以下工作：

- 检查 Python、Docker、Compose、pnpm 和 curl。
- 从 `.env.example` 创建 `.env`，生成随机 `MASTER_KEY`、`JWT_SECRET` 和 `UPDATER_TOKEN`，并把权限收紧到 `600`。
- 创建 `backend/.venv`，安装后端和测试依赖。
- 安装前端 pnpm 依赖。
- 启动开发用 PostgreSQL 与 Redis，执行 `alembic upgrade head`。
- 后台启动 FastAPI 和 Vite，日志写入 `logs/`，PID 写入 `.run/`。

开发地址：

- 前端：[http://localhost:5173](http://localhost:5173)
- 后端：[http://localhost:8000](http://localhost:8000)
- OpenAPI：[http://localhost:8000/docs](http://localhost:8000/docs)
- PostgreSQL：宿主机 `15432` 映射到容器 `5432`
- Redis：宿主机 `16379` 映射到容器 `6379`

常用命令：

```bash
make status
make logs
make logs be
make logs fe
make restart
make down
```

### 为什么改 Worker 后必须 `make restart`

账号 Worker 使用 `multiprocessing.spawn` 启动，与 FastAPI 主进程完全独立。Uvicorn 热重载只会重启主进程，已经运行的 Worker 仍会继续执行旧代码，甚至可能成为孤儿进程。

`make up` 默认关闭 Uvicorn reload。改动 `backend/app/worker/`、插件 loader、调度器、命令、Telegram 客户端或 Worker 会调用的 service 后，运行 `make restart`，它会停止 FastAPI、Vite 和本项目派生的 Worker，再重新启动。

需要细粒度开发时可以分别运行：

```bash
make dev-up      # 只启动 PostgreSQL 和 Redis
make migrate
make backend     # 前台运行 Uvicorn，带 --reload
make frontend    # 前台运行 Vite
make gateway-build
make gateway-test
make gateway-run # 前台监听 .run/gateway.sock
```

`make backend` 适合只改控制面 API。涉及 Worker 的改动仍要完整重启。

## 运行架构

FastAPI 进程同时承载 Web API、System Agent、Worker Supervisor、Account Bot manager 和 Interaction Bot manager。生产环境固定使用一个 Uvicorn worker，避免同一账号被多个 Supervisor 重复拉起。

每个运行中的 Telegram 账号对应一个独立 Worker 子进程。Worker 内运行 Telethon、命令、插件、调度器和账号级任务，通过 Redis 与控制面通信。PostgreSQL 保存持久数据和加密字段，Redis 还负责租约、去重、限流和短期状态。

选择 `codex_gateway` 的 Responses Provider 由 FastAPI 按需拉起 `telepilot-gateway` Go 子进程，通过权限收紧的 Unix Socket 同步内存路由和发起请求。Gateway 不纳入全局 `/readyz`，故障不能阻断 direct Provider、Web 或 Worker。完整开发与安全边界见 [内置 Codex Gateway](docs/CODEX-GATEWAY.md)。

几个边界不能混用：

- UserBot 负责用户账号消息、命令、插件、定时任务和 payout。
- Account Bot 是每账号可选的管理 Bot，承接远程运维和 `/agent`。
- Interaction Bot 负责关键词、付款确认、按钮和群会话。
- 一个 Bot Token 只能有一个主要 polling 或 webhook 消费者。
- AI、Interaction Bot、Webhook、台账和命中调试是可热关闭的平台模块；关键缓存读取失败时按 fail-closed 处理。
- `/healthz` 只检查 FastAPI 进程，`/readyz` 还检查 PostgreSQL、Redis、Supervisor 和两个 Bot manager。

详细数据流见 [架构说明](docs/TELEPILOT-ARCHITECTURE.md) 和 [平台能力](docs/PLATFORM-CAPABILITIES.md)。

## 仓库结构

| 路径 | 负责什么 |
| --- | --- |
| `backend/app/api/` | FastAPI router、请求校验和 HTTP 边界 |
| `backend/app/services/` | 可复用业务服务、Bot manager、台账、插件、AI 与 System Agent |
| `backend/app/worker/` | Supervisor、Worker runtime、Telegram、命令、插件 loader、调度器和 IPC |
| `backend/app/db/` | SQLAlchemy base、session 和模型 |
| `backend/app/schemas/` | Pydantic 请求与响应结构 |
| `backend/app/tests/` | 后端回归、安全、迁移和运行时测试 |
| `gateway/` | 内置 Codex Gateway Go module、协议契约与单元测试 |
| `backend/alembic/versions/` | 线性 Alembic 迁移历史 |
| `frontend/src/pages/` | 一级工作台与详情页面 |
| `frontend/src/components/` | 布局、系统助手、插件和通用 UI |
| `frontend/src/api/` | API client、手写类型与 OpenAPI 生成类型 |
| `frontend/src/lib/` | 导航、版本、流式协议和纯逻辑工具 |
| `frontend/tests/` | Playwright 视觉与无障碍测试 |
| `examples/plugins/` | 插件示例；其中纳入 `validate-plugin-examples.py` 的稳定公开 API 示例由 CI 维护 |
| `docs/` | 架构、部署、安全、插件和 Agent 文档 |
| `deploy/` | 备份、恢复、Caddy 示例和内网 Updater |
| `scripts/` | 本地启动、生产更新和插件示例校验 |

## 配置与敏感数据

`.env` 只放进程启动前必须知道的配置。账号、Bot、Provider、插件和业务规则优先通过 Web 配置并写入数据库。

开发时重点关注：

- `MASTER_KEY` 用于加密 session、API Hash、代理密码、Bot Token、TOTP、LLM Key、Webhook Token 和 Provider 兼容请求头。
- `JWT_SECRET` 只用于 Web 鉴权签名。
- `UPDATER_TOKEN` 只用于内网 Updater，不能与 `JWT_SECRET` 复用。
- `COOKIE_SECURE=false` 只适合本地 HTTP，生产 HTTPS 必须设为 `true`。
- `TRUST_FORWARDED_FOR=true` 只在可信反代后使用。
- 前端默认同源访问 API；只有独立开发拓扑才设置 `VITE_API_BASE`。

新增可复用凭据时，使用 `*_enc` 字段和 `MASTER_KEY` 加密，并同步加入 `app.scripts.rekey`。公开鉴权入口不能在凭据验证前创建或修改持久配置。

主进程或独立子进程调用 `logging.basicConfig()` 后必须安装 `install_sensitive_log_filter()`。测试要覆盖 Telegram Bot API URL、Authorization、代理凭据和常见 Provider Key 不会进入日志。

## 后端开发

### API 与 service

Router 负责鉴权、输入校验、错误映射和响应结构，业务逻辑放到 `backend/app/services/`。Worker、System Agent 工具和 Bot runtime 应复用同一个 service，不要反过来调用本项目 HTTP API，也不要在 handler 内复制事务逻辑。

数据库写入要明确事务边界。System Agent 工具的 handler 不自行 commit，写工具先生成 Action 预览，确认后由统一执行器提交。

新增任何注册面必须遵循[能力接线协议](docs/architecture/capability-protocol.md)，明确稳定命名、依赖、生命周期、注册所有权、generation 失效与 fail-closed 语义。

### 数据库迁移

当前迁移保持单线四位编号。创建迁移前先检查 head：

```bash
cd backend
. .venv/bin/activate
alembic heads
```

为下一编号创建草稿，比如当前 head 是 `0050` 时使用 `0051`：

```bash
alembic revision --autogenerate --rev-id 0051 -m "add example field"
```

检查生成文件中的 `revision`、`down_revision`、表名、索引、默认值和 downgrade。迁移完成后至少运行：

```bash
alembic upgrade head
alembic heads
alembic downgrade -1
alembic upgrade head
```

`alembic heads` 必须只有一个 head。已经发布的迁移不能重写；本质不可逆时，在迁移文件中写清 downgrade 限制，并补升级路径测试。

### 后端验证

```bash
cd backend
.venv/bin/ruff check app
.venv/bin/pytest -v
cd ..
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
```

修复 Bug 时先补能在旧代码上失败的回归测试。涉及登录、密钥、权限、资金、Webhook、插件安装或公开入口时，同时覆盖允许与拒绝路径。

## 前端开发

当前默认入口是 `/plugins`，一级导航在 `frontend/src/components/layout/Sidebar.tsx`。页面路由在 `frontend/src/App.tsx`，AI、交互、Webhook、台账和命中调试通过平台能力 gate 控制；模块关闭时保留 URL 和配置，不应白屏或误删数据。

前端约定：

- 服务端状态使用 TanStack Query，本地交互状态使用 React state。
- 继续使用现有 Radix、shadcn-style 组件和 lucide 图标，不为单个页面引入新的 UI 或 state library。
- 可编辑字段用表单控件，只读状态、ID 和渲染结果使用展示组件。
- 新页面要覆盖 loading、empty、error、disabled 和 success 状态。
- 桌面、834px 平板和 iPhone 13 宽度下不能出现横向滚动、按钮重叠或长中文溢出。
- 修改公开 API 后更新 `frontend/src/api/` 类型；`make codegen` 会直接导入应用并离线导出 OpenAPI，不需要先启动后端。若导入失败，应先修复依赖或应用初始化错误，不要改用运行中的旧服务生成快照。

前端基础验证：

```bash
pnpm --dir frontend typecheck
pnpm --dir frontend test
pnpm --dir frontend build
```

### 视觉与无障碍测试

Playwright 使用固定只读 API fixture，不写真实 Cookie、Token 或业务数据。运行：

```bash
pnpm --dir frontend test:a11y
pnpm --dir frontend test:visual
```

视觉基线覆盖手机、平板、桌面的明暗主题，存放在 `docs/frontend/baseline/screenshots/`。普通 `test:visual` 只比较，不覆盖截图；只有人工确认页面改动后才运行：

```bash
pnpm --dir frontend test:visual:update
```

更新后逐张检查页面内容、版本、长文本、遮罩和移动端布局。System Agent、桌宠或其它专项交互还要运行对应的 Playwright spec，不能只依赖静态基线。

## 插件与交互开发

插件运行时、事件信封、MessageOps、身份、存储、HTTP、AI 和安全边界已经拆成专项文档：

- [插件开发指南](docs/PLUGIN-DEV-GUIDE.md)
- [插件开发铁律](docs/PLUGIN-RULES.md)
- [插件 API 参考](docs/PLUGIN-API-REFERENCE.md)
- [插件安全边界](docs/PLUGIN-SAFETY.md)
- [开发者工具链](docs/PLUGIN-DEVTOOLS.md)

常用命令：

```bash
make plugin-new name=my_game profile=session_game
make plugin-check dir=plugins/local_imports/my_game
make plugin-register dir=plugins/local_imports/my_game
```

插件先在 dry-run 下验证，再用命中调试、短期 router trace 和录制回放确认入口。涉及 payout 时提供稳定幂等键，不在插件内自行补发。

原命令入口是回归底线。新增 Interaction Bot、按钮或 Webhook 入口时，不能破坏已有 UserBot 命令路径；通道、事件、payload、result、session、结算和 fallback 必须在 manifest 与文档中一致。

## System Agent 开发

System Agent 通过注册表调用有限业务工具，禁止万能 SQL、Shell、HTTP 和文件工具。新增工具时：

1. 先确认已有稳定 service，没有就先补 service 与测试。
2. 在对应领域注册 `ToolSpec`，给出有限 JSON Schema 和脱敏返回结构。
3. 只读工具返回事实，写工具实现 preview 与 execute，并进入 Action 确认。
4. 为 Web 与管理 Bot 的账号作用域、管理员权限、取消、重试和失败语义补测试。
5. 把关键过程写入 Durable Run event，避免只存在于临时流式连接。

工具结果、网页内容、源码和日志都属于外部数据，不能覆盖 system prompt。源码工具只读白名单目录，网页读取每次重定向都要重新做公网地址校验。完整约束见 [System Agent 文档](docs/SYSTEM-AGENT.md)。

## 提交前验证

普通后端或文档改动至少运行：

```bash
git diff --check
cd backend
.venv/bin/ruff check app
.venv/bin/pytest -v
cd ..
backend/.venv/bin/python scripts/validate-plugin-examples.py
```

普通前端改动至少运行：

```bash
pnpm --dir frontend typecheck
pnpm --dir frontend test
pnpm --dir frontend build
```

涉及页面、PWA、导航、布局或交互时，再运行：

```bash
pnpm --dir frontend test:a11y
pnpm --dir frontend test:visual
```

测试失败时记录命令、环境、第一条可行动错误和是否阻塞当前改动。静态检查、构建和 fixture 测试不能替代真实 VPS、NAS 或 Telegram E2E。

## 文档同步

以下改动必须在同一批更新对应文档：

- 公开 API、环境变量、默认开关或错误码。
- fail-open / fail-closed 语义、资金状态机或插件契约。
- 页面主入口、平台模块、部署步骤、备份恢复或更新流程。
- System Agent 工具、权限、Action 确认和隐私边界。

长期文档不要硬编码容易过期的当前版本号。版本历史放在 `CHANGELOG.md`，当前版本以四个源版本文件与生成的 OpenAPI `info.version` 共五处一致为准。接口请求体以 FastAPI `/docs` 和 Pydantic schema 为准，CLI 参数以 `--help` 为准。

## 版本与发布

- `main` 是稳定发布线，只接收已验证的 beta。
- 开发分支使用目标正式版本的 `-beta.N`。同一候选继续修复时只递增 beta 序号；加入新的 minor 或 major 级变化时提升基础版本并从 `beta.1` 开始。
- 开发过程中把变更写入 `CHANGELOG.md` 的 `Unreleased`，不要为每个小提交单独迭代版本号。
- 准备 beta 检查点、稳定发布、推送稳定检查点、创建 release/PR，或维护者明确要求发版时，才统一决定版本号。
- 发布前执行 `git fetch origin --prune`，同时检查 `origin/main` 与仍活跃的发布/开发分支。
- beta 检查点和稳定发布必须同步更新 `backend/app/__init__.py`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/src/lib/version.ts`，运行 `make codegen` 同步 `openapi/telepilot.openapi.json` 的 `info.version` 与 `frontend/src/api/schema.ts`，并用中文更新 `CHANGELOG.md`。
- Commit、PR 和 release 文案使用中文。

SemVer 判断：破坏兼容为 MAJOR，用户可感知的新能力或主入口变化为 MINOR，Bug、文案、小 UI、测试和兼容补丁为 PATCH。0.x 阶段的 `0.X.0` 表示阶段性能力版本，`0.X.Y` 表示同阶段补丁。

## PR 范围

适合直接准备 PR：

- 有复现和回归测试的 Bug 修复。
- 文档、示例和测试补充。
- 维护在 `examples/plugins/` 且已纳入 `validate-plugin-examples.py` 稳定公开 API gate 的示例插件。
- 边界清楚、依赖不变的小功能和小幅 UX 调整。

先开 issue：

- 数据库 schema、公开 API 或配置格式变化。
- 新依赖、大型重构、主入口和信息架构调整。
- 资金、鉴权、插件信任模型、Bot 消费方式和生产部署变化。
- 破坏 SemVer 兼容或需要迁移用户数据的改动。

提交主题使用中文，并说明实际影响：

```text
新增：系统助手接入持久运行事件
修复：流式响应中断时拒绝伪成功
文档：同步插件 AI facade 契约
测试：补充 payout ambiguous 回归
重构：提取协议适配器，不改外部行为
```

## 常见排查

| 现象 | 检查顺序 |
| --- | --- |
| 改了 Worker 代码但行为没变 | `make restart`，再用 `make status` 和 `make logs be` 确认旧 Worker 已退出 |
| `/healthz` 正常但服务不可用 | 请求 `/readyz`，检查 PostgreSQL、Redis、Supervisor 和两个 Bot manager |
| 前端登录请求在本地失败 | 确认后端监听 `127.0.0.1:8000`，Vite 代理使用项目默认配置，不要把开发目标改回可能解析到 IPv6 的 `localhost` |
| 迁移后接口 500 | `cd backend && . .venv/bin/activate && alembic current && alembic heads`，再检查模型与迁移是否同批提交 |
| 插件页面状态不一致 | 依次检查安装记录、账号启用状态、平台能力、manifest、Worker reload 确认和运行日志 |
| Telegram 消息重复消费 | 检查同一用户账号 Worker、Bot polling 和 webhook 是否被多个进程或部署同时消费 |

## 安全漏洞

不要公开提交包含利用细节、密钥或可复用攻击路径的 issue。使用 GitHub Security Advisories：`Security → Report a vulnerability`。生产侧应急处理见 [安全运维 SOP](docs/SECURITY-OPS.md)。

## 协作约定

- 对人友好，对代码严格。
- Issue 和 PR 要给出复现、预期行为、验证命令和风险边界。
- 不在讨论里发布密钥、Token、session、真实账号信息或未脱敏日志。
- 不在人身、身份和动机层面争论技术问题。
- 不在 issue 或 PR 中推广无关项目和服务。

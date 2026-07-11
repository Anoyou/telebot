# TelePilot 全项目代码审查报告

审查日期：2026-07-11
审查分支：`codex/0.33-interaction-framework`
审查提交：`84d933cad4948d95deae2bce4cd5ba65c1f96c21`
项目实际版本：`0.55.13`

## 0. 模型与审查口径

- 界面所选模型：`5.6 Sol`，思考等级 `High`（由用户确认，代理侧无法独立验证）。
- 实际运行模型：当前工具未暴露，无法核验。
- 子 Agent 模型：协作工具未返回模型标识，无法核验。
- 本报告只把能够从当前代码、命令输出或测试结果复核的内容写成事实；架构演进建议会明确使用“建议”表述。
- 审查期间没有修改业务代码，也没有触碰原有未跟踪文档。

## 1. 执行摘要

TelePilot 已经不是一个简单的 UserBot 面板，而是包含多账号进程隔离、插件运行时、AI 路由、交互 Bot、资金动作、Trace、Webhook 和在线更新器的完整自动化平台。项目的功能覆盖、测试规模、类型检查、日志脱敏和接口契约已经明显超过普通个人脚本。

当前最主要的风险不在语法错误或普通单元逻辑，而在以下系统边界：

1. Worker、数据库和 Redis 之间的状态收敛不完整。
2. 资金发送是外部副作用，但账本和补偿记录仍是事后 best-effort 写入。
3. 插件能力 facade 被称为“沙箱”，但并不隔离任意 Python 代码。
4. Redis 同时承载会话、幂等、限额、IPC 和日志，却采用无持久化的 `allkeys-lru`。
5. Updater 持有 Docker socket，Web 又能调用 Updater，扩大了插件或 Web 失陷后的影响范围。
6. 备份脚本和迁移回滚流程不足以支撑当前系统的真实持久化面。

综合评分：**6.8 / 10**。

| 维度 | 评分 | 结论 |
| --- | ---: | --- |
| 架构 | 7.2 | 账号隔离、事件契约和平台 facade 方向正确，但状态机分散 |
| 代码质量 | 6.4 | 测试充分，但核心文件体积过大、职责密度过高 |
| 工程化 | 7.6 | CI、版本同步、类型检查和插件校验较完整 |
| 性能与风险 | 5.8 | 资金一致性、Redis 策略、插件隔离和灾备需要优先补强 |

## 2. 总体架构评估

### 2.1 多账号 Worker 模型

优点：

- 每个账号使用独立 `spawn` 子进程，能隔离 Telethon Session、插件实例、事件循环和单账号崩溃。
- Worker 进程在导入数据库引擎前标记进程角色，并使用更小的数据库连接池，适合小型 VPS。
- 主进程保持单 Uvicorn Worker，避免重复启动账号 Supervisor。

不足：

- 账号状态同时存在于数据库、Supervisor 内存、Worker 和 Redis，缺少统一状态迁移表。
- 每账号一个 Python 进程使 RSS、数据库连接、Redis 连接、插件模块和重连压力随账号数线性增长。
- 当前没有足够证据说明 10、50、100 账号下的 RSS、连接数、启动耗时和重连峰值。

建议：保留每账号进程隔离，但显式定义 `starting/running/login_required/backoff/stopping/dead` 状态机；为启动和重连增加全局并发上限；建立多账号基准测试，而不是直接改成线程池或共享 Telethon 进程。

### 2.2 Redis IPC 与状态存储

Redis 同时承担 Pub/Sub IPC、交互会话、去重 claim、资金限额、AI 预算、运行日志和可靠队列。生产配置默认 `appendonly=no`、不保存快照、32MB、`allkeys-lru`（`docker-compose.yml:43-58`）。

这意味着内存压力下任何 key 都可能被淘汰，包括活跃会话、幂等标记和限额计数。Redis 重启还会清空全部非数据库状态。建议至少拆分关键状态与缓存：关键会话和资金幂等落 PostgreSQL，或使用独立 Redis DB/实例配合 `noeviction` 与 AOF；只有可重建缓存使用 LRU。

### 2.3 插件运行时

Manifest、generation guard、权限声明、HTTP/AI/消息 facade、Event Bus 和 Trace 的总体方向合理。它们能够降低可信插件误用平台能力的概率，并提供统一审计入口。

但是 `SandboxClient` 只代理 Telethon Client；插件仍以普通 Python 在 Worker 中执行，可以导入 `os`、`socket`、`subprocess` 或读取应用模块与环境变量。它不是恶意代码隔离边界。个人信任插件可以接受这种模型，但仓库被接管、发布包被替换、自动更新后代码变化仍属于真实供应链风险。

### 2.4 交互框架和 AI 框架

标准事件信封、Action、Trace、交互通道路由和 legacy 兼容层较完整。主要性能问题是插件逐个串行执行且没有通用执行期限（`backend/app/worker/plugins/loader.py:732-803`、`:5738-5741`）。一个慢插件会拖延同一事件的所有后续插件。

建议对插件增加超时、熔断、慢调用指标和连续失败隔离。只有确定无副作用的观察型订阅适合并发；资金和会话型事件仍应按 `account + chat + session` 串行。

AI Provider、fallback、usage 和插件 AI facade 已经统一。下一步应把所有预算保护统一为可配置的 strict 模式，避免 Redis 故障时撤销成本上限。

### 2.5 Session、MASTER_KEY 和个人信任模型

Telethon Session、API 凭据和部分 Token 使用 Fernet 加密，并提供 rekey 工具，这是正确基础。但 `MASTER_KEY` 与解密后的敏感数据仍存在于 Web/Worker 进程环境和内存中。同进程插件可以绕过 facade 直接读取这些信息。

对于个人自用，最重要的不是制造“绝对安全”错觉，而是：

- 明确当前插件信任等级等同于本机代码执行信任。
- 关闭未签名插件的默认放行。
- 对自动更新后的插件重新展示权限和源码版本变化。
- 备份 `MASTER_KEY`，但不要把它与数据库备份存放在同一位置。

## 3. 具体发现

### 3.1 High：失效 Session 事件没有消费者

证据：Worker 在未授权时只向 `worker_event:{account_id}` 发布 `EVT_LOGIN_REQUIRED`（`backend/app/worker/runtime.py:673-678`）。Supervisor 只订阅 `worker_global`（`backend/app/worker/supervisor.py:637-658`），全仓没有账号事件频道消费者。

影响：数据库可能继续保持 `active`；Supervisor 不知道应该转为 `login_required`，会不断拉起失效账号，最终还可能误标为 `dead`。

建议：Supervisor 消费账号事件并事务更新状态，或者让 Worker 退出前可靠持久化 `login_required`。增加 Session revoked 集成测试。

### 3.2 High：Worker 指数退避实现与日志语义不一致

证据：进程在某次两秒探测时存活便立即清零 `fail_count`（`backend/app/worker/supervisor.py:678-681`）；崩溃后设置未来 `next_retry_at`，却在同一轮立即调用 `start_worker`（`:724-732`）。

影响：存活两秒后再崩溃的 Worker 很难累计失败次数；所谓“等待 5/10/20 秒后重启”实际是立即重启，容易造成重连风暴。

建议：持续健康达到稳定窗口后才清零失败计数；崩溃时只记录下一次启动时间，由后续 tick 到期后启动。

### 3.3 High：Redis 故障时 kill switch 可能无法停止 Worker

证据：`stop_worker` 在本地 `terminate/join` 之前无保护地执行 Redis publish（`backend/app/worker/supervisor.py:560-579`）。publish 抛异常会直接退出函数。批量停止又逐账号串行执行（`:597-600`）。

影响：最需要紧急停止主动动作时，Redis 故障会让 Worker 继续运行；第一个账号失败还会阻止后续账号停止。

建议：IPC 通知放入 `try`，无论成功与否都执行本地终止；批量停止逐账号隔离异常并验证 PID 全部退出。

### 3.4 High：首次注册存在并发竞态

证据：注册接口先 `COUNT(web_user)` 再插入（`backend/app/api/auth.py:169-180`）；模型只约束 `username` 唯一，没有“全表只能一行”约束（`backend/app/db/models/user.py:18-20`）。

影响：首次开放服务时，两个不同用户名的并发请求都可能观察到零用户并成功创建管理员。

建议：固定 singleton 主键，或使用 PostgreSQL advisory lock/Serializable 事务；补两个不同用户名并发注册测试。

### 3.5 High：资金限额在故障时 fail-open

证据：读取限额配置失败直接允许，Redis 日累计失败也直接允许（`backend/app/services/payout_limit.py:89-116`）。

影响：基础设施故障恰好撤销了单笔或日累计保护，自动 payout、scheduler 和补偿扫描仍可能继续出账。

建议：只要用户配置过限额，自动付款路径应 fail-closed；可使用数据库或本地保守额度降级，并触发 kill switch/高优先级告警。

### 3.6 High：补付存在重复发送窗口

证据：Telegram `send_message` 成功后才写 Redis sent marker（`backend/app/worker/runtime.py:459-472`）；marker 写失败被吞掉（`backend/app/services/payout_compensation.py:122-139`）；之后才把数据库补偿单标为 sent（`backend/app/worker/runtime.py:1536-1546`）。

影响：如果 Redis 不可用且进程在数据库提交前退出，租约到期后可能再次发送同一 `+amount`，造成重复付款。

建议：引入持久化 `sending` 意图状态；不确定结果统一进入历史消息核验；最佳方案是目标记账 Bot 接受业务幂等键。

### 3.7 High：外部资金动作与账本之间没有可靠提交边界

证据：ActionEvent 在外部发送完成后才写入，数据库错误被吞掉并进入短暂熔断（`backend/app/services/action_tap.py:225-238`）。PostgreSQL 还默认使用 `synchronous_commit=off`（`docker-compose.yml:16-27`）。

影响：真实动作已经发生，但账本可能缺行；主机崩溃时近期已返回成功的数据库事务也可能丢失。这使当前“资金台账”更接近观察日志，而非严格财务账本。

建议：发送前写不可变 intent，发送后以条件更新写结果；保留 reconciliation job。资金/审计表启用同步提交，至少不要继承全库 `synchronous_commit=off`。

### 3.8 High：交互中心会被无关管理 Bot 请求拖垮

证据：`mode="interaction"` 时仍请求管理 Bot 和授权用户（`frontend/src/pages/Accounts/BotTab.tsx:1918-1932`）；随后无条件访问 `bot.enabled`（`:2389-2393`），直到 `:3511` 才按 mode 返回交互内容。

影响：管理 Bot 请求 404、500 或网络失败时，即使交互 Bot API 正常，交互中心也可能抛异常进入全局 ErrorBoundary。

建议：按 mode 启用查询，并在构造任何派生 JSX 前完成模式分支；为各 Query 增加明确错误态。

### 3.9 High：备份脚本卷名错误并漏掉插件持久化卷

证据：`deploy/backup.sh:32-39` 和 `deploy/restore.sh:26-51` 默认使用 `telebot_sessions`。当前 Compose 实际声明 `sessions`、`plugins_installed`、`plugin_repos`（`docker-compose.yml:108-113`、`:163-169`）。

影响：脚本可能创建并备份一个空卷，却正常报告成功；灾难恢复会丢失 Session、安装插件或私有插件仓库缓存。

建议：通过 `docker compose config` 或 Compose labels 动态解析真实卷；备份 DB、Session、installed plugins 和必要仓库数据；生成 checksum，并在 CI/测试机执行完整恢复演练。

### 3.10 High：插件或 Web 失陷可借 Updater 扩展到宿主机

证据：Web 获得 updater token，且默认复用 JWT secret（`docker-compose.yml:90-91`）；Updater 使用相同值并挂载工作区和 `/var/run/docker.sock`（`:126-142`）。Updater token 为空时甚至直接放行（`deploy/updater/server.py:349-352`）。

影响：同进程插件或 Web RCE 可调用 Updater；Docker socket 通常等价于宿主机 root 权限。

建议：默认不部署常驻 Updater。若保留，使用独立随机凭据、Unix socket 或 mTLS、受限 docker-socket-proxy、固定签名 tag/commit，并让 Updater 与 Web/插件网络隔离。

### 3.11 Medium：配置备份默认包含 Webhook token

证据：`system_settings` 导出没有排除字段（`backend/app/api/system_health.py:2016-2021`），而 Webhook token 明文存入 `SystemSetting`（`backend/app/api/webhooks.py:124-145`）。前端还把“系统设置”描述为普通全局配置，没有敏感标记（`frontend/src/pages/Settings/ConfigBackup.tsx:44-57`）。

影响：用户关闭“包含敏感数据”时，导出文件仍可能包含账号级 Webhook token。

建议：按 SystemSetting key 做敏感分类；默认排除或加密 `account_webhooks:*` 等 Token；补回归测试。

### 3.12 Medium：ZIP 安装缺少解压资源上限

证据：只检查 ZIP 原始字节数，然后直接 `extractall`（`backend/app/services/plugin_install_service.py:103-126`）；成员校验只处理绝对路径和 `..`（`:172-186`）。

影响：高压缩比 ZIP 可以耗尽磁盘、inode 或临时目录空间，阻断 Web 服务。

建议：限制成员数、单文件大小、总解压大小、压缩比和特殊文件，流式解压并实时累计。

### 3.13 Medium：插件兼容检查发生在代码执行之后

证据：installed plugin 先通过 `exec_module` 执行（`backend/app/worker/plugins/loader.py:4191-4203`），之后才检查身份和兼容版本（`:4237-4259`）。安装上传时 `manifest.py` 也会直接执行（`backend/app/services/plugin_install_service.py:218-234`）。

影响：最终显示为“不兼容”或“拒绝加载”的插件，顶层副作用已经发生。

建议：签名覆盖纯 JSON manifest；安装和启用前只解析数据并校验版本/依赖，通过后才启动隔离 runtime。

### 3.14 Medium：日志 verdict 过滤结果不完整

证据：后端最多读取 `limit * 3` 条事件，再在 Python 中计算和过滤 verdict（`backend/app/api/logs.py:728-762`）。前端固定请求 100 条（`frontend/src/pages/Logs.tsx:171-187`）。

影响：符合条件的事件如果位于第 301 条之后，即使仍在选定时间窗内也不可见，可能误判为“没有失败/卡住消息”。

建议：将 verdict 或可推导状态持久化为可查询字段，或使用可继续扫描的游标分页，并返回 `has_more/total`。

### 3.15 Medium：日志筛选制造请求风暴，Trace 详情却会过期

证据：多个输入状态直接进入 query key（`frontend/src/pages/Logs.tsx:171-225`），输入框 `onChange` 立即更新（`:357-378`、`:414-415`）；列表每五秒刷新，但已展开详情没有刷新间隔（`:221-225`）。

影响：输入长关键词或 ID 会产生大量请求和 Query cache 条目；与此同时，运行中 Trace 的详情仍可能保持旧 spans/actions。

建议：文本筛选增加 200-400ms debounce；ID 和时间范围使用“应用筛选”；列表刷新后 invalidate 当前 Trace，完成态停止轮询。

### 3.16 Medium：请求失败被伪装为空数据

证据：交互中心只处理 accounts loading，错误时 `data ?? []` 并显示“暂无账号”（`frontend/src/pages/Interaction/Index.tsx:61-66`、`:103-160`）。命中调试和 Webhook 页面存在同型实现。

影响：断网或后端 5xx 会误导用户去添加账号或重新配置 Token，掩盖真实故障。

建议：建立统一 QueryState 组件，按 error、loading、empty、success 顺序渲染，并保留重试入口。

### 3.17 Medium：后台插件配置任务重启后永久卡住

证据：任务行提交后仅使用裸 `asyncio.create_task` 启动，任务引用和完整输入没有持久化（`backend/app/services/plugin_config_action_jobs.py:67-97`）。

影响：FastAPI 重启或异常退出会留下永久 `queued/running` 任务，用户无法判断是否安全重试。

建议：使用持久任务队列；最低限度持久化完整输入、保留 Task 引用、shutdown 标记中断，并在 startup 收敛遗留任务。

### 3.18 Medium：迁移更新缺少强制备份和可信回滚

证据：更新脚本检测到迁移只输出建议备份，随后继续 pull（`scripts/prod-update.sh:129-132`、`:178-192`）；容器启动自动执行 Alembic upgrade（`docker-compose.yml:121-124`）；回滚说明只 checkout 旧代码（`deploy/README.md:113-121`）。

影响：Schema 已升级后，旧代码可能无法运行；“代码回滚”不等于数据库恢复。

建议：迁移前强制成功备份并校验；采用 expand/contract；发布材料声明数据库兼容范围和是否支持 downgrade。

## 4. 代码质量与最佳实践

### 4.1 后端

优点：

- FastAPI 依赖注入、AsyncSession 生命周期、JWT 密码版本、CSRF、Argon2、TOTP 和日志脱敏基础较扎实。
- 全量测试规模较大，Ruff 当前通过。
- Trace 使用有界异步批处理，避免普通观测写入直接阻塞关键消息路径。

问题：

- `account_bot_runtime.py` 约 8838 行、`loader.py` 约 7037 行、`system_health.py` 约 2300 行，模块边界已经无法靠命名保持清晰。
- 多个领域存在“捕获所有异常后降级或吞掉”的策略，需要明确哪些可以 best-effort，哪些必须 fail-closed。
- 账号状态、补偿状态和后台任务状态缺乏统一的条件更新/版本控制模式。

### 4.2 前端

优点：

- TypeScript 类型检查通过，路由级 lazy loading、PWA、CSRF axios 拦截器和 TanStack Query 基础合理。
- 未发现直接使用 `dangerouslySetInnerHTML` 导致的明显 XSS；Markdown 未启用 raw HTML。

问题：

- `BotTab.tsx`、`Extensions.tsx`、`Logs.tsx` 等页面过大，查询、表单、派生状态和 UI 混在同一组件。
- 前端没有 Vitest/RTL 测试脚本；多数 loading/error/empty 行为只能靠人工发现。
- 生产构建主 index chunk 约 473KB、ECharts 约 484KB、Markdown 约 334KB（未压缩），仍有进一步按入口拆分空间。

### 4.3 插件系统

Manifest、权限、配置 schema、版本兼容和插件合同校验器已经形成较完整开发体验。当前需要把“可信插件能力治理”和“恶意插件隔离”在命名、文档和部署上分开。

### 4.4 资金、风控与审计

已有 payout key、补偿单、限额 Lua、ActionEvent、Trace 和人工核销入口，说明系统已经考虑失败恢复。但当前仍无法保证：

- 外部发送成功必然有账本记录。
- 同一补偿绝不会重复发送。
- 风控基础设施失败时自动付款会停止。
- 人工核销和补偿重放具备完整并发条件更新。

资金模块应优先从“日志型记录”升级为“持久发送意图 + 状态机 + 对账”。

## 5. 性能与延迟优化

1. 为插件调用记录 P50/P95/P99，并对单插件设置 deadline。
2. 对纯观察订阅安全并发，资金/会话动作保持有序串行。
3. `ledger_service.py` 不应加载 30 天 ActionEvent 后在 Python 聚合；将金额、方向、chat、recipient 提升为结构化列并使用 SQL `SUM/GROUP BY` 与覆盖索引。
4. 日志列表改游标分页；大列表采用虚拟化；筛选增加 debounce。
5. 复用 LLM HTTP client/连接池时按 Provider 与代理维度管理生命周期，避免每次调用重新握手。
6. 建立 10/50/100 账号资源基准：RSS、数据库连接、Redis 连接、启动时间、CPU 峰值、FloodWait 和重连频率。
7. 对 Worker 启动、热重载和 Telegram 重连增加全局 Semaphore，避免同时冲击数据库和 MTProto。

## 6. 安全强化建议

1. 生产默认 `plugin_allow_legacy_unsigned_plugins=false`。
2. Manifest 改纯数据格式，兼容检查在任何代码执行前完成。
3. 第三方插件放入非 root 独立进程/容器，最小环境变量、只读文件系统、`no-new-privileges`、受控网络出口。
4. Updater 不复用 JWT secret，不向 Web/插件暴露 Docker socket 等价能力。
5. Webhook token、恢复码、Bot Token 和仓库凭证建立统一敏感字段注册表，导出和日志脱敏共用。
6. 资金和 kill switch 采用 fail-closed；故障时宁可暂停自动动作，也不要静默继续。
7. `MASTER_KEY` 与数据库备份分离保存；定期演练 rekey 和完整恢复。

## 7. 部署与运维

### 7.1 推荐轻量架构

对于 1C/1GB 个人 VPS，建议保留：

- PostgreSQL：权威数据与关键会话/幂等状态。
- Redis：短期 IPC 和缓存；关键部署使用 `noeviction` + AOF everysec。
- 单一 TelePilot 应用容器：FastAPI、Supervisor 与静态前端；可在同一镜像中由 Nginx/Caddy 提供静态资源。

移除常驻 Updater。更新由宿主机 systemd timer 或 CI SSH 执行：拉取签名 tag、备份校验、运行一次性 migration job、切换镜像、健康检查、失败时恢复数据库和旧镜像。

macOS 推荐 OrbStack、Colima 或 Docker Desktop 运行 Compose，但不挂载 Docker socket updater。纯开发可用宿主机 Uvicorn/Vite，PostgreSQL 与 Redis 放容器。

### 7.2 可观测性

至少增加以下告警：

- Redis eviction、内存使用率、AOF 状态。
- Trace queue dropped 数量。
- Worker crash/backoff/login_required 状态变化。
- 数据库备份年龄、大小和最近恢复演练结果。
- payout pending/sending/ambiguous 数量与最长滞留时间。
- 插件 P95/P99 执行时间和连续超时次数。

## 8. Telegram 风控与合规提醒

- UserBot 使用个人账号能力，不等同于 Bot API。主动私聊、批量入群、频繁群发、高频重连和自动资金行为都可能触发 FloodWait、PeerFlood 或账号限制。
- 每账号应设置主动动作总上限、冷启动期、重连上限和全局 kill switch，而不是只依赖插件声明。
- Trace 默认保存 sender ID、名称、文本预览和 payload（`backend/app/services/event_trace.py:99-115`）。在多人群或商业使用场景下，应提供明确告知、按账号/群排除、数据导出/删除和保留期设置。
- 对资金玩法或群互动，应明确自动化身份、规则、付款失败处理、申诉方式和数据用途。
- 项目应避免承诺绕过 Telegram 风控；拟人化只能降低突发行为，不能保证账号安全。

## 9. 优先级清单

### 立即修复（High）

1. Session 失效状态收敛。
2. Supervisor 真实指数退避。
3. Redis 故障时仍能强制停止 Worker。
4. 首次注册并发锁或数据库 singleton 约束。
5. payout 限额 strict/fail-closed 模式。
6. 补付重复发送窗口和持久化发送意图。
7. 资金表同步提交与可靠账本。
8. 交互中心管理 Bot 空值崩溃。
9. 备份卷动态解析和完整恢复演练。
10. Updater 与 Docker socket 权限隔离。

### 强烈建议（Medium）

1. Redis `noeviction` + AOF，并拆分关键状态和缓存。
2. Webhook token 导出过滤。
3. ZIP 解压资源配额。
4. 插件兼容检查前置。
5. 日志 verdict SQL 化、分页和筛选 debounce。
6. 统一前端 Query error/loading/empty 状态。
7. 持久后台任务队列。
8. 数据库迁移强制备份门和真实回滚说明。
9. 前端 Vitest/RTL/MSW 状态测试。

### 未来改进（Low）

1. 拆分超大模块和组件。
2. 日志筛选同步 URL，支持复现排障上下文。
3. PWA 更新状态由 Service Worker 生命周期驱动。
4. Bundle analyzer 和日志列表虚拟化。
5. 插件 SBOM、签名供应链和权限差异展示。
6. 隐私数据导出、删除和保留策略。

## 10. 重构路线图

### 短期（1-2 周）

- 修复全部 High 项。
- 关闭未签名插件默认放行。
- Redis 改为 `noeviction`，资金和 kill switch 改 strict。
- 修复备份卷并完成一次真实恢复演练。
- 增加 Supervisor、并发注册、Redis 故障、重复补付和交互页错误态测试。

### 中期（1-2 月）

- 引入资金发送意图和持久任务队列。
- 拆分 account bot runtime、plugin loader、BotTab 和 Logs。
- 插件 deadline、熔断和性能指标。
- CI 增加真实 PostgreSQL migration、Compose build、备份恢复和前端状态测试。
- 将日志 verdict、资金统计和常用聚合下推数据库。

### 长期

- 独立 plugin-runner 与最小权限 RPC。
- 签名插件供应链、SBOM 和不可变版本。
- 外部资金幂等协议与自动对账。
- 隐私导出/删除和群级数据保留策略。
- 基于真实账号规模数据决定是否引入 Worker 分片或独立节点。

## 11. 验证结果

| 验证 | 结果 |
| --- | --- |
| `backend/.venv/bin/python -m pytest backend/app/tests -q` | 1459 passed，3 skipped，7 warnings |
| `backend/.venv/bin/ruff check backend/app` | 通过 |
| `pnpm typecheck` | 通过 |
| `pnpm build` | 通过，Vite 2847 modules，PWA precache 84 entries |
| `scripts/validate-plugin-examples.py` | 通过；translate 历史示例按规则跳过 |
| `scripts/validate-installed-interaction-plugins.py` | 通过；6 个插件存在 usage/旧声明警告 |
| `alembic heads` | 单一 head：`0035` |
| `alembic current` | 未验证；本机 PostgreSQL 5432 未运行 |
| `git diff --check` | 通过 |
| 当前 HEAD 与远端同名分支 | 一致 |

测试通过说明已有行为覆盖较广，但不会自动覆盖进程崩溃窗口、Redis 故障、数据库提交与外部 Telegram 发送之间的分布式一致性问题。因此本报告中的 High 项仍需要专项故障注入和集成测试。

## 12. 修复实施记录（2026-07-11）

本轮在审查提交之后直接实施了可安全落地的高优先级与中优先级修复，未发布、未提交，也未迭代版本号；实际变更记录在 `CHANGELOG.md` 的 `Unreleased`。

已完成：

- Worker Session 状态落库、真实指数退避、Redis 故障本地关停。
- 数据库级单管理员约束与登录会话并发 claim。
- payout 风控 fail-closed、补偿 `sending` 租约与不确定投递人工收敛、人工核销 CAS。
- 插件配置任务重启/关闭收敛，以及 ActionEvent 降级可观测性。
- 交互页空值崩溃、错误态、ID 校验、日志筛选防抖与 Trace 刷新。
- 敏感配置默认排除、插件 ZIP 解压资源预算。
- Updater 独立强 token、Redis AOF/noeviction、真实卷备份、插件卷备份、校验和与迁移前备份门禁。

仍属于架构边界，不能在一次兼容补丁中诚实宣称“完全解决”：

- Telegram 与 PostgreSQL 不支持分布式事务或服务端幂等键，资金动作无法证明数学意义的 exactly-once；当前策略是在不确定时停止自动补发并转人工核对。
- 任意 Python 插件仍与账号 Worker 同进程运行，facade 不是恶意代码沙箱；完整隔离需要独立插件 runner、权限收缩和 IPC 协议。
- Updater 已改用独立强 token 且缺失时 fail-closed，但容器仍持有 Docker socket；Updater 自身一旦失陷仍等同宿主机高权限，彻底收缩需要 rootless Docker、socket proxy 白名单或宿主机受限更新服务。
- 历史 ZIP 插件仍以 `manifest.py` 表达元数据，无法在不执行 Python 的情况下完成全部兼容检查；远程插件的 `plugin.json` 路径已经静态解析，统一迁移需单独做格式兼容版本。
- verdict 是运行期推导值，当前查询仍不是数据库原生可分页字段；彻底解决需持久化 verdict 或引入可继续扫描的游标协议。
- 本地环境没有运行中的生产 Docker 数据卷，备份与恢复脚本完成了语法、配置和静态检查，但真实灾难恢复演练仍应在隔离副本上执行。

实施后验证：

| 验证 | 结果 |
| --- | --- |
| 后端全量测试 | 1488 passed，3 skipped，7 warnings |
| Ruff | 通过 |
| 前端 TypeScript 与生产构建 | 通过；Vite 2847 modules，PWA precache 84 entries |
| 插件示例与已安装交互插件校验 | 通过；历史插件保留 usage/旧声明警告 |
| Alembic heads | 单一 head：`0036` |
| Shell 语法、Compose 配置、`git diff --check` | 通过 |
| Alembic current | 本机 PostgreSQL 5432 未运行，无法执行在线 current/upgrade 演练 |

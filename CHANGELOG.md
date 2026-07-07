# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [SemVer](https://semver.org/lang/zh-CN/)：MAJOR.MINOR.PATCH。

- **MAJOR（主版本）**：破坏兼容的数据库迁移、配置格式变更、API 路径或语义不兼容、老版本无法平滑升级。
- **MINOR（次版本）**：用户可感知的新能力、主要入口/信息架构重组、后端能力完整前端化、新插件或重要工作流变化。
- **PATCH（补丁版本）**：bug 修复、文案、小 UI、错误提示、测试补充、兼容性补丁和不改变主要用户路径的小调整。

0.x 阶段额外约定：

- **0.X.0** 表示一个阶段性能力版本，例如交互框架、部署体验、插件系统、主入口重组等可命名的一批能力。
- **0.X.Y** 表示同一阶段内的修复、体验补丁、文档补充、兼容性更新或测试补齐。
- 不再把第三位当作每日流水号；不再使用 `feature` / `fix` / `polish` / `hotfix` / `refactor` 作为版本级别，正式段落统一写 `minor（次版本）` 或 `patch（补丁版本）`。

> 不要为每个微小提交单独迭代版本号。开发过程中先把变更积累在 `Unreleased`；只有准备发布、推送稳定检查点、创建 release/PR，或用户明确要求“推一版/发一版”时，才按本批改动的最高影响级别统一 bump 一次版本号。
>
> 发布时版本号在 4 处必须保持同步：`backend/app/__init__.py`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/src/lib/version.ts`。同时把 `Unreleased` 内容移动到新的正式版本段落，并使用中文更新说明。`backend/app/main.py` 通过 `from . import __version__` 自动跟随，无需单独改。

---

## [Unreleased]

## [0.51.2] — 2026-07-08 · patch（补丁版本） · PWA 顶部与资源面板补丁

### Changed
- 概览页“资源采样”信息移入“资源占用”区域，并拆分展示资源采样时间、宿主机运行时间和当前 TelePilot 项目运行时间，避免把服务器开机时间误读成项目运行时间。

### Fixed
- 修复 PWA 模式切换浅色主题后，iPhone 顶部安全区仍显示灰色或旧主题色的问题；启动脚本、主题切换逻辑、PWA manifest 和页面 chrome 背景现在会同步跟随浅色/深色主题。
- 修复生产 CSP 未随主题启动脚本更新的问题，避免线上冷启动阶段的主题同步脚本被浏览器拦截。
- 修复窄屏浏览器模式点击左上角导航菜单只出现遮罩、不显示抽屉的问题；移动抽屉定位和位移动画现在不会被通用玻璃态容器样式覆盖。
- 新增资源接口的项目运行时间字段，按当前 TelePilot 后端进程启动时间计算，并补充后端测试防止再次与宿主机运行时间混用。

## [0.51.1] — 2026-07-08 · patch（补丁版本） · 移动端与 PWA 体验补丁

### Changed
- 窄屏顶栏改为保留 TelePilot 品牌露出，并使用紧凑菜单图标作为浏览器窄屏导航入口，减少与 PWA 模式的视觉割裂。
- 首页与 AI 页面顶部统计卡在窄屏下改为 2x2 布局，设置页“添加模型 / 添加指令 / 绑定管理 Bot”三个入口改为同一行展示。
- 交互规则列表在窄屏下直接展示玩法、玩法版本、规则状态、触发方式、监听群和关键词摘要；选中规则后默认展示当前规则摘要，配置编辑器改为按需展开，减少规则切换时的卡顿感。
- 插件配置页标题区新增当前插件版本徽章，便于区分玩法与玩法版本。
- PWA 离线缓存提示改为“前端资源已缓存”，并说明这只表示控制台静态资源可离线打开，不代表 Bot 离线运行。

### Fixed
- 修复窄屏浏览器模式下点击左上角菜单只出现遮罩、不弹出导航抽屉的问题；抽屉定位现在由专用样式固定为 `position: fixed`，避免被玻璃态容器样式覆盖。
- 修复 PWA 顶部通知可能被 iPhone 刘海区域遮挡的问题，通知容器现在会避让顶部和左右安全区。

## [0.51.0] — 2026-07-08 · minor（次版本） · 交互框架第二轮结构收敛版本

### Added
- 新增只读聚合接口 `GET /api/plugins/installed-overview`，一次返回已安装插件的来源、版本、更新状态、全局启停、账号启停矩阵、最近加载错误与最近 trace 引用，供后续插件中心详情页复用。
- 插件管理页的“已安装插件”接入聚合视图，新增插件详情弹窗，可一页查看来源版本、更新状态、签名与信任等级、Lint 告警、账号启停矩阵、最近加载错误和 trace 跳转。
- `ctx.ai.stream_complete()` 新增真实 streaming 能力，支持 Responses 与 Anthropic Messages 协议按增量文本输出，并纳入账号级 LLM 预算与 usage 记账；旧 `ctx.ai.complete()` 继续保持整段返回行为不变。

### Changed
- 将 worker 侧 `ai_runtime.invoke` 拆分为请求构建、provider 调用、图片递送与文本渲染等内部 helper，保持现有 AI 命令行为不变，为后续流式发送和更细排障留出结构。
- 收敛交互模块 payload 的信封归一化逻辑，集中处理 `source`、`message`、`chat`、`raw`、`reply_to` 与 native raw 字段，保留现有扁平兼容字段和收付款通道约束。
- 账号级 LLM 预算失败对账会释放请求、token 与高价 provider 预扣，失败调用不再占用每分钟请求数或每日请求数名额。

### Fixed
- 将账号级 LLM 预算从基于历史用量查询的检查改为 Redis 原子预扣，覆盖每分钟请求、每日请求、每日 token 与高价 provider 每日调用次数；Redis/DB 异常继续 fail-open，并在调用失败或 fallback 成功时释放或修正预扣。

## [0.50.0] — 2026-07-07 · minor（次版本） · 消息排障与插件管理中心版本

### Added
- 日志中心新增“消息四段检查”，按当前账号、关键词、Chat ID、Message ID 或 trace 条件，把一次消息链路拆成收到、路由、插件、发送四段，便于直接判断消息卡在哪一步。
- 插件管理页新增 ZIP 上传安装入口，复用后端签名校验与安装服务；已安装插件列表新增账号级启停展开面板，可直接查看并切换不同账号的插件启用状态。

### Changed
- 清理历史交互框架与插件体验 `PLAN` 快照文档，避免计划稿继续和当前源码事实混在一起。

### Fixed
- 修复 UserBot 直通模式成功消费消息时没有 trace 的排障黑洞：直通链路现在记录 receive、route、plugin_invoke，并把插件最近 trace_id 写入运行状态。
- 修复 ZIP/package 插件卸载后残留账号启停、Feature 与全局配置的问题，卸载时会清理对应 `AccountFeature`、非内置 `Feature` 和 `PluginGlobalConfig`。
- 修复两个服务模块 import 排序不符合 Ruff 规则的问题，使全仓 `ruff check app` 可通过。

## [0.49.27] — 2026-07-07 · patch（补丁版本） · 订阅校验可见化补丁

### Added
- 插件 `event_subscriptions` 声明现在会在加载期暴露未知 filter key 与未知事件类型：远程 `plugin.json` lint 会说明未知字段的实际后果，Python `Manifest` 路径也会在插件加载时记录一次 warning，帮助尽早发现订阅拼写或事件类型错误。

## [0.49.26] — 2026-07-07 · patch（补丁版本） · 交互会话消费语义补丁

### Fixed
- 修复同一 chat 内只要存在一个交互 Bot 观察会话，该 chat 的全部 UserBot 消息就会被判为已消费、导致同群其它 Event Bus / `on_message` 插件被静音的问题；现在只有会话真正接手消息时才消费，无关消息会继续下放给后续插件链路。
- 保持 `channel="userbot"` 命令会话的接管语义不变：UserBot 通道会话即使本次不返回动作，也仍会消费当前会话消息，避免会话内输入误落到其它插件。

## [0.49.25] — 2026-07-07 · patch（补丁版本） · 交互轮询死信补丁

### Added
- 新增交互 / 管理 / 转账测试三条 Bot polling loop 共用的失败 update 死信队列（DLQ）：单条 update 重试耗尽后仍保持现有 offset 推进行为，但会把原始 update、失败原因、loop 来源与重试次数写入 Redis HASH + ZSET，避免 `payment_confirmed` 等关键事件无痕丢失。
- 新增 `GET /api/accounts/{aid}/bot/polling-dlq`、`POST /api/accounts/{aid}/bot/polling-dlq/{loop}/{update_id}/replay`、`DELETE /api/accounts/{aid}/bot/polling-dlq/{loop}/{update_id}` 死信管理端点，支持列出、重放与丢弃失败 update；重放成功会删除死信，重放失败会保留并更新错误与重放次数。
- 交互 Bot 配置响应新增 `polling_dlq_count`，用于提示当前账号是否存在待处理 polling 死信。

### Fixed
- 修复 Bot polling 中某条永久失败 update 在重试耗尽后仅推进 Telegram offset、没有持久化原文，导致后续无法追查和补偿的问题；DLQ 写入失败会被隔离记录，不会反向阻塞主链路 offset 推进。

## [0.49.24] — 2026-07-07 · patch（补丁版本） · 交互链路缺陷修复补丁

### Fixed
- 修复事件总线 `commands`/`command` 过滤器遇到无文本事件（纯媒体、贴纸、语音、callback_query 等 `message.text` 为空的更新）时 `"".split()[0]` 抛 `IndexError`、导致整轮订阅匹配崩溃的问题；空文本对命令过滤器直接判定不匹配，不再中断匹配流程。
- 修复 userbot 直通链路（`on_direct_message`）复用常驻 `ctx` 的问题：直通此前是唯一未做调用级隔离的分发路径，并发直通事件会共享同一个 `ctx.messages`，其 `actions` 列表跨 `apply()` 只增不清、长期泄漏。直通现在与其他链路一致，为每次调用复制独立的 `_LiveMessageOps`，`apply()` 也在进入前和结束后清空动作缓存，字段保留但不再累积。

### Changed
- 优化 userbot 会话消息派发：`_dispatch_userbot_session_message` 此前对每条 incoming/outgoing 消息都执行一次 Redis `SCAN` 查活跃会话，即使该群无任何会话。现在改为先查内存索引 `userbot_session_chats` 短路——仅当全量缓存刷新成功（新增 `userbot_session_chats_ready` 闸）且该群不在集合中时才跳过 SCAN；缓存未就绪（进程刚起、reload 后或 Redis 抖动刷新失败）时保持原有全量扫描，避免"空集合被误判为无会话"而漏投真实会话。

## [0.49.23] — 2026-07-07 · patch（补丁版本） · 插件发奖锚点提示补丁

### Fixed
- 补齐插件 live userbot 消息动作路径的 `suppress_reply_anchor_missing_notice` 支持；发奖或扣款找不到玩家近期消息时，可只返回动作失败结果，不再额外发送群内默认锚点缺失提示。

## [0.49.22] — 2026-07-06 · patch（补丁版本） · 十点半按钮入局拦截补丁

### Fixed
- 修复 `paid_pool` 会话已有参与者后，平台会在插件前拦截“加入牌局”和“规则”按钮的问题；十点半无感入局现在会把非庄家的加入点击交给插件处理，不再直接返回旧的“点点点”提示。
- 修复按钮扣款通知里付款方显示匿名或空白时，平台无法还原真实点击玩家的问题；UserBot 回复扣款动作现在会记录被回复用户，后续扣款通知可回填真实 `payer_user_id`。
- 未入局玩家误点牌桌操作时，提示文案改为明确说明需要先加入本局，便于线上排障和玩家理解。

## [0.49.21] — 2026-07-06 · patch（补丁版本） · 插件群聊白名单选择器补丁

### Added
- 通用插件配置页新增 `allowed-peer-multi-select` 控件，可从当前账号“允许会话”中选择群聊并保存为 Chat ID 数组，供远程插件实现群聊白名单。

### Fixed
- 红包插件等远程插件声明群聊白名单字段后，不再退回普通数组输入框；配置体验与交互规则里的监听群选择保持一致。

## [0.49.20] — 2026-07-06 · patch（补丁版本） · 按钮扣款失败反馈补丁

### Fixed
- 修复按钮触发 userbot 发奖或扣款时，找不到玩家近期消息只能在群内发送默认提示、点击者收不到即时失败反馈的问题；动作现在可通过 `failure_callback` 回答 callback。
- 失败回调成功回答 callback 后会标记为已处理，后续 `answer_callback` 动作不再重复 ack 同一个点击事件。
- 支持 `suppress_reply_anchor_missing_notice` 抑制群内默认锚点缺失提示，避免静默扣款入局失败时额外刷群。

## [0.49.19] — 2026-07-06 · patch（补丁版本） · 插件仓库缓存清理补丁

### Fixed
- 修复插件仓库缓存强制刷新只执行 `git reset --hard`、未清理未跟踪目录的问题；历史 `.bak-*` 插件目录不再被插件中心扫描成重复插件。
- 插件仓库刷新现在会在重置到远端引用后执行 `git clean -fd`，避免“插件显示最新但缓存里仍混有旧目录”的安装与更新排障噪音。

## [0.49.18] — 2026-07-06 · patch（补丁版本） · 付款投递重启窗口补丁

### Fixed
- 修复账号 worker 重启或插件热加载短暂窗口内，已匹配到付费插件的 `payment_confirmed` 事件可能因 Redis 暂无订阅者而直接丢失的问题；平台现在会在入口超时内短暂重试投递，再判定 worker 离线。
- 事件链路日志在 worker 离线失败时记录 `publish_attempts`，便于排查十点半等付费插件“到账已匹配但未入局”的边界。

## [0.49.17] — 2026-07-06 · patch（补丁版本） · 扣款通知与无感入局支撑补丁

### Added
- 转账通知 Bot 支持回复玩家发送 `-金额` 生成“扣减成功”模拟通知，并新增可配置的扣款通知模板，便于十点半无感入局模式测试与联调。
- 账号 Bot 配置页新增扣款通知模板编辑入口，和收款通知模板保持同一套预览、保存与后端校验链路。

### Fixed
- 扣减通知解析支持 HTML 模板与“扣减”语义，玩家作为付款方、userbot 作为接收方进入统一付款确认链路，确保插件仍按平台标准资金证据处理入局。
- 回复交互 Bot 或其他平台 Bot 发送 `+金额` 时，不再回退到规则默认收款人误触发付费插件；只有规则显式配置收款方时才允许继续匹配。

## [0.49.16] — 2026-07-06 · patch（补丁版本） · 活跃付费会话到账投递补丁

### Fixed
- 修复十点半等 `paid_pool` 插件已有活跃会话时，平台仍先按规则金额过滤到账通知的问题；现在付款确认会先投递给当前插件会话，由插件按本局实际底注自行判断，避免规则金额和大厅底注不一致时玩家无法入局。
- 补充活跃付费会话中“规则金额与到账金额不一致也应投递插件”的回归测试，防止后续再次把插件内金额判断前置到平台规则层。

## [0.49.15] — 2026-07-06 · patch（补丁版本） · 交互 Bot 收款误触发补丁

### Fixed
- 修复玩家在交互 Bot 通道里回复交互 Bot 或转账通知 Bot 的消息发送 `+金额` 时，平台仍回退到规则默认收款人并生成模拟到账通知的问题；这类误操作现在不会触发付费插件。
- 到账通知匹配增加收款人平台 Bot 身份防线：通知里带出的收款人 ID 若是交互 Bot、转账通知 Bot 或可信通知 Bot，不再匹配任何付费规则。
- 为“回复交互 Bot 不应触发付款规则”和“到账通知收款人 ID 是平台 Bot 不应匹配规则”补充回归测试。

## [0.49.14] — 2026-07-06 · patch（补丁版本） · 交互会话旁观事件补丁

### Fixed
- 修复交互 Bot 会话中，UserBot 旁观到同一条玩家答题消息后仍继续进入 legacy `on_message` 的竞态问题；九宫格猜骰这类插件不再被 UserBot legacy 路径抢先结算，从而能等待交互 Bot 逻辑通道生成 `edit_caption` 并原地更新题图 caption。
- 为 UserBot 旁观交互 Bot 会话“插件返回空动作也必须消费事件”的边界补充回归测试，避免后续再次出现发奖正常但原消息未编辑的问题。

## [0.49.13] — 2026-07-05 · patch（补丁版本） · 媒体动作与插件热更新补丁

### Added
- 标准交互动作新增 `edit_caption`，支持按 `message_id` 或 `message_id_key` 编辑 `send_photo` / `send_file` 产生的媒体 caption；交互 Bot 走 Bot API `editMessageCaption`，UserBot 通道走账号 worker 编辑同一条媒体消息。
- `send_photo` / `send_file` 现在支持 `save_message_id_key`，媒体题面发送成功后可保存 Telegram `message_id`，供后续 `edit_caption`、删除或替换动作复用。
- 交互 Bot 通道的 `send_file` 现在走 Bot API `sendDocument`，不再把文件动作误当图片发送；`send_photo` / `send_file` 的消息 ID 保存和后续 caption 编辑行为保持一致。
- `ctx.messages` 新增 `send_photo()`、`send_file()` 与 `edit_caption()` facade，插件可继续只生成标准 action，不接触 Bot Token、UserBot session 或 live client。

### Fixed
- 修复插件更新 IPC 偶发未执行时，周期配置同步只刷新配置、不强制重载已加载插件，导致页面版本已更新但 worker 内存仍运行旧插件逻辑的问题；现在会按安装记录版本与更新时间检测内存漂移并自动清缓存重载。

## [0.49.12] — 2026-07-05 · patch（补丁版本） · 交互路由与发奖排障补丁

### Fixed
- 修复交互 Bot 活跃会话的普通消息和 callback 仍可能先被宽泛 Event Bus 订阅消费的问题；10 点半这类有按钮和多阶段选择的玩法会先回到当前插件会话，未命中会话时仍回退 Event Bus。
- 修复严格 `input_schema.additionalProperties=false` 的插件规则保存时，平台会把历史额外字段继续写入 `module_config` 的问题；现在只保留插件 schema 明确声明且非平台控制的字段。
- 修复 worker 内 userbot 发奖 / 回复失败日志 helper 缺失的问题，并补齐 `chat_id`、`amount`、`reply_to_message_id`、`reply_to_user_id`、`reply_to_search_limit`、`error_code`、`worker_offline`、`reply_anchor_missing` 等结构化排障字段。

### Docs
- 插件开发文档按真实三模式更新：裸直通只覆盖 userbot 原始 Telethon event，userbot 命令会话与 interaction bot 规则会话走标准链路，`payout`、收付款和发奖始终由 userbot 执行。

## [0.49.11] — 2026-07-05 · patch（补丁版本） · Event Bus 付款会话补丁

### Fixed
- 修复 Event Bus 付款订阅直接投递插件入口时没有保存交互会话的问题；math10、24 点等只返回普通开局消息的插件，现在付款开局后会正确接续玩家答题消息并生成后续 `payout`。
- 修复 Event Bus 付款 payload 没有带入规则奖金和会话 envelope 的问题；交互规则里配置的 `module_prize` / `prize` 会覆盖默认奖金，Trace 中也能看到明确的 `session_key` 与 `session_scope`。

## [0.49.10] — 2026-07-05 · patch（补丁版本） · 后台交互动作补丁

### Fixed
- 修复插件交互入口里延迟任务复用 `ctx.messages` 时无法执行 `apply()` 的问题；十点半等需要后台刷新大厅、超时开局或延迟结算的插件现在可以继续通过 TelePilot 标准动作链路发送消息。
- 后台标准动作现在继承当前会话逻辑通道：交互 Bot 会话默认继续由交互 Bot 发送，UserBot 命令会话默认由 UserBot 回复，`payout` 仍始终由 UserBot 执行。

## [0.49.9] — 2026-07-05 · patch（补丁版本） · 交互会话发奖补丁

### Fixed
- 修复交互 Bot 付款/关键词触发的插件会话由 UserBot 观察到答题消息时没有投递回插件的问题；math10、24 点、10 点半等互动插件现在能按会话逻辑通道处理玩家回复，并继续通过 UserBot 执行 `payout` 发奖。
- 修复 UserBot 观察到的交互 Bot 会话执行 `end_session` 后未删除 Redis 会话的问题，避免答对后同一会话继续吞后续消息。
- 修复 UserBot 观察到的交互 Bot 会话返回 `update_session` 时被误判为 `session_not_found` 的问题，多阶段玩法可以正常写回 `session.data`。

## [0.49.8] — 2026-07-05 · patch（补丁版本） · 发奖锚点提示与规则参数补丁

### Added
- `payout` 与 `send_message(userbot_reply)` 支持 `reply_anchor_missing_text`，当平台找不到 `reply_to_user_id` 对应用户在当前群的近期发言时，会默认提示 `未找到对应用户（用户 ID）的近期消息。`，插件也可以自定义失败提示文案。
- 插件开发文档补充免费参与、按钮加入和互动游戏的发奖锚点最佳实践：插件可按自身玩法保存完整状态，仅从发奖锚点角度保留玩家 `tgid`，由平台负责搜索近期发言并回复发奖。

### Fixed
- 修复 Event Bus 付款/消息订阅入口返回 `start_session` 动作时没有先写入交互会话的问题，确保后续按钮和消息能接续到同一插件会话。
- 修复交互规则保存时，严格 `input_schema.additionalProperties=false` 的插件可能把旧的额外参数继续写回配置，导致插件实际读取到过期参数的问题；前后端现在都会按插件配置 schema 清理不应保存的字段。
- 找不到发奖回复锚点时不再只写 action 失败日志，会同步发送默认或插件自定义的用户可见提示，避免管理员只能从后台日志判断失败原因。

## [0.49.7] — 2026-07-04 · patch（补丁版本） · UserBot 发奖锚点执行补丁

### Fixed
- 修复 UserBot Event Bus 直执行路径里的 `payout` / `userbot_reply` 动作忽略 `reply_to_user_id` 的问题；现在插件返回目标用户 ID 时，会先搜索该用户在当前群的近期发言，再回复 `+金额`。
- 修复常驻插件命令上下文 `ctx.messages` 缺少 `payout()` 方法的问题，配置成自定义命令词的测试/发奖插件也能走平台统一 MessageOps、Trace 与限流链路。
- 为 loader 直执行路径补齐回归测试，覆盖 `payout`、`send_message(userbot_reply)` 和 live `ctx.messages.payout()`。

## [0.49.6] — 2026-07-04 · patch（补丁版本） · TOTP 策略与按钮参与发奖补丁

### Added
- `userbot_reply` 发奖动作支持 `reply_to_user_id` 与 `reply_to_search_limit`：免费答题、抽奖、按钮加入等玩法可用按钮确认参与，结算时由 userbot 在当前群搜索玩家近期发言作为回复锚点，不再要求玩家为了领奖额外发送一条消息。
- 事件探针和插件开发文档补充按钮参与者发奖锚点示例，说明可从 `payload.sender.user_id` 获取按钮点击者，并通过 MessageOps / 标准 action 交给平台代发和记录 Trace。

### Fixed
- 修复 TOTP 登录验证开启后总是每次登录都要求二次验证码的问题；设置页现在可选择“每次登录都验证”或“连续输错后验证”，并可单独配置失败阈值。
- 修复通知 Bot OTP 关闭时登录失败次数不记录的问题，确保 TOTP 的“连续输错后验证”模式可以独立生效。
- 修复按钮参与玩法发奖只能依赖玩家本轮发言的问题；当插件声明赢家 user_id 但没有明确 `reply_to_message_id` 时，平台会自动寻找该玩家近期消息，找不到则失败并记录原因，避免误发无锚点奖励消息。

## [0.49.5] — 2026-07-04 · patch（补丁版本） · 插件文档与裸直通分类补丁

### Changed
- 插件中心新增“裸直通”分类，将声明 `telegram_direct_passthrough.enabled=true` 的插件从普通互动/工具分类中单独归组，并在卡片中显示裸直通能力与高风险提示。
- 插件开发文档、Quickstart、HTTP / AI facade 示例和示例插件移除普通回复里的候选 `send_via` 模板，明确普通动作默认继承 `session.channel`，只有跨通道覆盖、迁移兼容或钱相关动作才显式指定通道。

## [0.49.4] — 2026-07-04 · patch（补丁版本） · 裸直通与会话通道补丁

### Fixed
- 修复 UserBot 裸直通仍会先经过 incoming 白名单、交互 Bot 关键词接管等普通链路 guard 的问题；开启 `telegram_direct_passthrough` 的插件现在会在普通消息分发前收到原始事件广播。
- 修复 `ctx.messages.send/edit/delete/pin` 默认写死 `interaction_bot` 的问题；普通动作默认不再携带通道选择，由平台按 `session.channel` 或 UserBot 来源链路补齐。
- 修复 UserBot Event Bus 与命令侧 live MessageOps 默认输出可能落到交互 Bot 的问题；无显式通道的 UserBot 侧普通消息动作默认归一为 `userbot_reply`。
- 修复默认通道补齐会污染 `answer_callback` / `answer_inline_query` 动作记录的问题，ACK 类动作不再携带误导性的 `send_via`。

### Docs
- 统一插件开发文档中的术语：严格意义上只有 `telegram_direct_passthrough` 是裸直通，Event Bus、会话入口和 legacy hook 都属于消息分发或会话路由。
- 明确“触发方式决定会话通道，插件默认不感知通道；`payout`、收款确认和发奖永远路由 userbot”的插件开发口径。

## [0.49.3] — 2026-07-04 · patch（补丁版本） · 推荐插件本体剥离补丁

### Changed
- 删除本体中残留的 `builtin/auto_reply` 与 `builtin/autorepeat` 插件实现，明确自动回复、自动复读只作为插件库推荐插件分发。
- 规则 dry-run 改为从已安装插件动态调用 `_dry_run_match`，未安装、禁用、签名不通过或插件未导出时返回明确提示，不再从 Core builtin 路径兜底。
- 同步修正测试、前端类型注释和消息链路文档中的旧 builtin/official 插件口径。

## [0.49.2] — 2026-07-04 · patch（补丁版本） · 插件库来源语义补丁

### Changed
- 推荐插件入口改为只从插件库读取并按普通 `repo` 安装记录落库，不再把 `auto_reply`、`autorepeat`、`math10` 等普通插件标记为 TelePilot 本体随包或内置插件。
- 删除仍被 Git 跟踪的随包 `official` 插件源码目录，明确 TelePilot 本体只保留平台能力与兼容壳，普通插件统一通过插件库安装到 `plugins/installed/{key}`。
- 前端和插件开发文档同步“插件库推荐/维护插件”口径，旧 `source=official` 仅作为历史安装记录兼容展示。

## [0.49.1] — 2026-07-04 · patch（补丁版本） · 交互插件加载与发奖补丁

### Fixed
- 修复旧版 `math10` 交互规则在 0.49 统一加载链路中没有同步为账号插件特性，导致插件已安装且规则启用时仍报“模块未加载或未启用”的问题。
- 修复 `payout` 发奖动作没有回写 `save_message_id_key` 的问题，确保十点半等插件发奖后仍能按消息 ID 执行后续清理或编辑。

## [0.49.0] — 2026-07-04 · minor（次版本） · 登录策略与交互路由收口

### Changed
- TOTP 登录验证新增“每次登录验证 / 连续输错后验证”策略与失败阈值设置；旧版已开启 TOTP 的配置保持每次登录验证，不会在升级后自动降级。
- 交互 Bot 主链路新增 R15 路由分类器与缓存索引，普通未命中消息可在命中判断阶段直接跳过，不再逐层触发转账、Event Bus、关键词规则和会话 handler。
- 已安装互动插件校验脚本同步新开发指南口径：显式 `capabilities: {}` 不再被误报为缺失，并强制校验 `plugin.json` 与 `MANIFEST` 的事件订阅 / 能力声明一致性。
- 远程插件库已按最新开发指南完成全量契约迁移：补齐 `usage`、`event_subscriptions`、`capabilities`，移除旧 notice 通道声明，并为交互插件补充 `session_expired` 会话过期兼容。

## [0.48.0] — 2026-07-04 · minor（次版本） · 消息链路统一与会话通道

### Added
- 新增“触发方式决定会话通道”模型：UserBot 命令开局后的会话统一由 userbot 收发，关键词 / 付款 / callback 开局后的会话统一由交互 Bot 收发，发奖 `payout` 始终通过 userbot 执行。
- 新增 `session.channel`、`session.expires_at`、`update_session` 与双通道 `session_expired` 投递，插件可把单局状态稳定写入 `session.data`，不再依赖进程内字典和自建超时任务。
- 新增 manifest `triggers.command`、`default_trigger_modes`、运行时 `interaction_trigger_modes`、`callback_fast_ack`，并支持 userbot 会话中按钮文本降级和合成 callback。
- 标准事件信封统一以 Event Bus 归一化结果为基底，补齐 `message.entities`、媒体摘要、消息时间、群标题 / username、`tp_event` 投影、`all_events`、`message_edited` 与稳定 reason code。

### Changed
- 已安装互动插件迁移到新模型：`guess_number` 改为单一 `on_interaction` 参考实现，使用 `session.data + update_session + payout`；`poetry_blank`、`dice_grid_hunt` 同步迁移核心交互入口，`lottery_plus`、`pt_promote` 补齐触发契约。
- 主进程交互 loop 改为按 chat 保序、跨 chat 并发处理，并把运行时游标搬出配置行；worker RPC 支持后台并发，worker 离线时入口 / 动作调用快速失败。
- 发送层默认 `parse_mode="plain"`，显式支持 HTML 模式和 `html_escape()`；Bot API HTTP client 复用连接池，userbot 发送与 payout 接入限速引擎。

### Fixed
- 修复交互 update 处理失败会静默推进 offset、callback 异常后按钮长时间转圈、`known_users` scope 恒真、关键词规则缺交互 Bot 时仍能保存等消息链路缺陷。
- 修复转账通知无规则、会话读写失败、动作截断、守卫丢弃等路径缺少 trace / runtime log 的问题，失败原因现在能在链路日志里定位。
- 修复 userbot 会话内消息无法喂入标准 `on_interaction`、按钮在 userbot 通道被静默剥除、同群双 bot 容易跨通道误投递的问题。

### Removed
- 删除旧的 worker auto-award 文案协议、交互 Bot 中奖公告解析和主进程 math10 本地答题 fallback；互动玩法发奖统一使用 `payout` 动作。

### Docs
- 补充消息链路统一阶段的插件开发文档，明确“触发方式 -> 会话通道”、单一 `on_interaction` 生命周期、`session.data` / `update_session`、`payout`、`tp_event`、`parse_mode="plain"` 与 `html_escape()` 的最终约定。
- 同步补充 userbot 按钮文本降级、`all_events` / `message_edited` / `session_expired`、`known_users`、filter / `rule_id`、`reason_code` 排障口径，减少把旧平铺 payload、旧通道选择和旧 notice 文案协议继续当主路径的误解。
- 远程插件与安全文档补充 `keyword_only`、`default_trigger_modes`、`callback_fast_ack`、关键词规则依赖交互 Bot 等收口说明，并注明旧环境未合入对应 runtime/worker 时以实例实际行为为准。

## [0.47.4] — 2026-07-03 · patch（补丁版本） · 插件额外参数保存补丁

### Fixed
- 修复交互规则页保存“插件额外参数与技术详情”时，账号插件配置为空会缺少 `command` 等带默认值必填字段，导致配置验证失败的问题。

## [0.47.3] — 2026-07-03 · patch（补丁版本） · 多可信转账通知 Bot 补丁

### Added
- 交互 Bot 转账确认配置支持多个可信转账结果通知 Bot 来源；旧的单个 `trusted_bot_id` 会自动兼容进新列表，测试模拟通知 Bot 仍会自动加入信任判断。

### Changed
- 账号详情页的转账结果通知 Bot 配置从单个 ID 输入改为多行 ID 列表，适配多个群分别使用不同独立转账结果 Bot 的场景。

## [0.47.2] — 2026-07-03 · patch（补丁版本） · 转账通知与匿名身份补丁

### Changed
- 账号详情页交互 Bot 配置中，将“真实转账结果通知 Bot ID”和“测试模拟通知 Bot Token”文案拆开，并显示测试模拟 Token 自动识别出的 Bot ID，避免把真实通知 Bot 和测试 Bot 混填。

### Fixed
- 修复 `0.47.1` 引入的回归：玩家在交互 Bot 规则里发送 `+金额` 时，如果测试模拟通知 Bot 不在当前群或无权发消息，不再让整个交互 update 抛出 `Bad Request: chat not found`；系统会记录中文警告并继续等待真实转账结果通知。
- 补齐 userbot 侧群组匿名管理员 / 频道身份归一化；当 Telegram 只提供频道/群身份或签名时，Event Bus 会把 `sender_chat`、签名和群身份稳定下发给插件，避免被误识别为普通用户。

## [0.47.1] — 2026-07-03 · patch（补丁版本） · 交互路由与日志探针补丁

### Added
- 日志 Trace 详情新增“开发探针”，可从标准事件信封、路由 span 和动作记录生成字段路径、订阅建议、MessageOps 动作建议、能力提示和未命中告警，方便按真实消息样本开发和排查插件。
- Event Bus 标准消息摘要补充实体、媒体、按钮、回复、转发、频道身份、via bot、话题线程、编辑和服务消息等信息，减少插件开发时对完整 `native_raw` 的依赖。

### Changed
- 前端构建授权迁移到 pnpm 11 可识别的 `pnpm-workspace.yaml`，同时保留 Docker 构建固定 pnpm 10 的路径，避免本机门禁因 esbuild 构建脚本授权失败。

### Fixed
- 修复交互 Bot 付费门槛链路：玩家发送 `+金额` 会重新进入交互 Bot 转账模拟入口，回复玩法 Bot 提示时不再把玩法 Bot 误判为收款人。
- 修复付款通知 Event Bus 旁路：外部转账通知必须先命中交互规则的群、金额、收款人和入口后才会投递给对应插件，避免十点半、24 点、猜骰、算数题等付费玩法互相吃错转账。
- 保存交互规则时会自动同步启用规则引用的账号插件，并通知 worker 热加载，避免规则存在但插件账号启用状态缺失导致入口不启动。
- 修复群组匿名管理员消息的身份识别：交互 Bot 和 Event Bus 会把 Telegram `sender_chat` 作为群身份下发给插件，匿名身份不会被误当成账号本人或普通用户。
- 修复账号详情页交互规则的“插件额外参数与技术详情”只保存规则副本的问题；保存规则时会同步写回对应账号插件配置，并让使用同一入口的规则保持一致。

## [0.47.0] — 2026-07-03 · minor（次版本） · 登录安全套件

### Added
- Web 登录新增可配置的登录安全套件，默认关闭；支持通知 Bot OTP 防爆破、TOTP 登录验证、服务器一次性恢复码有效期等策略统一配置。
- Web 登录新增“密码失败后通知 Bot OTP”挑战层：密码错误达到阈值后，下一次正确密码会先通过已配置的通知 Bot 发送 6 位登录验证码，验证码短时有效且一次性使用。
- 新增服务器一次性登录恢复码：可通过 `make auth-recovery` 或 `python -m app.scripts.auth_recovery` 生成，只保存哈希，必须在密码正确后使用，用于通知 Bot/TOTP 不可用时防止管理员被锁在系统外。
- 系统设置的当前用户页新增 TOTP 绑定与登录验证开关：可先生成密钥并验证，再决定是否强制登录时输入 TOTP。

### Changed
- 通知 Bot OTP 防爆破默认关闭，只有在系统设置里显式开启后才会记录失败次数并触发二次验证码。
- 登录页支持通知 Bot 验证码和服务器恢复码输入；TOTP 阶段也允许使用服务器恢复码兜底，但恢复码不能绕过密码。

## [0.46.8] — 2026-07-02 · patch（补丁版本） · 插件热重载缓存修复

### Fixed
- 修复安装型插件热重载只依赖加载瞬间模块清单的问题；运行期懒加载的插件子模块、以及来源位于插件目录下的辅助模块，现在会在更新/重载时一并清理，避免插件更新后仍执行旧代码。
- 修复 `reload_config(plugin_key)` 在清理插件注册表后可能无法注销旧命令的问题；插件命令现在按 owner 一次性清理，避免插件更新后已删除或改名的命令继续触发旧 handler。

## [0.46.7] — 2026-07-02 · patch（补丁版本） · 十点半管理员加入补丁

### Added
- 交互插件 payload 新增账号 owner/userbot 身份字段，插件可明确识别“系统 userbot 账号本人”，不再需要把开桌者、庄家或首位玩家当作管理员猜测。
- 交互 Bot 动作链路支持插件返回 `start_session` 后合并会话参与者，用于插件在运行中补充 paid/participant 玩家列表。

### Fixed
- 修复付费大厅中系统 userbot 本人无法通过“加入”免转账入局的问题；普通玩家发送或点击“加入”仍必须按底注转账。
- 修复插件返回 `start_session` 动作时交互 Bot 投递层可能把它当作普通未知动作记录噪音的问题；会话更新现在由运行时统一写入 Redis。

## [0.46.6] — 2026-07-02 · patch（补丁版本） · 插件配置动作保存修复

### Added
- 插件配置页新增最近配置动作状态入口；后台动作窗口关闭、最小化或页面刷新后，可以从配置页恢复查看执行过程与日志。
- 后端新增账号插件配置动作任务列表接口，用于前端恢复最近运行中的 URL 抓取、AI 整理等长任务。

### Fixed
- 修复插件配置动作成功后只把 `config_patch` 写入任务记录、没有自动保存到插件配置的问题；现在任务成功返回配置补丁时，会按 schema 层级自动写入账号级或全局配置，并通知插件热加载。
- 配置动作自动保存只校验本次补丁字段，避免旧版遗留配置中的非本次字段阻塞题库生成结果落库。
- 配置动作完成文案会去掉“请保存配置后生效”的旧提示，改为明确显示已自动保存。
- 修复安装型插件更新后 `feature` 索引仍可能保留旧 manifest 的问题；配置页会优先使用已安装插件的最新 `plugin.json` 元数据、版本、使用说明和交互声明。

## [0.46.5] — 2026-07-02 · patch（补丁版本） · 插件工作区分页修复

### Fixed
- 修复插件中心顶部分页选中态错误的问题；插件中心现在选中“插件中心”，不再误高亮“插件管理”。
- 将同一套插件工作区分页补到指令模板、定时任务、自动指令白名单和插件管理页，避免进入子页后插件管理入口消失，并保持与 AI 工作台一致的分页形态。

## [0.46.4] — 2026-07-02 · patch（补丁版本） · 插件与交互页体验补丁

### Changed
- 插件中心顶部入口改为与 AI 工作台一致的分页式导航，减少四个大卡片占用的首屏空间，并保持指令模板、定时任务、自动指令白名单和插件管理的原跳转能力。
- 交互中心移除“事件与动作调试”展示块；“身份配置”和“高级设置”在交互中心默认折叠，保留状态徽章和手动展开入口。
- 插件管理页“推荐插件”默认折叠，保留推荐数量和展开按钮，降低安装页首屏信息噪声。

### Fixed
- 修复安装型插件声明 `forward_message` 权限后，调用 Telethon `client.forward_messages` 仍被沙箱误判为未授权的问题；现在 `forward_message` 能力会正确映射到原生转发方法，并补充回归测试。
- 修复 Event Bus 付款通知投递给插件时重建 `source_actor` 会丢失 `external_bot` 类型的问题，避免插件无法区分通知 Bot 与真实付款人。

## [0.46.3] — 2026-07-01 · patch（补丁版本） · 插件仓库元数据自愈

### Fixed
- 修复插件仓库批量更新时只比较 `installed_plugin.version`，没有同步检查 `manifest_json.version` 与远程更新状态的问题；现在同版本但 manifest 仍停留在旧版本、或仍标记 `update_available=true` 时，会自动刷新安装目录与数据库元数据并触发插件热加载。
- 增加回归测试，覆盖“插件文件与安装版本已经是新版本，但数据库 manifest 仍为旧版本”的脏状态，避免 UI、诊断与 worker 后续读到旧插件契约。

## [0.46.2] — 2026-07-01 · patch（补丁版本） · 付款事件身份修复

### Fixed
- 修复 Event Bus 外部转账通知投递给插件时，`actor` / `player` 可能错误指向转账通知 Bot 的问题；现在付款事件中 `actor`、`player`、`payment.payer_user_id` 会优先使用已解析付款人或被回复消息的真实用户，转账通知 Bot 仅保留在 `sender` / `source_actor`。
- 补齐 Event Bus 付款 payload 的 `payment`、`raw_payment`、`reply_to`、`payer_user_id`、`payer_name` 与 `payment_amount` 字段，让交互插件不用再从通知 Bot 文本里反复猜字段。
- 增加回归测试，覆盖转账通知 Bot 回复玩家 `+金额` 消息时，插件收到的玩家身份必须是真实付款人。

## [0.46.1] — 2026-07-01 · patch（补丁版本） · 插件命令 MessageOps 追踪补丁

### Fixed
- 修复 userbot 插件命令中通过 `ctx.messages.apply` 发出的 `start_session`、发送、编辑等标准动作缺少当前命令 trace 的问题；现在这些动作会写入消息链路的动作记录，排查“命令已启动但后续付款/按钮无反应”时可以直接看到动作成功或失败原因。
- 当插件命令里的 MessageOps 动作部分执行失败时，运行日志会写入中文告警并关联 `trace_id`、`plugin_key` 和 `entry_key`，避免后台动作失败被静默吞掉。
- 补充回归测试，覆盖插件命令通过 MessageOps 写入交互会话时能记录 trace 与 `interaction_session` 动作。

## [0.46.0] — 2026-07-01 · minor（次版本） · 插件低延时直通模式

### Added
- 插件运行时新增 `telegram_direct_passthrough` 低延时直通能力：插件必须先在 manifest 声明高风险能力，账号配置再二次手动开启 `direct_passthrough.enabled=true`，命中后消息会在 Trace/Event Bus/legacy `on_message` 前直接交给 `on_direct_message`，用于抢红包等极低延时场景。

### Changed
- 远程插件文档和示例补充直通模式风险说明，明确普通插件仍应优先使用 Event Bus + Trace + MessageOps 标准链路。

## [0.45.14] — 2026-07-01 · patch（补丁版本） · Userbot 指令边界补丁

### Added
- 系统设置新增“账号本人必须带前缀”开关，默认保持开启；关闭后，仅当前 userbot 账号本人发出的 outgoing 消息可以直接用命令名触发已有 userbot 命令或插件注册命令。

### Fixed
- 修复 0.45.12 为支持群内 userbot 插件命令时误把系统前缀插件命令接入 incoming 群成员消息的问题；群成员发送 `。10d` 这类系统前缀插件命令现在会静默忽略，不再触发 userbot 插件命令链路。
- 保留账号本人发送 `{prefix}命令` 的原有 userbot 命令触发能力，并补充裸命令开关、前缀插件命令和群成员误触发的回归测试。

## [0.45.13] — 2026-07-01 · patch（补丁版本） · 插件配置空对象补丁

### Fixed
- 修复通用插件配置页在全局配置或账号配置为空对象时，表单初始化 effect 因空对象引用不稳定反复执行，导致用户输入被重置、配置参数无法填写的问题。
- 修复旧插件配置弹窗同类空配置对象重置风险，并补齐保存时对原始配置引用的依赖，避免敏感字段和只读字段过滤读取到旧值。
- 通用 schema 表单新增 `type: "object"` 字段编辑支持：有子字段时渲染嵌套表单，无子字段时提供 JSON 对象编辑器，避免对象配置被当作字符串保存。

## [0.45.12] — 2026-07-01 · patch（补丁版本） · 群内 userbot 插件命令触发补丁

### Fixed
- 修复群内成员发送系统前缀插件命令（例如 `。10d 6789`）时，消息只进入 incoming sudo 旧路径且被“自己私聊”限制拦下，导致 userbot 未真正开局的问题。
- 群内 incoming 系统前缀只派发已注册插件命令，不暴露 `。status`、`。sudo` 等内置管理命令，也不改交互 Bot 关键词、入场转账和庄家逻辑。
- 转账测试更新遇到系统前缀 userbot 命令文本时会主动跳过，避免命令消息被误记为转账候选路径，方便日志排查。
- 补充回归测试，覆盖群内成员使用系统前缀触发插件命令、群内系统前缀不暴露内置管理命令，以及转账测试入口跳过系统命令文本。

## [0.45.11] — 2026-07-01 · patch（补丁版本） · Userbot 会话路由补丁

### Fixed
- 明确区分交互 Bot 规则入口与 userbot 直接调用：规则启停、关闭状态、触发模式和静态金额只限制交互 Bot 新入口，不再限制 userbot 已创建的插件活动会话。
- 修复 `。10d 6666` 这类 userbot 命令开局后，对应交互规则处于关闭状态时，后续转账通知仍被 `rule closed` 拦截，导致玩家付款后十点半无反应的问题。
- 补充回归测试，覆盖停用且关闭的规则已有活动会话时，付款加入和按钮回调仍能继续投递给插件。

## [0.45.10] — 2026-07-01 · patch（补丁版本） · 活动会话付款路由补丁

### Fixed
- 修复 userbot 命令已直接启动插件玩法后，如果对应交互规则在配置中处于停用状态，后续转账通知仍只从启用规则中匹配，导致十点半等付费池玩法出现“能开局但付款加入没反应”的问题。
- 已有活动会话的停用规则现在可以继续接收付款通知和按钮/消息事件；停用规则仍不会通过交互 Bot 关键词新开局，也不会出现在玩法列表中。
- 补充回归测试，覆盖停用规则已有活动会话时的付款加入与 callback 投递路径。

## [0.45.9] — 2026-07-01 · patch（补丁版本） · 付款通知识别补丁

### Fixed
- 修复默认转账通知模板把“转账成功”放在代码块语言标记后，Telegram 实际消息正文不包含触发词，导致可信且可解析的付款通知无法匹配十点半等已开局付费池会话的问题；现在付款通知会继续按聊天室、收款人、金额和活动会话校验投递给插件。
- 补充回归测试，覆盖已开局付费池玩法中“通知正文没有触发词但可解析金额和收款人”的加入路径。

## [0.45.8] — 2026-07-01 · patch（补丁版本） · 命令入口隔离补丁

### Fixed
- 修复 `。命令 参数` 这类 userbot 插件命令同时被交互 Bot 当作普通群消息投递的问题；现在系统前缀命令只走 userbot 命令链路，规则关键词才走交互 Bot 规则入口，避免十点半、九宫格等玩法入口串线。

## [0.45.7] — 2026-07-01 · patch（补丁版本） · 付费池会话金额匹配补丁

### Fixed
- 修复付费池玩法已通过关键词开局后，付款通知仍先按规则静态金额过滤的问题；现在已有活动会话时会先把到账通知投递给插件，由插件按本局实际底注判断，避免 `。10d 100` 开局后转 `100` 不触发、转 `1000` 才进入插件的错配。

## [0.45.6] — 2026-07-01 · patch（补丁版本） · 远程插件后台清理补丁

### Fixed
- 修复远程 installed 插件运行时拿不到 `ctx.redis`，导致插件后台任务无法按 `save_message_id_key` 读取并清理已发送消息的问题；十点半等交互玩法结算后可正常删除加入提示、游戏面板、结算和奖励消息。

## [0.45.5] — 2026-07-01 · patch（补丁版本） · 模块玩法金额配置补丁

### Fixed
- 修复交互模块 payload 会把旧算数题默认 `math_prize=123` 兜底写成模块插件 `prize` 的问题，避免十点半、24 点、诗词填空等玩法误把框架默认值当作用户配置金额。
- 模块玩法金额解析统一为显式 `module_prize`、模块配置金额字段、规则付款金额、触发解析金额优先；插件额外参数和技术详情中的门槛金额会覆盖插件默认值。
- 模块 settlement 不再在没有明确金额配置时伪造 `123`，避免日志和插件侧读取到错误奖励/门槛金额。

## [0.45.4] — 2026-06-30 · patch（补丁版本） · 付费池玩法关键词开局补丁

### Fixed
- 修复 `paid_pool` 交互玩法配置了金额过滤后，关键词触发仍被旧“付费娱乐模块”提示拦截，导致十点半等新流程无法先创建大厅、后续转账只能返回“暂无等待中的牌局”的问题。
- 交互事件 payload 新增 `trigger.start_keywords`，插件在付款确认、会话兜底等非关键词事件中也能拿到正确的玩法启动关键词。

### Tests
- 补充 `paid_pool + amount + keyword` 回归测试，确认十点半关键词会进入 `start_ten_half` 插件入口；保留普通付费规则先提示转账的旧行为测试。

## [0.45.3] — 2026-06-30 · patch（补丁版本） · 配置动作后台任务补丁

### Added
- 通用插件配置动作新增后台任务接口：配置页按钮可启动长耗时动作，后端持久化任务状态，页面关闭或弹窗关闭不影响执行。
- 通用插件配置页新增聊天式进度窗口，支持最小化和关闭，实时展示配置动作过程日志，并提供跳转到日志页的入口。

### Changed
- 第三方插件 `ctx.ai` 默认文本调用超时上限从 60 秒调整为 600 秒，适配 URL 抓取后整理题库、长网页摘要等分钟级任务。

### Fixed
- 修复配置动作同步接口遇到插件普通异常时可能冒泡成 500 的问题，现在会归类为可读的配置动作失败。

## [0.45.2] — 2026-06-30 · patch（补丁版本） · 配置动作执行兜底补丁

### Fixed
- 修复配置页已渲染出插件动作按钮，但后端执行时只检查 `feature.manifest`，导致远程插件从 `installed_plugin.manifest_json` 透传的 `generate_knowledge_base` 被误报“未声明配置动作”的问题。
- 配置动作执行入口现在和功能矩阵输出使用同一套声明来源，支持 `feature.manifest`、`installed_plugin.manifest_json` 与 schema `x-config-actions` 兜底。

## [0.45.1] — 2026-06-30 · patch（补丁版本） · 插件配置动作透传补丁

### Fixed
- 修复远程插件 `plugin.json` 顶层 `config_actions` 在安装/仓库更新时没有写入功能 manifest 的问题；通用插件配置页现在能正确渲染快问快答“获取并整理为题库”等字段级动作按钮。
- 功能矩阵输出新增 installed plugin manifest 兜底读取，避免旧 feature 记录缺少 `config_actions` 时前端拿不到插件声明的配置动作。

## [0.45.0] — 2026-06-30 · minor（次版本） · 通用插件配置框架

### Added
- 通用插件配置页支持结构化配置组：数组对象字段可声明为行列表，支持简要信息展示、编辑、删除、复制、启停开关和拖拽/上下移动排序。
- 通用 schema 表单新增 `config-list`、`multi-select`、`list-select`、字段隐藏和字段级动作按钮等声明式控件，插件无需再为常见配置形态编写 TelePilot 专用页面。
- 新增通用插件配置动作 API：插件可在 manifest/schema 声明按钮，由后端注入受控 `ctx.http` / `ctx.ai` 后调用 `on_config_action`，并把返回的 `config_patch` 合并回当前配置表单。

### Fixed
- 修复插件通过 userbot 命令创建交互 Bot 牌局时，缺少交互会话登记导致后续按钮可能无法稳定路由到插件的问题；`ctx.messages` 常驻 facade 现在支持受控写入交互会话。
- 修复 `paid_pool` 参与者校验会把牌局发起人/控制者挡在按钮回调外的问题；已付款玩家外，发起人 callback 会交给插件继续判定，路人仍会被拦截并弹窗提示。

## [0.44.13] — 2026-06-30 · patch（补丁版本） · Responses SSE 空文本补丁

### Fixed
- 修复 Codex/cockpit-tools 反代返回 Responses SSE 时，正文在 `response.output_text.delta` 中、而 `response.completed` 事件只携带状态和 token 用量时，`.ai` 命令能联通但最终回复为空的问题。
- Responses SSE 解析现在会在 completed body 没有正文时保留前序文本增量，避免 Provider 测试、命令 AI 和插件 AI facade 拿到空结果。

### Tests
- 补充 Responses SSE “delta 有正文、completed 无正文”的回归测试，防止后续兼容改动再次丢失流式文本。

## [0.44.12] — 2026-06-30 · patch（补丁版本） · LLM Provider 测试超时补丁

### Fixed
- 修复 AI 页测试 LLM Provider 时，前端仍使用全局 15 秒请求超时，导致慢速 Codex 反代实际已在后端继续执行却被浏览器提前切断并显示 `timeout of 15000ms exceeded` 的问题。
- LLM Provider 的模型拉取、协议探测和测试模型接口现在使用独立长超时；后端测试模型调用也显式放宽等待时间，适配慢推理和多次兼容重试场景。

## [0.44.11] — 2026-06-30 · patch（补丁版本） · Codex 反代 SSE 响应兼容补丁

### Fixed
- 兼容 Codex/CLIProxyAPI 类反代在 Responses 请求中即使传入 `stream=false` 仍返回 `text/event-stream` 的情况；平台现在会解析 `response.completed` 和 `response.output_text.delta` 事件，不再把成功响应误报为“Responses 返回非 JSON”。
- 为 Responses SSE 返回补充回归测试，覆盖完整响应体和纯文本增量两种形态。

## [0.44.10] — 2026-06-30 · patch（补丁版本） · Codex 反代 Responses 兼容补丁

### Fixed
- 修复 cockpit-tools / CLIProxyAPI 这类 Codex 反代不支持 `temperature`、`reasoning`、`stream` 等 OpenAI Responses 参数时，AI 调用直接失败的问题；运行时会识别 `Unsupported parameter` 并逐个剥离可选参数重试。
- Responses 文本和 Responses 生图入口共用同一套兼容发送逻辑，避免只修文本、不修生图的参数兼容缺口。
- Responses 返回非 JSON 时，现在会在错误中带上状态码、content-type 和脱敏后的响应摘要，便于判断是空响应、HTML 网关页还是反代异常。

### Tests
- 补充 Codex 反代式连续参数拒绝、Responses 非 JSON 响应诊断两类回归测试。

## [0.44.9] — 2026-06-30 · patch（补丁版本） · 命令前缀示例补丁

### Fixed
- 修复 AI 图片命令缺少提示词时仍显示默认 `,image` 示例的问题；现在会使用当前命令前缀和当前模板名生成示例。
- 修复自动回复配置页的自动指令白名单说明写死 `,24d 100` 的问题，改为“命令前缀 + 指令名”口径。

## [0.44.8] — 2026-06-30 · patch（补丁版本） · Responses 兼容接口补丁

### Fixed
- 修复部分 OpenAI Responses 兼容接口返回 `Unsupported parameter: max_output_tokens` 时，LLM 调用直接失败的问题；运行时会在明确识别该错误后自动省略 `max_output_tokens` 重试一次。
- Responses 生图工具同样支持该兼容重试，避免兼容站点在图片生成/编辑入口因参数差异失败。
- Provider 协议探测会识别 Responses 兼容模式，并在检测结果中提示运行时将自动省略不支持的参数。

### Changed
- 后端 Telethon 依赖下限提升到 `>=1.44,<2.0.0`，部署重建依赖后跟进近期上游版本。

### Tests
- 补充 Responses 文本和生图参数兼容回归测试。

## [0.44.7] — 2026-06-30 · patch（补丁版本） · 交互按钮空动作路由补丁

### Fixed
- 修复全局 Event Bus 插件命中 `callback_query` 但返回空动作时，仍把按钮事件视为已处理，导致十点半“是否当庄”等会话按钮无法继续进入对应插件的问题。
- 交互 Bot 的 Event Bus 路由现在只有在插件返回实际动作后才终止后续交互规则/会话链路；空动作只作为观察性命中记录，不再吞掉其他插件按钮。

### Tests
- 补充 callback 回归测试，覆盖“空动作不消费会话按钮”和“有 `answer_callback` 动作仍正常消费”两条路径。

## [0.44.6] — 2026-06-30 · patch（补丁版本） · UserBot Event Bus 入口兼容补丁

### Fixed
- 修复 UserBot Event Bus 订阅缺少 `entry_key` 时被错误记录为 `entry_key_missing` failed 的问题；插件如果实现了 `on_event`，现在会直接投递到事件主入口。
- 修复仅声明 Event Bus 订阅、但仍依赖 legacy `on_message` 的 userbot 插件被新分发链路吞掉消息的问题；无法投递新入口时只记录 skipped，并继续让 legacy 入口处理。

### Tests
- 补充插件 loader 回归测试，覆盖“无 `entry_key` + `on_event` 正常投递”和“无 `entry_key` + legacy `on_message` 继续处理”两条链路。

## [0.44.5] — 2026-06-30 · patch（补丁版本） · 交互按钮 ACK 容错补丁

### Fixed
- 修复交互插件返回 `answer_callback` 后再编辑主消息时，如果 Telegram callback ACK 因过期或接口异常失败，后续 `edit_message` 被中断，导致按钮点击看起来“没反应”的问题。

### Tests
- 补充交互 delivery 回归测试，覆盖 `answer_callback` 失败后仍继续执行后续 `edit_message`。

## [0.44.4] — 2026-06-30 · patch（补丁版本） · 交互关键词路由修复

### Fixed
- 修复普通消息 Event Bus 订阅命中但返回空动作时，会提前吞掉后续交互规则关键词链路的问题。
- 修复 `十点半测试`、猜骰、21 点等通过交互 Bot 关键词启动的规则，在被通用消息订阅插件命中后没有任何响应的回归。

### Tests
- 执行账号 Bot / 交互 Bot 回归测试：`cd backend && ../backend/.venv/bin/python -m pytest app/tests/test_account_bot.py`。

## [0.44.3] — 2026-06-30 · patch（补丁版本） · 插件仓库标签展示补丁

### Changed
- 插件仓库列表和批量更新确认框统一使用“触发入口 / 能力”分组标签展示插件契约信息。
- 触发入口标签改为带颜色的独立标签，并直接展示命令、普通消息、按钮回调、会话关闭、付款确认等入口名称。
- 能力标签不再折叠成 `+1` / `+2`，改为直接展示所有能力名称，减少插件仓库卡片上的信息歧义。
- “触发入口”计数改为基于实际展示的入口标签数量，避免订阅数组数量和入口名称数量不一致。

### Tests
- 执行前端类型检查：`./node_modules/.bin/tsc -b --pretty false`。

## [0.44.2] — 2026-06-30 · patch（补丁版本） · 交互消息替换补丁

### Added
- `send_message` 标准动作新增 `replace_saved_message_id_key`，平台会在发送新消息并保存新消息 ID 后，按旧保存键删除上一条消息，适合滚动加入通知、倒计时提示等只保留最新一条的交互场景。
- `ctx.messages.send(...)` 支持传入 `replace_saved_message_id_key`，插件无需手写 action dict 也能使用同一能力。

### Fixed
- 修复远程/已安装交互插件无法稳定读取上一条已保存消息 ID 时，“发送新提示后删除旧提示”不生效的问题。

### Tests
- 执行交互发送替换回归测试：`backend/.venv/bin/python -m pytest -q backend/app/tests/test_account_bot.py -k "save_message_id_key or send_replaces_saved_message"`。

## [0.44.1] — 2026-06-30 · patch（补丁版本） · 插件页与日志展示补丁

### Changed
- 插件中心快捷入口“安装插件”改为“插件管理”，并增强视觉权重；同一入口相关提示统一使用“插件管理”口径。
- 插件中心的 AI 插件入口默认折叠，且在全局 AI 功能关闭时不再显示。
- 插件卡片把“可交互”和“AI 调用”标签移到右上角独立展示；事件数量文案由“订阅”改为“触发入口”。
- 插件管理页的推荐插件只展示首次部署推荐的自动回复和自动复读，并说明这些条目来自 TelePilot 预置推荐源，其他插件应通过用户自己的 Git 插件仓库安装。
- 已安装插件表新增“来自库”列，优先显示已保存的插件仓库名，匹配不到时回退为推荐源、本地导入或 Git 地址缩写。
- 原始运行日志的等级标签去掉补齐空格，`[INFO ]` 现在显示为 `[INFO]`；debug 记录阈值增加说明，明确“允许保留 debug 行”不等于当前链路一定会产生 debug 日志。
- 日志排障文案把用户可见的“订阅”统一替换为“触发入口”，减少旧概念干扰。

### Fixed
- 已安装插件接口补充返回 `source_url` 和 `source_label`，前端可以准确展示插件来自哪个库或来源。

### Tests
- 执行前端类型检查：`./node_modules/.bin/tsc -b --pretty false`。
- 执行前端生产构建：`./node_modules/.bin/vite build`。
- 执行后端 lint：`.venv/bin/ruff check app`。

## [0.44.0] — 2026-06-30 · minor（次版本） · AI 能力热插拔与插件契约展示

### Added
- 新增全局 `ai_enabled` 设置，可在系统设置中热插拔 AI 能力包；关闭后侧边栏、移动底栏和插件首页会隐藏 AI 入口，`/ai` 直达会显示启用提示页。
- LLM Provider 的 CRUD、拉取模型、协议探测和测试模型接口在 AI 关闭时统一返回 `AI_DISABLED`，避免继续发起外部模型请求。
- worker 热加载时会读取 AI 能力开关；关闭后不再查询或加载 LLM Provider，不解密模型代理配置，也不会把 provider 放进 worker 内存。
- AI 指令触发、插件 `ctx.ai` facade 和模板编辑器均接入 AI 开关：关闭时指令短路提示，声明 `ai_text` 的插件不会注入 `ctx.ai`，模板编辑器不再拉 provider 或允许新建 AI 模板。

### Changed
- 插件配置页的“最终版插件契约”改为“触发与权限”，以“触发入口 / 可用能力 / 风险提示”展示插件运行声明，避免直接暴露 `event_subscriptions`、`capabilities` 等开发者字段名。
- 插件能力标签现在会结合 `capabilities`、`permissions`、config schema 和使用说明推断；声明 `ai_text` 或明显调用 `ctx.ai` / LLM 的插件会显示 `AI 调用` 标签。
- 插件中心和插件安装页弱化“官方插件库”口径，面向用户改为“推荐插件 / 推荐源 / 插件库插件”；首次部署推荐只保留自动回复和自动复读。
- 账号详情和交互 Bot 旧入口同步使用“触发入口 / 可用能力 / 推荐源”等新口径，减少内部字段名和“官方库”概念对用户的干扰。
- 后端安装、dry-run、运行时缺插件等用户可见提示改为“插件库插件 / 推荐插件源”，内部兼容源枚举保持不变。
- 插件安装页统一“卸载”按钮样式，推荐插件已安装后也可在推荐插件卡片中直接卸载。
- 交互中心的账号选择器与插件中心保持一致，不再在下拉和摘要区展示账号手机号。

### Tests
- 补充 AI 能力开关的系统设置回归测试，以及 `ctx.ai` 在 AI 关闭时不加载 provider 的回归测试。
- 执行前端类型检查：`./node_modules/.bin/tsc -b --pretty false`。
- 执行前端生产构建：`./node_modules/.bin/vite build`。
- 执行后端目标测试：`app/tests/test_system_settings.py`、`app/tests/test_plugin_ai_facade.py`、`app/tests/test_plugin_loader.py`。

## [0.43.2] — 2026-06-30 · patch（补丁版本） · 日志排查体验优化

### Changed
- 日志中心默认进入“原始日志”，并把“原始日志”标签放到最前，方便先按连续控制台日志排查系统收到消息、插件启动和发送动作状态。
- 原始运行日志拆分时间、等级、元信息和正文渲染；`[INFO ]`、`[WARN ]`、`[ERROR]`、`[DEBUG]` 等等级字段按等级使用不同颜色，正文保持稳定可读。
- 运行日志默认隐藏热更新、配置刷新、`reload_config` 等低价值刷屏内容；审计日志默认隐藏登录/退出噪声，并在界面上明确运行日志用于消息与插件排查，审计日志用于追踪 Web 面板操作。
- 消息链路详情新增“排查结论”，直接展示未命中插件、插件执行失败、发送动作失败、链路告警或已完成等判断，并给出失败原因和下一步排查方向。
- 消息链路时间线默认只展示关键阶段、异常阶段和发送动作，保留“显示全部”用于查看完整 span/action 明细。
- 插件诊断详情新增“排查结论”和“最近异常阶段”，优先展示加载失败、最近调用失败、reason code 和 trace 入口；最近 span 原始 JSON 改为折叠展示。
- 消息链路、插件诊断、命令链路、动作发送和总览增加极短用途提示，降低初次使用门槛。
- 自动回复的 builtin / official 插件源码移除对 `app.db.models.feature` 的直接依赖，避免插件规范 lint 报内部模块引用警告。

### Tests
- 执行前端类型检查：`./node_modules/.bin/tsc -b --pretty false`。
- 执行前端生产构建：`./node_modules/.bin/vite build`。
- 执行自动回复插件 lint 验证：builtin / official 两份源码均无内部模块引用警告。

## [0.43.1] — 2026-06-30 · patch（补丁版本） · 原始日志控制台化

### Changed
- 日志中心的“原始日志”改为控制台式连续文本流，不再用事件卡片展示运行日志和审计日志。
- 原始运行日志默认展示全部来源，并沿用顶部账号、插件和关键词过滤；每行直接展示时间、等级、来源、账号、插件、入口、trace、会话、消息、操作者和原因代码等排障字段。
- 新增最低等级、隐藏热更新噪声、显示 detail、自动刷新、自动换行和复制当前视图等控制项，便于快速排查消息链路和插件运行状态。
- 审计日志同步改为控制台式文本流，保留 action 筛选与 detail 展开。

### Tests
- 执行前端类型检查：`./node_modules/.bin/tsc -b --pretty false`。
- 执行前端生产构建：`./node_modules/.bin/vite build`。

## [0.43.0] — 2026-06-30 · minor（次版本） · Trace 批量写入与官方插件外置

### Changed
- Trace 写库从消息热路径中的逐条同步 commit 改为内存队列 + 后台批量写入：`start_trace`、`record_span`、`record_action`、`finish_trace` 现在只做轻量入队，后台最多按 200 条或 0.2 秒窗口统一落库，避免一条消息产生多次数据库往返等待。
- native_raw Trace 保留策略改为启动期和系统设置更新时刷新缓存，消息进入 Trace 时不再为了读取保留配置额外访问数据库。
- worker 子进程启动和全局设置 reload 时同步刷新 Trace 设置缓存，退出时 flush 并停止后台写入器，保证多进程下的配置与队列行为一致。
- 官方可选插件入口改为读取 `OFFICIAL_PLUGIN_REPO_URL` 指向的远程官方插件仓库，`game24`、`math10`、`chatgpt_image`、`codex_image` 不再从 Core 随包目录安装。
- 历史 builtin 可选插件迁移逻辑改为从官方插件仓库查找源码；Core 中缺少源码时会给出安装官方插件仓库的提示，而不是继续依赖旧目录。

### Fixed
- Trace 后台写入器增加 graceful shutdown flush、跨事件循环隔离、队列满降级告警和单条失败拆分兜底，避免 Trace 存储异常反向打断 Telegram / 插件主流程。
- 保留外部指定 `trace_id` 的去重语义，批量写入时同一批重复 `trace_id` 不会因唯一键冲突拖垮后续 span/action。
- 交互付款规则在规则自身未填写金额时，会读取插件参数中的 `amount` / `bet` / `entry_amount` / `entry_fee` / `stake` 作为期望金额，避免十点半等玩法被无关小额转账误触发。
- 交互会话参与者策略优先采用插件当前声明，避免旧规则中遗留的 `solo_owner` 覆盖插件已更新的 `paid_pool`。
- `paid_pool` 会话会累计已付款玩家列表，不再由后一个付款人覆盖前一个付款人，并修复无付款人的关键词开局边界。
- `math10` 本地交互 fallback 和 `codex_image` dry-run 不再硬编码 import Core 内置插件目录；已安装插件存在时动态加载，未安装时返回可读提示。

### Removed
- 从 Core 删除 `game24`、`math10`、`chatgpt_image`、`codex_image` 在 `builtin/` 与 `official/` 下的历史源码副本，插件源码迁移到官方远程插件仓库维护。

### Tests
- 补充 Trace 缓冲写入、flush、重复 `trace_id` 去重和交互付款/付费池会话回归测试。
- 执行后端全量测试：`1019 passed, 2 skipped`。
- 执行插件示例校验和已安装交互插件契约校验；示例通过，存量插件仅保留 usage / 旧 `interaction_entries` 规范警告。
- 补充远程官方插件仓库入口测试，覆盖 official 标签过滤和远程官方插件安装记录。

## [0.42.0] — 2026-06-29 · minor（次版本） · Web 面板自更新执行器

### Added
- 生产 Docker 栈新增内部 `updater` sidecar，仅在 Compose 内网监听，不暴露公网端口；Web 后端通过共享 token 调用它执行更新任务。
- 检查更新机制支持当前分支 / `TELEPILOT_UPDATE_BRANCH`，不再写死 `origin/main`；生产候选分支也能在面板中检查和应用。
- 更新任务改为后台 job，Web 面板可轮询显示更新状态和最近日志，避免 `docker compose` 重启 Web 容器时把请求中途打断。

### Changed
- `scripts/prod-up.sh` 与 `scripts/prod-update.sh` 会传递 `TELEPILOT_HOST_PROJECT_DIR`，让 updater 在容器内调用宿主 Docker 时仍能正确定位项目目录。
- updater 触发完整更新时会跳过重建 updater 自身，避免自更新任务被中途杀掉；业务容器仍会按完整路径重建。
- 更新弹窗展示目标分支、运行模式、执行器、变更分类和 job 日志；普通后端 / 前端变更继续走增量重建，部署脚本、Compose、Dockerfile、依赖等关键变更仍回退完整更新。
- 部署文档补充 Web 自更新说明，明确首次启用 updater 仍需一次宿主机部署，后续常规补丁可从面板触发。

### Tests
- 补充分支选择、内部 updater 检查和更新 job 创建的单元测试。
- 执行系统健康测试、后端 ruff、前端 TypeScript 构建检查、updater Python 编译检查和 Compose 配置渲染检查。

## [0.41.7] — 2026-06-29 · patch（补丁版本） · 交互消息参与者拦截修复

### Fixed
- 修复 chat 级交互玩法会话中，非参与者发送任意普通消息都会被平台提前回复“这不是你的玩法，请由付款或开局本人操作”的问题；普通文本消息现在交给插件自行判断并可静默忽略，平台仅继续保护 callback 按钮操作。
- 修复十点半等付费多人玩法中，玩家转账消息和开局者无关聊天被误判为非法玩法操作的问题，避免交互 Bot 在群内刷无关提示。

### Tests
- 补充 paid_pool 普通消息不触发平台前置拦截的回归测试。
- 重新执行 `backend/app/tests/test_account_bot.py`，178 个测试通过。

## [0.41.6] — 2026-06-29 · patch（补丁版本） · 交互中心账号选择统一

### Fixed
- 交互中心顶部账号选择改为与插件中心一致的“选择配置的账号”样式和文案，避免把账号选择误读成交互 Bot 实例选择。
- 账号辅助信息保留状态、账号 ID、手机号和 Telegram ID；交互 Bot 运行状态继续放在右侧状态组，配置语义更清晰。

## [0.41.5] — 2026-06-29 · patch（补丁版本） · 交互规则编辑体验修正

### Fixed
- 修正交互中心规则详情中“插件参数与技术详情”重复展示“奖励与限流”已接管字段的问题；高级区现在只展示插件额外参数和技术字段。
- 修正交互中心“保存规则”按钮在页面滚动时固定到文档底部的问题；按钮改为挂载到页面视口层，始终停留在可视区域右下角。

## [0.41.4] — 2026-06-29 · patch（补丁版本） · 日志保留字段收口

### Fixed
- 继续收口绑定日志器的保留字段透传问题，避免 scheduler runtime 和 account bot 本地交互 fallback 在 detail 中重复携带 `plugin_key` / `source` 时触发 Python keyword 冲突。
- 统一以平台绑定的插件身份为准记录运行日志，插件 detail 中的同名保留字段不会再覆盖或打断平台日志链路。

### Tests
- 补充 scheduler 上下文日志和 account bot 本地交互 fallback 日志的重复 `plugin_key` 回归测试。

## [0.41.3] — 2026-06-29 · patch（补丁版本） · 交互入口日志修复

### Fixed
- 修复插件交互入口执行期间 `ctx.log()` 自动携带 `trace_id` / `plugin_key` / `entry_key` 时，与已绑定插件身份的日志器重复传入 `plugin_key`，导致猜骰等交互玩法在转账确认后启动失败的问题。
- 日志器现在会以当前加载的插件身份为准，忽略调用方重复传入的 `plugin_key` detail，避免日志写入中断插件业务流程，同时保留 `trace_id` 和 `entry_key` 便于排查。

### Tests
- 补充交互入口调用 `ctx.log()` 不重复传参的回归测试，并复跑相关 Event Bus / 交互入口日志测试。

## [0.41.2] — 2026-06-29 · patch（补丁版本） · 交互插件运行时修复

### Fixed
- 修复 UserBot Event Bus 日志路径中重复传入 `plugin_key` / `entry_key` 导致 `run_interaction_entry` 被 `TypeError: got multiple values for keyword argument 'plugin_key'` 打断的问题，避免平台日志错误掩盖真实插件异常。
- 修复已声明 Event Bus 订阅的插件在订阅未命中时被错误跳过 legacy `on_message` 的问题；管理员命令启动的猜骰等旧兼容游戏，现在后续玩家回复会继续交给 userbot 插件处理并触发原有发奖逻辑。

### Tests
- 补充 UserBot Event Bus 订阅未命中回退 legacy `on_message` 的回归测试。
- 补充废弃发送通道日志上下文不重复传参的回归测试，并复跑交互插件合约和账号 Bot 自动发奖测试。

## [0.41.1] — 2026-06-29 · patch（补丁版本） · AI 与日志页体验修正

### Fixed
- 修正 AI 页总览里“AI 中心”和“AI 工作台总览”重复形成双页眉的问题，改为保留统一页眉并用紧凑指标卡展示状态。
- 调整日志中心“原始日志”展示，不再使用事件卡片，改为连续日志文本流，同时保留日志等级、时间、来源、正文、元信息和可展开 detail。

### Tests
- 执行前端类型检查和生产构建。

## [0.41.0] — 2026-06-29 · minor（次版本） · 插件开发者体验与日志可读性改版

### Added
- 新增 `docs/PLUGIN-QUICKSTART.md`，用 `hello_ping` 演示 5 分钟复制最小 Event Bus + MessageOps 插件。
- 新增 `docs/PLUGIN-RULES.md`，把插件开发的必须、禁止、推荐规则整理成短契约。
- 新增 `examples/plugins/hello_ping` 入门示例，并纳入 `scripts/validate-plugin-examples.py`，校验 `ping` 返回 `send_message/pong`、非命中文本不返回动作。
- 在插件安装页开发指南 Tab 置顶 Quickstart、插件开发铁律和完整 API 参考三个入口。

### Changed
- 调整插件开发指南索引、README、速查表、插件概览和安全边界，明确安装、启用、禁用、更新、热重载和卸载的心智，并补充插件清理检查表。
- 统一插件示例验证命令为 `backend/.venv/bin/python scripts/validate-plugin-examples.py`，减少未激活虚拟环境时的误导。
- 统一主页面页眉样式，概览、插件中心、日志等页面使用同一工作台式标题区，减少页面标题风格漂移。
- 重做日志中心“原始日志”里的运行日志和审计日志展示，从横向表格改为可扫读事件卡片，并把原始 detail 默认折叠。

### Tests
- 执行插件示例验证、前端类型检查、前端生产构建、文档旧词审计和 `git diff --check`。

## [0.40.6] — 2026-06-29 · patch（补丁版本） · 最终证据台账收束补丁

### Docs
- 新增 `0.40.6` 最终证据台账，绑定当前分支、远端 SHA、服务器健康、线上 Trace/action、回滚开关恢复状态、文档审计和子 Agent 只读复核结论。
- 明确 `0.40.5` 是日志中心行为修复版本，`0.40.6` 只做发布材料与签收证据收束，不改变 Event Bus / Trace / MessageOps 运行时语义。

### Tests
- 重新执行最终版门禁验证：后端 ruff、D1-D6/D12 目标 pytest、全量 pytest、插件示例验证、已安装交互插件验证、Alembic head/offline SQL、前端类型检查、前端生产构建和 `git diff --check` 均通过。
- 复核线上 `0.40.5` 部署状态：本地、远端分支和服务器 commit 一致，容器 healthy，版本 API 正常；随后按 `0.40.6` 发布流程重新部署复验。

## [0.40.5] — 2026-06-29 · patch（补丁版本） · 日志中心选择体验与最终验收补丁

### Fixed
- 修复日志中心“消息链路”点击某条 trace 后，左侧列表被自动过滤到只剩当前 trace 的问题；现在点击只切换右侧详情，Trace ID 输入框才负责过滤。
- 修复“动作发送”页被已选 trace 隐式过滤的问题，避免用户从其它页查看 trace 后误以为动作列表丢失。
- 为 Trace ID 过滤框增加“清空”按钮，深链或手动过滤到单条 trace 后可以直接回到当前窗口列表。
- 修复生产 CSP 拦截首页主题初始化内联脚本造成的控制台红色错误，改用精确 SHA-256 hash 放行，不放开 `unsafe-inline`。

### Docs
- 新增 `0.40.5` 最终证据台账，记录服务器部署、业务页桌面/窄屏验收、真实 Trace、fixture action、三个回滚开关演练和最终 Go/No-Go 结论。

### Tests
- 重新执行前端类型检查和生产构建；部署后复验线上版本、容器状态、健康检查、关键业务页和日志页选择行为。

## [0.40.4] — 2026-06-29 · patch（补丁版本） · 最终版部署执行锁与发布复验

### Docs
- 继续补强全量 Event Bus 与 Trace 最终版计划，把最终版交付包、服务器部署目录探测、线上真实链路触发剧本、线上失败回滚剧本、插件开发指南最终审计清单、执行顺序锁和子 Agent 任务模板写入第 31 节。
- 新增 `0.40.4` 最终版证据台账，绑定当前本地门禁、服务器部署、业务页验收、真实 Trace 和回滚演练的最终签收路径。

### Fixed
- 修复 UserBot 入口在 `trace_enabled=false` 时连 Event Bus decision 一起跳过的问题；现在关闭 Trace 只降级链路写库，新插件 Event Bus 投递仍按 `event_bus_delivery_enabled` 独立生效。
- 补齐账号 Bot 系统通知和消息模板测试发送的 Trace/action 记录，用户可见 Bot 发送成功或失败会落 `event_action`，并尊重 `trace_enabled` 降级开关。
- 修正插件 API 参考中 Event Bus 主路径示例的 `payload.get("message")` 歧义，改为直接读取标准信封 `payload["message"]`。

### Tests
- 补充 Trace 关闭但 UserBot Event Bus 仍投递、账号 Bot 通知 action、消息模板测试发送 action 的回归测试。
- 重新执行最终版本地门禁：后端 ruff、D1-D6/D12 目标 pytest、全量 pytest、插件示例验证、已安装交互插件验证、Alembic head/offline SQL、前端类型检查和生产构建均通过。

## [0.40.3] — 2026-06-29 · patch（补丁版本） · 最终版签收与收束执行补丁

### Fixed
- 修正 README 项目状态中的当前版本号，确保用户可见版本与四处发布版本文件保持一致。
- 修复关闭 `event_bus_delivery_enabled` 后外部付款通知仍会进入 Event Bus 投递的问题，关闭后会记录降级原因并回退旧规则链路。
- 修复关闭 `trace_enabled` 后 scheduler 仍创建 Trace 和写入 `event_action` 的问题，关闭后定时任务继续执行但只保留旧日志级别排障。

### Changed
- 插件仓库展开列表常态展示插件 usage 摘要、事件订阅、能力声明和高风险能力提示，不再只在批量更新确认弹窗中展示最终版契约信息。

### Docs
- 为全量 Event Bus 与 Trace 最终版计划补充“最终版签收执行补丁”和“最终版收束补丁”，把最终版收敛为 D1-D12 施工映射、G0-G6 签收闸门、证据状态机、业务页验收剧本、部署回滚底线和最终报告硬格式。
- 新增 `0.40.3` 最终版证据台账，登记当前本地/远端 commit、服务器仍为 `0.37.0`、未跟踪文件处理和 D1-D12 当前签收状态。

### Tests
- 补齐 inline_query、chosen_inline_result、answer_inline_query、UserBot Event Bus、付款通知 Event Bus、scheduler Trace 回滚开关和系统设置写回的最终版门禁回归测试。
- 重新执行最终版自动验证组：后端 ruff、D1-D6/D12 扩展目标 pytest、全量 pytest、插件示例验证、已安装交互插件验证、Alembic head/offline SQL、前端类型检查和生产构建均通过。

## [0.40.2] — 2026-06-29 · patch（补丁版本） · 最终版执行封条与证据同步

### Docs
- 为全量 Event Bus 与 Trace 最终版计划补充“最终版执行封条”，明确执行前提、并行任务包、最短签收路线、报告模板和 D1-D12 证据门槛，防止把半落地状态误称为最终版。
- 新增 `0.40.2` 最终版证据台账，把当前自动验证、只读复核、浏览器登录态限制、服务器 SSH 阻塞和远端版本状态绑定到当前提交。

### Tests
- 重新执行最终版门禁验证：后端 ruff、D1-D6 定向 pytest、全量 pytest、示例插件验证、已安装交互插件验证、Alembic head/offline SQL、前端类型检查和生产构建均通过。

## [0.40.1] — 2026-06-29 · patch（补丁版本） · 最终版封口与文档验收补强

### Fixed
- 补齐运行时实际使用的 `reason_code` 稳定字典、日志页中文映射和测试，防止 Trace 输出与文档/前端排障表漂移。
- 修正 README 当前版本号仍停留在 `v0.37.0` 的发布材料不一致问题。
- 修复付款二次确认 callback 重放玩法事件时未继承当前 `trace_id` 的断链风险，确保后续插件动作继续归属同一条 Trace。
- 修复关闭交互会话时 `session_close` 子 Trace 污染父入口 Trace 的问题，确保最终确认消息仍归属原始入口链路。
- 修复 Delivery Executor 占位清理和编辑失败回退只写 runtime log、不落独立 `event_action` 的问题，避免日志页把复合动作误判为完全成功。

### Docs
- 将插件概览的快速开始改为 Event Bus + MessageOps 最小示例，旧 `on_command` / `on_message` 降级为管理员命令和历史兼容说明。
- 在插件速查表、API 参考和最终版计划中补充常见 `reason_code` 排障表。
- 补充最终版封口计划、可实现性锁定和执行冻结清单，明确最终版只关闭入口、协议、动作、日志、文档、部署断点，部署、浏览器/PWA、真实链路和回滚演练缺一不可。

### Tests
- 强化 `event_bus_demo` 示例验证，新增 native_raw 与废弃 notice 探针 fixture，并让验证脚本实际调用 `on_event` 校验 message、command、callback、inline、payment 等 action 形状。
- 补充插件注册命令的 Event Bus decision 回归测试，证明插件命令会记录 `admin_command` 订阅决策和 `event_action`。
- 补充 `session_close` 父 Trace 归属、占位删除 action、编辑失败回退 action 的最终版门禁回归测试。

## [0.40.0] — 2026-06-29 · minor（次版本） · Event Bus 与 Trace 最终版框架

### Added
- 新增 `event_trace`、`event_span`、`event_action`、`plugin_runtime_status` 数据模型和迁移，日志中心可以按 `trace_id` 串起消息接收、标准化、订阅匹配、插件调用、动作执行和失败原因。
- 新增统一 Event Bus 服务，把交互 Bot 消息、按钮回调、Inline Query、Inline 选择结果、外部付款通知和 UserBot 命令收敛为标准事件信封，并用稳定 `reason_code` 记录 matched / skipped / delivered。
- 插件 manifest 支持 `usage`、`event_subscriptions`、`capabilities`，官方、内置、示例和远程插件字段链路同步贯通到后端 schema、feature matrix、前端类型和 WebUI 风险提示。
- 新增可信插件 `telegram_native_raw` 能力边界：插件显式声明后才可拿到 JSON 兼容原生 Telegram 数据；日志默认只保存摘要和 `native_raw_meta`，完整 `native_raw` 需开启短保留期持久化。
- Delivery Executor 和 MessageOps 增加 `answer_inline_query`、动作结果 Trace、旧通道失败 action、settlement 记录和实际发送通道留痕。
- 日志中心重构为 Trace 视角，提供总览、消息链路、插件诊断、命令链路、动作发送和原始日志入口，并补齐 Inline、native_raw、Contract Guard、Telegram API 错误和插件加载失败展示。
- 插件中心、插件配置页、远程插件仓库和交互中心展示插件使用说明、事件订阅、能力声明、高风险能力、废弃通道告警和一键更新风险信息。
- 新增 `examples/plugins/event_bus_demo` 最终版示例，覆盖 message、command、callback、inline、chosen inline、payment fixtures，并演示 `ctx.messages`、`answer_inline_query` 和 `settlement`。

### Changed
- TelePilot 插件框架正式收敛为个人可信插件标准：账号主人主动安装和启用插件，业务风险由安装者承担；平台提供统一事件入口、统一消息操作出口、风险提示、审计、频控、急停和客观失败返回。
- 旧交互规则继续可用，但语义收敛为 Event Bus 订阅条件和规则过滤，不再作为第二套插件调度真相。
- 插件开发主路径切换为标准事件信封 + `ctx.messages` / 标准 action + Trace 排障；旧平铺 payload、`raw_event`、`event.reply/respond` 只作为迁移或历史说明。
- 远程插件仓库、私有 GitHub 仓库、`tree/<branch>` URL、仓库刷新和单仓库一键更新都会保留 `usage`、`event_subscriptions`、`capabilities` 等最终版字段。
- Trace 和 native_raw 保留策略接入系统设置；`native_raw_retention_days` 默认收紧为 1 天，Trace 清理会保留主链路记录并清理过期大字段。
- Contract Guard 定位调整为契约记录器和客观失败保护层：越声明调用可告警放行，不支持或废弃能力明确失败，并输出中文说明与稳定 `reason_code`。

### Fixed
- 修复旧 `notice` / `bbot_notice` / `notice_bot` 可能被误当可执行发送通道的问题；运行时和 lint 均使用 `send_channel_deprecated` 明确失败，不会自动改写到交互 Bot。
- 修复 `raw_event` 作为原生事件后门的风险；未声明 `telegram_native_raw` 的插件不会通过兼容 payload 拿到完整原生对象。
- 修复插件动作失败缺少 `event_action` 的断链问题，空文本、非法媒体、缺 inline query id、Telegram API 错误等都会记录失败动作。
- 修复插件加载失败只落旧 runtime log、日志页插件诊断不可见的问题；loader 失败会更新 `PluginRuntimeStatus`。
- 修复 worker 交互入口 timeout 测试因全局 `time.time` monkeypatch 与 60 秒 timeout 不匹配导致全量测试卡住的问题。

### Docs
- 重写插件 API 参考、远程插件规范、速查表、安全边界、README 和最终版计划，统一为 Event Bus + Trace + MessageOps 口径。
- 新增 `docs/TELEGRAM-FULL-EVENT-BUS-TRACE-PLAN.md` 和 `docs/release/0.40.0-final-evidence.md`，把最终版范围、Go / No-Go、证据台账、前端实测、部署回滚和残余风险写成可执行门禁。
- 文档明确外部转账通知 Bot 只是群里已有到账证据来源，不是 TelePilot 的主动发送通道；普通交互由交互 Bot，收款确认和发奖由 UserBot 或 settlement 承接。

### Tests
- 后端全量测试覆盖 Event Bus、Trace、native_raw、Inline、旧通道失败、插件加载状态、远程插件字段贯通、MessageOps / Delivery、UserBot 命令 Trace 和交互 Bot 回归路径。
- 插件示例验证脚本和已安装交互插件验证脚本补齐 `usage`、`event_subscriptions`、`capabilities`、废弃通道和旧风险字段检查。
- 前端类型检查和生产构建覆盖日志中心、插件风险提示、交互中心、插件配置页和仓库字段展示。

## [0.37.0] — 2026-06-28 · minor（次版本） · 插件开放事件框架

### Added
- 交互插件 payload 正式收口为标准事件信封，统一下发 `source`、`message`、`chat`、`sender`、`actor`、`source_actor`、`reply_to`、`payment`、`player`、`session`、`trigger`、`raw` 等顶层字段，插件不再需要从旧平铺字段里猜消息来源、业务主体和付款玩家。
- `event_from_interaction_payload(payload)` 补齐 `sender`、`source_actor`、`player` 等稳定引用，插件可直接转换成 TelePilot 事件对象后读取 `event.message`、`event.actor`、`event.payment`、`event.session`。
- 交互中心新增“事件与动作调试”面板，展示最近一次下发给插件的 payload、插件返回 actions、平台处理后的 actions、Contract Guard 告警和插件失败原因。
- 交互事件声明支持 `all_messages`，用于明确表示入口可接收当前会话内的所有消息事件。

### Changed
- TelePilot 交互插件框架明确采用个人可信插件标准：插件和插件仓库由账号主人主动安装，业务风险由安装者自行承担；平台负责风险提示、审计日志、调试告警、频控、急停、token/session 隔离和客观失败返回。
- Contract Guard 从硬阻断改为软告警：插件调用未声明动作或未声明受控通道时，会记录 `guard_level=warning` 并继续按插件请求尝试可用通道；`result_contract` 现在是可见契约和调试依据，不再是公共插件市场式强沙箱。
- `notice`、`bbot_notice`、`notice_bot` 明确为旧主动发送通道且不兼容：插件显式请求这些通道会得到 `guard_level=failed`、不可执行失败和迁移提示；正常插件消息请迁移到 `interaction_bot`、`userbot_reply` 或 `auto`。
- 插件开发主路径改为直接读取标准事件信封或使用 `event_from_interaction_payload(payload)`；`payload["event"]` 和旧平铺字段只作为历史兼容来源，不再写入新插件指南的主路径。
- Delivery Executor 增加旧/未知通道兜底防御：即使内部调用绕过 Contract Guard，也不会把 `bbot_notice` 等旧通道误当交互 Bot token 执行发送。

### Docs
- 更新插件开发指南、API 参考、远程插件规范、速查表、安全边界、交互 Bot 优化方案、README 和开放事件框架计划，统一说明标准事件信封、旧通道迁移、Contract Guard 软告警、外部转账通知 Bot 仅作为到账证据来源。
- 补充插件作者迁移要点：付费玩法只以 `source.type == "payment_confirmed"` 且 `payment.status == "confirmed"` 作为到账依据；真实玩家身份优先读取 `player.user_id` 和 `player.identity_confidence`。
- 补齐 `interaction_entries` 示例中的 `dispatch_modes`、`message_channels`、`money_channel`，并把远程插件示例里的旧平铺 payload 改成标准事件信封；需要持续状态的示例明确第三方插件不得默认依赖 `ctx.redis` 恒可用。

### Tests
- 补充标准事件信封字段、付款 display/source/reply 字段、`event_from_interaction_payload` 投影、Contract Guard 软告警、旧通道失败和 Delivery Executor 拒绝旧通道直达发送的回归测试。

## [0.36.2] — 2026-06-28 · patch（补丁版本） · 插件仓库更新超时修复

### Fixed
- 修复插件仓库“一键更新可升级”在 GitHub 拉取稍慢时继承前端 15 秒默认超时，导致页面报 `timeout of 15000ms exceeded`、服务端出现 499 的问题；仓库浏览、刷新、安装和批量更新现在使用适合 git 操作的独立超时。

### Changed
- 插件仓库缓存改为浅克隆与单远端引用刷新：首次拉取只获取当前版本文件，后续刷新不再执行全量 `fetch --all`，减少小型插件仓库更新时的等待时间。

## [0.36.1] — 2026-06-28 · patch（补丁版本） · 插件仓库单仓库批量更新

### Added
- 插件仓库支持按单个仓库一键更新：点击仓库行的“更新可升级”后，会刷新该仓库并把仓库中已安装且版本更高的插件批量升级到仓库版本，未安装插件和同版本插件自动跳过。

## [0.36.0] — 2026-06-27 · minor（次版本） · 交互插件主动通道收束

### Changed
- 交互插件主动发送通道正式收束为 `interaction_bot` 与 `userbot_reply`：普通交互内容、结果公告、按钮、会话提示默认由交互 Bot 承接；确需账号身份或低频代发时由 UserBot 承接。
- `auto` 默认候选顺序调整为 `interaction_bot -> userbot_reply`；带 inline keyboard 的动作只会保留 `interaction_bot`，避免按钮落到无法承接回调的通道。
- 外部转账通知 Bot 不再是 TelePilot 的发送通道；它只作为群里已有的到账证据来源，被平台监听、解析和校验后生成 `payment_confirmed`。
- 入口显式声明 `result_contract.send_via` 时不再自动兜底错误值；写错或写入已移除通道会触发 lint / Contract Guard 告警，并在运行时阻断不合规动作。

### Removed
- 移除 `bbot_notice`、`notice`、`notice_bot` 作为插件 `send_via` / `channel` / `channel_selector` 的合法发送通道或别名。
- Delivery Executor 不再读取转账通知 Bot token 执行插件动作；删除、置顶、按钮回调等 Bot 能力只由交互 Bot 承接。

### Fixed
- 修复“转账通知 Bot”概念容易被误解为 TelePilot 可主动控制的通知发送者的问题；前端旧通道标签改为“已移除通道”，文档统一改为“外部转账通知来源 / 到账证据来源”。

### Docs
- 插件 API 参考、远程插件指南、安全边界、速查表、插件概览和交互框架说明同步更新为“主动双通道 + 外部转账证据来源”口径。
- 插件开发文档明确：正常插件交互和结果消息若走普通 Bot，应由交互 Bot 发送；收款确认与发奖仍由 UserBot 或平台受控结算链路处理；转账通知 Bot 只用于确认是否到账。

### Tests
- 补充旧 `bbot_notice` / `notice` 通道被 Contract Guard 阻断、混合候选中已移除通道产生告警、Delivery Executor 不再使用转账通知 Bot token、远程插件 lint 拒绝已移除通道等回归测试。

## [0.35.2] — 2026-06-27 · patch（补丁版本） · 交互插件通道选择与回退

### Added
- 交互插件标准动作新增受控通道候选能力：插件可通过 `send_via_options`、`channel` 或 `channel_selector` 声明单通道、候选顺序和失败回退。
- `ctx.messages.send/edit/delete/pin` 支持 `channel=["interaction_bot", "userbot_reply"]` 与 `channel={"prefer": ["bot", "userbot"], "fallback": true}` 写法；旧的 `channel="interaction_bot"` 继续兼容。
- Delivery Executor 现在会按候选顺序执行发送，交互 Bot 发送失败或 token 不可用时可按插件声明回退，并写入运行时日志。

### Changed
- Contract Guard 从“单一 `send_via` 白名单”升级为“候选通道过滤”：不在 `result_contract.send_via` 白名单内的候选会被过滤，全部不命中才丢弃动作。
- 带 inline keyboard 的动作会自动收窄到可承接按钮回调的 Bot 通道，避免把按钮发到 `userbot_reply` 后无法回调。
- 交互入口的 `message_channels` 语义调整为“通道偏好”，不再表示插件后续回复必须绑定某个账号或 Bot；前端入口卡片同步显示“管理偏好 / 群内偏好”。

### Fixed
- 标准动作中的 `chat_id` 现在会被 Delivery Executor 正确使用；插件可在平台校验下向指定会话发送，而不是总是落回触发会话。
- 启动占位消息在发送通道回退到非交互 Bot 时会被清理，避免 Bot 发送失败后群里残留“正在启动”占位内容。
- 远程插件 lint 和 manifest 归一化支持 `bot`、`userbot`、`auto` 等通道别名，避免新规范写法被误判为不支持。

### Docs
- 插件 API 参考、远程插件指南、安全边界、速查表、插件概览和交互框架说明同步为“插件拥有通道选择权，框架拥有通道执行权”的新口径。

### Tests
- 补充通道候选归一化、按钮通道收窄、发送失败回退、指定 `chat_id` 发送、远程插件 lint 别名兼容等回归测试。

## [0.35.1] — 2026-06-27 · patch（补丁版本） · 插件仓库分支链接兼容

### Fixed
- 插件仓库新增、刷新、安装和已安装插件检查更新现在支持 GitHub `tree/<branch>` 分支页链接；后端会保留用户填写的原始 URL，同时在 git 拉取时转换为真正的 clone URL 并 checkout 指定分支。
- 远程插件直接安装同样支持 GitHub `tree/<branch>` 链接，避免临时插件分支必须先合入默认分支或手工改成 clone URL。

### Docs
- 插件仓库页面提示、API schema 和远程插件开发文档补充 GitHub 分支页 URL 的填写方式。

### Tests
- 补充插件仓库缓存刷新、远程插件安装和 source_url 校验的回归测试，覆盖普通 Git URL 与 GitHub `tree/<branch>` 链接。

## [0.35.0] — 2026-06-27 · minor（次版本） · 官方可选插件库与平台能力收口

### Added
- 新增随包官方可选插件库：`auto_reply`、`autorepeat`、`chatgpt_image`、`codex_image`、`game24`、`math10` 不再作为 builtin 自动 seed，而是可在“插件安装与管理”页按需安装。
- 插件中心首页新增首次部署推荐安装提醒：当自动回复、自动复读尚未安装时提示用户按需安装，用户也可以关闭提醒。
- 新增官方插件库 API：`GET /api/plugin-repos/official/plugins` 和 `POST /api/plugin-repos/official/plugins/{plugin_name}/install`，安装后复制到 `plugins/installed/{key}/` 并登记为 `official` 来源。

### Changed
- `scheduler` 进一步收口为平台能力，不再按普通插件展示或卸载；普通插件需要后台任务时仍通过 `ctx.scheduler` facade 注册。
- 官方可选插件安装后与远程/本地安装型插件走同一套运行目录、账号启用、配置页、卸载和 worker 热加载链路；已安装官方插件可手动禁用或卸载。
- 插件中心和账号详情页来源展示调整为“平台内置 / 官方插件 / 第三方”，避免把官方可选插件误认为不可移除的 builtin。
- 新账号配置复制不再默认复制自动回复，只保留平台定时任务配置，避免首次部署时无意启用官方可选插件。

### Fixed
- 旧数据库中已经启用、保存配置或被交互规则引用的历史 builtin 可选插件会自动迁移为 official installed 插件，保留账号配置、规则和全局配置；未使用过的旧 feature 行会被清理，避免插件中心误展示。
- `codex_image` 缺失实现时的运行日志和前端提示改为引导安装官方可选插件，不再提示检查 builtin 目录。
- 官方插件列表会从 `plugin.json.tags` 读取推荐标签，前端不再硬编码推荐插件 key。

### Docs
- README、插件概览、API 参考、HTTP facade、安全边界和远程插件指南同步更新为“平台能力 / 官方可选插件库 / 远程插件库”口径。
- 插件开发指南明确新插件应走远程仓库或 `plugins/local_imports` 本地导入，不再指导开发者把新插件放进 builtin 目录。

### Tests
- 补充官方插件库鉴权、官方插件安装、builtin registry 排除可选插件、loader 核心 builtin 清单等回归测试。

## [0.34.6] — 2026-06-27 · patch（补丁版本） · PWA 底部导航修复

### Fixed
- 修复 PWA/窄屏底部导航已有 6 个入口但容器仍写死 5 列，导致“系统”入口被挤到第二行、底部看起来错乱的问题；底部导航现在按实际入口数量动态分列。

## [0.34.5] — 2026-06-27 · patch（补丁版本） · 插件配置页窄屏体验修复

### Fixed
- 修复插件配置页在 PWA/窄屏下“保存配置”操作条和应用底部导航同时吸底，导致底部视觉错乱成两行的问题；移动端保存操作回到配置卡片内部，桌面端仍保持卡片底部 sticky。
- 优化命令示例徽标在窄容器中的换行表现，避免长命令、长参数或中文示例在使用说明卡片内显得过硬或挤压。

## [0.34.4] — 2026-06-27 · patch（补丁版本） · 插件配置页说明与预览规范

### Changed
- 插件配置页统一为“使用说明 → 功能总开关 → 插件配置 → 插件预览”的信息架构，专属配置页和通用 schema 配置页都不再把保存操作放到页面顶部。
- 有保存字段的长表单把“配置操作”固定到“插件配置”卡片底部，用户滚动配置时仍能看到保存状态和保存按钮。
- 通用 schema 配置页支持插件用 `x-ui-section`、`x-ui-order`、`x-ui-columns` 在平台容器内做分组、排序和列数控制。
- 插件预览从配置字段区拆成独立卡片；`template_preview` / `*_preview` 是建议项，缺失时只提示建议，不阻断保存或运行。

### Fixed
- 通用配置页不再把 `usage_preview` / `usage_guide` / `usage_instructions` / `ai_usage_guide` / `template_placeholders` 和预览字段当作可编辑配置保存。
- 插件中心和远程插件管理页会把缺少详细使用说明的插件标记为红色“高级规范警告”，普通 lint 提醒仍保持 amber 提示。

### Docs
- 插件开发文档、远程插件规范、安全规范、速查表和 README 同步更新，明确配置页必须由插件自声明详细使用说明，不再提供默认兜底说明。

### Tests
- 补充远程插件 metadata lint 回归测试，覆盖缺少使用说明的高级规范警告和 `x-usage-guide` 正常通过场景。

## [0.34.3] — 2026-06-27 · patch（补丁版本） · 私有插件仓库兼容

### Added
- 插件仓库支持 GitHub 私有仓库：添加仓库或编辑已保存仓库时可填写 GitHub Token，后端使用 `MASTER_KEY` 加密保存，前端和 API 只展示是否已配置凭证。
- 新增 `PUT /api/plugin-repos/{id}/credential`，用于更新或清除插件仓库凭证。
- 新增 `plugin_repo.auth_type` 与 `plugin_repo.credential_enc` 数据库字段，并补充 Alembic 迁移。

### Changed
- 插件仓库拉取私有 GitHub 仓库时改用临时 git extraheader 注入 token，不把 token 拼进仓库 URL、缓存 key、git remote 或 API 响应。
- 从插件仓库安装插件统一从已刷新的仓库缓存复制，避免私有单插件仓库二次无凭证 clone 失败。
- 从私有插件仓库安装的插件在检查更新和执行更新时会复用对应仓库保存的凭证。

### Fixed
- git 失败、超时和更新检查错误中的敏感 token 会统一脱敏，避免私有仓库凭证出现在错误提示或日志中。

### Tests
- 补充私有 GitHub 仓库凭证、git extraheader、错误脱敏、凭证清除和鉴权路由回归测试。

## [0.34.2] — 2026-06-27 · patch（补丁版本） · 分支审查与插件文档校准

### Fixed
- 修复第三方 `httpx` 请求日志可能输出 Telegram Bot API Token 的问题，新增统一日志脱敏过滤器并覆盖 Telegram Bot API URL。
- 修复本地插件仓库接口 `/api/plugin-repos/local/plugins` 被动态仓库路由抢先匹配导致返回 422 的问题。
- 修复远程插件仓库“刷新”语义：强制刷新失败时不再静默返回旧缓存，避免前端误报刷新成功。

### Changed
- 插件开发文档统一更新为个人可信插件标准模式，移除旧的 Route A / sandbox 权限模型口径，补齐交互通道、双调度方式和受控 facade 的当前说明。

### Tests
- 补充日志脱敏、本地插件仓库路由和强制刷新失败语义的回归测试。

## [0.34.1] — 2026-06-27 · patch（补丁版本） · 交互通道入口与文档口径补丁

### Changed
- 账号详情页的「联动交互 Bot」入口收束为「交互通道」，明确这里只维护当前账号的交互 Bot、通知 Bot 和通知模板，并提供跳转到「交互」中心的入口。
- 规则保存按钮统一固定在浏览器可视区域右下角，避免在账号详情页或交互中心随页面滚动位置漂移。
- 修正交互优化文档里 `result_contract.send_via` 默认通道的旧表述，和个人可信插件标准模式保持一致。

## [0.34.0] — 2026-06-27 · minor（次版本） · 个人可信插件标准模式

### Added
- 交互插件入口新增可信调度元数据：`dispatch_modes` 区分「管理员命令」与「群内玩法」，`message_channels` 声明不同调度方式的默认消息通道，`money_channel` 固定标识转账相关动作由 userbot 承接。
- 交互中心玩法入口卡片展示调度方式和通道分工，让用户能直接看出插件是管理员命令入口、群内玩法入口，还是两者都支持。
- 后端 feature manifest seed 会统一补齐交互入口调度字段，已安装插件无需重装即可在刷新后得到一致的入口元数据。

### Changed
- TelePilot 标准模式明确调整为个人可信插件模式：管理员安装并启用插件后视为信任插件业务逻辑，平台保留频控、审计、急停、token/session 隔离和受控代发。
- `result_contract.send_via` 从默认最小化为 `interaction_bot` 改为可信默认三通道：`interaction_bot`、`userbot_reply`、`bbot_notice`；插件主动声明白名单时仍会按白名单收窄。（历史记录：`bbot_notice` 已在 0.36.0 起移除，0.37.0 起显式请求会返回迁移提示。）
- 远程插件校验不再要求交互入口必须声明 `result_contract`，改为校验已声明的 `dispatch_modes` / `send_via` 是否使用平台支持值。
- 插件开发指南、API 参考、远程插件规范、速查表和交互优化方案同步更新，明确 userbot 主控监听/资金动作、交互 Bot 承接群内高频互动的标准分工。

### Tests
- 补充交互入口缺省 `result_contract` 时允许三通道代发的回归测试，并更新入口 normalize 测试覆盖 `dispatch_modes`、`message_channels`、`money_channel`。

## [0.33.2] — 2026-06-27 · patch（补丁版本） · 交互中心规则列表与编辑区优化

### Changed
- 交互规则的监听群配置改为只从「已允许会话」选择，并在找不到目标会话时引导到账号详情页的「允许会话」添加；历史保存但不在允许会话中的 Chat ID 会保留显示并可移除。
- 交互规则编辑区默认折叠「触发」「启动内容」「奖励与限流」步骤，将「命中后做什么」移动到「触发方式」之后，减少规则详情首屏高度。
- 调整交互中心规则列表布局：桌面宽屏下列表与右侧规则详情保持同一工作区高度并各自滚动，窄屏/PWA 下规则列表收敛为更紧凑的名称、状态和开关。
- 将交互中心顶部的 `Contract Guard` 标签汉化为「契约守卫」。

## [0.33.1] — 2026-06-27 · patch（补丁版本） · 交互中心规则编辑体验修复

### Changed
- 「交互」页升级为可直接操作的交互中心，支持在顶部选择账号与交互 Bot，查看运行态、规则覆盖、最近触发和最近错误，并直接新增/编辑交互规则、插件入口、触发词和参数。
- 重构交互规则列表展示样式，规则卡片固定展示动作、触发方式、监听群、会话范围、插件入口和启停状态，账号详情入口继续保留但不再是配置交互规则的唯一路径。
- 交互中心收敛重复状态展示：保留顶部账号摘要作为唯一运行态入口，规则编辑区隐藏重复状态条和总览卡片，并把规则清单提前为主要工作区。
- 交互规则编辑补强：玩法查询支持自定义 `{items}` 单项模板，启动占位消息支持按规则名称自动渲染，玩法入口选择更醒目，保存规则按钮恢复为右下角悬浮，转账成功代码块预览显示语言标题。

## [0.33.0] — 2026-06-27 · minor（次版本） · TelePilot 交互框架与部署体验收口

### Added
- 新增独立「交互框架」工作台页面，作为 TelePilot 内部与 AI 并列的重要框架入口，集中展示事件渠道、插件入口、动作契约和发送通道。
- 新增插件交互消息 facade：`ctx.messages.send/edit/delete/pin/answer_callback`，插件可生成平台标准动作，由 TelePilot 统一校验、审计和发送，不再需要每个插件自己拼普通 Bot API。
- 交互 runtime 支持 `answer_callback`、`delete_message`、`pin_message` 标准动作，并避免插件已 ACK 按钮后再次自动发送空 ACK。
- 交互入口新增 `result_contract` 运行时守卫：未声明 `send_via` 时默认只允许 `interaction_bot`；显式声明 `actions` 时丢弃未声明动作；`userbot_reply` 自动移除 `reply_markup`，避免按钮发到无法承接回调的通道。
- 新增 `app.services.interaction` 框架层，拆出 `contracts` 契约守卫和 `InteractionDeliveryExecutor` 受控发送执行器，账号 Bot runtime 保留兼容 shim。
- 远程插件仓库新增刷新接口与前端刷新按钮，可在插件页直接刷新原创插件库列表。
- 新增生产 Docker Compose 初始化脚本 `scripts/init-prod-env.sh`，可自动生成 `MASTER_KEY`、`JWT_SECRET`、`POSTGRES_PASSWORD` 和 `.env`，减少首次部署手工配置。

### Changed
- Docker Compose 快速部署文档改为 `./scripts/init-prod-env.sh` 后直接 `docker compose up -d --build`，并新增 `make init-prod-env`。
- 插件交互开发指南把 `ctx.client` 定位为常规命令与高级兼容入口，交互入口推荐使用 TelePilot 的事件信封和 `ctx.messages`。

### Tests
- 补充 `ctx.messages` 标准动作缓存、`result_contract.send_via` 守卫和 `userbot_reply` 按钮剥离的单元测试。
- 补充 interaction delivery executor 的直接单元测试，覆盖普通 Bot 发送、userbot_reply 转 worker 和 message_id 保存 key 校验。

### Docs
- 明确 0.x 阶段版本号规则：`0.X.0` 表示阶段能力版本，`0.X.Y` 表示同阶段补丁；版本级别使用中英并列口径。
- 更新插件 API 参考、速查表、远程插件规范、安全边界和交互 Bot 优化方案，补齐事件信封、按钮回调、发送通道、契约守卫和旧动作列表到 `ctx.messages` 的迁移路径。

## [0.32.0] — 2026-06-27 · minor（次版本） · 交互玩法身份与参与者策略

### Fixed
- 付费玩法新增 `payment` / `player` / `source_actor` 标准信封，明确区分到账证据、真实玩家和消息来源；独玩/按钮玩法缺少真实付款人 ID 时会先要求付款人点击确认，避免把未到账的 `+金额` 或转账通知 Bot 当成玩家。

### Docs
- 更新插件开发指南、远程插件规范和联动 Bot 优化方案，补充 `participant_policy`、双证据支付模型和 `payment_confirmed` 身份绑定规则。

## [0.31.6] — 2026-06-26 · patch（补丁版本） · 交互玩法运行时增强

### Added
- `_apply_interaction_actions` 新增通用 `send_message` 扩展字段：`edit_message_id`（插件指定编辑目标消息）、`pin`（发送后置顶，默认不置顶）、`save_message_id_key`（发送后将 message_id 写入 Redis 供后续编辑）。
- 交互 Bot 轮询配置新增 `callback_query` 事件订阅，按钮回调通过通用 `_try_handle_interaction_module_message` 路由到插件。

## [0.31.5] — 2026-06-26 · patch · 交互玩法付款人归属修复

### Fixed
- 修复转账通知 Bot 回复付款人消息后触发交互玩法时，TelePilot 将通知 Bot 误作为玩法 actor / session owner，导致 21 点等 inline keyboard 玩法按钮点击只 ACK 但无后续响应的问题。
- 交互玩法 payload 现在在 `payment_confirmed` 事件中优先使用显式 `payer_user_id`，没有时使用转账通知所回复消息的原始发送者作为付款人；通知 Bot 身份仍保留在 `sender_*` 与 source 字段中。

## [0.31.4] — 2026-06-25 · patch · CI 与无奖金入口兼容修复

### Fixed
- 修复 GitHub Actions 干净 checkout 不包含本机 `plugins/installed` 运行时目录时，已安装互动插件契约测试误失败的问题；本机有插件样本时仍会执行完整契约测试。
- 修复 `pt_promote` 这类无奖金字段的工具型交互入口在缺少本机插件元数据时，旧 `module_prize` 仍可能被保留并显示奖金的问题。

## [0.31.3] — 2026-06-25 · patch · 交互 Bot 按钮与插件运行修复

### Added
- 交互 Bot runtime 支持插件返回 `send_message.reply_markup` 发送 inline keyboard，并将按钮点击作为 `callback_query` 事件路由回对应活跃会话。

### Changed
- 插件交互 payload 补充 `callback_query_id`、`callback_data` 和按钮原消息文本；插件开发文档同步补齐按钮事件与 `reply_markup` 规范。

### Fixed
- 修复交互 Bot 轮询未订阅 `callback_query`、模块消息路由跳过按钮事件，导致 inline keyboard 无法使用的问题。
- 修复内置算术题 `math10` 通过主进程 fallback 成功时，worker 仍提前写入“模块未加载或未启用” WARN 的误导日志。

## [0.31.2] — 2026-06-25 · patch · 联动玩法查询列表精简

### Changed
- 联动玩法查询列表精简为规则名称与触发方式，不再重复展示插件 key、金额门槛、收款人、奖金、限时、CD 和日上限等详情。

## [0.31.1] — 2026-06-25 · patch · 联动玩法查询配置修复

### Added
- 联动交互 Bot 的玩法查询新增回复模板、无可用玩法提示和 Telegram HTML 预览，可配置 `{items}`、`{count}`、`{closed_count}`、`{chat_id}` 占位符。

### Changed
- 联动交互 Bot 规则区保存按钮改为规则区内 sticky 浮动按钮，避免固定在整页底部导致移动端当前编辑区域保存不顺手。

### Fixed
- 修复无奖金字段的工具型交互入口仍被保存和展示“奖金”的问题；后端会根据插件入口 schema 清理旧的 `module_prize`，玩法查询也不会再显示不存在的奖金。

## [0.31.0] — 2026-06-25 · minor · 联动玩法查询与规则体验优化

### Added
- 联动交互 Bot 新增可配置的“玩法查询指令”，群友可查询当前群已开启的联动玩法、关键词/转账条件、奖金和每用户限制。

### Removed
- 联动交互 Bot 页面移除“最近互动结果”列表，并删除对应的结构化结果查询 API 与专用运行日志记录；插件结果仍用于当次动作、会话结束和结算公告。

### Changed
- 联动交互 Bot 规则保存改为右下角悬浮按钮，减少移动端反复滚动保存的成本。

### Fixed
- 修复规则选择“启动玩法”并选择内置算术题时，账号未单独启用 `math10` 插件会提示“模块未加载或未启用”的问题。
- 修复九宫格竞猜交互路径未从标准 `actor.user_id` 读取答题人，导致同一用户答题冷却不稳定的问题。

## [0.30.5] — 2026-06-19 · patch · 联动占位与配置保存修复

### Added
- 转账通知 Bot 模板配置新增 Telegram HTML 消息预览，使用示例付款人、收款人与金额渲染最终效果。

### Changed
- 联动插件启动占位消息会被第一条正式交互 Bot 文本消息编辑覆盖；若正式结果由通知 Bot、UserBot 或图片动作发送，交互 Bot 会删除原占位消息，避免群内同时出现占位和正式结果。
- 账号 Worker 弹出层、设置页、插件页和各类保存/导入/安装按钮补齐一致的操作图标与加载态，提升移动端和 PWA 下的可见性。

### Fixed
- 修复插件配置按全局/账号分层保存时仍使用完整 schema 校验，导致 `pt_promote` 只保存 Cookie 时误报 `command`、`torrent_cooldown_seconds` 缺失的问题。
- 修复插件字段从账号配置迁移到全局配置后，旧账号级 Cookie 在 worker 运行时被过滤导致 PT 置顶促销误提示“请先配置 Cookie”的问题；旧值会作为兼容回退，直到成功保存全局配置。
- 修复通用插件配置页中全局字段被账号级旧值优先覆盖的问题；全局字段现在优先展示已保存的全局配置，仅在全局为空时用旧账号值辅助迁移。

### Tests
- 补充账号 Bot 联动动作、插件分层配置校验和 worker 配置合并回归测试。

## [0.30.4] — 2026-06-19 · patch · Redis 消费超时降噪

### Fixed
- 修复主进程可靠消费 `runtime_log` / `ratelimit_event` 时只捕获 Python 内置 `TimeoutError`、未捕获 redis-py `TimeoutError` 的问题，避免空队列等待被误记为生产错误日志。

## [0.30.3] — 2026-06-19 · patch · 插件口径统一

### Changed
- 用户界面、README、插件开发文档、示例文档和项目规范统一使用“插件”指代可安装、可启停、可配置的扩展能力；“模块化”仅保留为架构特色描述。
- 插件中心、日志筛选、配置备份、账号向导、系统设置和通用插件配置弹窗同步更新旧“模块”文案，减少前端入口称呼混用。
- 插件开发指南拆分页的开头说明改为当前维护版参考，不再把文档描述成旧版开发指南搬运内容。

### Fixed
- README 当前版本号从旧的 `v0.23.1` 同步为本次发布版本，避免文档首页显示过期版本。
- 架构文档中的 PluginContext 指引改到 API 参考与安全边界页，避免开发者从索引页寻找完整字段说明。

## [0.30.2] — 2026-06-19 · patch · 工作台细节补齐

### Added
- 系统概览资源采样卡新增宿主机已正常运行时间，方便直接判断服务持续运行状态。
- 联动交互 Bot 规则区新增就近保存按钮，规则编辑后无需滚动到底部才能保存。
- 联动规则监听群支持从账号“已允许会话”列表中多选 Chat ID，同时保留手填能力。

### Changed
- 账号 Worker 弹出层在移动端/PWA 改为更明显的底部面板，桌面端增强边框、阴影和标题层级，减少被忽略的概率。
- 联动规则启用开关移到左侧规则卡片，规则详情页移除重复的启用、排序、复制和删除入口。
- 规则触发区压缩 Chat ID、触发方式和启动关键词输入框尺寸，减少首屏空间占用。

### Fixed
- 转账结果通知 Bot 的信任来源文案从“用户 ID”修正为“Bot ID”，避免配置含义混淆。

## [0.30.1] — 2026-06-19 · patch · 联动规则编辑器精简

### Changed
- 联动交互 Bot 的规则编辑器改为按“触发”“启动内容”“奖励与限流”分段展示，弱化工程字段，把插件参数与技术详情收进折叠区，减少多插件规则混在同一页时的干扰。
- 玩法入口选择卡片改为直接展示插件名、入口名和玩法类型，并将启动占位消息、奖金、有效期、每用户 CD、每用户日上限统一放到更贴近规则流程的位置。
- 移除规则列表上方“监听群来源 / 规则范围”的说明块；监听群仍由每条规则独立填写，保存逻辑保持不变。

## [0.30.0] — 2026-06-19 · minor · Bot 联动入口拆分

### Changed
- 账号详情页将原“Bot 联动”拆为“管理 Bot”和“联动交互 Bot”两个独立页面，管理 Bot 专注远程管理、Token、运行状态和授权用户，联动交互 Bot 专注交互 Bot、转账结果通知 Bot、互动结果和规则配置。
- 旧链接 `?tab=bot` 继续兼容并自动落到“管理 Bot”，避免历史入口和外部跳转失效。
- 模块首页、消息模板实验室和系统设置中的相关跳转与文案同步改为“管理 Bot”，减少和联动交互 Bot 的职责混淆。

## [0.29.5] — 2026-06-19 · patch · 用户展示名隐私收敛

### Fixed
- 转账测试与通知 Bot 生成收款结果时，付款人和收款人展示名只使用 Telegram 本人的 `first_name` / `last_name`，不再把 `@username` 拼进“付款人/收款人”文案；规则匹配仍保留用户 ID 与 username 元数据，避免影响现有收款人识别。
- 新增插件运行时公开展示名 helper：Telethon/UserBot 实体标记为联系人时，不再把 `first_name` / `last_name` 当作玩家名公开输出，优先使用公开 username（按场景不带 @ 或保留 @），没有 username 时回退用户 ID。
- 内置 `game24`、`auto_reply`、`forward` 与最近会话标签统一接入公开展示名策略，避免玩家公告、自动回复模板或私聊标签泄露账号本地联系人备注。
- 补充账号 Bot、自动回复、24 点与运行时 helper 回归测试，覆盖带 username 的付款人/收款人仍输出纯展示名、联系人备注不会进入玩家公告，并保持 `language-转账成功` 代码块标识。

## [0.29.4] — 2026-06-18 · patch · 联动规则配置收敛

### Changed
- 账号 Bot 联动规则页把规则层并发配置改名为“规则占用”，并将插件内部“会话范围”收进技术详情，减少入口参数里两个相似作用域同时出现造成的重复感。
- 状态总览文案从“插件入口参数”收敛为“入口参数”，和当前规则编辑结构保持一致。

## [0.29.3] — 2026-06-18 · patch · 转账通知代码块标识

### Fixed
- 转账结果通知 Bot 的默认到账模板改为带 `language-转账成功` 的 HTML code/pre 代码块，并在读取旧版纯文本默认模板时自动升级，避免真实收款通知失去专用触发标识。

## [0.29.2] — 2026-06-18 · patch · 交互入口参数收敛

### Changed
- 账号 Bot 联动规则页移除旧的“付费与收款限制”“运行与限流”分区，把转账金额、收款人匹配收进“触发条件”，把奖金、参与有效期、并发策略、每用户 CD 和每用户日上限统一收进“入口参数”。
- 插件入口额外声明的配置在前端改名为“插件参数”，并提示已由平台接管的 `prize`、`timeout`、`valid_seconds`，避免和统一入口参数混在一起。

### Fixed
- 修复每用户 CD、每用户日上限被错误绑定到“按用户并发”的问题；现在按群会话的互动插件也可以独立限制同一付款人或消息发送者的调用频率。
- 修复关键词触发规则隐藏付款匹配字段时，后端会连带清空用户限流配置的问题。

### Tests
- 补充回归测试覆盖关键词规则保留用户限流，以及 `concurrency=chat` 时每用户 CD 和日上限仍会在插件成功后生效。

## [0.29.1] — 2026-06-18 · patch · 交互插件校验自动发现

### Changed
- 已安装互动插件校验脚本改为自动发现所有声明了非空 `interaction_entries` 的 installed 插件，不再固定只检查当前 6 个样本，避免后续安装 blackjack、dice_battle、idiom_chain 等互动插件时漏过契约漂移。

### Tests
- 新增回归测试覆盖 installed 交互插件校验脚本只纳入非空交互入口插件，避免工具/自动化插件因 `interaction_entries=[]` 被误判为互动插件。

## [0.29.0] — 2026-06-18 · minor · 交互插件统一调用与结果隔离

### Added
- 新增交互插件统一调用契约，支持 `interaction_profile`、`launch_mode`、`events`、`session_scope`、`payload_contract`、`result_contract`、`settlement` 与 `preserve_command_trigger`，让娱乐性、交互性插件按同一套入口接入交互 Bot。
- 新增标准交互 payload envelope，把来源、操作者、被回复消息、触发规则、会话与结算信息一并传给插件，插件可明确区分监听来源并选择回复发送通道。
- 新增结构化互动结果接口 `GET /api/accounts/{aid}/interaction-results`，前端可查看最近赢家、奖金、赢家消息 ID、发奖账号与发送状态，避免从群消息文本里猜结果。
- 新增 `examples/plugins/with_interaction` 最小交互插件示例和 installed 交互插件静态校验脚本，方便后续插件库按规范迁移。

### Changed
- 交互插件规范继续收敛到统一契约：示例、文档与校验链路同步覆盖 `interaction_profile`、`interaction_entries`、`preserve_command_trigger` 与 `on_interaction`，方便后续娱乐性、交互性插件按同一套规则接入。
- 账号 Bot 的交互规则工作台继续优化移动端宽度适配，减少固定栅格和截断文案在 PWA 窄屏下的拥挤感。
- 已安装互动插件的 `plugin.json` / `manifest.py` 对齐范围继续扩大到猜数字、诗词填空、九宫格、彩票、红包和 PT 促销，并新增 installed 交互插件静态校验脚本，避免安装态与运行态的交互契约继续漂移。
- 交互 Bot 运行时现在按插件声明的 `events` 做事件边界控制，同时保留旧插件未声明事件时的兼容放行，避免交互入口吞掉原本的命令触发路径。
- 账号 Bot 联动规则页改为规则列表与规则详情分区，并按交互类型分组入口选择，减少不同插件的输入框和选择框挤在同一页。

### Fixed
- 修复九宫格、猜数字、诗词填空、彩票、红包和 PT 促销等 installed 插件安装态元数据与运行态 manifest 不一致的问题。
- 修复旧日志或第三方插件写入未知 `send_via` / settlement 状态时，最近互动结果接口可能因为枚举过严而无法展示的问题。

### Tests
- 插件示例校验脚本现已把 `examples/plugins/with_interaction` 纳入稳定 API gate，保证最小交互示例的目录结构与 manifest 契约持续有效。
- 新增回归测试覆盖原始命令触发保留、事件白名单分发、结构化赢家提取、旧结果值兼容、installed 插件契约和 game24 / math10 交互入口。

## [0.28.0] — 2026-06-18 · minor · 交互 Bot 规则工作台

### Changed
- 账号 Bot 联动的规则配置改为“规则工作台”：左侧集中展示规则摘要，右侧编辑当前规则，避免多条规则的不同输入框和选择框全部挤在同一页。
- 交互 Bot 规则编辑器按触发条件、动作与入口、付费与收款限制、运行与限流、模块高级参数和管理指令分区展示，并在摘要里显示启用状态、触发方式和模块入口。
- 前端选择模块规则时会在模块只有一个交互入口的情况下自动补齐入口，并展示自动推断状态，降低后续娱乐和交互模块接入成本。

### Fixed
- 修复 24 点规则只配置 `module_key=game24`、未显式保存 `module_action` 时，群友转账触发后仍提示需要命令触发的问题；后端现在会根据模块声明的唯一交互入口自动推断 `start_paid_game`。

### Tests
- 新增回归测试覆盖交互 Bot 模块规则自动推断唯一入口，以及转账通知触发 24 点时由交互 Bot 直接开局的路径。

## [0.27.3] — 2026-06-08 · patch · 交互 Bot 会话作用域兼容修复

### Fixed
- 修复交互 Bot 规则启用“按用户冷却/每日次数”后，九宫格、24 点、十以内算数等群局模块可能被误存成用户私有会话，导致其他群友回复数字或答案无法进入同一局的问题。
- 交互 Bot 规则现在会独立保存模块入口会话作用域 `module_session_scope`，并优先读取插件 `plugin.json` / `manifest.py` 中声明的 `interaction_entries[].session_scope`，避免后续插件再次把用户限流作用域和模块会话作用域混用。

### Changed
- 前端交互 Bot 规则表单选择模块入口时，会保存入口声明的模块会话作用域，同时保留规则层“并发策略”用于用户冷却和每日次数。
- 插件开发文档补充交互 Bot、自动回复、UserBot 命令与插件本体的职责边界，明确 `session_scope` 与 `concurrency` 的区别，并更新远程模块联动入口开发规范。

### Tests
- 新增回归测试覆盖“规则按用户限流但群局模块仍按群保存会话”的场景，以及未来 installed 插件通过 `plugin.json` 声明 `session_scope=chat` 的通用兼容路径。

## [0.27.2] — 2026-06-02 · patch · PWA 写请求与缓存修复

### Fixed
- 前端请求层现在会在每次请求拦截器里强制补齐 `X-Requested-With: telepilot-ui`，并在 CSRF token 失效或缺失时自动刷新 token 后重试一次写请求，降低移动端 PWA 旧状态导致“缺少或非法请求头”的概率。
- 生产前端 Nginx 对 `index.html`、`sw.js`、`manifest.webmanifest` 等 PWA 入口文件改为禁用缓存，避免手机已安装 PWA 长时间持有旧入口或旧 service worker；带 hash 的静态资源继续使用长期缓存。

## [0.27.1] — 2026-05-31 · patch · 消息模板实验室同步与预览修复

### Fixed
- 修复消息模板实验室预览 `<pre><code class="language-...">` 时没有放行 `code` 的 `language-*` class，导致带语言标识的 Telegram 代码块被转义显示的问题。
- 修复插件真实配置保存后，消息模板实验室仍可能保留旧 catalog 草稿的问题；插件配置保存现在会刷新模板 catalog，实验室进入、回焦或 catalog 更新时会同步最新真实模板内容。

## [0.27.0] — 2026-05-31 · stable · 消息模板实验室

### Added
- 新增“消息模板实验室”：可从模块页的账号模块启用详情进入，按账号查看系统内置与模块模板，编辑样例变量后由后端渲染 Telegram HTML，并查看最终文本、格式解析结果和私聊测试发送结果。
- 新增消息模板后端 API，支持模板 catalog、渲染校验和私聊测试发送，模块模板会按模块分组展示并保留原始 `parse_mode`。
- 消息模板目录新增 AI 自定义命令输出模板，支持直接预览 AI 回复模板和默认输出模板。

### Security
- 消息模板测试发送仅允许发送到当前账号已授权、已启用且和 Bot 建立过私聊的用户，禁止向群聊或未知 chat_id 发送。
- 测试发送前新增 Telegram HTML allowlist 校验，并写入审计日志；未知标签、未闭合标签或非法属性不会触发真实发送。

### Changed
- 消息模板实验室的模板目录改为按模块/功能折叠展示，默认只展开当前选中模板所在分组，避免模板多时目录过长。
- 消息模板实验室的 Telegram HTML 预览会自动换行长模板标识，并把实体摘要改为面向用户的格式解析说明。
- 消息模板测试发送卡片补充授权私聊用户的完整处理路径，并支持一键跳转到当前账号的 Bot 联动授权用户页。
- 状态摘要面板在窄内容区下改为优先纵向排布，避免标题和操作区互相挤压。

## [0.26.3] — 2026-05-30 · patch · worker Redis 监听稳定性修复

### Fixed
- 修复 worker 命令/全局 Redis pubsub 监听在空闲读超时时反复重连，导致交互入口命令可能落在重连窗口并最终显示“worker 调用超时”的问题。
- 修复运行日志/限流事件可靠消费在 Redis 阻塞读空闲超时时刷错误日志的问题，并把 worker Redis 连接池默认上限从 4 提高到 8，给插件锁和 IPC 回执留出连接余量。

## [0.26.2] — 2026-05-30 · patch · 交互入口慢插件超时修复

### Fixed
- 修复 `pt_promote` 等需要连续访问外部站点的交互入口超过 5 秒就被交互 Bot 判定为“worker 调用超时”的问题；主进程现在会给插件交互入口最多 60 秒完成业务处理。

## [0.26.1] — 2026-05-29 · patch · 插件交互入口失败计数修复

### Fixed
- 修复插件交互入口返回“已处于置顶状态/不再处理”等业务失败时仍会被规则层当作成功扣次数的问题，配合 `pt_promote 1.0.6` 保证置顶失败、重复触发或已冷却时不消耗用户每日次数。

## [0.26.0] — 2026-05-29 · stable · 交互 Bot 规则模板与用户限流

### Added
- 交互 Bot 规则支持按用户设置冷却和每日调用上限，关键词触发与转账通知触发共用同一套计数逻辑；成功才扣次数，失败、重复触发、冷却中或已处于置顶状态不会消耗次数。
- 模块启动关键词支持更易读的数字模板，例如 `置顶 id=数字` 和 `猜骰 num=数字`，群友发送 `置顶 id=12345` 时会把提取到的 ID 直接传给模块入口。

### Changed
- 账号交互 Bot 规则模板改为“核心字段常驻、低频配置折叠”的布局：付费限制、运行参数、模块高级参数和规则管理指令按当前动作与触发方式动态显示。
- 自动回复的置顶促销次数提示改为“今日已成功置顶促销 x/y 次”，并在追加到命令结果时保留 HTML 链接、标题、副标题和解析模式。

### Fixed
- 修复仅关键词触发的规则仍可能保存隐藏金额/收款人条件，导致无付费规则被旧字段阻断的问题。
- 修复 `payment` / `both` 触发路径没有进入按用户冷却与每日上限检查的问题，并补上执行中占位锁，避免同一用户并发穿透限制。

## [0.25.0] — 2026-05-29 · stable · 自动回复变量与交互 Bot 正式版

### Added
- 自动回复支持正则捕获组和更易读的变量模式，例如 `置顶 id=数字` 可匹配 `置顶 id=12345` 并渲染为 `{prefix}pt {id}`，也支持 `{prefix}`、`{amount|1000}` 默认参数、`2s` / `2m` / `2h` / `2d` 冷却时间、按用户冷却和每人每日调用上限，继续沿用自动指令白名单。
- 自动回复冷却中会提示该用户今日次数、每日上限和剩余 CD；达到每日上限时提示当日不可再用，并新增 `arcd` 管理命令，可由管理员回复群友消息或指定用户 ID 重置他的用户冷却和今日次数。

### Fixed
- 自动回复触发白名单命令时，命令内部多次 `edit()` 会合并到同一条回复消息；最终一次达到每日上限时，次数提示会直接追加到命令结果底部，不再额外刷出一条消息。
- 自动回复执行慢命令时会先写入短期占位锁，避免同一用户在命令完成前并发穿透冷却或每日上限。
- 转账联动会同时解析被引用的转账消息正文，修复当前消息只包含 `转账成功` 或语言标识、金额在引用消息里时不触发的问题。
- `arcd` 重置命令会同时清理会话级冷却、用户级冷却、执行中占位锁和今日次数，避免显示已重置但仍处于冷却中。

## [0.25.0-rc.8] — 2026-05-28 · rc · 开发文档与交互模块体验修复

### Changed
- 模块管理的开发指南页改为内置完整文档工作台，支持一屏阅读合集，也可以按概览、API、HTTP、安全、远程、速查和 AI facade 分篇查看；文档内链接会直接切换到对应分页。
- 账号 Bot 联动里的“模块入口参数”收纳为默认折叠的“模块高级参数”，常用的奖金和参与有效期改为只从上方业务输入框维护。
- 保存转账联动配置时会自动剔除 `module_config` 里的 `prize`、`timeout` 和 `valid_seconds`，避免高级 JSON 覆盖页面上的奖金和有效期。

### Fixed
- 转账联动现在会识别 Telegram HTML `<code>/<pre>` 块里的 `language-转账成功` 标识，正文不含单独“转账成功”行时也能正常触发模块。
- 内置“发十以内算数题”会自动提供 `发十以内算数`、`十以内算数`、`开算数题` 关键词，并在无付费门槛时允许关键词直接开局。
- 九宫格竞猜远程模块的交互 Bot 入口触发后会直接发送题图并进入 1-9 答题会话，不再提示用户额外发送 `dicegrid` 指令。
- 九宫格竞猜答对后会结束交互会话，并输出可被 UserBot 自动发奖识别的中奖公告。
- 自动发奖识别范围补充 `dice_grid_hunt` 模块规则，避免中奖公告进入人工发奖路径。

## [0.25.0-rc.7] — 2026-05-28 · rc · 交互 Bot 联动修复与优化

### Added
- 新增内置 `math10` 交互 Bot 模块，支持转账命中或关键词开局、群内答题、中奖公告和会话清理，并保留旧 `action=math10` 兼容路径。
- 设计文档补充三角联动中 UserBot 的职责边界，说明低频 outgoing、必要 incoming 订阅、Bbot 不碰钱和 UserBot 发奖的分工。

### Changed
- `interaction_bot_service` 收敛为 `account_bot_service` 的 façade，避免转账联动配置、归一化和 token 读取逻辑双份漂移。
- 测试发送不再通过 in-process 转账探针直连触发联动，改为提示 Bbot 通过 polling 自然接收并触发。
- game24、math10 中奖公告会根据自动发奖状态切换“自动发放”或“人工回复”提示，减少群内认知割裂。

### Fixed
- 转账通知可信来源改为默认拒绝；启用转账触发规则时必须配置可信通知 Bot ID，避免任意用户伪造付款文本触发模块。
- UserBot 自动发奖改为优先校验 Bot ID，并把公告消息 ID 纳入 24 小时去重 key，降低误发和重启重复发奖风险。
- 转账通知模板包含坏占位符或格式错误时，Bbot 会回退到默认通知模板继续发送，并把渲染失败写入 warning 与 runtime log，便于在 GUI 中定位配置问题。

## [0.25.0-rc.6] — 2026-05-28 · rc · 前端状态视觉推广与协作流程沉淀

### Added
- 新增共享状态视觉组件，统一状态胶囊、指标卡、进度条、总览面板和区块标题的展示方式。
- 新增 TelePilot Agent Playbooks，把发布检查、插件排障、UI 验收、部署检查和项目健康巡检沉淀为项目级协作流程。

### Changed
- 概览、AI 中心、模块中心、扩展管理、账号详情、系统设置、日志中心和指令模板页接入统一状态视觉语言，增强运行状态、过滤状态、账号/模块计数和风险信号的可扫性。
- 系统设置、日志和模板编辑器保持高密度工作台结构，仅用轻量标题和状态胶囊补强层级，避免表格/表单工作流被卡片化。
- 指令模板编辑器增强 Telegram 最终预览，可展示解析模式、示例占位符映射和 reply_text 模板的即时预览。
- 基础卡片边框改为使用统一边界色，提高深色界面中相邻工作区的层次辨识度。

## [0.25.0-rc.5] — 2026-05-28 · rc · 内置插件 manifest 规范化

### Changed
- ChatGPT2API 与 Codex 图片生成内置插件统一使用 `usage_preview` 提供配置页顶部说明，并保留 `template_placeholders` 兼容旧读取路径。
- 通用插件配置页会把 `usage_preview` / `ai_usage_guide` / `template_placeholders` 作为只读说明展示，不再重复渲染到配置字段区，并支持替换非敏感 schema 字段占位符。
- Codex 图片生成 manifest 补齐 `delete_message` 权限、封闭配置 schema，并为所有配置字段显式标注账号级作用域。
- 自动复读、消息转发和定时任务内置 manifest 的规则/平台 schema 改为封闭对象；自动复读补齐 `resolve_entity` 权限声明。
- 内置插件包索引补齐 `chatgpt_image`，保持动态扫描结果与包索引一致。

## [0.25.0-rc.4] — 2026-05-27 · rc · 插件目录自愈与工作台布局统一

### Changed
- 工作台主内容区域改为更适合大屏的统一宽度策略，概览、AI、模块中心、日志中心和系统设置使用一致的页面标题、图标与内容位置。
- 模块中心卡片布局调整为信息区与右侧“配置”操作区分离，避免模块名称、状态标签和操作按钮在窄宽度下相互挤压或竖排。

### Fixed
- 远程/仓库插件更新时，如果 `plugins/installed/<key>` 目录缺失但记录仍保留 `source_url`，现在会自动重新拉取并恢复插件目录，避免老安装记录只能卸载重装。
- 修复多插件仓库更新时临时克隆目录过早释放导致子插件目录复制不可靠的问题。
- AI 近期调用、日志中心运行日志和审计日志表格在窄视口下支持横向滚动，避免内容撑破页面。

## [0.25.0-rc.3] — 2026-05-27 · rc · forward_to 媒体复读与 CI 修复

### Added
- 自定义指令模板 `forward_to` 新增 `copy_media` 转发方式，可复制发送被回复消息的贴纸、图片、文件等媒体；纯文本消息会自动退回普通文本发送。
- 自定义指令模板配置页新增“复读媒体（贴纸/图片/文件）”选项。

### Fixed
- 修复 GitHub Actions 上 Python 3.12.13 环境中两个远程插件安全测试因 `asyncio.get_event_loop()` 无默认 loop 而失败的问题。

## [0.25.0-rc.2] — 2026-05-27 · rc · 插件安装表切换与配置表拆分

### Added
- 新增 `plugin_global_config` 独立配置表与迁移 `0028`，插件全局配置读写改走独立表，并保留从 `feature.manifest["global_config"]` 回退读取的兼容路径。
- 新增 LLM 用量 `triggered_by_account_id` 字段与迁移 `0027`，为交互 Bot 触发链路后续归属真实账号预留审计字段。
- 新增存量插件 lint 回填脚本，可对已安装插件重新扫描 metadata lint warning 并写入 `installed_plugin.lint_warnings`。

### Changed
- ⚠️ 升级提示：手工拷贝到 `plugins/installed/<key>/` 但未通过安装接口的目录现在被识别为"孤立目录"，不再出现在模块中心，也无法启用。请改用模块仓库安装或 zip 安装。
- 插件 loader 授权切换为单读 `installed_plugin` 表，不再依赖 `PluginInstall` / `RemotePlugin` 运行时状态。
- `PluginInstall` 与 `RemotePlugin` 转为只读兼容快照，zip、Git、仓库和本地导入安装、更新、启停、卸载统一写入 `InstalledPlugin`；迁移 `0029` 会从老表回填缺失安装记录。
- ⚠️ 迁移提示：`0029` 的 downgrade 是 no-op；回退到 `0028` 后，`PluginInstall` / `RemotePlugin` 老表只保留 `0029` 之前的存量数据，升级后写入 `InstalledPlugin` 的新安装状态不会被逆向回填。
- 模块中心 `FeatureInfo` 暴露 `lint_warnings`，前端模块卡片可折叠展示 lint 提醒。
- `ctx.ai.complete()` 移除 `provider_hint` 兼容入口，并对 `tag` / `tags` 别名发出 `DeprecationWarning`；新模块应使用 `provider_tag`。
- 插件 HTTP facade 的策略错误与响应过大错误会带上 `plugin_key`，便于从日志定位触发插件。
- installed 插件模块缓存清理改为维护已加载模块名集合，减少 reload 时全量扫描 `sys.modules` 的开销。

### Fixed
- 修复 `SandboxClient` 子类无法被 loader 识别为已沙箱化 client 的问题，改用 `_is_sandboxed` marker 判断。
- 为声明但当前未支持的 facade 权限（如 `ai_vision` / `ai_image` / `ai_stt`）写入 runtime warning，避免 manifest 权限被静默忽略。
- 明确插件 AI quota 跨午夜释放语义，按 acquire 当天的 daily key 回滚，保持软上限记账一致。
- 补齐前端 `pnpm typecheck` 脚本，使项目约定的类型检查命令可直接运行。

### Docs
- 拆分 `PLUGIN-DEV-GUIDE.md` 为概览、API 参考、HTTP、远程模块、安全与速查页，并清理旧锚点引用。
- `PLUGIN-AI.md` 补充 `plugin_ai_quota` 配置示例、Redis 降级行为、软上限误差与跨日记账说明。
- 远程模块文档改为说明 `InstalledPlugin.enabled` 是当前安装状态来源，老表仅保留兼容读取。

## [0.25.0-rc.1] — 2026-05-27 · rc · 插件运行时安全基线与 AI/HTTP facade

### Added
- 新增插件安全 HTTP facade `PluginHTTP`，支持 `external_http` 权限、域名白名单、账号代理、响应体流式大小限制和基础 SSRF 防护，为后续 `ctx.http` 能力落地提供运行时基础。
- 新增插件安全 AI facade `PluginAI`，仅对声明 `ai_text` 的插件注入 `ctx.ai`，复用现有 LLM fallback、usage 与账号预算链路，并向插件返回脱敏 Provider 元数据。
- 新增插件 AI 配额预扣服务与插件 LLM 用量聚合 API，`ctx.ai` 调用会按插件/账号预扣 prompt + output token，并用 Redis 滚动窗口控制并发消耗；前端模块中心可展示插件消耗。
- 模块中心新增来源/信任徽章与加载失败提示，可展示内置核心、签名通过、远程 Git、孤立目录、签名失败等状态。
- 新增 `installed_plugin` 统一安装记录表的兼容迁移，并在 zip/Git/仓库/本地导入安装、更新、启停和卸载时同步安装记录，为后续切换运行时读新表做准备。
- 新增 `examples/plugins/with_http` 与 `examples/plugins/with_ai` 示例及 CI 校验脚本，防止示例插件随 API 演进腐化。

### Changed
- 第三方 installed 插件加载改为统一授权入口，综合校验 `PluginInstall`、`RemotePlugin`、账号开关、签名状态和孤儿目录，避免不同安装路径绕过禁用状态。
- `Manifest.permissions` 默认改为空列表，要求插件显式声明运行时权限；非 TelegramClient 能力由独立 facade 处理。
- `feature_matrix` 增加插件来源、孤儿状态与签名状态字段，减少前端对模块来源的猜测。
- 本地 `plugins/installed/*/plugin.json` 若没有安装记录会被视为孤儿目录，模块中心不再自动写入 feature 行；已有孤儿 feature 行会被清理。
- `sum` 插件改为通过 `ctx.ai.complete()` 调用平台 LLM，不再直接 import 后端数据库和 LLM 私有服务。
- installed 插件运行期收到的 Telegram event 会通过 `SandboxEvent` 包装，`event.reply/edit/delete/get_reply_message` 与 `event.client` 同样遵守 manifest 权限。
- 远程/Git 插件安装更新新增静态 lint warning，提示私有 `app.db` / `app.services` import 与缺少 timeout 的 HTTP 调用，但本阶段只告警不阻断。

### Fixed
- 修复 zip 安装插件在 `PluginInstall.enabled=False` 或签名失败时仍可能被 worker 按其它状态路径加载的问题。
- 修复磁盘存在但没有安装记录的孤儿插件缺少结构化失败状态的问题，现在会写回 `AccountFeature.state=failed` 和可读 `last_error`。
- 修复已加载 installed 插件在全局授权失效后热更新时状态不同步的问题，reload 会卸载实例并写回 disabled/failed 状态。
- 修复 installed 插件目录名、`MANIFEST.key` 与 `Plugin.key` 不一致时可能污染全局注册表的问题，import 失败也会回滚注册表副作用。
- 修复 `ctx.http` 可通过请求级参数覆盖平台策略的问题，现在会拒绝自动重定向、请求级 timeout 和未白名单参数。
- 修复插件 AI 配额只按输出上限预扣的问题，长 prompt 现在也会计入预扣估算；Redis 结算不再复活无 TTL 的过期 key。

### Docs
- 更新插件开发指南，修正远程模块 API、权限表、`codex_image` / `translate` 描述、`cleanup_mode` 语义，并补充 `ctx.http` / `ctx.ai` 当前使用方式。

## [0.24.2] — 2026-05-26 · patch · 插件配置页 AI 下拉与模板预览

### Changed
- 通用插件独立配置页接入 TelePilot LLM Provider 查询，`llm-provider-select` / `llm-model-select` 不再只在弹窗模式可用。
- 模型下拉支持按字段声明过滤文本模型，避免文本插件误选 `gpt-image-*` 等图像模型。

### Fixed
- 修复部分插件在独立配置页里 Provider 下拉显示“尚未配置 TelePilot AI Provider”但实际已配置的问题。
- 补齐模板预览示例上下文中的总结类占位符（`chat_display`、`time`、`message_count`、`summary`），使输出模板预览与运行时更一致。

## [0.24.1] — 2026-05-26 · patch · 插件 AI 配置与沙箱实体解析

### Added
- 远程模块沙箱新增 `resolve_entity` 能力，允许明确声明该能力的插件调用 Telethon `client.get_entity`，避免实体解析被误归入普通聊天读取权限。
- 插件配置页支持 `llm-provider-select` 与 `llm-model-select` 动态控件，可直接选择 TelePilot 已配置的 AI Provider 和已启用模型。

### Fixed
- 修复 `sum` 等远程模块在真实 Telegram 事件流程中触发实体解析时仍报缺少 `client.get_entity` 权限的问题。

## [0.24.0] — 2026-05-26 · minor · 安全加固与模块体验升级

### Added
- 新增 `python -m app.scripts.rekey` 运维脚本，支持 MASTER_KEY dry-run 验证与原子重钥，覆盖账号、代理、Bot、LLM、TOTP 与转账通知相关密文字段。
- AI `video` 模式改为独立视频插件后端入口，可配置 `video_bridge` 等插件承接生成视频，不再误挂到现有文本/图片 LLM Provider。

### Changed
- Web 写操作升级为 `X-Requested-With` + double-submit CSRF token 校验，前端会自动获取并回传 `csrf_token`。
- 前端 Nginx 与后端 API 补充 CSP、nosniff、Referrer-Policy、Permissions-Policy 等安全响应头，并同步更新安全运维文档。
- 账号模块启用详情列表新增本地/远程来源与版本号标签，缩小并固定标签列宽；更新说明收进“有更新”标签的悬停/点击提示里。

### Fixed
- 收窄远程模块规范扫描的逗号命令识别范围，避免普通中文标点和逗号分隔配置被误报为硬编码指令前缀。
- 修正模块中心把 worker 运行状态误当成账号开关状态的问题，避免详情页显示已启用而列表仍显示未启用。

## [0.23.1] — 2026-05-22 · patch · README 轻量部署与界面细节

### Changed
- 统一 AI 与模块管理页面的标签胶囊样式，优化模块规范警告展开、远程模块版本状态、AI 快速上手折叠和概览标题图标。
- README 新增最轻量 Docker Compose 启动示例，折叠截图、架构、调优等繁杂内容，并说明启动密钥、数据库命名、Web 面板可配置项和仍需保留在启动或反代配置中的内容。

### Fixed
- 修正生产容器未挂载 Git 工作树时的更新弹窗文案，不再显示“远程有 0 个新 commit”和未知 commit，而是提示需在宿主机执行更新命令。

### Verification
- `pnpm --dir frontend exec tsc -b --pretty false`
- `PYTHONPYCACHEPREFIX=.tmp/pycache python3.12 -m py_compile backend/app/api/system_health.py`
- `pnpm --dir frontend build`
- `git diff --check`

## [0.23.0] — 2026-05-22 · minor · 远程模块更新检查与配置体验

### Added
- 远程模块安装、更新和本地导入时会静态扫描 `plugin.json` / `manifest.py` 的模板默认值，发现疑似硬编码 `,xxx` 命令前缀时给出非阻断警告，提醒模块改用 `{prefix}` 占位符。
- 已安装远程模块支持手动和后台自动检查更新，可在“已安装模块”中配置检查开关与间隔；发现新版本后会在“账号模块启用详情与配置”和“已安装模块”入口提示。

### Changed
- README 与公网部署文档改为更面向普通用户的说明方式，新增部署方式选择表和源码混合运行路径，降低首次部署理解成本。
- 模块开发指南明确要求用户可见模板、默认值和示例使用 `{prefix}` 或运行时前缀，不再写死英文逗号。
- 从模块中心进入账号模块配置页时，返回按钮会回到模块中心；定时任务 cron 示例移动到触发类型区域并按行展示。

## [0.22.0] — 2026-05-21 · minor · 交互 Bot 与转账通知拆分

### Added
- 交互 Bot 与转账通知 Bot 运行时拆分为独立 polling；测试转账 Abot 仅负责 `+数字` 生成转账通知，Bbot 只消费真实转账通知并启动模块。
- 交互 Bot 规则与转账通知链路补齐 bot-to-bot 场景下的收款人识别，支持按收款人 ID / 用户名进行判定。
- 新增 `make prod-update` 生产增量更新入口，可按远程变更范围只重建 `web` / `frontend`，纯文档变更不重启服务，部署或依赖关键文件会自动回退完整更新。
- 检查更新弹窗支持展示运行环境、更新计划、变更组件、备份提醒和手动服务器命令，并根据本地源码模式或生产容器模式选择合适的更新动作。

### Changed
- 转账通知不再通过内部探针接力到 Bbot，避免测试 Abot 抢占交互 Bot 的职责边界。
- 后端更新接口改为返回更新计划并区分本地源码、生产容器有宿主机更新器、生产容器手动更新和不支持环境；容器内不再用 `docker compose restart` 冒充更新生效。
- 后端 Docker 镜像构建拆分依赖安装与源码安装层，减少普通后端代码变更时的生产重建耗时。

---

## [0.21.4] — 2026-05-21 · fixed · 定时任务保存刷新与编辑消息钩子

### Added
- 插件 loader 支持独立的 `on_message_edited(ctx, event)` 编辑消息钩子，编辑消息不会进入既有 `on_message` 流程。

### Fixed
- 定时任务保存时立即刷新 `next_fire`，避免编辑 cron / 一次性时间 / 间隔后列表仍显示旧的下次触发时间。
- 编辑消息事件不会被交互 Bot 的普通新消息占用判断提前吞掉，确保显式实现编辑钩子的插件可以收到事件。

---

## [0.21.3] — 2026-05-21 · fixed · 定时任务默认上海时区

### Fixed
- 将系统时区默认值统一为 `Asia/Shanghai`，修复未显式配置时定时任务按 UTC 计算或显示导致时间多出 8 小时的问题。
- 定时任务保存 `_cron_timezone` 标记，发现旧任务缺少时区标记或时区变化时会自动重算 `next_fire`。

---

## [0.21.2] — 2026-05-21 · fixed · 定时任务 cron 秒字段解析

### Fixed
- 修复定时任务 6 字段 cron 表达式按错误字段顺序解析的问题；现在 `0 5 11 * * *` 会按“秒 分 时 日 月 周”解析为每天 11:05:00。
- 定时任务试运行日志补充 cron 字段解释，便于区分 5 字段与 6/7 字段表达式。

### Added
- 定时任务编辑页的 cron 输入框下方新增实时预览，展示字段解释、触发含义和未来几次触发时间。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_scheduler_plugin.py`
- `backend/.venv/bin/ruff check backend/app/worker/scheduler_runtime.py backend/app/api/rules.py backend/app/tests/test_scheduler_runtime.py`
- `pnpm --dir frontend exec tsc --noEmit`
- `git diff --check`

---

## [0.21.1] — 2026-05-21 · fixed · 配置枚举选项汉化

### Fixed
- 通用模块配置页支持 `enumNames` 与 `enumDescriptions`，下拉选项会显示中文名称，并在字段下方展示每个选项的说明。
- 修复远程模块配置中枚举字段只能显示 `delete_only` 这类内部值，导致用户难以理解选项含义的问题。

### Verification
- `pnpm --dir frontend exec tsc -b --pretty false`
- `git diff --check`

---

## [0.21.0] — 2026-05-21 · added · 远程模块受控成员管理权限

### Added
- 远程模块沙箱新增 `moderate_chat` 高危权限，按声明开放受控的 `ban_user`、`kick_user`、`mute_user`、`unban_user` 成员管理方法。
- `moderate_chat` 只暴露包装方法，不开放 raw MTProto、真实 client、session 或 Telethon 私有状态，第三方模块仍需通过权限表调用。

### Verification
- `backend/.venv/bin/ruff check app/worker/plugins/sandbox.py app/tests/test_plugin_security_regression.py`
- `backend/.venv/bin/python -m pytest app/tests/test_plugin_security_regression.py -q`
- `git diff --check`

---

## [0.20.1] — 2026-05-21 · fixed · 前端 Docker pnpm 版本固定

### Fixed
- 固定前端 Docker 构建使用 `pnpm@10.23.0`，并在 `frontend/package.json` 声明 `packageManager`，避免 Corepack 自动拉取 pnpm 11 后忽略 `pnpm.onlyBuiltDependencies` 导致 `esbuild` 构建脚本被拦截。

### Verification
- `pnpm --dir frontend install --frozen-lockfile`
- `pnpm --dir frontend exec tsc -b --pretty false`
- `pnpm --dir frontend build`
- `git diff --check`

---

## [0.20.0] — 2026-05-21 · added · 交互 Bot 模块化与转账测试增强

### Changed
- 将 24 点交互 Bot 路径统一迁移到模块 `on_interaction` 入口，移除账号 Bot runtime 内的专用开局与答题调度。
- 更新模块开发文档，只保留当前实际支持的交互 Bot 标准动作，并补充 24 点端到端示例与排障清单。
- 优化交互 Bot 规则默认文案，去掉 24 点专属占位，统一使用通用互动模块语境。
- 交互 Bot 中奖公告中的人工发奖提示改为显示实际账号持有者，例如 `@username`。
- 交互 Bot 付费模块关键词提示改为展示实际规则名、收款人和参与金额。

### Added
- 新增测试用转账结果通知 Bot 消息模板配置，支持在测试环境自定义模拟转账通知格式。
- 测试用转账通知模板新增付款人用户 ID 占位符，交互模块可拿到真实付款人用于按用户会话继续流程。

### Fixed
- 修复账号详情页长 TabList 在 PWA 窄屏模式下被自身宽度裁切，导致页签无法完整横向滚动的问题。
- 修复交互 Bot 模块启动关键词与规则触发词使用包含匹配导致 `123456`、`aa123` 误触发 `123` 的问题。
- 修复交互 Bot 配置保存时可能用前端只读默认值覆盖已探测 `transfer_bot_id` 的问题；清空测试通知 Bot token 时也会同步清空旧 Bot ID。
- 修复仅配置收款人用户 ID 的转账规则会误命中缺少收款人 ID 的通知的问题；无 ID 时必须命中文本兜底。
- 修复转账通知模板变量未按 HTML 转义渲染的问题，避免付款人或收款人名称破坏 Telegram HTML 消息。
- 修复关闭按用户隔离的交互会话时只删除平台 session、未向模块发送 `session_close`，导致模块内部状态残留的问题。
- 修复 24 点交互 Bot 状态 TTL 长于平台会话有效期，过期状态仍阻塞新开局的问题。
- 修复 userbot 自动发奖监听会把任意交互模块中奖文案都纳入自动派奖范围的问题，现在只处理算数题与 24 点模块。
- 修复前端镜像构建使用 `npm` 而不是项目 `pnpm-lock.yaml` 的问题，Dockerfile 改为 `pnpm install --frozen-lockfile` 与 `pnpm build`。
- 修复交互 Bot 调用 worker 超时时可能静默丢消息的问题，超时会稳定返回“worker 调用超时”。
- 修复付费交互模块在按用户隔离时会话错误绑定到转账通知 Bot，而不是实际付款人的问题。
- 交互模块支持返回结束会话动作，彩票这类长期轮回模块可以只处理转账下注，不再误进入短会话流程。
- 未知交互动作类型现在会写入 runtime 日志，方便远程模块作者在日志页排查。
- 修复关闭某条交互规则时会误清理同群其他规则模块会话的问题。
- 修复回复 `+金额` 测试转账通知会绕过关闭状态或非 payment 触发规则的问题；真实转账通知也会跳过已关闭规则并继续匹配后续开启规则。
- 修复算数题与 24 点中奖公告中的动态用户字段未完全 HTML 转义的问题。
- 修复 worker 侧交互关键词拦截对空 `chat_ids` 的判断与 runtime 不一致的问题。
- 修复本地测试转账通知 probe 使用已转义文案，导致收款人含特殊字符时无法命中规则的问题。

### Verification
- `backend/.venv/bin/ruff check app`
- `backend/.venv/bin/python -m pytest`
- `pnpm --dir frontend exec tsc -b --pretty false`
- `pnpm --dir frontend build`
- `git diff --check`

---

## [0.19.2] — 2026-05-20 · fixed · 模块开发指南合并与 Bot 状态显示修复

### Changed
- 合并内置模块与远程模块开发指南，统一在 `docs/PLUGIN-DEV-GUIDE.md` 维护 Plugin API、远程安装、沙箱权限、模块分类与交互 Bot 声明规范。
- 将 `docs/REMOTE-PLUGIN-GUIDE.md` 改为兼容跳转页，避免旧链接失效，同时更新 README、前端开发指南说明与远程模块安装错误提示。
- 明确远程模块 `plugin.json` 与 `manifest.py` 需要同步声明 `category`，且只有互动娱乐型模块才声明 `interaction_entries`。

### Fixed
- 修复管理 Bot 已禁用或 token 已清空时仍显示旧 polling 冲突错误的问题。
- 补充回归测试，确保图片生成类内置模块保持工具能力分类，不会误作为交互 Bot 启动模块。

### Verification
- `backend/.venv/bin/ruff check backend/app/services/account_bot_service.py backend/app/services/remote_plugin_service.py backend/app/tests/test_account_bot.py backend/app/tests/test_plugin_security_regression.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_account_bot.py backend/app/tests/test_plugin_security_regression.py -q`
- `pnpm --dir frontend exec tsc --noEmit`
- `git diff --check`

---

## [0.19.1] — 2026-05-20 · fixed · 后端 CI lint 修复

### Fixed
- 修复 0.19.0 合并后后端 CI lint 中 `account_bot_runtime.py` 与 `account_bot_service.py` 的 import 排序问题。

### Verification
- `backend/.venv/bin/ruff check backend/app/services/account_bot_runtime.py backend/app/services/account_bot_service.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_account_bot.py -q`
- `pnpm exec tsc --noEmit`（frontend）
- `git diff --check`

---

## [0.19.0] — 2026-05-20 · added · 交互 Bot 与转账联动发布

### Added
- 新增独立交互 Bot 配置与运行入口，管理 Bot 保持原有远程管理职责，交互 Bot 专门负责群内高频互动。
- 新增转账联动规则列表，支持多监听群、多关键词、金额过滤、通知/十以内算数题动作、奖金与通知模板配置。
- 新增测试用转账结果通知 Bot，可在群内回复 `+数字` 时生成模拟“转账成功”通知，正式群也可只配置官方通知 Bot 用户 ID 作为信任来源。
- 新增临时算数题中奖后自动派奖链路：交互 Bot 公告赢家并引用赢家答案，userbot 识别公告后回复中奖消息发放奖金。
- 新增交互规则“启动模块”动作，命中转账规则后由交互 Bot 自己启动模块流程；首个样板接入为 24 点游戏。
- 新增模块 `category` 与 `interaction_entries` 声明，模块中心按“互动娱乐 / 自动化 / 工具能力”三类汉化分组，交互 Bot 只允许选择声明了交互入口的模块。
- 新增交互规则触发方式、模块启动关键词、付费门槛、金额匹配方式、开关指令、关闭提示、参与有效期和并发策略配置。

### Changed
- 账号 Bot 页面拆分为“管理 Bot”和“交互 Bot / 转账联动测试”两块配置，交互 Bot 可单独保存/清空身份配置，转账结果通知 Bot 作为可选测试配置。
- 交互配置保存会保留规则列表，顶层旧字段自动从规则汇总，兼容旧接口 `/bot/transfer-notice`。
- 交互 Bot 启停生命周期从管理 Bot runtime 中拆出，前端显示配置启用状态和实际 polling 运行状态。
- 交互 Bot 模块动作与 userbot 管理/插件命令解耦，userbot 只保留最后根据中奖公告自动回复发奖的职责。
- 24 点仅补充交互入口声明与交互 Bot 适配，不改动原 UserBot 模块交互逻辑。
- 插件开发指南补充交互 Bot 兼容规范，第三方模块需显式声明分类和交互入口后才可接入交互 Bot。

### Fixed
- 修复交互 Bot 实际可响应但前端可能显示为已停止/状态不清的问题。
- 修复交互配置未加载完成时保存可能用默认规则覆盖现有规则的问题。
- 修复 userbot 自动发奖的 `+奖金` 回复可能被交互 Bot 误识别为新一轮转账的问题。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests -q`
- `backend/.venv/bin/python -m py_compile backend/app/api/account_bots.py backend/app/main.py backend/app/schemas/account_bot.py backend/app/services/account_bot_runtime.py backend/app/services/interaction_bot_runtime.py backend/app/services/interaction_bot_service.py backend/app/worker/runtime.py`
- `pnpm exec tsc --noEmit`（frontend）
- `pnpm build`（frontend）
- `git diff --check`

---

## [0.18.13] — 2026-05-19 · fixed · 前端 Docker 构建修复

### Fixed
- 前端 Docker 构建阶段补充拷贝根目录 `CHANGELOG.md`，修复 `Sidebar.tsx` 通过 `?raw` 引用更新日志时在干净镜像构建中找不到文件的问题。
- 新增仓库根目录 `.dockerignore`，避免 `docker compose build frontend` 把本机 `frontend/node_modules`、缓存或运行数据带入构建上下文，导致干净 VPS 与本地环境表现不一致。
- 后端在 Docker 容器内会把旧配置残留的 `/plugins/installed` 自动纠正为 `/app/plugins/installed`，把 `/data/plugin_repos` 自动纠正为 `/app/data/plugin_repos`，避免远程模块落到非持久化根目录后丢失。

### Verification
- `npm run build`（frontend）
- `backend/.venv/bin/ruff check backend/app/settings.py backend/app/tests/test_settings_paths.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_settings_paths.py -q`
- `git diff --check`

---

## [0.18.12] — 2026-05-19 · fixed · 远程模块目录与全局配置保存修复

### Fixed
- 生产 compose 显式固定远程模块安装目录为 `/app/plugins/installed`，插件仓库缓存为 `/app/data/plugin_repos`，避免 Oracle / VPS 旧 `.env` 把容器写到 `/plugins/installed` 后重建丢失。
- 修复远程模块全局配置保存时原地修改 `Feature.manifest` 导致 SQLAlchemy 未识别 JSON 字段变更的问题；现在保存会替换 manifest dict，确保配置真实落库。

### Verification
- `backend/.venv/bin/ruff check backend/app/services/feature_service.py backend/app/tests/test_feature_service.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_feature_service.py -q`
- `git diff --check`

---

## [0.18.11] — 2026-05-19 · fixed · 运行日志与 24 点发奖排障修复

### Changed
- 24 点游戏发奖失败日志新增可读排障提示，会区分无发言权限、引用消息不可回复、慢速模式和 FloodWait 等常见原因。
- 24 点游戏发奖失败日志的 detail 追加 `exc_type`、`exc_repr` 和 `hint`，便于从运行日志里直接定位失败原因。

### Fixed
- 运行日志落库兼容 Python logging 数字级别，插件传 `10/20/30/40` 或字符串数字时会规范化为 `debug/info/warn/error`。
- 修复 CI 后端 lint 中 `.encode("utf-8")` 在 Python 3.12 / Ruff 规则下报错的问题。

### Verification
- `backend/.venv/bin/ruff check backend/app`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_supervisor_reliable_consumer.py backend/app/tests/test_commands.py::test_run_template_forward_to_delete_immediately_skips_success_edit -q`
- `git diff --check`

---

## [0.18.10] — 2026-05-19 · fixed · 原生生图调用链与超时体验修复

### Fixed
- 修复带 `config_schema` 的远程模块在模块中心不显示“配置”按钮的问题，通用配置页入口不再只依赖硬编码模块白名单。
- 修复 AI 原生生图绑定普通 OpenAI 主模型时误走 `/images/generations` 的问题；现在会自动切换到 Responses `image_generation` 工具，`gpt-image-*` / `dall-e-*` 模型仍保留 Images API 路径。
- 修复 LLM 调用失败时只显示“所有 provider 都失败”的泛化提示；现在会透出脱敏后的最后错误，方便区分接口、模型、代理和网络问题。
- 原生生图的超时控制拆分连接、写入、读取阶段：连接 / 代理 / 连接池快速失败，长等待只留给已连接后的生图响应读取。

### Verification
- `backend/.venv/bin/ruff check backend/app/services/llm_client.py backend/app/services/llm_invoke.py backend/app/worker/command.py backend/app/tests/test_llm_runtime.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_llm_runtime.py backend/app/tests/test_commands.py::test_responses_client_generate_image_uses_image_generation_tool backend/app/tests/test_commands.py::test_openai_client_generate_image_uses_images_api backend/app/tests/test_ai_runtime.py::test_ai_runtime_image_llm_uses_native_image_generation -q`
- `git diff --check`

## [0.18.9] — 2026-05-19 · changed · 指令与模块入口收口及本地导入

### Added
- 模块安装页新增“本地导入”能力：支持扫描 `plugins/local_imports/` 下符合开发规范的插件目录并一键导入，用于本地调试。
- 新增本地导入插件 API：可列出本地候选插件并执行导入安装，导入后自动触发热加载。
- 自定义指令模板列表新增“启用”快捷按钮：单账号环境下直接跳转到该账号的“自定义指令”页签；多账号环境下先选择目标账号再跳转。

### Changed
- 账号 Worker 弹层在仅有一个账号时改为窄宽度自适应展示，不再使用双账号宽度的固定大面板。
- 侧边栏版本号弹出的“更新日志”改为自动读取本地 `CHANGELOG.md`，不再维护手写静态版本摘要。
- 允许会话术语继续全站统一（账号页、向导页、配置包页等口径一致）。

### Removed
- 模块中心与主要导航中下线“指令别名”入口。
- 前端主路径中下线“消息转发（forward）”配置入口与相关展示，避免与自定义指令转发能力重复。

### Verification
- `pnpm --dir frontend exec tsc -b --noEmit`
- `backend/.venv/bin/python -m py_compile backend/app/services/plugin_repo_service.py backend/app/api/plugin_repo.py`

---

## [0.18.8] — 2026-05-19 · fixed · 允许群组白名单与 Bot 联动稳定性修复

### Changed
- 账号详情原“忽略群组”重做为“允许群组”白名单语义：名单为空时默认允许全部；名单非空时仅放行名单内会话触发 incoming 流程。
- 账号详情对应 Tab 与说明文案同步改为“允许群组 / 已允许会话”，避免黑白名单语义混淆。
- 自定义指令模板 `forward_to` 配置文案补充明确：当“触发后自动删除指令消息”为留空或 `0` 时，会保留并编辑原指令消息展示成功提示。

### Fixed
- 修复账号 Bot 联动页“远程模块高风险开关”在深色模式下文字不可读的问题，卡片与行项颜色已适配暗色主题。
- 修复 `forward_to` 指令在“成功后立即删除指令消息”场景下仍先编辑成功提示的多余风险；现改为直接删除，不再编辑。
- 为账号运行告警通知增加短时去重节流，避免插件配置错误反复触发时出现轰炸式重复通知。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_ignored_peers.py backend/app/tests/test_commands.py backend/app/tests/test_account_bot.py -q`

---

## [0.18.7] — 2026-05-18 · fixed · 远程模块启用与部署更新体验修复

### Added
- 检查更新现在会展示 `origin/main` 相对当前版本的变更文件列表，方便更新前判断影响范围。

### Changed
- 检查更新和拉取更新只检查 / 拉取 `origin/main`，不再 fallback 到 `master`。
- 一键部署脚本写入 `.env` 前会检测发布端口是否被占用；如果冲突会自动选择下一个可用端口，并把最终端口写入配置和部署完成提示。

### Fixed
- 远程模块在安装管理页点击“启用”时，会同步打开所有现有账号的账号级开关，避免全局开关已启用但 worker 实际仍未加载的半启用状态。
- 非 Git 工作树部署环境下检查更新 / 拉取更新会返回中文部署提示，不再暴露 `git root not found` 底层错误。

---

## [0.18.6] — 2026-05-18 · changed · 自动指令白名单入口优化

### Changed
- 模块中心的“自动指令白名单”入口会带上当前选中账号，减少进入页面后再次选择账号的步骤。
- 自动回复配置页新增“自动指令白名单”安全入口，方便配置“关键词命中后由平台内部触发指令”的场景。

### Verification
- `pnpm --dir frontend exec tsc -b --noEmit`
- `git diff --check`

---

## [0.18.5] — 2026-05-18 · fixed · 模块指令权限边界收紧

### Fixed
- 移除 `owner_only=False` 模块的 incoming 公开指令旁路，确保模块 `commands` 只能由当前账号 outgoing 指令或平台内部动作触发；普通成员仍可通过 `on_message` 参与答题、口令、领取等流程。

### Docs
- 模块开发指南补充“指令权限底线”，明确 `owner_only=False` 只开放消息监听，不开放普通成员指令执行。
- 模块开发指南补充配置项完整性要求，建议把帮助、撤销、取消、自动删除、冷却/超时和消息模板等用户常调行为配置化，并要求帮助模板支持 `{prefix}` 占位符。

### Verification
- `backend/.venv/bin/ruff check backend/app`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_plugin_loader.py backend/app/tests/test_auto_reply.py backend/app/tests/test_game24_plugin.py -q`
- `cd backend && .venv/bin/python -m pytest`
- `git diff --check`

---

## [0.18.4] — 2026-05-18 · fixed · CI 后端 lint 收口

### Fixed
- 整理 Codex 生图错误处理测试的 import 顺序，修复 GitHub Actions 后端 `ruff check app` 未通过的问题。

### Verification
- `backend/.venv/bin/ruff check backend/app`
- `backend/.venv/bin/python -m py_compile backend/app/tests/test_codex_image_errors.py`
- `git diff --check`

---

## [0.18.3] — 2026-05-18 · fixed · 生产环境远程模块库拉取

### Fixed
- 生产后端 Docker 镜像补充 `git` 与 `openssh-client`，修复 VPS 部署后无法克隆 / 刷新远程模块库内容的问题。
- 远程模块库执行 git 操作前新增运行环境检查；缺少 git 时返回明确的 `GIT_NOT_FOUND` 业务错误，不再在前端只显示“服务器内部错误”。

### Verification
- `backend/.venv/bin/ruff check backend/app/services/remote_plugin_service.py backend/app/tests/test_plugin_security_regression.py`
- `backend/.venv/bin/python -m py_compile backend/app/services/remote_plugin_service.py backend/app/tests/test_plugin_security_regression.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_plugin_security_regression.py -q`
- `git diff --check`

---

## [0.18.2] — 2026-05-18 · fixed · AI 原生生图后端接入

### Added
- AI 指令 `image + LLM Provider 原生生图` 接入真实调用链：Responses 协议会调用 `image_generation` 工具，OpenAI-compatible chat 协议会尝试 `/images/generations`。
- LLM fallback / retry / usage 统一调用层新增 `native_image` 分支，原生生图结果会复用现有图片发送逻辑，把 base64 data URI 或图片 URL 发回 Telegram。

### Changed
- 原生生图模式不再附加识图反幻觉提示词，避免把“只描述真实图像”的约束混入生图提示词。
- AI 模板编辑页移除“原生生图（预留）”文案，并提示 Responses / Images API 两种实际调用路径。

### Verification
- `backend/.venv/bin/python -m py_compile backend/app/services/llm_client.py backend/app/services/llm_runtime.py backend/app/services/llm_invoke.py backend/app/worker/ai_runtime.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_commands.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_ai_runtime.py backend/app/tests/test_commands.py -q` 通过，84 passed。
- `git diff --check`

---

## [0.18.1] — 2026-05-18 · fixed · Codex 生图恢复内置发布

### Fixed
- 将 Codex 图片生成从 `plugins/installed/codex_image/` 恢复为真正的内置实验模块，代码放回 `backend/app/worker/plugins/builtin/codex_image/`，确保生产 Docker 镜像和一键部署会随包带上该模块。
- 更新 codex_image dry-run、测试导入、builtin registry 断言和缺失模块提示，不再把它描述为 installed 下沉兼容模块。
- 清理 README、模块开发文档和模块中心失败提示中的旧 installed 口径，明确 Codex 图片生成现在由 builtin registry 自动 seed，旧账号无需迁移。

### Verification
- `backend/.venv/bin/python -m py_compile backend/app/worker/plugins/loader.py backend/app/api/rules.py backend/app/tests/test_codex_image_errors.py backend/app/tests/test_feature_registry.py backend/app/tests/test_plugin_loader.py backend/app/worker/plugins/builtin/codex_image/__init__.py backend/app/worker/plugins/builtin/codex_image/manifest.py backend/app/worker/plugins/builtin/codex_image/plugin.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_codex_image_errors.py backend/app/tests/test_feature_registry.py backend/app/tests/test_plugin_loader.py -q` 通过，32 passed。
- `git diff --check`

---

## [0.18.0] — 2026-05-18 · feature · 前端信息架构与资源占用面板重构

### Added
- 新增 TelePilot 机器人抛投纸飞机品牌图标，并同步网页 favicon、PWA 图标、iOS 主屏图标、侧边栏和登录页 Logo。
- 新增服务器开箱部署脚本 `scripts/install-server.sh`，支持 SSH 到 Debian / Ubuntu 服务器后一条命令安装依赖、拉取仓库、生成生产 `.env` 并启动 Docker 生产栈。
- 概览页新增“账号 Worker”抽屉式账号入口，小屏 / PWA 下使用轻量账号列表，降低完整账号卡片浮层带来的卡顿。
- 资源占用面板新增应用内存明细浮层，可查看 Web 主进程、账号 worker、派生子进程，以及 PostgreSQL / Redis / 前端容器的 CPU 与内存。
- 后端资源 API 新增 USS 独占内存、派生子进程发现、项目容器 Docker stats 采样和容器指标读取失败提示。
- 侧边栏版本号支持点击查看更新日志，侧边栏底部改为 GitHub 图标与 TelePilot 项目名入口。

### Changed
- 概览页重构为账号、AI、模块中心、日志与资源占用的轻量工作台，移除重复的快速开始、底部账号卡片、新手指引和当前摘要。
- 新手指引移动到顶部操作区，开启时保留跑马灯边框并直接展示大内容指引。
- 系统状态从概览正文移入顶部按钮浮层，资源占用卡片下半部分明确改为服务器 / 宿主机 CPU、内存和磁盘指标。
- PWA standalone 模式下顶部左侧改为 Logo + TelePilot，不再显示侧边栏按钮；底栏改成贴底轻量标签栏，并使用黑色反色选中态。
- 顶部系统状态、检查更新、主题、紧急停用和用户按钮在小屏 / PWA 下统一为正圆图标按钮，桌面端保留带文字胶囊按钮。
- 紧急停用确认从浏览器级 `confirm` 改为应用内部确认弹窗。
- AI 入口、模块中心和系统设置页的命名进一步收口：AI 保持独立入口，“命令”文案统一向“指令”靠拢，系统页改为“用户与管理 / 前缀与通知 / 备份与恢复”等更清晰分组。
- 模块开发文档将“模式 C / Schema 弹窗”口径收敛为通用独立配置页 / Schema 驱动独立页兼容说明，明确新增模块推荐使用 `rules`、`single`、`platform`，`schema` 仅作为旧别名或普通单配置页字段来源。
- 模块开发文档新增统一配置页样式规范，固化顶部冻结“配置操作”、独立“使用说明 / 功能总开关 / 配置”卡片、配置宽度自适应和用户界面“模块”命名要求。
- README、模块开发指南、远程模块指南和公网部署文档同步到 0.18 线，移除过期验证结果、手动 venv/pnpm 公网部署路径和用户侧“插件 / 命令”旧口径。
- 模块独立配置页的保存操作改为顶部冻结工具条，长配置表单滚动时也能直接保存或撤销。
- 账号详情的模块启停页不再按模式 A/B/C 分类，统一展示为平台能力和模块列表，并按模块 key 首字母排序；模块配置页顶部统一使用可操作的“功能总开关”，规则驱动页说明改为和单配置页一致的“使用说明”样式。
- 单配置和通用配置页统一调整为“使用说明 → 功能总开关 → 配置”的独立卡片顺序，配置区域移除窄宽限制并按屏幕宽度自适应排布；用户界面里的调用入口文案进一步从“插件”收口为“模块”。

### Fixed
- 修复 macOS fallback 误把 `vm_stat` 累计计数当作物理页总数，导致整机内存显示数百 TB 的问题。
- 修复 PWA 底栏点击后先显示灰色、触摸页面其它区域或滚动后才刷新为选中态的问题：底栏改为按钮驱动路由，并在触摸开始时同步更新选中路径。
- 修复 PWA 下“账号 Worker”和“应用总内存”等浮层不居中、打开动画造成卡顿和错位的问题。
- 修复 PNG Logo 外层仍使用旧 SVG 黑底容器，导致桌面侧边栏 Logo 出现大黑边的问题。
- 修复 ChatGPT2API 图片 caption 未按 HTML 渲染的问题；模板占位符会先转义，HTML 解析失败时自动去标签后重发纯文本。

---

## [0.17.1] — 2026-05-18 · fixed · PWA 导航与 ChatGPT2API 配置体验

### Added
- ChatGPT2API 配置页新增消息模板编辑、占位符快捷插入和 Telegram HTML 预览，生成图片 caption 可自定义状态、提示词、模型、画幅、格式、耗时、参考图和代理等信息。
- ChatGPT2API manifest 新增默认消息模板，并标记为实验性插件，配置页同步展示实验性说明和更完整的命令示例。

### Changed
- 明确版本号只在发布、推送稳定检查点、创建 release/PR，或用户要求“推一版/发一版”时统一迭代；开发过程中的微小提交先累积到 `Unreleased`。
- PWA 底栏、通用选项卡、桌面侧边栏和导航选中态统一为更柔和的 Liquid Glass 视觉，弱化硬边割裂感并增强背景折射和选中态层次。
- PWA standalone 模式下左上角不再显示侧边栏按钮，改为展示 TelePilot 项目名，导航入口交给底栏承载；普通移动浏览器仍保留侧边栏按钮。
- ChatGPT 图片助手统一命名为实验性 `ChatGPT2API`，并补充 `--size`、回复图片编辑和管理命令说明。

### Fixed
- 修复 PWA 模式下移动侧边栏只出现遮罩、不显示抽屉的问题：关闭态遮罩不再拦截点击，并让抽屉位移由专用 data-state 样式控制。

### Verification
- `backend/.venv/bin/ruff check backend/app/worker/plugins/builtin/chatgpt_image backend/app/tests/test_chatgpt_image_plugin.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_chatgpt_image_plugin.py -q`
- `pnpm --dir frontend build`
- `git diff --check`

---

## [0.17.0] — 2026-05-18 · feature · ChatGPT2API 实验性插件

### Added
- 新增实验性内置插件 `chatgpt_image`，显示名为 `ChatGPT2API`，按 chatgpt2api 的核心逻辑在 TelePilot 插件内完成 ChatGPT 图片生成、回复图片编辑、最近图片续改、模型检测、额度刷新、代理测试和插件状态查看。
- 新增 ChatGPT2API 专属配置页，支持自定义文生图、编辑和管理命令，配置模型列表、默认模型、生成张数、画幅、输出方式、风格模板、轮询超时与参考图数量。
- Token 池改为逐条管理：每条 token 可填写备注，已保存 token 按首尾各 10 字符脱敏显示，并支持粘贴 `chatgpt.com/api/auth/session` 完整 JSON 自动提取 `accessToken`。
- 将 CPA、sub2api、健康检测与自动禁用失效 token 配置收进默认折叠的高级容器。
- 配置页新增支持的命令格式示例、输出消息模板、占位符快捷插入和 Telegram 消息预览。

### Changed
- ChatGPT2API 不提供 OpenAI 兼容 `/v1/*` HTTP 服务，也不内置 Docker、号池或外部服务；它只作为 Telegram 侧原生插件调用 ChatGPT Web 图片链路。
- ChatGPT2API 的网络出口改为直接跟随当前 Telegram 账号代理，不再提供插件内独立代理配置，避免启动阶段重复读取代理表。
- 健康检测失败不再配置额外异常通知聊天 ID；用户触发的失败会直接用中文更新原聊天消息，详细错误写入插件日志。
- 插件配置接口对 `chatgpt_image` token 池做专用脱敏与回填，编辑备注或保存其他配置时不会覆盖真实 token。
- PWA 底栏升级为五栏浮动玻璃风格，补齐 AI 与系统入口，并统一移动端安全区留白。
- 模块中心 AI 入口、AI 命令中心和账号详情页选项卡统一使用通用 TabList 视觉，移动端/PWA 下保持居中或安全横向滚动。
- 新手指引入口按钮统一使用通用 Button 尺寸与圆角，账号页指引浮层在移动端改为内联布局以避免 PWA 下错位。
- PWA 名称与图标统一为 TelePilot，替换旧的 Userbot 名称和旧占位图标。

### Verification
- `backend/.venv/bin/ruff check backend/app/worker/plugins/builtin/chatgpt_image backend/app/tests/test_chatgpt_image_plugin.py`
- `backend/.venv/bin/python -m pytest backend/app/tests/test_chatgpt_image_plugin.py -q`
- `pnpm --dir frontend build`
- `git diff --check`

---

## [0.16.10] — 2026-05-18 · fixed · AI 模板占位符与 Codex 生图稳定性

### Added
- AI 自定义命令模板新增协议占位符：`{api_format}` / `{api_protocol}` 展示本次实际调用协议，`{configured_api_format}`、`{web_search_api_format}`、`{endpoint}` 和 `{web_search}` 展示配置协议、联网协议、接口路径与联网状态。
- AI 自定义命令模板新增 `{command}`、`{mode}`、`{sources}` 等常用占位符，并在编辑器中按内容、上下文、模型、协议、统计和运行分组展示。
- 新增命令防误触配置 `COMMAND_ECHO_GUARD_PREVIOUS_MESSAGES`，默认检查前 8 条群聊消息，设为 0 可关闭；前端模板配置页支持开关和 1~50 条可调窗口。

### Changed
- 已配置的 API Key、Access Token、Bot Token 和代理密码等敏感输入框改用黑点占位展示，留空保存仍表示保留现有密钥。
- Codex 图片生成插件的默认主模型从 `gpt-5.4` 对齐为 `gpt-5.5`。
- 通用插件配置弹窗支持 `x-ui-widget: textarea`，方便插件声明多行配置项。
- 系统设置中的运行日志等级保存后会即时影响新日志落库，日志页面按系统时区显示时间。

### Fixed
- Codex 图片生成在流式连接刚开始中断且尚未拿到 `response_id` 时，会自动重试一次；如果后续无法恢复，会返回更准确的中文错误说明。
- Codex 图片生成的安全审核拦截、参考图下载、`response.created` 流式事件和旧模型配置兼容处理更稳定。
- 修复后端 CI 中 `_run_git` timeout 测试依赖 `/tmp` 目录状态导致在 GitHub Runner 上误判失败的问题。
- 群聊中自己发送纯命令时，如果最近 N 条消息内已有他人发送完全相同的文本，会静默跳过，降低参与抽奖或接龙时误触命令的风险。

### Verification
- `backend/.venv/bin/ruff check backend/app/services/llm_format.py backend/app/services/llm_invoke.py backend/app/worker/ai_runtime.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_llm_format.py` 通过。
- `backend/.venv/bin/pytest backend/app/tests/test_ai_runtime.py backend/app/tests/test_llm_format.py` 通过。
- `pnpm --dir frontend build` 通过。
- `git diff --check` 通过。

---

## [0.16.9] — 2026-05-17 · changed · 插件模板配置分组

### Added
- 插件通用配置弹窗会按字段名自动归类：普通配置保留在顶部，`*_template` 模板进入“消息模板”折叠组，`*_preview` 预览进入底部“预览结果”。
- 模板预览现在会根据当前表单里的模板内容和示例变量实时渲染，而不是只显示 schema 中的静态默认预览。

### Changed
- 每个模板预览都使用独立的 `TelegramHtmlPreview` 气泡展示，方便同时检查开局、进行中、成功、超时、取消和错误提示等多条消息。
- 版本号口径校正为 `0.16.9`，避免误标为 `0.17.0`。

### Fixed
- 修复后端 CI 的 ruff import 排序与测试写法问题。
- 修复完整 pytest 在 `backend/` 目录运行时无法导入仓库根目录 `plugins.installed.codex_image` 的问题。
- 修复旧测试替身或自定义 `build_client` 不接受 `api_format_override` 时导致 inline provider override 测试失败的问题。

### Verification
- `pnpm --dir frontend build` 通过。
- `cd backend && .venv/bin/ruff check app` 通过。
- `cd backend && .venv/bin/python -m pytest -q` 通过。
- `git diff --check` 通过。

---

## [0.16.8] — 2026-05-17 · changed · Telegram 消息预览

### Changed
- 自定义命令模板里的消息格式预览改为 Telegram 风格聊天预览：浅色聊天背景、示例用户消息、TelePilot 蓝色气泡和时间状态，效果更接近真实消息。
- 通用 `TelegramHtmlPreview` 组件升级为消息气泡预览，插件配置里的模板预览也会使用同一套视觉效果。
- 开发文档新增 Telegram 消息预览规范，说明模板预览应展示替换占位符后的最终消息效果，并优先复用 `TelegramHtmlPreview`。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_llm_runtime.py -q` 通过。
- `git diff --check` 通过。

---

## [0.16.7] — 2026-05-17 · changed · AI 模式化默认参数

### Changed
- AI 命令模板的模型参数默认值改为跟随模式：chat 使用 `temperature=0.7`、`reasoning_effort=medium`、`timeout_seconds=60`；search 使用 `0.2 / medium / 90`；image 使用 `0.8 / low / 180`；video 预留使用 `0.8 / low / 300`。
- 切换 AI 模式时，如果当前参数仍是旧模式默认值或为空，会自动带入新模式默认值；用户手动改过的值不会被覆盖。
- 模型参数说明改为展示当前模式默认值，避免生图、视频等长耗时任务沿用普通 chat 的短超时。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_llm_runtime.py -q` 通过。
- `git diff --check` 通过。

---

## [0.16.6] — 2026-05-17 · changed · AI 参数默认值

### Changed
- AI 命令模板的模型参数现在提供默认值：温度 `0.7`、推理强度 `medium`、超时时间 `60` 秒。
- 模型参数说明同步汉化默认值含义，减少新增 AI 命令时需要手动补齐基础参数的步骤。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_llm_runtime.py -q` 通过。
- `git diff --check` 通过。

---

## [0.16.5] — 2026-05-17 · feature · AI 模型参数配置

### Added
- AI 命令模板新增“模型参数”折叠区，支持配置温度（temperature）、推理强度（reasoning_effort）和单次调用超时时间。
- 模型参数均提供中文说明：温度解释稳定/创作取向，推理强度解释思考预算，超时时间解释长推理与本地桥接场景。
- 后端统一 LLM 调用链支持透传 temperature、reasoning_effort 和 timeout_seconds；OpenAI Chat/Responses 支持推理强度，Anthropic 先透传 temperature。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_llm_runtime.py -q` 通过。
- `git diff --check` 通过。

---

## [0.16.4] — 2026-05-17 · feature · Provider 协议检测

### Added
- 模型提供商新增“检测协议”能力，可在保存前探测 `/models`、`/chat/completions`、`/responses` 和 Anthropic `/messages` 是否可用。
- 协议检测会根据探测结果自动填入推荐的默认 API Format 与联网搜索 API Format，降低新建 Provider 时选错协议的概率。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_llm_runtime.py -q` 通过。
- `git diff --check` 通过。

---

## [0.16.3] — 2026-05-17 · feature · Provider 联网搜索协议覆盖

### Added
- 模型提供商新增“联网搜索 API Format”配置，支持默认聊天协议与联网搜索协议分开设置。
- OpenAI 兼容 Provider 默认启用自动协议覆盖：日常调用可走 `chat_completions`，联网搜索调用会临时切到 `responses`。
- 新增数据库迁移 `0024`，为 `llm_provider` 增加 `web_search_api_format` 字段。

### Changed
- Provider 列表和编辑弹窗展示联网搜索协议，降低 `。ai search` 配置成本。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py backend/app/tests/test_ai_runtime.py backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_llm_runtime.py -q` 通过。
- `git diff --check` 通过。

---

## [0.16.2] — 2026-05-17 · fix · AI 搜索提示与命令样式

### Changed
- 带命令前缀的说明统一使用命令胶囊样式，避免和普通正文混在一起。
- AI 模式说明补充 search 模式需要 OpenAI Responses API provider（api_format=responses）。

### Fixed
- `search` 模式绑定到不支持联网搜索的 provider 时，Telegram 端错误提示会明确指出需要 Responses API provider。

### Verification
- `pnpm --dir frontend build` 通过。
- `git diff --check` 通过。

---

## [0.16.1] — 2026-05-17 · fix · AI 导航图标统一

### Changed
- 侧边栏 AI 入口改用更轻量的闪光图标，和 AI 命令中心的入口气质保持一致。
- AI 命令中心切换器补齐“总览”图标，并把“已配置的命令”保留在同一组入口中。
- 日志中心主切换器与运行日志来源切换器补齐图标，减少只有文字的页签。

### Verification
- `pnpm --dir frontend build` 通过。
- `git diff --check` 通过。

---

## [0.16.0] — 2026-05-17 · feature · AI 模块信息架构收敛

### Added
- 自定义命令模板页支持 `edit`、`new=ai`、`provider_id`、`aiCapability`、`returnTo` 深链，用于直接打开 AI 模板抽屉、预填 Provider、展开指定能力组并保存后返回。
- 插件中心新增“图片生成 (codex_image)”显眼入口，并支持 `highlight=codex_image` 滚动高亮。
- AI 总览新增工作原理、配置示例和术语速查折叠说明，替代原独立帮助页。
- AI 总览新增账号启用 AI 命令摘要，显示已有多少账号启用了至少一条 AI 命令。
- AI 总览“去启用”支持按账号数量分流：单账号直接进入命令 Tab，多账号先选择账号。
- AI 消息格式新增 `{model_id}` 原始模型 ID，占位符 `{model}` 改为输出更友好的模型展示名。
- AI 命令模板新增 `chat/search/image/video` 模式字段，开始对齐 TeleBox `ai.ts` 的二级指令结构。

### Changed
- `/ai` 收敛为总览、模型提供商、调用记录三种 Tab 状态，支持 `tab=overview|providers|usage` URL 驱动并在切换时保留 query。
- `/ai/providers?new=1` 兼容跳转为 `/ai?tab=providers&newProvider=1`，进入模型提供商 Tab 后自动打开新建 Provider 弹窗并清理一次性 query。
- `/ai/chat`、`/ai/routing`、`/ai/search`、`/ai/output`、`/ai/vision`、`/ai/images` 改为精确深链到命令模板、Provider 筛选或插件中心，不再保留独立 AI 子页。
- AI 总览改为三张状态卡、三步引导和 AI 命令快览，最近调用摘要统一使用最近 20 次口径。
- AI 总览帮助区的“推荐配置”改名为“配置示例”。
- 命令模板编辑器中的 AI 路由、联网搜索和回复样式从平铺字段改为默认折叠的能力组，非默认配置会自动展开。
- `search` 模式会自动启用联网搜索；`image` 模式可桥接到账号已启用的 `codex_image` 插件，也支持直接创建 `image` 命令模板。
- 模型提供商列表支持 `filter=modality:vision` 过滤视觉/多模态 Provider。
- 最近调用内容迁移为 `/ai` 内部组件，供调用记录 Tab 复用。
- 后端标准 LLM 调用薄包装从 `services.ai_runtime` 重命名为 `services.llm_invoke`，避免与 worker 侧 Telegram 事件运行时混淆。

### Removed
- 删除 AI 模块历史 wrapper 和独立子页：`Providers`、`Help`、`Usage`、`Chat`、`Routing`、`Search`、`Vision`、`Images`、`Output` 与 `_shared`。
- 删除孤儿帮助页 `AISettings.tsx`，其内容拆入 AI 总览的内联折叠组件。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_ai_runtime.py backend/app/tests/test_scheduler_runtime.py -q` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py -q` 通过。
- `git diff --check` 通过。

---

## [0.15.23] — 2026-05-17 · feature · AI 能力页可操作化

### Added
- AI 模块新增聊天问答、能力路由、联网搜索、视觉理解、图片生成、输出模板等独立能力页入口。
- 输出模板页支持选择 AI 命令后直接编辑解析模式、消息模板、占位符转义和预览，并保存到对应命令模板。
- 联网搜索页支持直接为 AI 命令启用/关闭 `web_search`，并调整搜索上下文强度。
- 能力路由页支持直接切换固定模型/自动路由，并配置自动路由失败时的兜底 Provider。

### Changed
- 自定义命令编辑器的 AI 消息格式编辑区抽取为通用输出模板组件，供命令模板页与 AI 输出模板页复用。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.15.22] — 2026-05-17 · fix · AI 入口可见性

### Added
- 左侧主导航新增“AI”入口，直达 AI 模块总览页。
- 模块中心的“AI 模块入口”区域新增“AI 总览”按钮，避免只能进入模型提供商、用量或帮助页。

### Verification
- `pnpm --dir frontend build` 通过。
- `git diff --check` 通过。

---

## [0.15.21] — 2026-05-17 · feature · AI 模块能力化

### Added
- AI 模块首页重构为能力控制台，集中展示模型提供商、AI 命令模板、联网搜索、视觉理解、能力路由和输出模板入口。
- 自定义 AI 命令模板新增“联网搜索”开关与搜索上下文强度配置，支持在输出模板里使用 `{sources}` 显示搜索来源。
- OpenAI Responses 调用链支持 `web_search` 工具，并从 Responses 返回体中提取来源链接。

### Changed
- 标准 LLM runtime 增加联网搜索参数透传，worker 渲染 AI 回复时会把搜索来源注入模板上下文。

### Fixed
- 修复 AI 模块首页首次加载后因 React hooks 调用顺序变化导致 `/ai` 页面打不开的问题。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_commands.py -q` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.20] — 2026-05-17 · fix · 自定义命令启用入口

### Fixed
- 账号详情新增“自定义命令”页签，可按当前账号启用或停用全局命令模板，开启后 worker 热加载生效。
- 账号概览中的“复用命令模板”入口改为直达账号级启用页，模板库文案同步为“账号详情 → 自定义命令页签”。

### Verification
- `pnpm --dir frontend build` 通过。
- `git diff --check` 通过。

---

## [0.15.19] — 2026-05-17 · fix · Codex 生图错误处理

### Fixed
- Codex 生图插件对齐原始 TeleBox 插件的轮询容错：普通轮询异常会继续等待，鉴权/HTML 登录页/防护页会给出明确提示，不再把整段网页源码显示成 `HTTP 0`。
- Codex 生图插件支持从 Responses 完成事件里递归提取最终 `image_generation_call.result` 图片，减少只依赖轮询造成的失败。
- 生图失败提示去掉重复的 `❌ ❌` 前缀，并补充 Token 失效、ChatGPT 防护页、后端网络/代理不可达等可操作排查提示。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_codex_image_errors.py` 通过。
- `backend/.venv/bin/python -m py_compile plugins/installed/codex_image/plugin.py` 通过。
- `backend/.venv/bin/ruff check plugins/installed/codex_image/plugin.py backend/app/tests/test_codex_image_errors.py` 通过。
- `pnpm --dir frontend build` 通过。
- `git diff --check` 通过。

---

## [0.15.18] — 2026-05-17 · fix · 命令示例跟随前缀

### Fixed
- TG 内置命令、游戏插件、图片生成插件、账号 Bot 与前端配置页中的命令示例改为读取当前命令前缀，不再固定显示逗号前缀。
- 九宫格骰子竞猜模板新增 `{prefix}` 占位符，进行中提示和奖励参数错误提示可随系统前缀热加载变化。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_auto_reply.py backend/app/tests/test_worker_command.py backend/app/tests/test_sudo.py backend/app/tests/test_scheduler_runtime.py` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_account_bot.py` 通过。
- `pnpm --dir frontend build` 通过。
- `git diff --check` 通过。

---

## [0.15.17] — 2026-05-17 · fix · 自动回复命令直接派发

### Fixed
- 自动回复生成白名单内命令文本时，会直接进入 worker 命令派发器执行，不再依赖插件发送出的消息再次回流为 outgoing 事件。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_auto_reply.py backend/app/tests/test_worker_command.py backend/app/tests/test_scheduler_runtime.py` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.16] — 2026-05-17 · fix · 自动命令白名单实时热加载

### Fixed
- worker 收到 `reload_config` 后会同步刷新命令上下文，自动命令白名单保存后立即生效，不再等周期性 reconcile。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_scheduler_runtime.py backend/app/tests/test_feature_registry.py` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.15] — 2026-05-17 · changed · 自动命令白名单独立入口

### Changed
- 自动命令白名单从定时任务规则页拆出，模块中心新增独立入口，按账号配置自动动作允许触发的命令。
- 模块中心“平台能力”说明改为系统级基础模块的集中入口，不再暗示无需手动配置。

### Fixed
- 本地已存在于 `plugins/installed` 的模块会同步登记到 feature 表；`codex_image` 等随项目落盘的实验模块可在模块中心出现并按账号启用配置。

### Verification
- `pnpm --dir frontend build` 通过。
- `backend/.venv/bin/python -m pytest backend/app/tests/test_feature_registry.py` 通过。

---

## [0.15.14] — 2026-05-17 · fix · 远程模块配置入口

### Fixed
- 模块中心的“远程模块”卡片支持显示“配置”按钮；没有专用配置页的远程模块会打开通用配置弹窗，并保存账号级模块配置。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.15.13] — 2026-05-17 · fix · 代理 URL 自动归一化

### Fixed
- 新增或编辑代理时支持直接粘贴完整代理 URL（如 `http://10.10.8.33:6152`、`socks5://127.0.0.1:6153`），后端会自动拆分类型、主机、端口与认证信息，避免把完整 URL 当作主机名导致测试连接失败。
- 账号登录、账号退出与 LLM 代理解析兼容历史上已保存为完整 URL 的代理主机字段。
- 代理管理页的主机输入提示同步为支持完整 URL 粘贴。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_proxy_url_normalization.py backend/app/tests/test_accounts.py` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.12] — 2026-05-17 · feature · 代理配置可编辑

### Added
- 代理与标识页的已添加代理支持行内编辑，可修改类型、主机、端口、用户名和密码，并支持清空已保存密码。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_accounts.py` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.11] — 2026-05-17 · fix · 代理入口不可达提示

### Fixed
- 代理连通性测试先探测代理入口 TCP 可达性；当后端连不到代理地址时，返回“代理入口不可达”与 Docker/局域网访问提示，不再只显示底层 python-socks 错误。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_accounts.py` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.10] — 2026-05-17 · fix · 新增账号网络错误提示

### Fixed
- 新增账号发起登录时，后端连接 Telegram 失败会返回结构化 `LOGIN_START_FAILED` 错误，不再冒泡成“服务器内部错误”。
- 新增账号向导的代理提示同步到“系统设置 → 代理与标识”。

### Verification
- `backend/.venv/bin/python -m pytest backend/app/tests/test_accounts.py` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.9] — 2026-05-16 · feature · 首页承载账号管理

### Changed
- 抽离账号管理复用面板，统一承载账号列表高频操作：新增账号、新手指引、启停、详情、删除与空状态。
- 首页 Dashboard 直接接入账号管理面板，首页可完成核心账号管理流程，不再仅展示状态卡。
- `/accounts` 路由改为复用同一面板实现，保持原有交互与路径兼容。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.15.8] — 2026-05-16 · feature · 模块中心与 AI 入口文案收口

### Changed
- `/plugins` 与 `/plugins/manage` 前端展示文案从“插件”统一收口为“模块”（仅 UI 文案，后端/API/plugin 命名保持不变）。
- 模块中心首页新增 AI 入口卡，提供“模型提供商 / AI 用量 / AI 帮助”直达入口，并明确 AI 能力属于模块配置流程。
- 保留现有 `/ai/*` 页面实现与路径兼容，未调整 Settings/Dashboard/Sidebar/App 路由。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.15.7] — 2026-05-16 · fix · 恢复代理与标识配置入口

### Fixed
- 系统设置新增“代理与标识”页签，恢复代理库与设备标识模板入口，避免后端已实现的代理配置在前端不可达。
- 风控模板移入“安全”页签并默认折叠，避免系统设置首屏过长。
- 健康概览的代理库为空提示改为可点击入口，直接跳到系统设置“代理与标识”页。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.15.6] — 2026-05-16 · fix · 修复新手指引按钮文字可见性

### Fixed
- 账号管理页“新增账号”和系统设置平台页“保存”在新手指引高亮时改为浅底描边强调样式，避免流光高亮下文字变白不可见。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.15.5] — 2026-05-16 · fix · 修复 CI 插件导入路径

### Fixed
- 为 backend CI job 设置 `PYTHONPATH=..`，确保从 `backend/` 工作目录运行 pytest 时可导入仓库根目录的第三方插件包。

### Verification
- `PYTHONPATH=.. MASTER_KEY=... JWT_SECRET=... ../backend/.venv/bin/python -m pytest app/tests/test_codex_image_errors.py` 通过。

---

## [0.15.4] — 2026-05-16 · fix · 修复 CI 测试环境变量

### Fixed
- 为 backend CI job 注入测试专用 `MASTER_KEY` 与 `JWT_SECRET`，避免远端 pytest 在 settings collection 阶段因缺少必填配置失败。

### Verification
- `backend/.venv/bin/ruff check backend/app` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.15.3] — 2026-05-16 · fix · 修复 CI 后端 lint

### Fixed
- 修复 `account_bot` 数据模型 import 排序，使新增 backend CI gate 在远端环境通过。

### Verification
- `backend/.venv/bin/ruff check backend/app/db/models/account_bot.py` 通过。

---

## [0.15.2] — 2026-05-16 · fix · 完成审查后续收口

### Fixed
- Config Bundle dry-run/confirm 增加预览签名绑定，目标账号配置快照、可用 feature/template、文件或冲突选项变化后必须重新预览。
- Config Bundle chat/peer/group 相关嵌套字段冲突现在会触发二次确认；缺失 feature/template 等不可恢复冲突会被 blocked。
- Config Bundle confirm 写库成功后会通知目标账号 worker reload 配置、命令和忽略列表。
- feature/rule/audit/runtime log 等路径统一敏感字段脱敏且避免误伤 `max_tokens` 等非敏感计数字段，Codex Image token 改为 write-only 展示。
- 健康检查纳入 worker runtime 存活与失败计数，异常态刷新更快并支持手动刷新；Audit 日志筛选改为后端过滤。
- 修正 account_bot 测试接口前端类型漂移，`testAccountBot` 现在返回后端实际响应。
- README、安全运维、远程插件、插件开发和部署文档同步当前安全边界与 TelePilot 命名。

### Added
- 新增 Config Bundle 签名、blocked conflict、worker reload 回归测试。
- 新增敏感字段脱敏、日志查询筛选、健康检查 worker runtime 聚合测试。

### Verification
- `backend/.venv/bin/ruff check backend/app/api/config_bundle.py backend/app/api/features.py backend/app/api/logs.py backend/app/api/rules.py backend/app/api/system_health.py backend/app/schemas/config_bundle.py backend/app/services/audit.py backend/app/services/config_bundle_service.py backend/app/services/redactor.py backend/app/worker/supervisor.py backend/app/tests/test_config_bundle.py backend/app/tests/test_logs_api.py backend/app/tests/test_redaction_security.py backend/app/tests/test_supervisor_reliable_consumer.py backend/app/tests/test_system_health.py` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telepilot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_config_bundle.py backend/app/tests/test_redaction_security.py backend/app/tests/test_supervisor_reliable_consumer.py backend/app/tests/test_system_health.py backend/app/tests/test_logs_api.py` 通过（46 passed）。
- `PYTHONPYCACHEPREFIX=/private/tmp/telepilot_pycache backend/.venv/bin/python -m pytest backend` 通过（590 passed, 2 skipped）。
- `pnpm --dir frontend build` 通过。

---

## [0.15.1] — 2026-05-16 · fix · 收紧远程插件与自动命令安全边界

### Fixed
- 远程插件与插件仓库 API 现在要求 Web 登录态，避免未登录请求触发安装、启停、更新、卸载等高风险操作。
- 第三方插件命令 handler 现在接收沙箱 client，不再绕过 manifest 权限拿到原始 Telethon client。
- account_bot 的 Telegram 远程插件 install/update/uninstall/第三方启停改为 admin-only，并新增 Web 端细粒度高风险开关；默认全部关闭，执行前仍需二次确认。
- scheduler 与自动回复触发命令时新增账号级白名单控制；非白名单命令会被拦截并记录，不再默认复用 Telegram 命令分发。

### Added
- 新增 account_bot 远程插件高风险策略迁移与前端配置入口。
- 新增主 CI workflow，覆盖后端 ruff/pytest、前端 build 与版本号同步检查。
- 新增远程插件鉴权、插件沙箱命令 client、account_bot 远程插件策略、自动命令白名单相关回归测试。

### Verification
- `backend/.venv/bin/ruff check backend/app/api/account_bots.py backend/app/api/plugin_repo.py backend/app/api/remote_plugin.py backend/app/schemas/account_bot.py backend/app/services/account_bot_runtime.py backend/app/services/account_bot_service.py backend/app/worker/command.py backend/app/worker/plugins/loader.py backend/app/worker/scheduler_runtime.py backend/app/worker/runtime.py backend/app/worker/plugins/builtin/auto_reply/plugin.py backend/app/tests/test_account_bot.py backend/app/tests/test_plugin_security_regression.py backend/app/tests/test_remote_plugin_repo_auth.py backend/app/tests/test_worker_command.py` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telepilot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_account_bot.py backend/app/tests/test_plugin_security_regression.py backend/app/tests/test_remote_plugin_repo_auth.py backend/app/tests/test_worker_command.py backend/app/tests/test_scheduler_runtime.py` 通过（64 passed）。
- `PYTHONPYCACHEPREFIX=/private/tmp/telepilot_pycache backend/.venv/bin/python -m pytest backend` 通过（576 passed, 2 skipped）。
- `pnpm --dir frontend build` 通过。

---

## [0.15.0] — 2026-05-16 · feature · TelePilot rename 收口

### Changed
- 项目品牌、Web/PWA 标题、前后端包名、启动通知、账号 Bot/NotifyBot 文案和 `,version` 输出统一为 TelePilot。
- README、部署文档、插件开发指南和远程插件指南同步 0.13/0.14 已完成能力，并明确 0.15 后的新命名与兼容边界。
- worker PID 目录迁移到 `~/.telepilot/worker-pids`，启动清理仍会扫描旧 `~/.telebot/worker-pids`，避免升级后残留旧 worker。
- 本地启动/停止脚本改为基于当前仓库 `backend` 路径识别孤儿 worker，同时兼容旧 `telebot/backend` 路径。

### Added
- 插件 manifest 新增 `min_telepilot_version` 字段；旧 `min_telebot_version` 继续作为 legacy alias 解析，避免远程插件生态硬断。
- 远程插件 `plugin.json` 元数据支持 `min_telepilot_version`，并保留旧字段写入兼容。
- 前端主题存储切换为 `telepilot-theme`，首次读取会兼容迁移旧 `telebot-theme`。

### Compatibility
- 后端 CSRF 过渡期同时接受 `X-Requested-With: telepilot-ui` 与旧 `telebot-ui`，避免旧前端缓存或脚本升级后直接 403。
- Docker volume、数据库默认账号/库名、`TELEBOT_WORKER_PROC` 等底层兼容名暂不强制迁移，避免升级后历史数据“消失”或 worker 连接池语义变化。

### Verification
- `git diff --check` 通过。
- `backend/.venv/bin/ruff check backend/app/main.py backend/app/worker/plugins/manifest.py backend/app/worker/plugins/loader.py backend/app/worker/supervisor.py backend/app/services/remote_plugin_service.py backend/app/tests/test_csrf_header.py backend/app/tests/test_plugin_loader.py` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telepilot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_csrf_header.py backend/app/tests/test_plugin_loader.py` 通过（17 passed）。
- `PYTHONPYCACHEPREFIX=/private/tmp/telepilot_pycache backend/.venv/bin/python -m pytest backend` 通过（567 passed, 2 skipped）。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。
- `bash -n scripts/up.sh scripts/down.sh scripts/prod-up.sh deploy/backup.sh deploy/restore.sh deploy/backup-keys.sh` 通过。
- `docker compose -f docker-compose.yml config` 与 `docker compose -f docker-compose.dev.yml config` 均可渲染（dev compose 仅提示 `version` 字段已过时）。

---

## [0.14.17] — 2026-05-16 · fix · 支持原账号重新登录覆盖 session

### Fixed
- 新增“重新登录并保留配置”入口：账号处于 `login_required` 时可直接从账号详情发起重登，成功后覆盖原账号 session，不再需要删除账号或手动迁移忽略群组。
- 登录向导支持 `relogin` 模式：锁定原手机号、复用原账号 ID，重登成功后返回当前账号详情页。
- 后端重登会同时用当前 `MASTER_KEY` 重新加密保存 session、API ID、API Hash，并恢复账号为 active，解决只换 session 但 API 凭据仍旧密钥加密的问题。
- 后端增加手机号与 Telegram 用户 ID 校验，避免把其他 Telegram 账号误覆盖到当前账号配置上。

### Verification
- `git diff --check` 通过。
- `backend/.venv/bin/ruff check backend/app/api/accounts.py backend/app/services/login_service.py backend/app/schemas/account.py backend/app/tests/test_accounts.py` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_accounts.py backend/app/tests/test_config_bundle.py` 通过。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.14.16] — 2026-05-16 · fix · Config Bundle 支持忽略列表迁移

### Fixed
- 账号级 Config Bundle 现在会导出、dry-run 和 confirm 写入忽略列表，支持把旧账号几十个忽略群组迁移到重新登录后的新账号。
- Config Bundle dry-run 结果增加“忽略列表”实体展示，避免只迁移插件规则和命令绑定时遗漏账号级忽略名单。
- 设置页 Config Bundle 文案补充“忽略列表”，明确它适用于账号配置迁移，不包含 session / API key / Bot Token 等敏感登录凭据。

### Verification
- `git diff --check` 通过。
- `backend/.venv/bin/ruff check backend/app/api/config_bundle.py backend/app/services/config_bundle_service.py backend/app/schemas/config_bundle.py backend/app/tests/test_config_bundle.py` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_config_bundle.py` 通过（11 passed）。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。
- 本地读取 `aid=1` Config Bundle：`features=7`、`rules=2`、`command_links=5`、`ignored_peers=56`、`size_bytes=8275`。
- 本地全量备份 round-trip：导出包含 `ignored_peers=56`；同文件导入结果 `imported=0`、`skipped=86`、`warnings=0`。

---

## [0.14.15] — 2026-05-16 · polish · 优化命令前缀触发预览

### Changed
- 设置页命令前缀预览改为更接近 Telegram 的左右对话气泡：被回复原文在左侧，用户触发命令与 AI 回复在右侧。
- 预览气泡缩小宽度、增加内边距和蓝色回复样式，让手机端观感更真实。
- AI 回复内的引用块、分割线和模型标识改为同色系样式，避免预览区域显得像普通表单文本。

### Verification
- `git diff --check` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/__init__.py` 通过。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.14.14] — 2026-05-16 · fix · 明确旧密钥导致的账号与 Bot 失效

### Fixed
- 账号恢复前会先验证 session / api_id / api_hash 是否能用当前 `MASTER_KEY` 解密；失败时直接置为 `login_required` 并提示恢复原 `MASTER_KEY` 或重新登录账号，不再启动后反复 down。
- worker 启动阶段遇到账号凭据解密失败时，会写入清晰运行日志并停止自动重启该账号。
- 账号 Bot 启用时会校验已保存 Bot Token 是否还能解密；旧 `MASTER_KEY` 加密的 token 会提示“重新保存 Bot Token”，不再只显示泛泛的 422。
- 前端错误解析支持 FastAPI 校验数组，减少 `Request failed with status code 422` 这类无效提示。

### Verification
- `git diff --check` 通过。
- `backend/.venv/bin/ruff check backend/app` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/services/account_service.py backend/app/services/account_bot_service.py backend/app/worker/runtime.py backend/app/worker/supervisor.py backend/app/__init__.py` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_account_service.py backend/app/tests/test_account_bot.py backend/app/tests/test_kill_switch_supervisor.py` 通过（21 passed）。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.14.13] — 2026-05-16 · fix · 修复账号启停与紧急停用

### Fixed
- 账号暂停现在会通过 supervisor 真正停止对应 worker，不再只写 DB 状态或仅发送 worker 自己可能收不到的 pause 消息。
- 账号恢复现在会通过 supervisor 直接拉起对应 worker；kill switch 开启期间不会误拉起 worker。
- 全局紧急停用现在会停止当前所有 worker，并阻止 active 账号在总闸开启期间被自动拉起；解除后自动恢复 active 账号 worker。
- 更新紧急停用相关前端文案，从“暂停”改为“停止”，与实际控制语义一致。

### Verification
- `git diff --check` 通过。
- `backend/.venv/bin/ruff check backend/app` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/services/account_service.py backend/app/worker/supervisor.py backend/app/api/rate_limit.py backend/app/__init__.py` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_account_service.py backend/app/tests/test_kill_switch_supervisor.py backend/app/tests/test_system_health.py` 通过（25 passed，1 个 Alembic 配置弃用警告）。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.14.12] — 2026-05-16 · fix · 修复命令前缀预览

### Fixed
- 修复设置页命令前缀触发预览的 JSX 语法错误，恢复三段式 Telegram 对话预览。
- 预览中补齐“被回复原文 → 用户触发命令 → AI 回复结果”的顺序，并在回复标题中展示当前命令前缀。

### Verification
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.14.11] — 2026-05-16 · polish · 资源占用与新手引导细化

### Changed
- 概览页资源占用改为突出展示 TeleBot 本项目合计 CPU / 内存，并补充主进程、worker、整机资源对照。
- 命令前缀触发预览限制为略大于手机屏幕的宽度，让 Telegram 对话示例更接近真实手机观感。
- 设置页“猜你想要？”里的“绑定机器人”改为点击后再选择账号；只有一个账号时直接进入该账号 Bot 联动页。
- 新手指引小条统一补充“点击展开详情”，并修复第一步“新增账号”和第二步“保存”按钮文字可见性。
- 插件中心“账号视角”改为“选择配置的账号”，并移入“账号插件启用详情与配置”容器。

### Verification
- `git diff --check` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/__init__.py` 通过。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.14.10] — 2026-05-16 · patch · 完成 B2 下沉与文档同步

### Changed
- 新手指引的 Siri 流光高亮改为细外框跑马灯，不再把整个按钮或卡片填成彩色，避免遮挡按钮文字。
- `codex_image` 从 builtin 物理下沉到 `plugins/installed/codex_image/`，builtin registry 不再自动包含它；旧账号已启用时会按 installed 兼容模式加载，缺少本地代码时进入 failed 并写 runtime log。
- `codex_image` dry-run import 改为 installed 路径，并补齐 installed 插件元数据与 `send_file` 权限声明。
- 前端真实移动 Settings / Features 旧实现文件到 Plugins / AI 目录，减少“新入口壳页面继续引用旧目录”的双维护状态。
- README 与插件开发文档同步 0.14 当前状态，明确 TelePilot 完整 rename 仍属于后续 0.15。

### Verification
- `git diff --check` 通过。
- `backend/.venv/bin/ruff check backend/app` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/worker/plugins/loader.py backend/app/api/rules.py backend/app/tests/test_codex_image_errors.py backend/app/tests/test_feature_registry.py plugins/installed/codex_image/__init__.py plugins/installed/codex_image/manifest.py plugins/installed/codex_image/plugin.py` 通过。
- `pnpm --dir frontend exec tsc -b --noEmit` 通过。
- `pnpm --dir frontend build` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m pytest backend/app/tests/test_codex_image_errors.py backend/app/tests/test_plugin_loader.py backend/app/tests/test_feature_registry.py backend/app/tests/test_feature_service.py` 通过，38 passed。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m pytest backend` 通过，551 passed, 2 skipped。

---

## [0.14.9] — 2026-05-16 · polish · 新手指引开关与流光高亮

### Changed
- 账号页“新手指引”恢复为带文字的大按钮，点击后先询问是否开始指引模式，不再默认强制开启。
- 第一阶段的小条提醒恢复到“新增账号”按钮附近，并改为浮层定位，展开后不再把账号卡片向下挤压。
- 新手指引通过 `guide=1` 显式进入指引模式；未开启时不显示步骤提示，也不高亮按钮。
- 高亮效果从普通呼吸动画改成类似 Siri 的七彩流光边框，应用于当前步骤的目标按钮和插件状态区域。

### Verification
- `git diff --check` 通过。
- `pnpm --dir frontend build` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/__init__.py` 通过。

---

## [0.14.8] — 2026-05-16 · polish · 插件中心第三步指引

### Changed
- 新手指引不再首次进入账号页时强制弹窗，改为原“新手指引”按钮位置的小星星开关，需要时再展开。
- 插件中心第三步同时高亮“命令模板”“插件启用状态网格”“安装插件”三处，并补充 A/B/C 三点说明。
- 新手指引最后一步按钮从“跳过这步”改为“我学会了！”，更符合已到最后一步的语义。
- 插件安装页的新手指引完成按钮同样改为“我学会了！”。

### Verification
- `git diff --check` 通过。
- `pnpm --dir frontend build` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/__init__.py` 通过。

---

## [0.14.7] — 2026-05-16 · polish · 设置快捷入口与新手指引贴边

### Changed
- 系统设置顶部“已搬家”改为“猜你想要？”，提供“添加模型 / 添加命令 / 绑定机器人”三个直接入口；绑定机器人支持先选择账号再进入对应账号的 Bot 联动页。
- 命令前缀触发预览收窄 Telegram 对话气泡宽度，回复引用块改为内容自适应，避免像整页通知卡片。
- 新手指引从左下角悬浮改为贴近当前操作按钮：账号页靠近“新增账号”，设置页靠近“保存前缀”，插件页靠近“安装插件”，并为目标按钮增加呼吸提示。
- 新手指引补充“跳过这步”，可以直接进入下一步所在页面；最后一步可结束指引。
- 插件中心首页文案继续收敛为“沉淀模板 → 按账号复用 → 新账号免重配”的表达，并明确远程插件安装后回插件中心按账号启用和配置。

### Verification
- `git diff --check` 通过。
- `pnpm --dir frontend build` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/__init__.py` 通过。

---

## [0.14.6] — 2026-05-16 · polish · 新手指引与 AI 用量增强

### Added
- 系统设置的“命令前缀”下新增 Telegram 左右气泡预览，展示“回复原文 → 发出命令 → AI 返回结果”的真实触发感。
- 新手指引升级为三步：添加并启用账号、设置命令前缀、启用命令模板或调用插件，并在账号 / 设置 / 插件页面提供左下角呼吸悬浮入口。
- AI 最近调用增加摘要卡片，展示请求数、成功数、失败数、Fallback 次数、总 Token 与平均耗时。

### Changed
- 插件安装页移除重复的“按账号启用”Tab，安装页只负责安装 / 更新 / 卸载，账号级启用和配置统一回插件中心首页。
- 插件安装页补充“返回上一页”，深链进入时可回到插件中心。
- AI 最近调用表格改为中文表头，并补充 fallback / 错误类型信息。

### Verification
- `git diff --check` 通过。
- `pnpm --dir frontend build` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/api/llm_usage.py backend/app/main.py` 通过。

---

## [0.14.5] — 2026-05-16 · fix · 恢复远程插件安装入口

### Added
- Plugins 中心新增“安装插件”入口，统一进入 `/plugins/manage?tab=plugins` 管理 Git 仓库、远程插件安装、更新、启用、禁用和卸载。
- 新增 `/api/llm/usage/recent`，AI 中心“最近调用”可直接读取 `llm_usage` 表，不再停留在接口预留空态。

### Changed
- 0.13 安全迁移提示里的远程插件运维入口改为“前往插件安装”，补齐 Telegram 高危插件命令移除后的 Web 替代路径。
- 插件安装与管理页默认停留在“安装与更新”Tab，并用更明确的中文说明远程插件安装后还需要按账号启用。
- AI Usage 表格补充来源、模型提供商名称、耗时和 fallback 状态，便于验证命令 / scheduler 的 LLM 调用。

### Verification
- `git diff --check` 通过。
- `pnpm --dir frontend build` 通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/telebot_pycache backend/.venv/bin/python -m py_compile backend/app/api/llm_usage.py backend/app/main.py` 通过。

---

## [0.14.4] — 2026-05-16 · polish · 两步新账号指引

### Changed
- 新手指引简化为“添加并启用账号 / 启用命令模板或调用插件”两大步，一次只聚焦当前要做的事。
- 新手指引第二步补充命令前缀入口，明确先去插件中心复用模板或开启插件，再到系统设置确认 TG 内命令前缀。
- 账号详情“新账号下一步”三张卡片标题放大加粗，让新账号配置入口更醒目。

### Verification
- `git diff --check` 通过。
- `pnpm --dir frontend build` 通过。

---

## [0.14.3] — 2026-05-16 · polish · 插件中心文案与逐步新手引导

### Changed
- 插件中心顶部说明改为更自然的大白话：强调“把常用回复、转发、AI 命令整理成模板，再按账号启用复用”。
- 插件中心三入口（命令模板 / 命令别名 / 定时任务）改为更稳健的自适应卡片布局，优化小屏与中等宽度下的长文显示，避免文案挤压。
- 账号详情“新账号下一步”的启用插件数量挪到卡片右上角，让三张卡片按钮位置更统一。
- 新手指引改为逐步式流程：一次聚焦一步，支持上一步 / 下一步 / 去执行当前步骤，不再一次性展示全部四步。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.14.2] — 2026-05-16 · polish · 新账号引导与 AI 中心再收敛

### Changed
- AI 中心将“模型提供商”和“最近调用”整合为同页 Tab，AI 帮助缩小为顶部按钮入口，减少首页卡片重复感。
- 插件中心统一“定时任务”命名，替换原“调度中心”文案；入口描述改成“模板可跨账号复用”的大白话说明。
- 账号详情“快捷入口”改为“新账号下一步”，明确它用于新账号复用模板、开启插件、复制成熟配置。
- 账号管理页新增“新手指引”按钮，并首次进入自动弹出 4 步引导动画，可随时从顶部按钮再次打开。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.14.1] — 2026-05-16 · fix · 前端 IA 追补与资源展示修正

### Changed
- 调整 AI 中心首页文案：将“路由策略”卡片改为“AI 帮助”，说明聚焦“工作原理 / 术语速查 / 推荐配置”，避免与“模型提供商”入口语义重叠。
- 调整 `/ai/help` 页面定位与标题文案为“AI 帮助”，移除重复的“模型提供商列表”Tab，仅保留“工作原理”“术语速查”“推荐配置”三个 Tab。
- Dashboard 资源占用从“主进程 CPU / 主进程内存”改为“项目 CPU / 项目内存”，统计口径改为 FastAPI 主进程 + 全部账号 worker 子进程合计。
- 账号详情概览将“能力与迁移入口”改为“快捷入口”，用大白话说明它用于跳转到插件、命令模板和配置复制。
- 账号详情页移除过渡性质的“命令”Tab，命令管理统一从 `/plugins/templates?account=:aid` 进入。

### Verification
- `backend/.venv/bin/python -m pytest -q backend/app/tests/test_system_health.py` 通过：17 passed。
- `pnpm --dir frontend build` 通过。

---

## [0.14.0] — 2026-05-16 · feature · 前端 IA 中心化

### Changed
- 前端顶层导航从 8 个入口收敛为 6 个用户目标导向入口：概览、账号、插件、AI、日志、系统；调度与模板不再占据顶层菜单，为后续 Plugins 中心化做路由落点。
- 新增 Plugins 中心页，将平台能力、内置插件、远程插件、实验性能力分区展示；`codex_image` 固定进入实验性区，让 0.13 的 experimental 标识在前端更显眼。
- 将命令模板、别名、调度能力接入 `/plugins/templates`、`/plugins/aliases`、`/plugins/scheduler`，并保留旧入口重定向，避免旧书签失效。
- 新增 AI 顶级入口 `/ai`，拆出 Provider、Routing、Usage 三个子入口；Usage 页以最小表格展示最近调用，并在后端接口尚未开放时提供温和空状态。
- Settings 瘦身为账号、平台、安全、迁移四组，隐藏 LLM Providers、命令模板、别名管理旧入口，并提供“已搬家”跳转提示。
- 账号详情概览增加能力与迁移入口卡片：启用插件数、启用命令数、复制配置；旧命令 Tab 降级为迁移提示页。
- Config Bundle 支持从 `/settings?tab=backup&source=:aid` 预填源账号；Plugins 支持 `/plugins?account=:aid` 按账号视角打开。
- Logs 页新增 Runtime / Audit 顶层 Tab，Audit 接入 `/api/logs/audit` 并支持动态 action 过滤，便于查看 sudo、Config Bundle confirm、account_bot confirm 等安全决策记录。
- 命令模板新增 AI provider 空态引导：没有 Provider 时禁用 AI 类型创建并跳转到 `/ai/providers`；已删除 Provider 的 AI 模板会显示“provider 缺失”警告。
- Plugins 首页增加一次性安全迁移 banner，提示 0.13 已移除 `,reboot`、`,plugin install` 等 Telegram 内高危命令；account_bot 配置页补充危险操作二次确认提示。
- 移动端新增 4 项底部 Tab Bar（概览、账号、插件、日志），AI 与系统设置保留在汉堡菜单；账号详情 Tabs 在窄屏改为横向滚动，避免挤压重叠。
- 收敛专属插件配置页入口：`FEATURE_CONFIG_PAGE_KEYS` 改由 `frontend/src/pages/Plugins/_shared/featureConfig.ts` 统一维护，减少账号详情与旧插件中心双份维护。
- B5 清理 legacy feature key：移除已无引用的 `FEATURE_LEGACY_KEYS` 导出，并补充 registry 测试确保 `group_admin` / `monitor` 不会回到内置功能列表。
- B2/F9 先落地 `codex_image` 下沉兼容检测：暂不做物理迁移；若旧账号启用但运行节点缺实现，worker 会记录 runtime_log、标记 failed，Plugins 页显示恢复提示。
- 移除旧前端 URL 兼容重定向：`/scheduler`、`/templates`、`/settings/commands`、`/settings/aliases`、`/settings/llm-providers`、`/ai-settings`、`/matrix`、`/extensions`、`/remote-plugins` 不再作为入口保留，统一从 `/plugins/*`、`/ai/*`、`/settings` 进入。
- 补齐新入口的中文文案与返回体验：AI / Plugins 子页增加“返回上一页”，从菜单点击进入时返回点击前页面，直接打开深链时回到对应中心页。
- README 与插件开发指南同步 0.14 IA、共享 feature config helper、Audit 日志和 `codex_image` 兼容策略说明。

### Verification
- `pnpm --dir frontend build` 通过。

---

## [0.13.1] — 2026-05-16 · fix · 本地启动与 0.13 可见性修复

### Fixed
- 修复本地 `make up` 首次启动时 `.env.example` 默认 `localhost:5432/6379` 与 `docker-compose.dev.yml` 暴露端口 `15432/16379` 不一致导致 Alembic 连接失败的问题；开发启动脚本现在只在当前进程环境中映射端口，不回写 `.env`，也不影响生产部署。
- 修复 `scripts/_lib.sh::auto_tune_env` 在已有 `MEMORY_TIER` 时因 shell 变量边界解析触发 `unbound variable` 的问题。
- 修复 `ai_runtime.invoke()` 收口后测试与旧调用约定无法 monkeypatch 默认 LLM client 的问题，恢复 AI inline provider override、图片/贴纸、语音转写路径的本地测试稳定性。
- 修复内置 feature 旧行更新 manifest 时未可靠写回 `x-experimental` 的问题，确保 `codex_image` 的“实验性”标识能从后端正确下发到插件列表和账号 feature 列表。
- 恢复 `app.db.models.feature.BUILTIN_FEATURES` 兼容导出，避免旧调用点导入失败。

### Changed
- 将 Config Bundle 从“系统设置 → 全局控制”底部移到独立的“备份恢复”Tab，降低入口隐藏感。
- 为 Config Bundle 补充大白话说明，明确它用于把 A 账号的规则、插件配置和自定义命令绑定复制到 B 账号；上传配置包只做预览，只有点击“确认写入目标账号”才会真正改数据。

### Verification
- `ruff check backend/app` 通过。
- `pytest backend` 通过：548 passed, 2 skipped。
- `pnpm --dir frontend build` 通过。

---

## [0.13.0] — 2026-05-15 · refactor · TelePilot 安全 / 架构 / Config Bundle 主线收口

### Changed
- PR1 安全收敛：删除高危 Telegram 命令入口，不再支持通过普通 Telegram 命令触发项目级 `,reboot/,rb`、远程插件运维 `,plugin install/remove/enable/disable/update`、以及 `,sudo add/,sudo del`；`sudo ls` 保留为只读查询。
- PR2 命令层拆分起步：已将 sudo 权限闸门与命令网关辅助逻辑抽离到 `backend/app/worker/commands/sudo_guard.py`，对外行为不变；dispatcher / builtin / template / long_message 等其余分层未在本版本落地。
- PR3 引入 `ai_runtime.invoke()` 统一标准 AI 调用入口，减少多路径调用漂移。
- PR4 补齐 sudo 关键路径 audit 记录，保证拒绝与关键操作可审计。
- PR5 收敛内置 feature 注册表：将动态扫描与惰性缓存逻辑抽离到 `backend/app/feature_registry.py`，`BUILTIN_FEATURES` 既有访问方式保持不变。
- PR6 将内置 `codex_image` 标记为 experimental（`x-experimental: true`），前端扩展列表与配置页增加实验性提示，不改变运行逻辑。
- PR7/PR8 Config Bundle 收口：支持 export/import 1MB 限制、dry-run 与 confirm 显式确认流程，并记录确认审计。
- PR9 补充 PluginContext 契约文档，明确字段能力边界与推荐访问模式。
- PR10 收口 account_bot confirm：token 改为 hashed + single-use，并修正 owner/action mismatch 时序。
- PR11 文档线收口：README 与 TelePilot 架构文档明确当前仓库与运行形态仍为 TeleBot，未承诺 rename/workflow/artifact/template renderer/marketplace。

---

## [0.12.1] — 2026-05-14 · refactor · Feature 配置页 CRUD 骨架抽取

### Changed
- 把 AutoReply / Autorepeat / Forward / Scheduler 四个 feature 配置页中重复的 rule CRUD 骨架（features+rules 查询、save/delete/toggle/dry-run mutation、规则编辑 Dialog 外壳、Dry-run Dialog 外壳）抽到 `frontend/src/pages/Features/_shared/` 共享模块。
- 新增 `useRuleCrud` hook 与 `RulePageHeader` / `RuleFeatureToggleCard` / `RuleInfoBox` / `Field` / `RuleEditDialogShell` / `DryRunDialogShell` 共享组件。
- 三个简单页面按字符数瘦身 27–35%：AutoReply -29%、Autorepeat -35%、Forward -27%；Scheduler 因深层嵌套 `setForm` 被 prettier 风格展开行数表观增加，CRUD/Dialog 逻辑已迁移到 hook，等效精简。
- 后续新增 feature 配置页时复用共享骨架即可，无需再复制 200+ 行 CRUD/Dialog 模板。

### Verification
- `tsc -b --noEmit` 通过。
- 浏览器手测：自动回复 / 自动复读 / 转发 / 调度四个页面的 CRUD、dry-run、立即执行行为均与重构前等价；不改任何对外行为。

---

## [0.12.0] — 2026-05-13 · feature · 小 VPS 资源预算与失败隔离

### Added
- 生产 `docker-compose.yml` 为 web / postgres / redis / frontend 增加明确的 `mem_limit` / `mem_reservation`，用于小 VPS 场景的资源预算与失败隔离。
- 新增 `scripts/_lib.sh::auto_tune_env`，在 `.env` 未显式设置 `MEMORY_TIER` 时按 Docker 可用内存写入 tiny / small / large 档位。
- worker 子进程新增独立 DB / Redis 连接池默认值，并通过 `app.worker.entry.worker_entry` 在导入 runtime 前标记 `TELEBOT_WORKER_PROC=1`。
- 新增 `log_incoming_messages_default` 与系统设置热加载开关，默认关闭逐条 incoming message 可见性日志。

### Changed
- PostgreSQL / Redis 默认资源参数收紧，降低小部署的峰值占用；web 增加 `init: true` 与 uvicorn `--limit-concurrency 64 --backlog 64`。
- worker 配置 reconcile 默认间隔从 60s 调整为 180s，并在插件加载完成后主动 GC 一次。
- Dashboard 资源面板和健康状态轮询降频，所有轮询查询在后台标签页停止 refetch。
- 前端非首屏路由改为 lazy load，Vite 将 markdown / radix 拆分为独立 chunk，降低首屏 JS 压力。
- `system_health` 进程采样缓存 psutil `Process` 实例，移除每次读取时的固定 `sleep(0.05)`；runtime_log 5 分钟计数增加短 TTL memo。

### Fixed
- 修复本地 OrbStack / Docker Desktop 场景下内存档位误读 Mac 宿主机 RAM 的问题：`detect_memory_tier` 现在优先读取 `docker info .MemTotal`。
- 撤回 `pip --no-compile` / 删除 `__pycache__` 的镜像瘦身尝试，避免冷启动现场编译依赖导致 web RSS 明显升高。

### Verification
- 本地 OrbStack 约 1GiB / 1CPU 环境：PR fixed 总峰值约 `152.0 MiB`，低于 main fresh 的 `178.3 MiB`；平均总占用略高，定位为资源预算 / 峰值削平 / 失败隔离收益，而非平均 RSS 显著下降。
- VPS forced-tiny smoke：web 约 `210-215 MiB / 320 MiB`，postgres / redis / frontend 均在各自限制内。
- VPS 10 分钟轻负载验证：所有容器保持 healthy，`OOMKilled=false`，`RestartCount=0`。
- 后端 / 前端验证沿用优化 PR 验证：`ruff check backend/app`、`pytest backend`、`tsc -b --noEmit`、`vite build`。

---

## [0.11.4] — 2026-05-12 · fix · 远程插件模板预览

### Fixed
- 远程插件 `ConfigDialog` 中的 `template_preview` / `*_preview` 不再渲染为可编辑输入框，改为使用 `TelegramHtmlPreview` 展示只读 HTML 预览卡片。
- `readOnly: true`、`template_preview`、`*_preview`、`template_placeholders` 字段统一只读展示，并在保存配置时过滤掉，避免把预览和说明写回运行配置。
- `message_template` / `*_template` 保持可编辑多行输入，继续兼容现有远程插件 schema。

### Documentation
- 插件开发指南和远程插件指南补充自动弹窗对 `readOnly`、`template_placeholders`、`template_preview` / `*_preview` 的渲染规则。

### Verification
- 前端构建通过：`pnpm --dir frontend build`。

---

## [0.11.3] — 2026-05-12 · docs · 插件文案模板规范

### Documentation
- 修正插件消息模板预览规范：基础表单风格可参考 LLM / 自定义命令配置页，但消息模板预览应对齐“通用模板 → 自定义命令模板”的输出模板预览。
- 明确所有会发送、编辑或回复给 Telegram 用户看的插件文案都必须模板化，包括开局、进行中、答对、超时、取消、错误提示、媒体 caption 和重复触发提示。
- 明确用户可见文案不应硬编码在 `plugin.py` 中；代码里只保留模板默认值、不可恢复兜底错误、内部日志和开发者调试信息。
- 版本线说明：`1.1.x` 曾是误写；由于 `0.10.2` 后的 Sudo / 远程插件链路修复属于重要稳定性修复，版本线按 `0.11.0 -> 0.11.1 -> 0.11.2 -> 0.11.3` 延续。

### Verification
- 文档和版本号同步检查通过。

---

## [0.11.2] — 2026-05-12 · polish · 远程插件配置弹窗与文档

### Changed
- 远程插件 `ConfigDialog` 对齐自定义命令 / LLM 配置弹窗的宽度、滚动高度、字段间距和统一表单控件风格。
- 消息模板、提示词、正文、长文本等 schema 字段在自动配置弹窗中使用多行文本输入，减少模板编辑时的拥挤感。
- 运行期远程插件安装目录加入 `.gitignore`，避免本地安装/更新插件产生未跟踪文件；保留仓库内已有的 `translate` 示例。

### Documentation
- 插件开发指南补充 Schema 驱动弹窗的 UI 约定，要求自动配置弹窗对齐 LLM 配置页体验。
- 远程插件指南补充模板字段的占位符说明、示例值和只读预览建议。

### Verification
- 前端构建通过：`pnpm --dir frontend build`。

---

## [0.11.1] — 2026-05-12 · fix · 远程插件启用与热更新修复

### Added
- 插件开发指南新增“发布与交互体验检查清单”，覆盖版本同步、消息复用、状态管理、防滥用、公平性、资源清理和降级策略。
- 远程插件指南补充版本一致性、热更新确认、模板占位符、冷却、清理延迟和结束命令等发布前检查项。
- 新增远程插件启用流程测试，覆盖首次全局启用自动补账号级启用行，以及已有账号选择不被覆盖。

### Changed
- 远程插件首次在“插件管理”点“启用”时，如果还没有任何账号级配置，会自动为现有账号创建启用行，减少“已启用但不能触发”的误解。
- 远程插件 install / enable / update / uninstall 的 worker reload 通知改为事务提交后执行，确保 worker 热加载时读到已提交的版本、开关和账号配置。
- 远程插件从仓库安装时，`default_enabled=true` 会同步打开远程插件全局开关，并把账号级状态设为等待 worker 激活的 disabled，而不是提前标 active。
- 插件管理页远程插件按钮顺序调整为“更新 / 启用或禁用 / 卸载 / 按账号管理”，并为启用、禁用、卸载补齐图标。

### Fixed
- 修复 `owner_only=False` 的远程插件命令无法被群内 incoming 消息直接触发的问题，例如群成员发送 `。cy 100` 能进入公开插件命令分发。
- 修复远程插件更新后偶发仍运行旧版本的问题：installed 插件 reload 会清理模块缓存、注册表旧类和 `__pycache__`。
- 修复配置默认值合并前后不一致导致热重载误判命令配置变化的问题。
- 修复远程插件禁用、更新、卸载后 reload 时机过早，worker 可能读到旧数据库状态的问题。

### Verification
- 后端定向测试通过：`cd backend && .venv/bin/pytest app/tests/test_plugin_loader.py app/tests/test_plugin_security_regression.py -q`。
- 后端静态检查通过：`cd backend && .venv/bin/ruff check app/services/remote_plugin_service.py app/services/plugin_repo_service.py app/api/remote_plugin.py app/api/plugin_repo.py app/worker/commands/plugin_cmd.py app/worker/plugins/loader.py app/tests/test_plugin_loader.py app/tests/test_plugin_security_regression.py`。
- 前端类型检查通过：`cd frontend && pnpm -s exec tsc --noEmit`。

---

## [0.11.0] — 2026-05-12 · hotfix · Sudo 安全收紧 + 远程插件文档规范

### Added
- 远程插件指南补充配置弹窗数据来源说明：
  - `ConfigDialog` 不直接读取磁盘上的 `plugin.json` / `manifest.py`；
  - 前端实际读取 `/api/accounts/{aid}/features` 返回的 `features[].config_schema`；
  - 该字段来自数据库 `Feature.manifest.config_schema`；
  - 修改远程插件 `config_schema` 后，需要执行“更新插件”并确认后端完成元数据回写。
- 插件开发指南和远程插件指南补充抢答/答题类插件奖励交互规范：
  - 奖励金额优先通过触发命令传入，例如 `,game 100`；
  - 开局时把本轮奖励写入局状态，避免局中配置变化导致奖励不一致；
  - 答对后建议两步反馈：回复答对者纯文本 `+金额`，再编辑原题目消息追加结算信息；
  - 图片题面插件必须声明 `send_file` 权限；
  - 图片生成应避免隐式依赖未声明系统库，第三方依赖需要在文档中明确安装约束；
  - 配置页只放稳定配置项，如 `command`、`timeout`、`auto_next`。

### Changed
- 调整游戏插件开发规范中的推荐配置字段：`reward` 不再作为抢答类插件的首选固定配置项，改为“可选默认值/兜底值”，单局动态参数优先从命令参数读取。
- 更新可复制游戏插件骨架：示例改为从命令参数读取奖励金额，并在开局时冻结到 `RoundState.reward`。

### Fixed
- 修复群聊中发送 `..`、`...` 等多个小数点会被误判为 sudo 命令，并公开回复“Sudo 权限拒绝”的问题。
- incoming sudo 现在只对真实可派发的命令做权限检查；纯标点、重复前缀和未知文本会静默忽略，避免普通聊天内容误触发。
- Sudo 新增全局总开关，默认关闭；开启后也只允许在账号自身 chat（收藏夹语义）触发，群组和普通私聊里的 sudo 前缀都会静默忽略。
- 全局命令前缀也增加重复前缀静默过滤：例如前缀为 `。` 时，发送 `。。。` 不再提示“未知命令”。

---

## [0.10.2] — 2026-05-11 · fix · 远程插件契约收紧 + 开发文档发布

### Added
- 远程插件文档明确升级为“远程安装规范 + 主插件开发指南”的双文档契约：
  - `REMOTE-PLUGIN-GUIDE.md` 负责安装、更新、沙箱、账号启用、插件仓库等远程插件专属流程；
  - `PLUGIN-DEV-GUIDE.md` 负责运行时 API、配置命名、消息发送边界、并发抢答、调度生命周期、日志、测试清单等通用开发规范；
  - 从本版本开始，不再兼容旧版“只有 `plugin.json` + `plugin.py`”的单文件远程插件结构。
- 插件开发指南补齐插件工程规范：
  - 新增“命令回调 / on_message / 定时任务”的可用对象矩阵，说明什么时候用 `event.reply`，什么时候用 `ctx.client` / `ctx.scheduler`；
  - 新增抢答类插件标准模板：`chat_id -> asyncio.Lock` + 二次状态检查，避免并发双发奖；
  - 新增统一配置字段建议：`command`、`reward`、`timeout`、`auto_next`、`message_template`、`status_interval_seconds` 等；
  - 新增后台任务与调度生命周期说明，避免插件卸载后遗留幽灵任务；
  - 新增奖惩系统接入约定、发布前最小测试清单、可复制的游戏插件骨架。

### Changed
- 远程插件安装与更新改为强校验标准插件包结构：
  - 安装阶段仍只解析静态 `plugin.json`，不会 import 未信任 Python；
  - 同时要求仓库内必须存在 `manifest.py`、`plugin.py`、`__init__.py`；
  - 缺少运行期文件时返回可读错误，并引导开发者按 `docs/REMOTE-PLUGIN-GUIDE.md` 更新插件。
- 插件安装目录解析统一锚定项目根目录，避免不同启动目录下把远程插件写到错误位置。
- 远程插件更新流程支持从插件仓库的子目录重新安装覆盖，更新前后都会重新校验运行期结构。
- 插件中心的远程插件交互收口：
  - 已安装插件如果仓库版本更高，显示“可更新”并把按钮切换为“更新”；
  - 已安装远程插件增加“按账号管理”入口；
  - 账号级启用远程插件前会检查全局远程插件开关，避免 UI 提示已启用但 worker 找不到实现；
  - 更新 / 卸载按钮样式与禁用态统一。

### Fixed
- 修复 Codex 生图发送图片时引用了未定义 `reply_msg`，导致 Telegram 发送阶段报 `NameError` 的问题。
- 修复 Codex 生图图片发送回归测试缺口：新增测试覆盖显式 `reply_to_id`、PNG 文件名和后缀。
- 修复前端 API 错误解析不识别 FastAPI `{ detail: { code, message } }` 的问题，现在远程插件安装失败能显示后端给出的明确原因。

### Verification
- 后端定向测试通过：`backend/.venv/bin/python -m pytest backend/app/tests/test_plugin_security_regression.py backend/app/tests/test_codex_image_errors.py`。
- 后端静态检查通过：`backend/.venv/bin/python -m ruff check backend/app/settings.py backend/app/services/remote_plugin_service.py backend/app/services/plugin_install_service.py backend/app/services/plugin_repo_service.py backend/app/worker/plugins/loader.py backend/app/tests/test_plugin_security_regression.py`。
- 前端类型检查通过：`pnpm -C frontend exec tsc -b --noEmit`。
- 前端生产构建通过：`pnpm -C frontend build`。

---

## [0.10.1] — 2026-05-11 · feature · 插件仓库系统 + 调度平台化 + Codex 生图增强

### Added
- **插件仓库系统**：新增后端 `plugin_repo` 表与完整 CRUD API，支持添加 Git 仓库、浏览仓库内插件、选择性安装。
  - 仓库地址持久化存储在数据库，跟随系统配置，不依赖浏览器 localStorage。
  - 自动扫描仓库根目录或一级子目录的 `plugin.json`，识别单插件/多插件仓库。
  - 前端 Extensions 页面「插件仓库」卡片：添加仓库 → 展开浏览 → 点击安装，已安装插件标记状态。
- **调度平台化**：定时任务从普通插件语义收口为平台基础能力。
  - 新增 `scheduler_runtime.py` 平台调度器模块，提供 `PlatformScheduler` 与 `SchedulerFacade`。
  - `PluginContext` 新增 `ctx.scheduler` capability，插件可注册/注销定时任务。
  - 新增系统级路由 `/scheduler`（调度中心）与侧边栏「调度」一级菜单，支持账号选择器。
  - 插件中心与账号详情中的「基础能力→配置」统一跳转调度中心，形成单真相双入口。
- Codex 生图插件新增 `image_model` 配置项，支持选择底层图片模型（auto / gpt-image-2 / gpt-image-1.5 / gpt-image-1 / gpt-image-1-mini）。
- Codex 生图消息模板新增 `{image_model}` 占位符，主模型与图片模型分开展示。
- Codex 生图 `aspect_ratio` 和 `image_size` 新增 `from_reference` 选项，回复图片生成时自动使用原图比例和分辨率。
- 前端 Codex 配置页同步新增图片模型下拉选择器。
- `system_health` 进程采样改为优先 psutil，失败回退 ps，提升 Oracle/Linux 环境稳定性。

### Changed
- 调度器兼容壳（builtin scheduler plugin）移除自建 tick loop，只保留兼容注册与方法表。
- Codex 生图错误提示全面白话化：
  - 401/403 鉴权失败：区分 Token 过期、无效、权限不足，附带修复建议。
  - 429 限流：说明原因和等待策略。
  - 额度用完：显示计划类型和恢复时间。
  - 内容审核拦截、超时、余额不足等常见错误均有 ❌ + 💡 结构化提示。
- Codex 生图轮询状态展示优化：显示 API 返回的实际状态（in_progress / queued 等），不再只显示"正在等待"。
- 参考图比例匹配容差从 ±0.02 放宽到 ±0.05，减少因图片压缩导致的比例误判。
- PWA 移动端 Tabs 自动换行居中，Card/Content 增加 min-w-0 防止长内容撑破布局。
- 插件开发指南增加平台调度器说明、`ctx.scheduler` 用法与迁移约束。

### Fixed
- 修复 `from_reference` 在无参考图时传给 API 的 "from_reference" 字符串导致请求失败的问题，现在自动 fallback 到默认值。
- 修复 `_poll_codex_response` 吞掉 HTTP 4xx/5xx 错误的问题，现在正确提取并返回错误信息。
- 修复轮询时 `status=failed` 等终态被忽略的问题，现在立即报错而非空等超时。
- 修复 Extensions 页面缺少 `X` icon import 导致 CI 构建失败。
- 修复 `system_health` 在部分 Linux 环境下 CPU/内存读取失败的问题。

### Verification
- 后端测试通过：`pytest backend/app/tests`（492 passed, 2 skipped）。
- 前端类型检查通过：`pnpm -C frontend exec tsc -b --noEmit`。

---

## [0.10.0] — 2026-05-10 · feature · 账号 Bot 联动 + 日志可观测性 + Sudo 收紧

### Added
- 新增账号绑定 Bot 联动系统（每账号独立 Bot Token、独立授权、独立运行）：
  - 新增数据模型：`account_bot`、`account_bot_user`；
  - 新增迁移：`backend/alembic/versions/0021_account_bot.py`；
  - 新增 API：`/api/accounts/{aid}/bot`、`/bot/test`、`/bot/restart-runtime`、`/bot/users`；
  - 新增运行时：`account_bot_runtime` polling manager，应用启动自动拉起、关闭自动停止；
  - 新增前端账号详情页「Bot 联动」Tab，支持配置、测试、授权用户与角色管理。
- 日志系统新增最小落库级别配置 `runtime_log_min_level`（`debug/info/warn/error`），支持在系统设置中切换排障与日常模式。
- 日志中心新增“插件日志”独立视图与 `plugin_key` 过滤，支持按插件维度快速定位故障。
- 新增 `TelegramHtmlPreview` 组件，用于消息模板 HTML 预览。

### Changed
- Sudo 权限模型改为默认拒绝：`allowed_chat_ids` / `allowed_commands` 空值不再等于“全部允许”，显式全部需设置 `*`（前端通过 `allow_all_*` 开关表达）。
- Sudo 管理页合并“允许命令”与“当前可用命令”选择体验，支持点击卡片启停，减少手填。
- 运行日志保留策略在原有“保留天数/长度截断”基础上增加“最小级别”控制，便于生产环境降噪。
- 24 点插件完成结构化重构：配置解析、事件适配、判题、发奖、超时、公告拆分；同 chat 加锁避免并发双发奖。
- 24 点插件 manifest 与运行时配置统一为 `command + timeout`，移除历史错位字段。
- 24 点插件对“是否本人消息”判定改为优先 `sender_id == self_tg_user_id`，减少部分群组中 `outgoing` 标记不稳定导致的误跳过。
- Codex 生图与 24 点配置页数值输入交互优化：允许先清空再输入，保存时再校验，避免输入框强制回弹。

### Fixed
- 修复插件日志与系统日志混叠：`source=event` 不再包含插件日志，插件日志独立归类为 `source=plugin`。
- 修复 Codex 生图网络中断错误提示不友好问题，常见 chunked stream 断连改为可读提示并继续脱敏。
- 修复 Sudo incoming 拒绝提示在群聊噪声过高的问题：对未配置/未授权场景默认静默。
- 修复配置页部分数值输入无法删除为空再重输的问题（24 点、Codex 生图）。

### Verification
- 后端测试通过：`backend/app/tests/test_game24_plugin.py`、`backend/app/tests/test_supervisor_reliable_consumer.py`、`backend/app/tests/test_plugin_loader.py`、`backend/app/tests/test_sudo.py`。
- 前端类型检查通过：`pnpm -C frontend exec tsc -b --noEmit`。

---


## [0.9.8] — 2026-05-10 · feature · Codex 生图体验 + 插件模式与调度器收口

### Added
- Codex 图片生成插件新增完整账号级配置：
  - `command` 触发指令名，支持中文命令；
  - `message_template` 消息模板，统一用于生成中状态编辑与最终图片 caption；
  - `image_size`、`aspect_ratio`、`image_format` 默认图片参数；
  - `status_interval_seconds`、`delete_command_message`、`show_revised_prompt`、`reasoning_effort`、`custom_instructions` 等高级行为配置。
- Codex 生图命令支持临时覆盖图片参数：`--比例/--ratio`、`--size/--resolution`、`--format/--格式`，例如 `,cximg --比例 4:3 --size 1536x1024 --format jpeg 云海里的城市`。
- Codex 图片生成配置页新增消息模板编辑器、占位符快捷插入、实时预览、分辨率/比例/格式下拉配置。
- 自定义命令 AI 消息模板配置新增实时预览，便于保存前确认最终 Telegram 展示效果。
- 前端新增 `plugin-modes.ts` 统一插件模式分类工具；后续插件可通过 `config_schema["x-ui-mode"]` 自动进入规则驱动、单配置对象、Schema 弹窗或平台内置分组。

### Fixed
- 修复 Codex 生图发送裸 `bytes` 导致 Telegram 中显示 `unnamed` 且没有图片后缀的问题：现在使用带 `.name` 的 `BytesIO` 上传，并根据图片 magic bytes / 配置格式生成 `.png`、`.jpg` 或 `.webp` 文件名。
- 修复 Codex 生图生成中状态消息没有按 HTML 解析，导致 `<b>...</b>` 标签原样展示的问题；模板 HTML 解析失败时会退回纯文本，避免把标签直接发给用户。
- 优化 Codex / LLM 错误提示：额度耗尽、限流、鉴权失败、网络异常等错误改成人可读提示，并继续脱敏 token、路径和密钥片段。
- 修复自定义触发指令名变更后不能实时生效的问题：命令型插件通过 `command_config_keys` 触发 worker 重新注册命令。
- 命令解析从 `\w+` 放宽为非空白 token，支持中文插件命令。

### Changed
- 定时任务从普通插件 tick loop 迁移为 worker runtime 的平台基础能力：只要账号 worker 运行，调度器就按规则执行；插件壳仅保留兼容入口和调度算法复用。
- 账号详情页与插件中心的插件列表改为按模式分组展示：规则驱动、单配置对象、Schema 弹窗、基础能力；表格列宽统一，配置按钮不再依赖启用状态。
- Codex Image 与 24 点游戏配置页调整为“当前状态 + 使用说明”置顶，命令示例统一读取系统命令前缀和当前配置。
- 内置插件 manifest 补充 `x-ui-mode` 元数据：`forward` / `autorepeat` 为规则驱动，`game24` / `codex_image` 为单配置对象，`scheduler` 为平台内置。
- 插件开发指南更新前端配置页规则，补充模式自动分类、命令型插件热重载要求、Codex Image / 24 点页面布局规范和平台调度器说明。
- 后端 ruff 静态检查收口：整理 import、类型注解、异常链、未使用变量和测试静态问题。

### Verification
- 后端全量测试通过：`469 passed, 2 skipped`。
- 后端静态检查通过：`ruff check app`。
- 前端生产构建通过：`pnpm -C frontend build`。

---


## [0.9.7] — 2026-05-10 · feature · 插件前端规范 + LLM/IPC 可靠性闭环

### Added
- 新增 Codex Image 内置插件：将 TeleBox_Plugins 的 `codex_image` TypeScript 插件转换为 TeleBot Python 插件，并接入内置插件路由、配置页与规则 dry-run。
- 插件开发文档第 12 节重写为完整前端集成规范：补充规则驱动、单配置对象、Schema 驱动弹窗三种模式，以及新增功能插件的前后端适配清单。
- LLM 用量流水落库：新增 `llm_usage` 表、模型与 runtime usage callback，记录账号、provider、model、token、fallback 与错误类型。
- LLM 成本限额：系统设置支持每账号每分钟调用、每日调用、每日 token、高价模型每日调用限制；worker 调用前执行硬门禁。
- Redis IPC 可选 ACK：reload 类控制命令支持等待 worker 确认，worker 离线时继续依赖 DB 状态与周期 reconcile 收敛。
- 插件 manifest 兼容检查：loader 支持 `min_telebot_version` 与 `requires_features`，不兼容或缺依赖插件会跳过加载。

### Fixed
- 修复 scheduler 修改 cron 表达式后 next fire 未及时重算，导致新触发时间不被识别的问题。
- LLM runtime fallback 从选路层补到调用层：主 AI 命令与 scheduler 调用失败后会按 retry + fallback chain 自动切换 provider。
- 补齐 sudo / owner_only 安全模型：支持 incoming sudo 命令，插件默认 owner-only，内置群消息插件显式放开。
- 修复插件 `run_plugin` 命令模板占位问题，改为调用已加载插件注册的命令。

### Changed
- 前端系统设置页新增 LLM 成本限额配置入口。
- 插件 loader 对第三方插件继续保持最小 capability sandbox，并在派发前校验 owner / sudo 权限。
- reload_config / reload_commands / sudo / rules / remote plugin reload 通知改为 ACK 优先、周期 reconcile 兜底。

### Verification
- 后端全量测试通过：`462 passed, 2 skipped`。
- 前端类型检查通过：`pnpm -C frontend exec tsc -b --noEmit`。
- 前端生产构建通过：`pnpm -C frontend build`。

---

## [0.9.6] — 2026-05-09 · fix · 插件配置弹窗修复 + 远程插件安全加固

### Fixed
- 前端插件配置弹窗崩溃：修复 ConfigDialog hook 顺序，补全 enum / array 字段渲染
- 内置功能配置入口统一：插件页、账号详情页的「配置」按钮改为跳转真实功能页
- 插件管理页已安装列表补齐版本列和操作列

### Changed
- 远程插件安全闭环继续加固：安装阶段只读 plugin.json，worker默认不再扫描执行 installed 插件，只有 RemotePlugin.enabled + AccountFeature.enabled 双开关通过后才按需加载
- 远程插件热加载从不适配第三方的 reload_plugin 改走 reload_config，支持按 plugin_key 强制卸载旧实例后重载

---

## [0.9.5] — 2026-05-08 · chore · Codex 审查合并 + 仓库清理

### Changed
- 合并 Codex GPT-5.5 审查产出的代码改动（sandbox、LLM runtime、测试用例等）
- 清理非必要文件（archive/、agent-plans/、审查文档）
- 更新 .gitignore 排除开发计划与归档目录

---

## [0.9.4] — 2026-05-08 · feature · 插件配置系统 + 账号级配置统一

### Added
- 新建 ConfigDialog 组件：JSON Schema 驱动的自动表单渲染
- Feature API 返回 config_schema
- 内置插件 config_schema 补全：game24 / scheduler / translate

### Changed
- 账号详情「功能开关」更名为「插件启停」
- Account Detail 和 Extensions 的「配置」按钮统一为 ConfigDialog 弹窗
- 所有 Feature 配置页返回按钮改为 /accounts/:aid?tab=features
- Account Detail Tab 支持 URL 参数 ?tab= 控制默认激活项
- 插件开发指南补充 config_schema 规范（level 字段 + 验证清单）

---

## [0.9.3] — 2026-05-08 · fix · 插件中心重构 + 版本号 bump

### Changed
- 插件中心从 4 Tab 重构为 3 Tab（账号插件管理 / 插件管理 / 开发指南）
- 废弃功能矩阵（NxM 格子），替换为账号级插件管理
- 合并本地内置插件、第三方插件、远程插件到统一列表

---

## [0.9.2] — 2026-05-08 · feature · 插件中心重构 + 账号级插件管理

### Added
- 远程插件安装支持 default_enabled 参数
- 账号级远程插件管理 API（enable-accounts / disable-accounts）
- 新建 Alembic 迁移 0019（remote_plugin.default_enabled）

---

## [0.9.1] — 2026-05-08 · fix · 远程插件 API 导入修复

### Fixed
- remote_plugin.py API 导入 get_db 路径错误（应为 deps.DBSession）
- 移除未使用的 Depends / AsyncSession / select 导入

---

## [0.9.0] — 2026-05-08 · feature · 远程插件管理系统 + 安全加固

### Added
- **远程插件管理系统**：
  - `RemotePlugin` 数据库模型 + Alembic 迁移（0018、0019）
  - 远程插件 API（list / install / enable / disable / update / uninstall / enable-accounts / disable-accounts）
  - 远程插件 Service 层（git clone、manifest 解析、热加载触发、Feature 表联动）
  - `/plugin` Bot 命令（list / install / remove / enable / disable / update，install 支持 `--default` 参数）
  - 前端远程插件管理 Tab（卡片布局、安装输入框、默认启用开关、启用/禁用/更新/卸载）
  - 插件验证函数 `validate_manifest()`（必填字段校验）
- **账号级远程插件管理**：
  - 安装时可勾选「默认为所有账号启用」（`default_enabled`），自动注册到 Feature 表 + 为所有账号创建 AccountFeature 行
  - 功能矩阵 Tab 自动展示远程插件列（因为注册到了 Feature 表）
  - 按账号启停远程插件 API（`enable-accounts` / `disable-accounts`），复用 `feature_service.bulk_set_enabled`
  - 卸载时自动清理 Feature / AccountFeature 关联行
- **插件中心 UI 重构**：
  - 远程插件从独立页面合并到插件中心第四 Tab（功能矩阵 / 已加载插件 / 远程插件 / 开发指南）
  - 侧边栏移除「远程插件」独立入口，统一走「插件」
  - `/remote-plugins` 旧路由自动跳转到 `/plugins`
- **远程插件开发文档**：`docs/REMOTE-PLUGIN-GUIDE.md` + `docs/REMOTE-PLUGIN-DEV-PLAN.md`

### Fixed
- 插件名称正则去除 `.`，防止 `..` 路径穿越攻击
- `remote_plugin` Schema 字段 `installed_at` 与 model 对齐
- 安装后 toast 根据是否默认启用显示不同提示文案

### Changed
- 插件开发指南（PLUGIN-DEV-GUIDE.md）全面重写，合并远程插件规范
- Sprint5 和 CONTRACTS 归档到 `archive/plans/`
- 版本号统一升至 0.9.0

---

## [0.8.0] — 2026-05-08 · feature · Sprint5 功能实现

### Added
- **Generation Guard (Task #18)**：Account 模型添加 `generation` 字段，防止并发操作冲突
- **命令别名系统 (Task #19)**：
  - CommandAlias 模型和数据库迁移（0016_command_alias.py）
  - 贪心最长匹配别名解析
  - `,alias` 管理命令（set/del/ls）
- **Sudo 系统 (Task #20)**：
  - SudoUser 模型和数据库迁移（0017_sudo_user.py）
  - Sudo 模式：允许指定用户代表账号执行命令（默认前缀 `.`）
  - 权限检查：tg_user_id、chat_id 白名单、命令白名单
  - `,sudo` 管理命令（add/del/ls）
- **Conversation API (Task #21)**：
  - conversation.py 实现多轮对话工具类
  - 支持 send()、get_response()、click_button()、mark_read()
  - 超时处理和自动 handler 清理

### Security
- Sudo 权限在命令派发前检查，权限拒绝返回清晰错误信息
- 基于白名单的 chat 和命令访问控制

### Changed
- CommandContext 新增 `sudo_users` 和 `sudo_prefix` 字段
- runtime.py 加载 sudo 配置到命令派发上下文

### Fixed
- `runtime.py` 补充缺失的 `from typing import Any` 导入（F821）
- `conversation.py` 将 `asyncio.TimeoutError` 改为内置 `TimeoutError`（Python 3.12 兼容）
- 前端 `SudoManagement.tsx` / `AliasManagement.tsx` 修复 TypeScript 类型错误：queryFn 泛型、不存在的 AlertDialog 组件替换为 `window.confirm`、`getAccounts` → `listAccounts`
- `ruff check` 自动修复存量 import 排序和无用 f-string 问题

### Tests
- 新增 `test_conversation.py`（10 用例）：超时、队列、context manager、handler 注册、send、click_button
- 新增 `test_alias.py`（8 用例）：默认值、贪心最长匹配、参数透传、别名→内置/模板派发
- 新增 `test_sudo.py`（13 用例）：权限检查全分支、generation guard 递增与匹配

---

## [0.7.1] — 2026-05-07 · patch · Docker 部署优化与前端构建修复

### Added
- **GitHub Actions CI (Frontend)**：新增 `.github/workflows/frontend-ci.yml`，在推送或 PR 到 main 分支时自动校验前端构建。

### Fixed
- **Docker 部署路径报错**：修复 ARM64 构建时 `nginx.conf` 找不到以及 `docs/` 目录在容器内路径不匹配的问题。
- **Frontend 生产构建失败**：
  - 修正 `Extensions.tsx` 在 TypeScript strict 模式下的类型推断错误。
  - 修正 `ConfigBackup.tsx` 中 `includeSensitive` 变量名拼写错误导致的 `Cannot find name 'include_sensitive'` 编译报错。
- **TypeScript 编译放宽**：调整 `tsconfig.app.json` 中的 `strict: false`，确保在生产环境（尤其是低配 ARM 服务器）中构建更加稳健。

---

## [0.7.0] — 2026-05-07 · feature · 更新检查器 & 配置备份 & 命令转发增强

### Added
- **分步确认式更新检查器**（TopBar 刷新按钮 → UpdateDialog 状态机）
  - 点击顶栏刷新图标打开对话框，自动 `git fetch` 对比本地/远程 commit
  - 三步流程：检查更新 → 拉取更新 → 重启应用，每步需用户手动点击确认
  - 支持 origin/main 和 origin/master 分支自动检测
  - 重启前二次确认弹窗，重启后 5 秒倒计时自动刷新页面
  - 重启方式自动检测：docker-compose → Makefile → SIGTERM 兜底
- **配置备份与恢复**（Settings → 备份与恢复卡片）
  - 导出：12 个数据库类别可选导出为 JSON 文件下载
  - 类别：系统设置、命令模板、账号命令绑定、LLM Provider、转发规则、自动回复规则、风控模板/规则、插件功能配置、账号设置、忽略列表、通知 Bot
  - 敏感数据开关：默认排除 session/api_key/token/phone 等加密字段，手动开启后包含
  - 导入：上传 JSON 文件，按 ID 字段去重检测，已存在的记录自动跳过
  - bytes 类型字段自动 hex 序列化/反序列化

### Changed
- **自定义命令 forward_to 支持 4 种转发模式**
  - `forward_native`（原生转发，携带原作者）/ `copy_text`（纯文本复制）/ `quote`（带"来自 X"前缀）/ `link_only`（仅发消息链接）
  - 成功提示文案根据模式动态显示（转发/复制文本/引用转发/链接）
- **自定义命令支持"立即删除"选项**：转发成功后立即删除命令消息（无需设置延迟秒数），与延迟删除互斥
- **转发插件 target_chat_id 改为可选**：留空或非法值时默认转发到当前消息来源的 chat
- **Game24 配置页命令前缀动态化**：命令示例改为从 `getSystemSettings` API 实时读取 `command_prefix`，不再硬编码 `,`
- **Game24 插件提示文案优化**：开始游戏时的规则说明更简洁，示例算式更直观

### Added (Docs)
- 插件开发指南新增 §10「前端配置页规范」：明确命令前缀必须动态获取，禁止写硬编码

---

## [0.6.0] — 2026-05-07 · feature · 24 点游戏插件 & Builtin 插件热发现

### Added
- **24 点游戏插件**（`builtin/game24`）：群内互动小游戏，支持竞速答题与奖金发放
  - 触发方式：`,24d <奖金金额>`（例：`,24d 2000`）— 由自己发送（outgoing），在群里触发
  - 随机生成 4 个 1–13 的整数，保证有解（递归求解器验证）
  - 双通道消息处理：`on_outgoing` 监听自己的触发指令，`on_message` 监听群内他人的答题
  - 表达式安全求值：ast 白名单过滤，仅允许 `+ - * /` 和括号，禁止函数调用/属性访问/幂运算
  - 支持用户输入运算符别名：`x` / `X` / `×` → `*`，`÷` → `/`
  - 第一个答对者自动发奖：Bot 回复其消息 `+<奖金数量>`
  - 超时机制：默认 500 秒，超时无人答对自动宣布结束
  - per-chat 游戏状态隔离，同一群已有进行中游戏时拒绝开新局
  - 可配置项：`trigger_pattern`（自定义触发正则）、`timeout`（限时秒数）
  - 权限声明：`send_message` / `edit_message` / `read_chat` / `delete_message`

### Changed
- **插件框架 `message_channels` 声明式消息方向**：`Plugin` 基类新增 `message_channels` 类属性（默认 `{"incoming"}`），插件声明需要监听的方向后，loader 自动向其派发对应事件，无需重写新 hook；loader 统一 incoming/outgoing 两个 dispatcher，按插件声明过滤
- **插件命令注册支持实例属性**：loader `_activate` 优先读取插件实例的 `commands`（`on_startup` 动态设置），回退到类属性，支持插件根据 config 动态注册不同命令名
- **Builtin 插件动态发现与热重载**：`BUILTIN_FEATURES` 从静态常量改为运行时文件系统扫描，新增 builtin 插件无需重启后端进程即可生效
  - 新增 `scan_builtin_manifests()`：动态扫描 `app/worker/plugins/builtin/` 目录，import 各子包 `manifest.py` 读取 `MANIFEST.key / display_name`
  - 新增 `_LazyBuiltinFeatures(dict)`：继承 dict 的惰性字典，首次访问自动触发扫描，支持 `refresh()` 强制重扫
  - 主进程侧：`seed_builtin_features()` 每次调用前 `BUILTIN_FEATURES.refresh()`，自动 seed 新插件行
  - Worker 侧：`reload_account_config()` 第一步刷新 `BUILTIN_FEATURES`，配合 `discover_plugins()` 激活新插件
  - 保留 `FEATURE_AUTO_REPLY / FORWARD / SCHEDULER / GAME24` 等具名常量向后兼容
  - 修复 `builtin/__init__.py` 缺少 `game24` 导入
- **功能矩阵启用后即时反馈**：toggle mutation 改用乐观更新（`onMutate` 立即改本地缓存），不再等 invalidateQueries；1.5 秒后再重新拉取确认 worker 已激活
- **账号详情页功能开关动态化**：`FEATURE_KEYS` 硬编码列表改为从 feature-matrix API 动态获取，新增插件自动出现在开关列表
- **已加载插件 Tab 显示内置插件**：数据源从 `installed-packages`（仅第三方）改为同时展示 builtin（来自 feature-matrix）和第三方插件
- **Feature 配置页通配路由**：新增 `:aid/features/:featureKey` 通配路由，未实现配置页的 feature 统一显示 TodoPage 占位
- **Game24 触发改为命令注册**：从正则匹配改为 `commands` 字典注册，指令名默认 `24d`（可通过 config.command 自定义），跟随系统命令前缀；答题保持 incoming `on_message`
- **Game24 配置页面**：配置项从 `trigger_pattern` 改为 `command`（指令名），支持设置触发指令名和答题限时

### Fixed
- **Docker 部署路径报错**：修复 aarch64 构建时 `nginx.conf` 找不到以及 `docs/` 目录在容器内路径不匹配的问题。
- **Frontend 生产构建报错**：解决 `Extensions.tsx` 在 strict 模式下的 TypeScript 类型推断错误（TS2339/TS6133），并放松 `tsconfig` 的 `noUnused` 限制以确保编译通过。

### Affected Files
- `backend/app/worker/plugins/builtin/game24/__init__.py`
- `backend/app/worker/plugins/builtin/game24/manifest.py`
- `backend/app/worker/plugins/builtin/game24/plugin.py`
- `backend/app/worker/plugins/base.py`（新增 `message_channels` 声明式属性）
- `backend/app/worker/plugins/loader.py`（新增 outgoing 派发器 + `reload_account_config` 加 refresh）
- `backend/app/db/models/feature.py`（`scan_builtin_manifests` / `_LazyBuiltinFeatures` / `BUILTIN_FEATURES` 重构）
- `backend/app/services/feature_service.py`（`seed_builtin_features` 加 refresh）
- `backend/app/worker/plugins/loader.py`（`reload_account_config` 加 refresh）
- `frontend/src/pages/Extensions.tsx`（乐观更新 + builtin 插件列表）
- `frontend/src/pages/Accounts/Detail.tsx`（FEATURE_KEYS 动态化）
- `frontend/src/App.tsx`（通配 feature 路由）
- `frontend/src/pages/Features/Game24Config.tsx`（game24 专属配置页）

---

## [0.5.2] — 2026-05-07 · hotfix · AI provider 实时刷新与稳定性修复

### Added
- **AI inline 强制刷新命令**：新增 `,ai @refresh`（兼容 `@reload`）用于手动刷新 worker 内 `LLMProvider` 快照并回显最新 provider 列表。
- **provider 列表用法提示增强**：`@list` 输出中新增 `@refresh` 用法提示，便于用户现场排障。

### Changed
- **provider 变更广播范围扩大**：`create/update/delete LLM provider` 后，reload 通知从“仅启用 ai 模板的账号”升级为“全账号广播”，降低边界场景漏刷风险。
- **reload 发送策略收紧**：`notify_reload()` 改为逐账号发送并汇总失败账号，不再静默吞异常。

### Fixed
- **新增 provider 后 `,ai @list` 非实时问题**：`_run_ai` 在执行前继续主动刷新命令上下文，并在 provider miss 场景下触发一次兜底刷新后重查，修复“刚新增 provider 仍提示未找到”的问题。
- **reload 失败不可见问题**：provider reload publish 失败现在会明确记录异常并抛出错误，避免“前端成功、worker 未刷新”的假成功状态。

### Affected Files
- `backend/app/api/commands.py`
- `backend/app/services/command_service.py`
- `backend/app/worker/command.py`
- `backend/app/worker/inline_override.py`


### Added
- **项目归档与预览支持**：
  - 新增 `docs/screenshots/` 文件夹用于存放 UI 截图
  - `.gitignore` 增加 `archive/` 与 `.github_backup/` 规则
- **预置设备伪装**：新增 Android 选项（Samsung Galaxy S24 / Android 14 / Telegram 12.6.4）
- **部署依赖**：`backend/pyproject.toml` 补全 `psycopg2-binary` 依赖，解决 Docker 模式下 Alembic 无法运行同步迁移的问题

### Changed
- **Claude API 协议优化**：`AnthropicClient` 切换为 **Streaming (SSE)** 模式调用，解决 Anyrouter 代理返回 400 的兼容性问题
- **README 全面优化**：
  - 更新为真实的用户名 `Anoyou`
  - 增加 **Docker 部署（生产环境推荐）** 指南
  - 增加 **Prerequisites**（API ID/Hash 申请与网络代理配置）
  - 增加 **Configuration** 核心安全项说明
- **Docker 构建优化**：前端镜像 Build Context 提升至项目根目录，支持构建时访问 `docs/` 进行文档打包

### Fixed
- **Worker 缓存过时**：在执行每个 AI 命令前强制从 DB 刷新 `LLMProvider` 列表，解决“前端新增 provider 后 ,ai @list 仍显示未找到”的问题
- **Worker 环境补全**：修复 `_run_ai` 缺失 `api_format` 导致无法正确识别模型协议的 bug
- **文件名大小写修复**：`Dashboard.png` -> `dashboard.png` 确保在 Linux 容器下正常渲染

---

## [0.5.0] — 2026-05-06 · RC1 · 开源前打磨

### Added
- **GitHub Actions CI**：`.github/workflows/ci.yml` 自动化测试流水线
  - Backend job：pytest + ruff + alembic upgrade（PostgreSQL 16 + Redis 7 services）
  - Frontend job：pnpm build（Node 20 + pnpm 9）
  - 推送到 main 或 PR 时自动触发
- **开源协议**：MIT License，同步更新 `pyproject.toml` 和 `package.json` license 字段
- **应急响应工单模板**：`docs/SECURITY-OPS.md` 新增 §6，提供安全事件记录模板

### Changed
- **README 重写为开源向**：
  - 简洁的 Feature 列表（8 项核心功能）
  - Quick Start 分本机自用 / 公网部署两条路径
  - FAQ 回答"多用户支持"、"为什么用 userbot"
  - 状态声明：Alpha / 个人自用 / 欢迎 fork 但暂不接大 PR
- **SECURITY-OPS 润色**：
  - 顶部新增"如果你打算公网部署"提示
  - "某 web 用户账号"改为"管理员账号"（单租户语境）
  - 应急 SOP 措辞更精准

### Fixed
- **inline @provider 覆盖 bug**：`,ai @provider 问题` 现在将会正确使用 已配置的provider 的 default_model，而不是错误地使用命令模板里配置的 model
  - 旧逻辑：`override_model = cfg.get("model")` 总是从模板读取，导致切换 provider 后 model 不匹配
  - 新逻辑：当用户指定 `@provider`（未指定 `:model`）时，清空 `override_model`，让 `build_client` 使用该 provider 的 `default_model`
  - 优先级现在是：`@name:model` > provider.default_model > 模板 model（仅当无 inline override 时）
  - 新增测试用例：`test_run_ai_inline_provider_without_model_clears_template_model`

### Removed
- **仓库归档清理**（已在 Wave 2 完成，本次补充文档）：
  - `agent-plans/SPRINT4-*.md` 归档到 `archive/plans/SPRINT4/`
  - `archive/plans/SPRINT4/README.md` 溯源文档更新

### Notes
- 首个 Release Candidate（RC1），可以开源了
- CI 徽章 URL 需要替换为实际 GitHub 仓库地址
- README 截图占位（Coming soon），待后续补充

---

## [0.4.2] — 2026-05-06 · Sprint 4 hotfix · 版本检测

### Added
- **前后端版本不一致检测**：避免再发生"代码改了但 uvicorn 没重启 / SW 缓存老前端 → 用户看老行为以为代码没生效"的幻觉式 bug。
  - 后端：`GET /api/system/version`（**public 无鉴权**）返回 "version": "0.7.1"
  - 前端：`GlobalAlertBar` 拆成 `VersionMismatchBar` + `KillSwitchBar`；启动 + 每 60s 拉一次后端版本，不一致时顶部弹**琥珀色横幅**："前后端版本不一致 · 前端 vX.Y.Z · 后端 vA.B.C — 请 `make restart` + 硬刷"，含一键"硬刷新"按钮。
- **agent-plans/README.md §4.1**：新增"必跑 `make restart`"规范，列明哪些情况必须整套重启 + 哪些 `--reload` 能搞定。
- **agent-plans/README.md §3** 共用基础设施：把 `make up` / `make restart` / `make down` 提到首位，`make backend`（带 reload）降级为"调试时用"。

### Notes
- 单元测试不要直接 `from app.api.system_health import router` 然后调 `/api/system/version`——这是 public 端点没鉴权依赖，pytest 拿到的是 `VersionInfo(version=__version__ = "0.7.1"
APP_STAGE: str | None = "feature"
int 4")`，下次 bump 版本号时 `stage` 字段如果摘掉得记得这里同步。

---

## [0.4.1] — 2026-05-06 · Sprint 4 hotfix · UX 体验四件套

### Added
- **内置命令面板**：自定义命令模板页顶部新增「内置命令（只读）」卡片，列出 worker 静态注册的所有命令（name + aliases + doc），让用户起自定义模板名时一眼知道哪些已被占用。
  - 后端：`GET /api/commands/builtin` 从 worker `_BUILTIN` 字典读取（无 DB 依赖，无运行时状态）
  - 安全：纯只读，无敏感数据
- **扩展中心**（路由 `/extensions`）：合并原「功能矩阵」+「插件管理」+「插件开发指南」三处入口，三 tab 形式呈现：
  - Tab 1：功能矩阵（账号 × 功能 启停状态总览，含克隆其他账号规则功能）
  - Tab 2：已加载插件（builtin + 第三方装到 `data/plugins/installed/` 的）
  - Tab 3：开发指南（`docs/PLUGIN-DEV-GUIDE.md` 通过 vite raw import 打包进前端，react-markdown + remark-gfm + rehype-highlight 渲染）
- 前端依赖：`react-markdown` / `remark-gfm` / `rehype-highlight` / `highlight.js` / `@tailwindcss/typography`。

### Removed
- **`group_admin` / `monitor` 残留清理**（v0.4.0 删了 builtin 目录但前端 / DB 未跟上）：
  - `frontend/src/pages/Features/GroupAdmin.tsx` / `Monitor.tsx` 文件
  - `App.tsx` 两条路由
  - `Wizard.tsx` cloneConfig 列表中的两项
  - `Detail.tsx` `FEATURE_KEYS` 数组中的两项
  - `feature.py` 的 `FEATURE_GROUP_ADMIN` / `FEATURE_MONITOR` 常量 + `BUILTIN_FEATURES` 映射
  - 数据库 `feature` / `account_feature` 表中的旧行（迁移 `0014_drop_legacy_features`）
- 老路由 `/matrix`、`/plugins`、`/settings/plugins` 改为 redirect 到 `/extensions`（书签兼容）
- `frontend/src/pages/FeatureMatrix.tsx` / `Plugins.tsx` / `Settings/PluginManager.tsx` 文件（内容并入 `Extensions.tsx`）

### Changed
- Sidebar 顶层菜单 7 项 → 7 项（"功能矩阵" + "插件管理" 合成 "扩展中心"，腾出位置但不增加）。

### Fixed
- alembic 0012 误写 `down_reversion = "0.7.1"` 与 0011 构成分叉，汇总验收时修正为 `down_revision="0011"`，再次回到单 head。

---

## [0.4.0] — 2026-05-06 · Sprint 4 Wave 2

### Added
- **定时任务插件**（迁移无新增，复用 `rule.config`）：完整实装 cron / once / interval 三种模式；前端 `Features/Scheduler.tsx` 完整规则编辑页 + dry-run；通过 `ctx.engine.acquire("send_message_group")` 走风控（`scheduler/plugin.py` 20 行 → 283 行）。
- **多 Telegram Bot 通知通道**（迁移 `0013_notify_bot`）：项目启动时发"📦 telebot vX.Y.Z started"；worker 进 dead 状态时发告警。
  - `bot_token` Fernet 加密入库，GET 返 `has_token: bool`，永远不返明文（约定 D）
  - 是 Bot Token（@BotFather 创建）走 HTTP API，与 userbot session 完全独立
  - 前端 `Settings/NotifyBots.tsx` 提供 CRUD + 一键测试
- **插件开发指南**：`docs/PLUGIN-DEV-GUIDE.md` 含目录骨架、Manifest 字段、Plugin/Context hook、风控接入、Telethon 速查、沙箱权限说明。
- **首个插件样例**：`examples/plugins/translate/`——翻译插件，作为开发模板（不放进 builtin/）。
- **公网部署文档**：`docs/DEPLOY-PUBLIC.md` + `deploy/Caddyfile.example`（Let's Encrypt 自动证书 + HSTS + 写操作限速 + reverse_proxy）。

### Removed
- `group_admin` / `monitor` 两个 builtin 插件骨架（PRD 列出但实际不需要的功能；20 行占位删除）。
- 插件市场 UI（`PluginMarket.tsx` / `plugin_repo_service.py` / 远程订阅 API / zip 上传 API）：单用户场景没人订阅远端仓库；用 `git clone` 到 `data/plugins/installed/<key>/` 替代。
- `plugin_repo` 表（迁移 `0012_drop_plugin_repo`）。

### Changed
- `PluginManager.tsx` 简化为"已加载列表 + enable/disable + uninstall"。
- `Settings/Index.tsx` 移除 `<PluginMarket />` 嵌入；新增 `<NotifyBots />`。
- `main.py` lifespan 启动时尝试发项目启动通知；`supervisor.py` 在 account 进 dead 时发告警（NotifyBot 未配置时静默跳过）。

### Fixed
- alembic 0012 误写 `down_revision="0010"` 与 0011 构成分叉，汇总验收时修正为 `down_revision="0011"`，再次回到单 head。

---

## [0.3.1] — 2026-05-06 · Sprint 4 Wave 1

### Added
- 内置命令新增 `,del N`：撤回当前会话中自己最近 N 条消息（含命令消息本身）。
- 自定义命令模板新增 `aliases` 字段（迁移 `0011_command_aliases`），支持一个模板多个短命令入口。

### Changed
- Telethon 依赖升级到 `>=1.43,<2.0.0`。
- 内置命令支持短别名：`help(h)`、`status(s, st)`、`id(i)`、`version(v)`。
- `,help` 输出改为折叠展示主命令与别名，并合并展示自定义模板别名。
- 命令模板设置页新增别名输入与展示列；保存时对别名格式和冲突做校验。
- 前端版本号改为 `frontend/src/lib/version.ts` 单点定义，export const APP_VERSION = "0.7.1";

### Fixed
- 自定义命令名/别名冲突规则统一：同账号维度下，模板 `name + aliases` 不可互撞，也不可与内置命令及其别名冲突。

## [0.3.0] — 2026-05-05 · Sprint 3 · LLM 重构 + 安全加固

### Added — LLM Provider 体系大升级
- **多模型支持**（迁移 `0008`）：`llm_provider.models` JSONB 数组，单 provider 可启用多个模型；老数据自动把 `default_model` 回填到 `models[0]`。
- **API 协议解耦**（迁移 `0009`）：新增 `llm_provider.api_format`，独立于 provider 厂商：
  - `chat_completions`（OpenAI 经典 `/v1/chat/completions`）
  - `responses`（OpenAI 2024 新协议 `/v1/responses`，国内反代如 anyrouter 只接这个）
  - `anthropic_messages`（Anthropic `/v1/messages`）
  - 老数据按厂商自动回填；同一个反代可改协议而不改 provider。
- **LLM 走代理**（迁移 `0007`）：`llm_provider.proxy_id` 外键，支持调用 LLM 时走指定 SOCKS5/HTTP，绕开网络封锁。`mtproxy` 类型 schema 层拒绝。
- `services/llm_client.py` 重构：`OpenAIClient` / `ResponsesClient` / `AnthropicClient` 三客户端，按 `api_format` 路由。错误信息统一带 host 让用户一眼看出是哪个反代不通。
- `services/llm_format.py`（新建，336 行）：输出模板渲染器
  - `{key}` 占位符 + `{?key}...{/?}` 条件块（未知 key 留空，不抛 KeyError）
  - 派生变量 `answer_first_2` / `answer_rest`，配合 `<blockquote expandable>` 实装"前两行 + 折叠"
  - 默认走 HTML（telethon 1.36 不支持 markdownv2）
  - 4000 字符截断（TG 单条 4096 上限留余量）

### Added — 安全加固
- **JWT 版本校验**（迁移 `0010`）：`web_user.pwd_version` 默认 0
  - JWT payload 加 `pwd_v`
  - 改密后 `pwd_version += 1`
  - 旧 token 上的 `pwd_v` 不匹配自动失效
  - 这是 0.2.1 SECURITY-OPS 里"改密后强制下线"的真正实装；之前只是清当前 cookie，旧 token 在另一台设备上还能用

### Added — 命令模板编辑增强
- 新增 4 个预设输出模板（simple / quote / minimal / translate）一键插入。
- 占位符按钮 + 条件块按钮 → 不会写模板的用户也能拼出消息格式。
- ProviderModelSelect / ProviderModelsSection：在多模型 provider 里展开式选具体 model。

### Changed
- LLMProviders 页改造：API format 选择 / 模态 / tag / cost_tier / 模型列表管理。
- CommandTemplates 页改造：输出模板可视化编辑、provider+model 双层选择。

### Notes
- 升级路径：`alembic upgrade head` 一次跑齐 0007 → 0008 → 0009 → 0010。
- Sprint 2 完成 (0.2.0) 时 LLM 是骨架级（仅 OpenAI/Anthropic 直连 + 单模型 + chat_completions 写死），本次相当于把这条线做到生产可用级别。

---

## [0.2.1] — 2026-05-03 · Sprint 2 hotfix

### Fixed
- alembic 0003 分叉（`0003_command_template` 与 `0003_add_device_profile` 同号同 down_revision）线性化为 `0003 → 0003b`，`alembic upgrade head` 现在能跑通。
- 自动回复"突然不工作"——根因是 0006 给 `llm_provider` 加了 `modality` / `tags` / `cost_tier` / `notes` 但生产 PG 没升级；worker 启动时 `select(LLMProvider)` 报 UndefinedColumnError 导致 supervisor 把账号置 dead。修复后跑了 `alembic upgrade head`。
- `test_accounts.py` 的 `AsyncMock(db)` fixture 没跟上 `device_profile.get_default` 的引入，9 个 confirm 系列测试失败；新增 `_stub_device_profile` autouse fixture 修复。

### Added
- 日志中心拆成「消息日志 / 系统日志」两个 tab：
  - **消息日志** (`source=event`)：incoming 消息事件 / plugin 命中 / 命令派发，排查"为什么没回复"用
  - **系统日志** (`source=system`)：worker 启停 / IPC reload / 风控状态 / 技术异常，排查"账号是不是真的活着"用
  - 后端 `/api/logs/runtime` 加 `source` 过滤参数，自动兼容历史 `worker` / `plugin` 旧值
- `worker/runtime.py` `_log()` 与 `worker/plugins/loader.py` `_log()` 增加 `source` 关键字参数（默认 `system` / `event`）。
- `RecentPeersResponse`：`fetch_recent` 拆 `(worker_alive, items)`，前端可精准引导"worker 离线 vs 没收到消息"两种空状态。

### Changed
- 忽略 tab 在最近活跃为空时显示 worker 在线/离线徽章 + 三态精准提示文案。
- 风控基础动作中文标签全部对齐后端 `_DEFAULTS`（之前 8 个 key 名错位 + 6 个未覆盖）。
- 详情页 tg_user_id 缺失警示条上加"重启 worker 同步"按钮（pause → 1s → resume）。

---

## [0.2.0] — 2026-05-03 · Sprint 2

### Added
- 自定义命令模板（reply_text / forward_to / ai / run_plugin）+ `,help` 自动列出。
- LLM Provider 抽象（OpenAI / Anthropic，API key Fernet 加密入库，仅 worker 进程内解密）。
- 忽略群组：per-account `ignored_peers`、worker `recent_peers` LRU、前端一键加入。
- 账号头像（懒加载、缓存 24h）+ Humanize 子区。
- 插件模块化 tier C：Manifest + zip 安装 + 仓库订阅 + 权限沙箱。
- 转发插件（4 mode：原生 / 复制 / 引用 / 仅链接）。
- Web 端：修改密码 / 禁用 TOTP。
- KillSwitch 全局红色横幅 + AccountStatusBadge 复合状态。
- 风控每个动作显示中文标签 + 一句话说明。
- 内置命令：`,version`：在 TG 内查看版本与运行环境。
- `docs/SECURITY-OPS.md` 生产部署安全清单。

### Changed
- 账号详情 "忽略" tab 改名 "忽略的群组"，最近活跃为空时显示精准引导。
- "出口/代理" tab 改名 "出口/伪装"。
- 老账号 `tg_user_id` / `tg_username` 缺失时给出"重启 worker 自动同步"提示。
- `backend/app/main.py` 的 `FastAPI(version=...)` 改为读 `app.__version__`，避免与 `__init__.py` 不同步。
- 前端版本号改为 `frontend/src/lib/version.ts` 单点定义，Sidebar 通过 `APP_VERSION_LABEL` 引用。

### Fixed
- 紧急停用后 UI 状态联动（之前 banner 不存在 + badge 还显示运行中）。

---

## [0.1.0] — 2026-04-xx · MVP / Sprint 1

### Added
- Telethon 客户端初始化。
- 登录向导（验证码 + 2FA）。
- Worker 子进程模型（mp spawn context + atexit/SIGTERM 守护）。
- IPC pub/sub（Redis）。
- 自动回复（关键词 / 正则 / 作用范围 / 冷却 / 白黑名单 / reply_to）。
- 风控引擎（18 actions × 5 policies × 3 层继承）。
- FloodWait / PeerFlood / SlowMode 异常映射。
- 代理库 + 连通性测试。
- 顶部网络环境徽章（IP / 国家 / 缓存 5min）。
- 内置命令：`,help` `,id` `,status` `,ping` `,pause` `,resume`。
- 两轮 review 修复（21 处真实改动，详见 `REVIEW-FIXES-REPORT.md`）。

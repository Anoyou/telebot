# 树形组合化改造方案（Tree Composability Round）v1.1

> 2026-08-14 定稿；同日 v1.1 修订（T1 卡点裁定）：核实 kill_switch 为"全停"级开关而非发送级闸 → T1/T3 施工顺序对调、值守语义改为"暂停叶子投递"（详见 §4 WP-T1/WP-T3、§5、§8）；v1.1a：值守观测口径裁定（枝关闭优先）；v1.1b：T4 声明表口径 10 叶（codex_image 残留目录出表，见 §4 WP-T4）。
> 来源：DeepSeek Harness（Cordis"一切皆插件"+ 运行模式）设计思想对照 TelePilot 现状的完整讨论，结论收敛为本方案。
> 本文是**派工文档**：每个 WP 可单独交给一个 worker 施工。施工前必读 §1 词汇表与 §2 全局纪律。
> ⚠️ 项目铁律：**计划文档 ≠ 代码现状**。本文引用的行号是 2026-08-14 快照，动手前必须 grep 复核。

---

## §1 树的词汇表（理解本方案的钥匙）

TelePilot 的目标架构用一棵树描述。**改造原则：能力皆可组合，安全边界不可替换**（不是字面的"一切皆插件"）。

| 树语 | 含义 | 对应代码 |
|---|---|---|
| **根系** | 内容采集：telethon 收消息、按需联网 | worker/tg_client.py 等 |
| **树干（树本体）** | 账号身份与安全、汇聚路由、插件加载器、动作审计与确认、风控限速、worker 监督与 IPC。**永不可选、永不可拆** | `platform_capabilities.py` docstring 已定义内核清单 |
| **粗枝** | 可选能力模块，只有部分叶子需要：`ai` / `interaction_bot` / `webhooks` / `ledger` / `dispatch_debug` | `services/platform_capabilities.py`（五模块 + starting/ready/quiescing/stopped/failed 状态机 + CMD_RELOAD_CONFIG 收敛） |
| **树叶** | 插件（builtin + installed） | `worker/plugins/`、`plugins/installed/` |
| **三种嫁接方式** | 叶子接到树上的三种方式：①**直通**（贴干长：manifest `capabilities.telegram_direct_passthrough` + 账号双开关，原始 userbot 事件先到、可 consume 截断后续链路）②**userbot 通道**（前缀命令，树自己的嘴回复）③**交互 bot 通道**（关键词/按钮，长在 interaction_bot 枝上） | loader.py 直通分发段（快照行号 1216/1249/1779-1985 一带）；`manifest.py` |
| **修枝剪（Profile/预设）** | 有名字的开关组合，自上而下强制约束。**修枝剪优先级 > 叶子需求** | 本轮 WP-T1 新建 |
| **按叶点枝** | 叶子在 manifest 声明需要哪些枝（`requires_platform_capabilities`），系统据此自动点亮枝 | 字段已存在（`manifest.py`），但 13 个存量插件全部未填（已 grep 证实） |
| **钱随枝走** | 资金动作与 ledger 枝共存亡：枝断则一切资金动作 fail-closed 拒绝 | 本轮 WP-T3 新建（当前**不存在**此闸，已 grep 证实） |

核心定位纠偏（用户拍板）：TelePilot **不是资金系统**。资金是娱乐类叶子的副产物。树干要焊死保护的是**账号本身**（session 凭据、封号风险、行为审计），不是钱；钱的安全规则属于 ledger 枝。

---

## §2 全局纪律（每个 worker 必须遵守）

1. **不混轮**：交互框架三通道漂移类 bug（会话时序、信封嵌套、执行体 parity）不属于本轮。遇到只在 PR 描述里记录，不顺手修。
2. **fail-closed 默认**：所有新增闸门在"读不到状态/状态未知"时一律按"断"处理。
3. **向后兼容承诺**：不改三种嫁接方式的任何契约；存量已安装插件不强拆、不强制迁移（声明缺失只警告）。
4. **行号复核**：本文行号动手前必须重新定位。
5. **进度真相源**：每 WP 完成即在 `agent-plans/EXECUTION-STATE.md` 记 `done + commit 哈希`（沿用现有格式，本文末尾附可粘贴模板）。
6. **测试与运行**：以根目录 `AGENTS.md` / `CONTRIBUTING.md` 为准；插件相关测试沿用 FakeDB 惯例（见 `backend/app/tests/` 既有用例）。
7. **CHANGELOG 纪律**：每 WP 合入时在 CHANGELOG.md 记一条用户可读条目。

## §3 明确不做（Non-Goals，防跑偏）

- ❌ 不引入 Cordis / 不重写 `loader.py`（约 8900 行，保持原样）
- ❌ 不改多进程模型（supervisor 每账号子进程 + Redis IPC 保持）
- ❌ 不把结算/补偿代码物理搬出内核（WP-T3 只加闸，"代码在但不通电"）
- ❌ 不做账号级/会话级 Profile（只做全局部署级）
- ❌ 不做 Profile 自由编辑器（只有官方内置预设）
- ❌ 不做枝的"自动休眠"（本轮只自动点亮 + 提示可关）
- ❌ 不开放 AI Provider 插件化（等 llm_router 契约硬化后另立轮次）
- ❌ 不动 userbot 通道 / 直通 / 交互 bot 通道的语义契约

---

## §4 工作包

### WP-T1 值守预设（修枝剪第一把）——预估 2~5 天（v1.1：依赖 WP-T3 先行）

**树语**：给工作台挂一把修枝剪。值守模式 = 不管叶子要什么，强制断掉所有枝和出站动作，只留树干观测。

**现状**：`kill_switch_service.py` 有 `get_enabled/set_enabled/converge_runtime`；五模块开关与收敛编排在 `platform_capabilities.py`；`config_bundle_service.py` 有导出/dry-run 纯函数可参考。**没有**"命名组合一键应用"。**已核实（v1.1 卡点）**：`converge_runtime(enabled=True)` = `supervisor.stop_running_workers()` + 停 account/interaction bot manager——kill_switch 是"全停核按钮"（worker 下线、观测中断），**不是**发送级闸；值守不得用它兜底。kill_switch 保留为独立的更高一级紧急手段，与值守并存。

**要做**：
1. **首任务（必须先做）**：kill_switch 作用面已核实（见上），首任务改为——核实"暂停叶子投递"的机制落点：worker 的 pause 原语（`CMD_RESUME` 对偶的暂停指令、runtime 中 `paused` 事件的真实极性与覆盖面）、scheduler facade 任务能否随暂停冻结；写出"值守目标态 → 机制"映射表，发裁定人确认后再写实现。
2. 新建 `services/runtime_profile_service.py`：
   - `PRESETS` 硬编码 dict（本轮只有 `production`=全开基线 与 `safe_watch`=值守）。
   - `apply(preset_key, operator)`：先把当前各开关状态快照存 SystemSetting（`profile_rollback_snapshot`），再落开关、触发收敛、写审计。
   - `restore(operator)`：按快照精确还原。
   - `dry_run(preset_key)`：返回 diff 列表（将改哪些开关、从什么到什么）。
3. **值守目标态（结果规格，v1.1 修订已拍板）**：`kill_switch=false`，userbot 保持在线；**暂停对全部叶子的事件投递**（直通 / userbot 通道 / 交互 bot 通道三路一律不投）并冻结插件定时任务——从源头掐断一切插件出站（含 `telegram_native_raw` 叶：不投递即不执行，无需逐条封 facade）；平台自身观测落库、日志、告警通知（notify）照常；资金动作零执行由 **T3 枝闸**保证（值守把自己注册为 T3 判定的 deny 原因，故本 WP 依赖 T3 先行）；ai / webhooks / interaction_bot 枝停，dispatch_debug 留。**v1.1a 裁定（枝关闭优先）**：interaction_bot 按现有模块语义完全停止（polling manager 停；Telegram 侧未取更新由 Bot API 保留、恢复后续取），**不新增"停用但采集"的第三态**；值守期间的实时观测承诺只覆盖树干的眼睛（userbot 来源的直通与命令两路）；交互 bot 盲区必须在进入审计与工作台值守状态卡片中明示。
4. API：`POST /api/platform/profile/apply|restore|dry-run`、`GET /api/platform/profile`（当前状态与预设精确匹配则显示预设名，否则显示"自定义"）。
5. 前端：平台能力页加"运行模式"卡片——当前模式、值守一键按钮（确认弹窗内展示 dry-run diff）、恢复按钮。

**验收（v1.1a）**：集成测试证明值守下 userbot 来源两路（直通 / 命令）入站均被平台观测落库但**零投递到插件、零出站、零 payout**（payout 拒绝断言引用 T3 闸的错误码）；交互 bot 通道改为模拟入口验证——update 若抵达 `_handle_interaction_update()`，值守下只观测落库不投递（纵深防御），并断言 manager 已随枝停止；声明 `telegram_native_raw` 的叶在值守下不被调用；恢复后快照逐项还原、投递/定时任务/interaction_bot 采集恢复；进入/退出各一条审计（含操作者，进入审计记录"interaction_bot 采集已停"）；收敛超时有明确失败态而非假成功。

### WP-T2 接线规范（协议先行，运行时缓行）——预估 2~3 天，可与 T1 并行

**树语**：树上现在有三套各自为政的"登记表"（插件 loader、System Agent ToolRegistry、平台能力模块）。本 WP 只统一**表格格式**（协议），不成立新机构（不建统一运行时——那要等第二个接入方出现）。

**要做**：
1. `docs/architecture/capability-protocol.md`（中文）：命名约定（plugin key / feature key / module key / tool name）；依赖声明（叶→枝 `requires_platform_capabilities`，叶→叶 `requires_features`）；生命周期状态机（沿用 starting/ready/quiescing/stopped/failed）；注册所有权（谁注册谁负责 unload 清理；generation 失效惯例，对齐 `registry.py` ToolRegistry 与 loader 的 generation）；fail-closed 原则。
2. `backend/app/services/capability_protocol.py`：`typing.Protocol` 接口定义（如 `CapabilityModule` / `Registrable` / `Disposable`），≤150 行，**禁止任何运行时机制**。
3. 文档中为三套现有注册表各写一节"符合性映射与差距"（只写不改代码）。
4. CONTRIBUTING/开发指南加一条：新增注册面必须符合本协议。

**验收**：文档 + Protocol 类合入；零行为变化（不允许触碰现有三套注册表的代码）。

### WP-T3 钱的保险丝（fail-closed 资金闸）——预估 3~5 天 + 狠测（v1.1：提前至 T1 之前施工，不依赖 T1）

**树语**：让"钱随枝走"成真。今天关掉台账枝只关外壳（查询/统计/导出），结算引擎焊在内核永远待命——已 grep 证实 `action_core.py` / `ledger_service.py` / `payout_limit.py` / `payout_compensation.py` **零处**检查模块状态。

**要做**：
1. 定义单一判定函数（放 `platform_capabilities.py` 或新小模块）：`ledger_actions_enabled() = ledger 模块 ready 且无任何 deny 原因`，并附一个极简的进程内 deny-reason 注册点。本 WP 内置唯一原因"模块未 ready"；后续 T1 把"值守激活"注册进来（**本 WP 不依赖 T1**）。读不到状态 → False（纪律 2）。
2. 接闸点必须覆盖**四类面**（施工时逐一重新定位并在 PR 列清单）：E1 loader 直执行动作面（快照参考 `_apply_userbot_payout_action`，loader.py:3267 一带）、E2 `services/interaction/delivery.py` 的 payout 应用路径、E3 worker RPC 动作面、**E4 `worker/runtime.py` 的 payout 补偿扫描/重放循环**（`_periodic_payout_compensation_scan`，runtime.py:1649 一带；闸断时跳过扫描，义务保留 pending）；另加 `ledger_service` 暴露给上层的记账写口（防旁路）。
3. 拒绝行为统一：复用刚完成的错误语义收口风格（错误码族），审计记 `FAILED_CLOSED`，插件收到明确 error 而非静默丢弃。
4. **队列语义（已拍板）**：闸断时——新资金动作一律拒绝；已 record 未 settle 的既有义务保留 pending 不清除、不自动重试；闸恢复后由现有补偿/结算路径幂等处理**恰好一次**。
5. 台账查询/导出面继续跟随现有 ledger 开关，不在本 WP 范围。

**验收**：四类面 × 闸开/闸断 矩阵回归测试；quiescing 切换窗口内新请求拒绝；进程重启后闸状态仍生效（含"读不到状态按断处理"用例）；恢复后 pending 义务恰好结算一次（幂等用例）。**本 WP 测试标准从严，宁可多写。**

### WP-T4 声明补齐 + 按叶点枝 + 看树视图——预估 1~2 周

**树语**：让"枝按叶的需求自动生长"成真，并给树装一面镜子。

**要做**：
1. **补作业**：为 builtin 中仍被注册的插件（`forward`、`scheduler`）与 8 个有源码的 installed 插件（bot_mute_guard、dice_grid_hunt、guess_number、lottery_plus、poetry_blank、pt_promote、redpack-byRBQ、sum）补 `requires_platform_capabilities` 声明。逐个读代码判断，PR 里每插件一行理由（如 lottery_plus → ledger+interaction_bot）。被 `_NON_CORE_BUILTIN_COMPAT_KEYS` 排除的目录不动。**v1.1b 勘误**：`codex_image` 本地仅剩 `__pycache__` 残留、源码已迁外部插件仓库——按残留目录处理不入表（同 feature_registry 对 compat 残留的哲学：残留目录不算代码）；其声明随外部仓库下次发版携带，由本 WP 的新装/升级 schema 校验强制。推导器与看树视图必须容忍"DB 已装但本地无源码"的叶：标记源缺失 warning、不参与 demand 计算、不崩溃（codex_image 残留即现成测试用例）。**v1.1c 声明落点口径**：TelePilot 仓库内只提交被 Git 跟踪的叶（builtin `forward`/`scheduler` + 历史特例 `lottery_plus`）；其余 7 个 installed 插件的声明写入本机运行时副本即可生效，**禁止 `git add -f` 扩大仓库跟踪范围**（`plugins/installed/*` 属部署态）。声明的规范落点是独立插件库：按已批准声明表在插件库适配并发版（含 codex_image），缺声明的升级会被本 WP 的 schema 校验拦截（设计内自愈）。T4 交付新增：插件开发指南补《requires_platform_capabilities 声明》章节（五模块含义、何时声明、缺失后果：存量仅 warning、新装/升级拦截）。
2. **schema 强制**：`schemas/plugin.schema.json` 增加该字段校验（enum 限五值，允许空数组=什么枝都不要）；`plugin_install_service` 对**新装/升级**强制"字段必须存在"；devkit 脚手架模板默认带字段。存量已安装未声明 → 插件中心 warning badge，不拦截。
3. **推导器**：`platform_capabilities` 增加 `compute_demand()` → `{module: [依赖它的插件 key]}`（跨账号取并集）。启用/安装插件时所需枝未开 → **默认自动点亮**并写审计（"因 X 需要，自动启用 Y"）；若该枝被预设/管理员强制关（引入 `forced_off` 标记语义，修枝剪优先）→ 启用失败并返回明确错误。枝开着但无叶需要 → 工作台提示"可关"，**不自动关**。
4. **看树视图**：`GET /api/platform/tree` 返回 `{trunk: {userbot 状态, kill_switch, 当前 profile}, branches: {模块: {state, demanded_by}}, leaves: [{key, 嫁接方式(直通/命令/交互), enabled, requires}]}`；工作台简单渲染（对标 dsh 的 `--dump-config`：一眼说清这台机器跑的是什么树）。
5. 可选加分项：System Agent 已有 features 工具面的话，顺带暴露只读"看树"查询。

**验收**：端到端——全新部署只启用 `sum` 型工具叶 → 看树显示台账枝灭、资金 API 拒绝（依赖 T3）；启用 `lottery_plus` → ledger+interaction_bot 自动点亮且有审计；值守激活时启用游戏插件被拒并提示原因；10 叶（forward、scheduler + 8 有源码 installed）声明齐全、新装缺声明被 schema 拦截；源缺失残留叶显示 warning 且不参与 demand。

### WP-T5（挂起，勿开工）AI 写操作工具开闸

只读桥已在 0.95.0 建成（`system_agent/plugin_tools.py`：manifest.agent_tools → ToolRegistry 动态注册 + IPC 执行 + 外部文本标记，硬约束 read_only=True）。**扳机条件**：出现第一个真实需要写操作工具的插件需求时，另立 WP——写语义必须走 PreparedAction 确认流。在那之前不做。

---

## §5 顺序与依赖

```
WP-T3 钱的保险丝 ──→ WP-T1 值守预设 ──→ WP-T4 按叶点枝
     │（v1.1 对调：值守的"零 payout"由 T3 闸保证，值守注册为 T3 的 deny 原因；T4 的 forced_off 依赖 T1 预设语义）
WP-T2 接线规范（全程可并行，与任何 WP 无代码交集）
WP-T5 挂起
```

推荐派单：T3 与 T2 同时开两单；T3 验收后开 T1；T1 验收后开 T4。每单提示词 = 本文件 §1+§2+§3 + 对应 WP 小节。

## §6 整轮验收（Definition of Done）

1. 工作台一键值守/恢复可用，审计可查，三嫁接方式零出站验证通过。
2. ledger 枝断时三执行体资金全拒的回归测试常绿，且默认 fail-closed。
3. 10 叶声明齐全（codex_image 残留出表，见 §4 WP-T4 v1.1b）；新装插件缺声明被拦；按叶点枝 + 看树视图上线。
4. 协议文档与 Protocol 类合入，零行为变化。
5. CHANGELOG 逐 WP 记录；EXECUTION-STATE.md 本计划分节全部 done + commit 哈希。

## §7 EXECUTION-STATE.md 粘贴模板（启动施工时贴入）

```markdown
## 当前计划：树形组合化改造 v1（agent-plans/2026-08-14-tree-composability-plan.md）

- [ ] WP-T3 钱的保险丝 — pending（v1.1 先行）
- [ ] WP-T2 接线规范 — pending（可并行）
- [ ] WP-T1 值守预设 — pending（依赖 T3）
- [ ] WP-T4 声明补齐+按叶点枝+看树视图 — pending
- [~] WP-T5 AI 写操作工具 — 挂起（扳机：首个真实写工具需求）
```

## §8 附录：本方案依据的已核实代码事实（2026-08-14 快照）

- 五模块 + 状态机 + 内核清单：`backend/app/services/platform_capabilities.py`（docstring 明确"userbot、插件加载器、Action/审计/结算/补偿属于平台内核"）
- 总闸作用面**已核实（2026-08-14 T1 卡点，advisor 复核）**：`converge_runtime(true)` → `supervisor.stop_running_workers()` + 停 account/interaction bot manager（kill_switch_service.py:47、supervisor.py:778 一带快照）= 全停级，非发送级闸
- worker 在线时资金可出的两条平台路径（T3 的 E1/E4 依据）：loader.py:3267 一带 `_apply_userbot_payout_action`；runtime.py:1649 一带 `_periodic_payout_compensation_scan`（均不受 ledger_enabled 约束）
- 资金路径零模块闸：grep `platform_capabilities|module_enabled|ledger_enabled` 于 action_core/ledger_service/payout_limit/payout_compensation → 零命中
- `requires_platform_capabilities` 字段存在但 13 插件零填写：grep 于 `plugins/installed/*/manifest.py` + `worker/plugins/builtin/*/manifest.py` → 零命中
- agent_tools 只读桥已建成：`backend/app/services/system_agent/plugin_tools.py`（含 IPC 执行、`mark_external_text` 防注入、脱敏）
- ToolRegistry：`backend/app/services/system_agent/registry.py`（register/unregister/generation）
- 直通链路：`backend/app/worker/plugins/loader.py`（"直通只适用于 userbot 来源"、consume 截断、权限感知包装、独立 messages facade 复制）
- 插件级热重载：loader `reload_plugin(account_id, plugin_key)`
- builtin 扫描与 compat 排除清单：`backend/app/feature_registry.py`
- 收付款强制走 userbot、离线不降级：`services/interaction/delivery.py`（记忆记录 + 回归测试锁定，施工时复核）

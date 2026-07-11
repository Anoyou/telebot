# 日志中心重构执行计划（一页式消息漏斗）

> 状态：待实施。基于 `codex/0.33-interaction-framework` 分支源码核对。
> 目标：把日志中心从"6 个专家 tab + 埋在 trace 详情里的四段检查"重构成
> **一页式消息流**——顶部筛选、每行直接显示"收到→匹配→执行→发送"漏斗、
> 一眼区分"卡住 / 正常没响应 / 失败"。
> 原则：**不推倒重来，拍平呈现层**。数据层（EventTrace/Span/Action 三表）不动。
> 纪律：先锁后改（判定逻辑先补测试再搬），每步可独立验证，收尾四件套。

---

## 核心判断（为什么这次工作量比看起来小）

用户要的能力**代码里几乎都已存在**，只是被埋起来、且判定散在前端：

- `buildQuickCheckStages`（收到/路由/发送四段）——已存在于 `frontend/src/pages/Logs.tsx:1957`，但只在点开单个 trace 详情后才显示。
- `buildTraceDiagnosis`（卡点判定 + 大白话结论 + 下一步）——已存在于 `Logs.tsx:2078`，前端逐条算。
- `reasonDisplay`（reason_code → 中文，覆盖 ~60 个 code）——已存在于 `Logs.tsx:2997` 附近。
- `list_event_traces`（`backend/app/api/logs.py:477`）已有全套筛选：account/source_channel/event_type/chat_id/message_id/update_id/sender_user_id/plugin_key/status/trace_id/reason_code/keyword/since/until/limit。
- reason code 权威集合：`backend/app/services/event_bus.py:64` `EVENT_REASON_CODES`。

**所以这次做三件事：**
1. 把前端的漏斗+判定逻辑搬到后端，成为**唯一判据**（现在前后端两套，会漂移）。
2. 新增一个 `GET /api/logs/messages` 聚合端点，每条 trace 直接返回算好的漏斗 + verdict + 卡点 + 大白话 + 下一步。
3. 前端砍掉 6 个 tab，拍平成一页筛选 + 每行漏斗，移动端（390px）与桌面同等可用。

---

## 已定决策（用户已拍板，不要再问）

1. **旧的纯 tab 端点一并清掉**（不是保留降级）。要清的 5 个：
   - `GET /api/logs/trace/overview`（`logs.py:411`）
   - `GET /api/logs/trace/plugins`（`logs.py:606`）
   - `GET /api/logs/trace/plugins/{plugin_key}`（`logs.py:626`）
   - `GET /api/logs/trace/actions`（`logs.py:657`）
   - `GET /api/logs/trace/commands`（`logs.py:696`）
2. **删掉前端旧判定函数**（`buildQuickCheckStages`/`buildTraceDiagnosis`/`buildPluginDiagnosis` 等），前端只信后端返回的 verdict/funel/reason_text/next_step。
3. **移动端（390px）与桌面同等可用**——不是缩水版。

**保留不动的端点：**
- `GET /api/logs/trace/events`（`logs.py:477`，列表 + 全套筛选）——新端点复用它的筛选逻辑。
- `GET /api/logs/trace/events/{trace_id}`（`logs.py:551`，单条详情 + spans + actions + runtime logs）——每行展开时用。
- `GET /api/logs/runtime`（`logs.py:737`）、`GET /api/logs/audit`（`logs.py:353`）——独立日志，与消息漏斗无关，保留。

---

## 删除前的引用清单（Codex 删端点/函数前逐一 grep 确认）

**前端 API 层集中在 `frontend/src/api/system.ts`（不是散在 Logs.tsx）：**
- `getTraceOverview` → `/api/logs/trace/overview`（system.ts:120）
- `getTracePlugins` → `/api/logs/trace/plugins`（system.ts:145）
- `getTracePluginDetail` → `/api/logs/trace/plugins/{key}`（system.ts:156）
- `getTraceActions` → `/api/logs/trace/actions`（system.ts:176）
- `getTraceCommands` → `/api/logs/trace/commands`（system.ts:185）

  这 5 个前端 api 函数随对应端点一起删。**先 grep 确认它们只被 Logs.tsx 引用**，别处引用要先处理。

**后端测试引用（清端点时同步改，不能只删端点留测试）：**
- `backend/app/tests/test_logs_api.py:12-13` 引入了 `list_event_traces`、`trace_overview`。
- `test_trace_overview_filters_failed_actions_by_account_id`（`test_logs_api.py:137`）测的就是 `trace_overview`——这个函数删了，测试要删或改写。
- `test_list_event_traces_filters_by_trace_and_reason_code`（`test_logs_api.py:94`）测 `list_event_traces`——**这个保留**（端点保留）。

**路由/导航（不动）：**
- 侧栏 `frontend/src/components/layout/Sidebar.tsx:46` `/logs` 入口不变。
- `frontend/src/App.tsx:26,238` 懒加载 `Logs` 不变。页面内部重写，路由壳不动。

---

## 执行顺序（串行，每步独立验证）

1. **步骤 1 — 后端判定逻辑 + 单测（先锁）**
2. **步骤 2 — 后端聚合端点 `/api/logs/messages`**
3. **步骤 3 — 前端 API 层收敛（system.ts）**
4. **步骤 4 — 前端一页式重写（Logs.tsx）**
5. **步骤 5 — 清理后端旧端点 + 同步改测试**
6. **步骤 6 — 收尾验证四件套 + 手机端冒烟**

> 顺序理由：先把新判定+端点建好并测试锁住（1-2），再让前端切过去（3-4），
> **确认前端完全不依赖旧端点后，最后才删旧端点（5）**。这样任何一步失败都能独立回滚，
> 且不会出现"端点已删、前端还在调"的中间断裂态。

---

## 步骤 1 — 后端判定逻辑 + 单测

**新建** `backend/app/services/log_funel.py`（判定纯函数，无 DB 依赖，方便测试）。

把前端 `buildQuickCheckStages` + `buildTraceDiagnosis` 的逻辑翻译成 Python 纯函数：

输入：一条 trace 的 spans（含 phase/status/reason_code）+ actions（含 status/error_code）+ trace.status。
输出：

```python
@dataclass
class MessageFunel:
    received: str   # "pass"            （有 receive span 即 pass）
    routed:   str   # "pass"|"skip"|"fail"（skip=subscription_not_matched 等正常跳过）
    ran:      str   # "pass"|"stuck"|"skip"|"fail"
    sent:     str   # "pass"|"fail"|"none"（none=没有发送动作）
    verdict:  str   # "responded"|"no_response_normal"|"stuck"|"failed"
    stuck_at: str | None      # "routed"|"ran"|"sent"|None
    reason_code: str | None
    reason_text: str          # 大白话（复用 reason 中文映射）
    next_step: str            # 下一步建议
```

**判定规则（对齐现有前端逻辑，务必逐条核对 `Logs.tsx:1957` 与 `:2078`，行为不能变，只是换语言换位置）：**

- `verdict=failed`：有失败 action（`EventAction.status in (failed,error)`）或 trace.status in (failed,error) → 卡点定位到 sent 或对应 phase。
- `verdict=no_response_normal`：路由阶段是 `subscription_not_matched` / `event_type_not_subscribed` / `source_not_subscribed` / `scope_not_matched` / `filter_not_matched` / `command_not_matched` 等"没插件关心"类 reason → routed=skip，这**不是故障**，reason_text 要明确写"这不是故障：没有插件关心它，正常跳过"。
- `verdict=stuck`：有 plugin_invoke span 但无对应完成 span / 无 action 且 trace 未正常结束（对齐前端"消息进入了插件但没进入任何处理完成阶段"的判定，`Logs.tsx:1898` 附近）→ ran=stuck。
- `verdict=responded`：正常走完，有成功 action 或正常 finish。

**reason 中文映射**：把前端 `reasonDisplay` 那份 code→中文表（`Logs.tsx:2997` 起）搬到后端一处（可放 `log_funel.py` 或 `event_bus.py` 旁），前后端共用一份来源。**注意**：前端删除旧 `reasonDisplay` 后，中文文案由后端 `reason_text` 直接给，前端不再自己映射。

**新建测试** `backend/app/tests/test_log_funel.py`，至少覆盖四种 verdict 各一例 + 边界：
- responded（有成功 action）
- no_response_normal（subscription_not_matched）
- stuck（有 plugin_invoke 无完成/无 action）
- failed（失败 action / trace failed）
- 直通消息（第一轮补的 direct passthrough trace：receive/route/plugin_invoke span）能被正确判为 responded 或 stuck——**这是第一轮补 trace 的验收点，务必加这一例**。

**验证**：`cd backend && .venv/bin/python -m pytest app/tests/test_log_funel.py -v`

---

## 步骤 2 — 后端聚合端点

在 `backend/app/api/logs.py` **新增** `GET /api/logs/messages`：

- **复用** `list_event_traces` 已有的全部筛选参数（account_id/source_channel/event_type/chat_id/message_id/update_id/sender_user_id/plugin_key/status/trace_id/reason_code/keyword/since/until/limit）。可以抽一个共享的 query builder，避免复制粘贴筛选逻辑。
- 额外加一个 `verdict` 筛选参数（`responded|no_response_normal|stuck|failed`），对应前端"结果快筛"。verdict 筛选在 Python 侧对已取出的行过滤即可（避免复杂 SQL）；或先按现有条件取行、算 funel、再按 verdict 过滤。
- 对每条 trace：取其 spans + actions（注意 **N+1 问题**——用一次批量查询按 trace_id 分组，不要每行一次查询），调 `log_funel` 算出漏斗，拼进响应。
- response_model 新增 `MessageFunelItem`（含上面 MessageFunel 的字段 + trace 基本信息：trace_id/ts/account_id/source_channel/chat_id/chat_label/sender/text_preview）。

**性能**：默认 limit 100，批量取 span/action 后在内存里 group by trace_id。参考现有 `_trace_summaries_with_counts`（`logs.py` 内）已有的批量计数模式，别退化成逐行查询。

**验证**：新增端点测试（构造几条 trace+span+action，断言 verdict/funel 正确）；`pytest -k "logs or funel"`。

---

## 步骤 3 — 前端 API 层收敛

`frontend/src/api/system.ts`：

- **新增** `getMessageFunel(params)` → `GET /api/logs/messages`，TS 类型 `MessageFunelItem` 对齐后端 response_model。
- **保留** `getEventTraces`（列表，可能仍用于某些筛选）、`getEventTraceDetail`（单条详情，展开行时用）、`getRuntimeLogs`、`getAuditLogs`。
- **删除**（连同类型）：`getTraceOverview`、`getTracePlugins`、`getTracePluginDetail`、`getTraceActions`、`getTraceCommands`。删前 grep 确认无 Logs.tsx 之外的引用。

**验证**：`pnpm --dir frontend typecheck`（删函数后若有残留引用会报错，据此清干净）。

---

## 步骤 4 — 前端一页式重写

`frontend/src/pages/Logs.tsx`（当前 3024 行，重写后应大幅精简）。

**目标布局（参考已确认的预览图）：**

```
┌─ 日志 · 消息流 ─────────────────────────────┐
│ 账号[全部▾] 时间[近1小时▾] 结果 ●全部 ○未响应 ○失败 ○已响应  │
│ 🔍 chat / 消息 / 发送者 / trace_id …                        │
├──────────────────────────────┤
│ 时间   会话      消息预览    收到 匹配 执行 发送  结果        │
│ ────────────────────────── │
│ 10:42  群·牛牛群 "24点 3345" ●─●        ✓已响应       │
│ 10:41  群·牛牛群 "开一局"    ●─●─◍··○   ⚠卡住         │
│ 10:41  群·吹水群 "哈哈"      ●─⊘         ·未响应(正常)  │
│ 10:40  私·@bob   ",pay 100"  ●─●─●─✕    ✕失败         │
└──────────────────────────────┘
  ●通过 ◍进行中/卡住 ⊘正常跳过 ✕失败 ○未到达
```

- **顶部筛选栏**：账号下拉、时间范围、结果快筛（全部/未响应/失败/已响应，对应 verdict）、搜索框（一个输入框，后端 keyword already 支持 chat/消息/发送者/trace_id 混合搜索）。
- **消息流列表**：每行左边基本信息，右边四个漏斗点（用后端 funel 的四段状态渲染颜色/图标），最右 verdict 徽章。
- **展开行**：点某行 → 展开该 trace 的详情。**复用现有 Timeline 组件**（`Logs.tsx:1078` 那套 span+action 时间线合并排序）和详情面板，数据来自 `getEventTraceDetail`。大白话结论/下一步直接用列表项已带的 `reason_text`/`next_step`（或详情端点回带）。
- **删除**：`buildQuickCheckStages`、`buildTraceDiagnosis`、`buildPluginDiagnosis`、`reasonDisplay`（中文改由后端给）、6 个 tab 的 tab 切换与各自面板、`LogToolGuide`。保留 Timeline、InfoCell 等纯展示组件。

**移动端（390px）同等可用：**
- 列表在窄屏改为卡片式：每张卡片顶部一行时间+会话+verdict 徽章，中间消息预览，底部四段漏斗横排。四个漏斗点在窄屏也要能看清状态。
- 筛选栏在窄屏折叠成一个"筛选"按钮展开抽屉，或纵向堆叠——不要横向溢出。
- 展开详情的 Timeline 在窄屏纵向排列。

**验证**：`pnpm --dir frontend typecheck` + `build` + 浏览器冒烟（桌面 1280 / 移动 390 无横向溢出，四段漏斗、结果快筛、展开详情、搜索均可用）。

---

## 步骤 5 — 清理后端旧端点 + 同步改测试

**只有在步骤 4 完成、前端确认不再引用旧端点后才做这步。**

- 删 `logs.py` 中 5 个端点函数：`trace_overview`、`trace/plugins`、`trace/plugins/{key}`、`trace/actions`、`trace/commands`。
- 删对应的 response_model 若无其它引用（`TraceOverview`、`PluginRuntimeStatusItem` 等——**先 grep**，`EventActionItem`/`PluginRuntimeStatusItem` 可能被详情端点复用，别误删）。
- 改 `test_logs_api.py`：删 `test_trace_overview_filters_failed_actions_by_account_id` 及对 `trace_overview` 的 import；**保留** `test_list_event_traces_filters_by_trace_and_reason_code`（端点保留）。
- **不删** `EventTrace`/`EventSpan`/`EventAction`/`PluginRuntimeStatus` 任何数据库模型或写入逻辑——只删读取端点。

**验证**：`pytest -k "logs or funel"` + 全量 `pytest`。

---

## 收尾四件套（每步适用，最后整体跑一遍）

1. 后端全量：`cd backend && .venv/bin/python -m pytest`
2. 触达文件 `ruff`（不顺手改无关旧文件的 import 排序）
3. 前端：`pnpm --dir frontend typecheck` + `pnpm --dir frontend build`
4. `git diff --check`
5. 手机端冒烟：本地 mock API 打开 `/logs`，390px 与 1280px 均无横向溢出，四段漏斗/结果快筛/展开详情可用。

沿用既往习惯：写 CHANGELOG `Unreleased`，不 bump 版本、不自动 commit，留给人合。

---

## 契约变更点（PR 描述必须显式列出）

1. **移除 5 个只读端点**（trace/overview、trace/plugins、trace/plugins/{key}、trace/actions、trace/commands）——若有外部脚本/收藏依赖会 404。自用项目可接受，但要写明。
2. **漏斗判定唯一来源变为后端**——前端不再自算 verdict/中文，行为若与旧前端有细微差异，以后端为准。步骤 1 的测试要锁住判定规则，避免语义漂移。
3. **reason code 中文文案来源从前端移到后端**——两端曾各有一份，现统一。

---

## 绝对不要做的事

- 不删/不改 EventTrace/Span/Action/PluginRuntimeStatus 表结构与写入逻辑（数据层不动）。
- 不动 `/api/logs/runtime`、`/api/logs/audit`、`/api/logs/trace/events`、`/api/logs/trace/events/{id}` 四个保留端点。
- 不动侧栏 `/logs` 路由与 App.tsx 懒加载壳。
- 不在步骤 4 完成前删旧端点（避免前端调用断裂）。
- 不顺手改无关旧文件的 ruff/import 问题。

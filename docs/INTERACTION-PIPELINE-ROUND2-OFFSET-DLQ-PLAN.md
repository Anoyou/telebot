# 交互链路修复 · 第二轮 #1 —— offset 失败 DLQ（死信持久化 + 可重放）

> 状态：已执行（codex，0.49.25）｜ 目标版本：0.49.25（patch）｜ 前置基线：0.49.24
> 类型：**扩展行为**（不推翻现有"失败仍推进 offset"契约），新增死信持久化与重放能力
> 作者：Claude（第一轮 bugfix 计划的延续，源自 `INTERACTION-PIPELINE-BUGFIX-ROUND1-PLAN.md` §6 第二轮预告 #1）

---

## 0. 这份计划要解决的真实问题（一句话）

交互 / 管理 / 转账三条 polling loop 里，一条**永久失败**的 update（插件持续抛异常、DB/Redis 处理异常等）在重试 3 次耗尽后，offset 仍会照常推进到 batch 内最大 update_id —— **消息被静默跳过、永久丢失**。尤其 `payment_confirmed`（付款到账通知）落在这条路上时，等于**丢钱事件无痕**。

## 0.1 核心设计决策（先读，决定整份计划的形态）

**不要把"失败就不推进 offset"当成方案。** 原因：Telegram getUpdates 的 offset 是单调游标，一条 poison update（永久失败）如果不推进，会把它后面所有 update 全部堵死 —— 一条坏消息瘫痪整个 bot。这比"丢一条"更糟。

**正确方向 = 推进照旧 + 失败落死信（DLQ）。** 即：
- 失败 update 重试耗尽后，**仍推进 offset**（现有行为正确，保留）；
- 但在推进之前，把这条**完整 update 原文 + 失败上下文**持久化到一个死信存储（DLQ）；
- 提供**列出 / 重放 / 丢弃**死信的能力，事后人工或自动补偿。

**这对现有固化测试的影响是"扩展"而非"推翻"：**
`test_interaction_polling_retries_failed_update_then_advances_offset_with_error`（`backend/app/tests/test_account_bot.py:4557`）现在断言：失败后 `handle.await_count == 3`（重试 3 次）、`_set_interaction_runtime_state` 仍带 `last_update_id=42` + `error` 含 boom、`_write_interaction_runtime_log` 被调一次。
→ **这些断言全部保留不动**（推进 offset 是对的）。只**新增**一条断言：失败 update 被写入 DLQ（即新的 `_record_polling_dead_letter` 被调用、带 update_id=42 和原文）。
→ 所以这不是"改契约撞车"，是"给同一行为补一层持久化"。风险等级低于第二轮 #2（session 独占）。

---

## 1. 现状锚点（0.49.24 实测，供 codex 核对）

全部在 `backend/app/services/account_bot_runtime.py`：

- **重试函数** `_handle_polling_update_with_retries`（1075）：`_POLLING_UPDATE_RETRY_DELAYS_SECONDS = (1.0, 3.0)`（122，即最多 3 次尝试），耗尽后调 `_write_polling_update_failure_log`（写运行日志）并 `return clean`（返回错误串，**不重抛**）。
- **交互 batch 处理** `_handle_interaction_polling_updates_batch`（1131）：按 chat 分桶并发，`batch_last_update_id = max(所有 update_id)`（1138，**与成功失败无关**），失败收集进 `batch_errors`，返回 `(batch_last_update_id, batch_error)`。
- **三条 loop 全部调用同一个 `_handle_polling_update_with_retries`**（0.49.24 实测，codex 已核实并纠正了本计划早前的错误描述）：
  - 管理 loop `_polling_loop`（1163）→ 在 **1214** 调 `_handle_polling_update_with_retries`。
  - 交互 loop `_interaction_polling_loop`（1354）→ 经 `_handle_interaction_polling_updates_batch` 在 **1148** 调 `_handle_polling_update_with_retries`。
  - 转账测试 loop `_transfer_test_polling_loop`（1415）→ 在 **1446** 调 `_handle_polling_update_with_retries`。
- **含义（关键）**：三条 loop 的"重试耗尽"是**同一个函数里的同一个点**（`_handle_polling_update_with_retries` 的 `return clean` 处，约 1115）。因此 **DLQ 写入只需在这一个函数里做一次**，三条 loop 各自只需把"我是哪条 loop / token 角色"作为参数传进来。不需要在三条 loop 里各复制一份 DLQ 逻辑（这是本计划早前版本的错误设想，已更正）。
- **offset 提交点**（各 loop 各自，均"batch_last_update_id 非空即推进"，与本次改动无关，仅供参考）：交互 1396 `_set_interaction_runtime_state`、管理 1236 `row.last_update_id`、转账 1460。
- **失败日志** `_write_polling_update_failure_log`：当前只写 runtime_log，**不持久化 update 原文**，无法重放。DLQ 写入紧挨它。
- **固化测试**：`test_account_bot.py:4557`（见 §0.1）。

---

## 2. 目标设计

### 2.1 DLQ 存储

用 **Redis** 存死信（与现有 `_claim_interaction_trigger` 等去重键同源，运维简单；自用单进程足够）。不用新建 DB 表，避免 migration。

**用两类 key（codex 校准，不要把整条 JSON 直接当 ZSET member）：**

```
account_bot:polling_dlq:{aid}:idx     # ZSET，member = "{loop}:{update_id}"，score = failed_at
account_bot:polling_dlq:{aid}:items   # HASH，field = "{loop}:{update_id}"，value = JSON
```

**为什么不能把整条 JSON 当 ZSET member**（codex 指出的三个坑）：
1. 删除某个 update 时得先拼出完整 JSON 字符串才能 `ZREM`，脆弱；
2. 重放失败后要更新 error/attempts → JSON 变了 → 成了 ZSET 里的新 member，旧的删不掉、重复堆积；
3. `update_id` 在三条 loop（三个不同 bot token）之间**可能相同**，单用 update_id 不唯一。

**复合 id = `dlq_id = "{loop}:{update_id}"`** 贯穿始终（idx 的 member、items 的 field、API 路径、删除/重放的定位键都用它）。`loop ∈ {interaction, management, transfer_test}`。

- 每条死信 JSON（存在 items HASH 的 value 里）：
  ```json
  {
    "dlq_id": "interaction:42",
    "aid": 1,
    "loop": "interaction",
    "update_id": 42,
    "update": { "...原始 update 完整原文..." },
    "error": "sanitize 后的错误串",
    "failed_at": 1720300000.0,
    "attempts": 3,
    "token_role": "interaction",
    "replay_attempts": 0,
    "last_replayed_at": null
  }
  ```
- **容量上限** `_POLLING_DLQ_MAX = 500`：写入后按 idx 的 score（failed_at）用 `ZREMRANGEBYRANK` 裁掉最旧的 dlq_id，**同时** `HDEL` items 里对应 field（两个 key 必须同步裁剪，别只裁 idx 留下 items 孤儿）。
- **敏感信息**：update 原文可能含用户消息文本。自用项目可接受原样存；但 token 绝不入 DLQ（结构里只存 `token_role` 不存 token）。

### 2.2 写入时机（三条 loop 已收敛到同一函数，落点唯一）

**关键事实（codex 校准，已实测 0.49.24）**：交互 / 管理 / 转账三条 loop **都调用同一个** `_handle_polling_update_with_retries`（分别在 1148 / 1214 / 1446 调用）。所以 DLQ 写入点**只有一处** —— 就在该函数重试耗尽、当前调 `_write_polling_update_failure_log` + `return clean` 的地方，追加一次 `_record_polling_dead_letter(...)`。**不需要在三条 loop 各复制一遍。**

为区分死信来自哪条 loop，给 `_handle_polling_update_with_retries` 增加参数：它已有 `loop_name`（"interaction bot" / 等字符串），再加一个规范化的 `loop`（`"interaction" | "management" | "transfer_test"`）和 `token_role`，三条 loop 调用处各传自己的标签。DLQ 记录用这个 `loop` 拼 `dlq_id`。

写 DLQ **必须包在自己的 try/except 里**：DLQ 写失败绝不能反过来影响主流程（否则 DLQ 自己成了新的丢消息点）。DLQ 写失败只记一条 log.warning，主流程照常推进 offset。

### 2.3 重放能力

新增内部函数 + API 端点（放 `backend/app/api/` 下现有交互/账号相关路由文件，codex 择位）。**所有定位都用复合 `dlq_id = {loop}:{update_id}`，不用裸 update_id**（跨 loop 撞号，codex 校准）：

- `_list_polling_dead_letters(aid, limit)` → 死信列表（按 failed_at 倒序）。
- `_replay_polling_dead_letter(aid, dlq_id)` → 从 DLQ 取出该条，重新走对应 loop 的 handler。
- `_discard_polling_dead_letter(aid, dlq_id)` → 直接删除该条（同步删 idx + items）。
- API：
  - `GET    /api/accounts/{aid}/bot/polling-dlq` → 列表（可返回分 loop 计数）
  - `POST   /api/accounts/{aid}/bot/polling-dlq/{loop}/{update_id}/replay` → 重放
  - `DELETE /api/accounts/{aid}/bot/polling-dlq/{loop}/{update_id}` → 丢弃

  （路径用 `{loop}/{update_id}` 两段拼出 dlq_id；`loop` 限定在 `{interaction, management, transfer_test}` 白名单，非法值返回 400。若 codex 偏好单段，可用 URL-safe 的 `{dlq_id}`，但冒号需转义。）
- **鉴权**：沿用现有账号 API 的鉴权中间件，不新开无鉴权端点。

**重放时 token 来源必须重新加载（codex 校准，DLQ 不存 token）：**
- `management` → 从 `AccountBot` 解密主账号 Bot token
- `interaction` → `_load_interaction_runtime_config(aid)`
- `transfer_test` → `_load_transfer_test_runtime_config(aid)`
- 若当前 token 缺失 / 功能已关 / 账号 Bot disabled → replay 返回明确错误，**不删除 DLQ 条目**。

**删除规则（codex 校准，避免"重放失败又丢一次"）：**
- replay **成功** → 删除该 DLQ 条目（idx + items 同步删）。
- replay **失败** → 保留，更新 `error` / `replay_attempts += 1` / `last_replayed_at`；**不改原始 `failed_at`**（保持在 idx 里的时间序位置）。
- replay **被取消 / token 缺失** → 保留，不改 `failed_at`，只更新最后错误信息。

### 2.4 可观测

- runtime 状态里已有 `interaction_last_error`。**新增死信计数**，让前端/`,version` 类命令能看到"有 N 条死信待处理"。非必须，但强烈建议 —— 否则死信静默堆积，失去"可见化"意义。
- **命名（codex 校准）**：DLQ 覆盖三条 loop，不只是 interaction，别用 `interaction_dlq_count` 这种窄名。最少给一个总计数 `polling_dlq_count`；若前端要分 loop 展示，再额外返回 `interaction_dlq_count` / `management_dlq_count` / `transfer_test_dlq_count` 分项。

---

## 3. 任务划分（可并行度）

DLQ 核心逻辑集中在 `account_bot_runtime.py`，API 在 `api/`，测试在 `test_account_bot.py`。文件重叠度高，**建议串行单 Agent 完成核心，再分出 API/测试**。若一定要并行：

- **Agent-R（核心，必须先做）**：§2.1 DLQ 存储原语（`_record_polling_dead_letter` / `_list` / `_replay` / `_discard`）+ 三条 loop 的写入点接线（§2.2）。这是地基，其他都依赖它。
- **Agent-A（API，依赖 R）**：§2.3 三个端点 + 鉴权。
- **Agent-T（测试，依赖 R）**：§4 测试。

> ⚠️ **文件归属提醒（给 codex）**：本计划集中改 `account_bot_runtime.py` + `test_account_bot.py` —— 这正是第一轮 bugfix 计划划为"禁碰"的两个文件（当时因为 offset 归属未定）。现在**明确交由 codex 执行**，Claude 第一轮不碰、本轮也不碰代码，只出计划。执行期间这两个文件的主权归 codex。

---

## 4. 测试要求

改 / 加在 `backend/app/tests/test_account_bot.py`：

1. **改造现有固化测试** `test_interaction_polling_retries_failed_update_then_advances_offset_with_error`（4557）：
   - **保留**：`handle.await_count == 3`、`_set_interaction_runtime_state` 带 `last_update_id=42` + error 含 boom、`_write_interaction_runtime_log` 调一次。
   - **新增断言**：mock `_record_polling_dead_letter`，断言它被调一次、参数含 `update_id=42`、`update` 原文含 message text "hello"、`loop="interaction"`、`attempts=3`。
   - 语义从"失败推进 + 记 error"升级为"失败推进 + 记 error + 落 DLQ"。
2. **新增** `test_polling_dead_letter_recorded_and_capped`：写入超过 `_POLLING_DLQ_MAX` 条，断言 DLQ 只保留最近 N 条、最旧的被裁剪。
3. **新增** `test_polling_dead_letter_replay_success_removes_entry`：DLQ 里一条死信，mock handler 这次成功，重放后该条从 DLQ 消失。
4. **新增** `test_polling_dead_letter_replay_failure_keeps_entry`：重放仍失败，该条留在 DLQ、error/attempts 被更新。
5. **新增** `test_dlq_write_failure_does_not_break_offset_advance`：mock `_record_polling_dead_letter` 抛异常，断言主流程仍推进 offset（DLQ 写失败不反噬主链路，§2.2 的隔离要求）。
6. **管理 / 转账 loop 的 DLQ 落点**：三条 loop 现在共用 `_handle_polling_update_with_retries`，DLQ 写入点单一。但仍各补一条"该 loop 失败 update 写入 DLQ 且带正确 `loop` 标签"的测试（`management` / `transfer_test`），确认三条 loop 都把自己的标签正确传进了统一函数。
7. **新增** `test_polling_dead_letter_ids_do_not_collide_across_loops`（codex 校准，锁 dlq_id 设计）：
   - 写入 `interaction:42` 一条、`transfer_test:42` 一条（同 update_id、不同 loop）；
   - `_list_polling_dead_letters` 能看到**两条**（不互相覆盖）；
   - `_discard_polling_dead_letter(aid, "interaction:42")` 后，`transfer_test:42` **仍在**；
   - 重放其中一条不影响另一条。

验证：
```
cd backend && .venv/bin/python -m pytest app/tests/test_account_bot.py -q
```
全量回归：
```
cd backend && .venv/bin/python -m pytest app/tests/ -q
```

---

## 5. 收尾（发布 0.49.25）

按项目规则（`frontend/src/lib/version.ts` 顶部注释的四处同步）：

1. 四处版本号 bump 到 **0.49.25**（缺一不可）：
   - `backend/app/__init__.py`（第 14 行 `__version__`）← **易漏，重点核对这处**
   - `backend/pyproject.toml`
   - `frontend/package.json`
   - `frontend/src/lib/version.ts`
2. `CHANGELOG.md` 的 `Unreleased` 内容转成 `## [0.49.25] — <date> · patch（补丁版本） · 交互轮询死信补丁` 段，中文描述。建议条目：
   - **Fixed**：修复交互/管理/转账三条 Bot 轮询链路中，重试耗尽的失败 update 仍推进 offset 导致消息（含 `payment_confirmed` 付款到账）永久丢失的问题；失败 update 现在会在推进前写入死信队列（DLQ），支持列出、重放与丢弃。
   - **Added**：新增 `GET /api/accounts/{aid}/bot/polling-dlq`、`POST .../{loop}/{update_id}/replay`、`DELETE .../{loop}/{update_id}` 死信管理端点与 `polling_dlq_count` 计数。
3. 单独一个提交（不改历史），线性推进。

---

## 6. 给 codex 的执行提示

- 先做 §3 Agent-R 的 DLQ 原语 + 三条 loop 接线，跑通 §4 的 1/2/5，再做 API 和其余测试。
- **§0.1 的基石不能动摇**：推进 offset 是对的，DLQ 是补持久化，不是改成"失败不推进"。任何试图"失败就 return 不推进"的写法都会引入 poison update 堵塞，直接否掉。
- DLQ 写入的 try/except 隔离（§2.2）是硬要求，不能省。
- 完成后向用户汇报：三条 loop 是否都接了 DLQ、固化测试如何改的、四处版本号是否一致。

## 7. 本计划范围外（后续第二轮其余项，仅备忘，不在本计划做）

- #2 session 是否独占 chat（改 `test_userbot_observed_interaction_session_consumes_even_without_actions` 契约）
- #3 未知 filter key 安装期 lint
- #4 整理债：math10 清理、payout_mode 恒 auto、信封 normalize 收敛、runtime/loader 两套 action 执行合并

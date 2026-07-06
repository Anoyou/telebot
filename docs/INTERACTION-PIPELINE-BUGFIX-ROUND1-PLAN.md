# 交互链路修复 · 第一轮（纯 Bug 修复）

> 状态：已完成并发布为 `0.49.24` ｜ 类型：bugfix，不改任何对外契约
> 执行结果：F1/F2/F3 均已落地；`backend/app/tests/test_event_bus.py` 与 `backend/app/tests/test_plugin_loader.py` 已通过回归。

---

## 0. 本轮边界（执行前必读，硬约束）

本轮**只修三个无争议的纯 Bug**，全部落在 `backend/app/services/event_bus.py` 与 `backend/app/worker/plugins/loader.py`。

### 0.1 禁止触碰的文件（第二轮范围，本轮聚焦无关）

**`backend/app/services/account_bot_runtime.py` 和 `backend/app/tests/test_account_bot.py` 本轮一律不碰。**

原因：offset 失败策略（第二轮议题）会集中改这两个文件，且 offset 重试行为已被 `test_account_bot.py` 的测试固化成契约。本轮三条修复与它们零重叠，为避免与第二轮设计撞车、保持本轮 diff 聚焦，不碰。若发现任何修复"必须"改动这两个文件，**停止并上报**，不要自行合并。

> 注：早前担心的"codex 有未提交 offset diff"已澄清不存在——当前工作区没有待推 offset diff，版本为 `0.49.19`，`git status` 仅本计划文档 untracked。不对 0.49.18/0.49.19 是否包含 offset 改动作任何断言；**第二轮基于当前 0.49.19 实际代码重新核验后设计**，不假设有待推分支。

### 0.2 明确不在本轮范围（留给第二轮设计）

以下问题**真实存在但已被测试固化成契约**，属于"改契约"而非"修 Bug"，本轮不动：

- **offset 失败仍推进** —— 已被 `test_interaction_polling_retries_failed_update_then_advances_offset_with_error` 固化为期望行为。第二轮单独设计（方向：DLQ 持久化 + 可重放/跳过，而非"失败就不推进"，避免 poison update 卡死后续）。
- **interaction session 独占整个 chat** —— 已被 `test_userbot_observed_interaction_session_consumes_even_without_actions` 固化。第二轮重新定义 session 是否应独占 chat。
- **`all_events` 不含 inline** —— 已被 `test_all_events_matches_callback_and_session_events_but_not_inline` 固化，是**设计契约不是 Bug**（inline 走 `inline_all` scope 或显式订阅）。不动。

### 0.3 更正的措辞（避免执行者被旧表述误导）

- `keyword` filter **不会崩溃**，它只是"整条精确相等"匹配（`text not in keywords`），不是子串匹配。想要子串用 `contains`。本轮**不改 keyword 语义**。
- 真正会崩的只有 `commands`/`command` filter（见任务 1）。

---

## 1. 任务 F1 —— `commands` filter 空文本 IndexError

### 现状（已亲验）
`backend/app/services/event_bus.py:516`：

```python
commands = _string_list(filters.get("commands") or filters.get("command"))
if commands and text.lstrip("/,").split(maxsplit=1)[0] not in [item.lstrip("/,") for item in commands]:
    return False, "filter_not_matched"
```

`text`（第 505 行 `str(message.get("text") or "").strip()`）为空串时，`"".split(maxsplit=1)` 返回 `[]`，`[0]` 抛 **IndexError**。异常未被 `_filters_match` 捕获，冒泡出 `_match_one` / `match_subscriptions`。

**触发条件**：任何带 `commands` 或 `command` filter 的订阅，遇到无文本事件（纯媒体消息、callback_query、贴纸、语音等 `message.text` 为空的事件）即崩溃一次匹配流程。

### 改动
在取 `commands` 后、split 前先判空。空文本对"命令过滤器"必然不匹配，直接返回 `False, "filter_not_matched"`。

参考实现（以实际代码风格为准）：

```python
commands = _string_list(filters.get("commands") or filters.get("command"))
if commands:
    if not text:
        return False, "filter_not_matched"
    head = text.lstrip("/,").split(maxsplit=1)
    if not head or head[0] not in [item.lstrip("/,") for item in commands]:
        return False, "filter_not_matched"
```

**可选顺带修**（低优先，若一并做需在 PR 说明）：`commands` 不剥离 `@botname` 后缀，导致群里标准写法 `/start@yourbot` 匹配不中。若修，在取 `head[0]` 后 `.split("@", 1)[0]`。**不确定是否要改语义时，本轮只修崩溃，@ 后缀留到确认后再说。**

### 验收
- 新增回归测试（放 `backend/app/tests/test_event_bus.py`，**不是** test_account_bot.py）：带 `commands` filter 的订阅，分别喂 `message.text` 为空串、纯媒体（无 text）、callback_query 三类事件，断言 `_filters_match` / `match_subscriptions` 返回不匹配且**不抛异常**。
- 保留原有：文本命令能正常匹配 / 不匹配的用例不回归。
- 全量 `pytest backend/app/tests/test_event_bus.py` 通过。

---

## 2. 任务 F2 —— 直通路径共享 ctx + `_LiveMessageOps.actions` 无限增长

### 现状（已亲验）

**问题 A：直通不隔离 ctx。** `backend/app/worker/plugins/loader.py:889`：

```python
await handler(ctx, event)
```

`_dispatch_userbot_direct_passthrough`（第 853 行起）是**唯一**直接把共享 `ctx` 传给插件 handler 的分发路径。其余所有路径（event bus entry `:756`、legacy `:4277`、命令 `:4910`、interaction entry `:536`）都用 `dataclasses.replace(ctx, messages=..., client=..., log=...)` 生成 per-invocation 隔离 ctx。

**问题 B：`_LiveMessageOps.actions` 只 extend 不清、无读取者。** `loader.py:3227`：

```python
self.actions.extend(normalized)   # 第 3227 行，之后从不清空
```

`_LiveMessageOps`（`:3199` 起）的 `self.actions`（`:3213` 初始化）在每次 `apply()` 里 `extend`，**永不清空**。常驻 `ctx.messages`（`_activate` 里创建的那个）被直通和后台任务复用时，该列表无限增长 = 内存泄漏。且经 grep 确认 `_LiveMessageOps.actions` **无任何读取者**（只有 BufferedMessageOps 变体的 `.actions` 在 `:768/5539/5545` 被读）——它是死数据 + 泄漏。

### 改动

**A（必做）**：直通路径也做 ctx 隔离。在 `await handler(ctx, event)` 之前，为本次调用构造独立 messages 的 ctx，与其他分发路径一致：

```python
call_ctx = replace(ctx, messages=_LiveMessageOps(
    state,
    plugin_key=plugin_key,
    trace=...,          # 参照其他路径如何取 trace；直通此前无 trace，可传 None
    default_send_via=...,  # 参照 _activate 里常驻 ops 的构造参数
))
await handler(call_ctx, event)
```

> 注意：直通的设计目标是"原样、低延迟下放"，不要在此路径引入昂贵操作（trace/normalize 等）。ctx 隔离只是把 `messages` 换成一个独立实例，成本极低（一次对象构造），符合直通定位。若 `_LiveMessageOps` 构造涉及 I/O，改用更轻的隔离方式并在 PR 说明。

**B（必做，与 A 相互独立但一起做）**：消除 `_LiveMessageOps.actions` 泄漏。**采用方案 1（保留字段 + 执行后 clear）**：

1. **（采用）** 在 `apply()` 末尾清空：动作已即时执行完，执行后 `self.actions.clear()`。**保留 `self.actions` 字段本身**。
2. ~~直接删除字段~~ —— **不采用**。理由（codex 校准）：`ctx.messages` 是插件可见的 facade，"本仓无读取者"不等于"外部/自研插件不读 `ctx.messages.actions`"。删字段是破坏对外契约的动作，不属于纯 bugfix 范围。本轮只清空、不删字段。

> 即：保留 `self.actions` 字段，但改成"每次 apply 只承载本次动作、结束即清空"的语义。具体写法（codex 校准，`extend` 前先清一次除残留、`finally` 里再清确保异常路径也不漏）：
>
> ```python
> self.actions.clear()              # 清掉上次运行态残留
> self.actions.extend(normalized)   # 只承载本次动作
> try:
>     ...                           # 现有的 _apply_userbot_event_bus_actions 等逻辑
> finally:
>     self.actions.clear()          # 无论成功失败都不累积
> ```
>
> 这样字段始终存在（不破坏对外 facade），但不再无限增长。若某处确实需要在 apply 之后读取本次动作，执行者需先确认无此用法再定；有疑问停下上报，不擅自改语义。

### 验收
- 新增回归测试（`backend/app/tests/test_plugin_loader.py`）：
  - **ctx 隔离**：并发触发两个直通插件（或同插件两次），断言各自 handler 收到的 `ctx.messages` 不是同一实例（`is not`），且一个插件的 apply 不污染另一个。
  - **无泄漏**：连续多次触发同一直通插件的 `apply`，断言常驻 `ctx.messages.actions` 不随调用次数线性增长（`clear()` 方案：断言每次 apply 后为空；若退回删字段方案：断言该属性已不存在）。
- 现有直通测试 `test_direct_passthrough_consumes_raw_event_before_event_bus` 不回归。
- 全量 `pytest backend/app/tests/test_plugin_loader.py` 通过。

---

## 3. 任务 F3 —— `userbot_session_chats` 预筛短路（降延迟）

### 现状（已亲验）
`state.userbot_session_chats`（`loader.py:3185` 初始化）本意是"哪些 chat 有活跃 userbot 会话"的快速前置索引，写点有 5 处（`:1191/:1267/:3579/:3757/:4751`，其中 3579 是全量重建），discard 点 `:3623`。

但 grep 确认：**它从未被读作 guard 短路**。结果 `_dispatch_userbot_session_message`（`:3591` 的 `_load_userbot_sessions_for_chat`）对**每条** incoming/outgoing 消息都执行 Redis `SCAN`（`:3607` `_redis_keys` 遍历 `{prefix}{account_id}:*` 模式），即使该 chat 根本没有任何会话。热路径每消息一次 SCAN。

### ⚠️ 正确性前提（必须先确认，否则会引入"漏会话"新 Bug）

短路 `if chat_id not in state.userbot_session_chats: return False` 的**方向性安全条件**：

> 集合**宁可偏大（假阳性 → 多扫一次 SCAN，无害），绝不可偏小（假阴性 → 漏掉真实会话，有害）**。

假阴性有**两个来源**，两个都要堵：

**来源一：创建路径漏 add。** 短路成立的必要条件之一是：**"凡创建 userbot 会话，必同步 add(chat_id) 到 `userbot_session_chats`"**。

执行者**必须先核对**所有创建/写入 userbot session 的路径都已 add：
- 已知 add 点：`:1191`、`:1267`、`:3757`、`:4751`，以及全量重建 `:3579`。
- 逐一确认这些是否覆盖了全部"写 `{_USERBOT_SESSION_KEY_PREFIX}...` key"的地方。用 grep 找所有 `redis.set(...session key...)` / `_save_userbot_*session*` 写点，对照 add 点。
- **若发现存在"写了 session key 但没 add chat_id"的路径 → 先补 add，再加短路。** 顺序不能反。
- （已核对结论，供参考，仍需执行者复核）：0.49.19 下四条写路径 `1190`(start_session) / `1264`(update_session) / `1570`(button_map，改的是已存在 session) / `4749`(manifest command) 均已 add，前提在"创建路径"这一侧成立。

**来源二（codex 补强，必做）：全量缓存尚未成功初始化。** `_refresh_userbot_session_chat_cache`（`:3560`）在 Redis 短暂失败时会 `except → return`（`:3575`），此时 `state.userbot_session_chats` 停在**初始空 set**。若此刻无条件执行 `chat_id not in set` 短路，会把**所有**消息判成"无会话"→ 长期全量假阴性，直到下次刷新成功。"创建必 add"防不住这条（进程刚起、reload 后缓存还没建起来时，历史 session 都不在集合里）。

**解决：加 `cache_ready` 标记，只有全量刷新成功后才信任"空集合=真的无会话"。**
- 在 `_AccountState` 增加 `userbot_session_chats_ready: bool = False`（与 `userbot_session_chats` 同处初始化，`:3185` 附近）。
- `_refresh_userbot_session_chat_cache` **成功完成全量重建后**（`:3579` `state.userbot_session_chats = chats` 之后）置 `state.userbot_session_chats_ready = True`；`except` 分支（`:3575`）**不置位 / 保持原值**，即刷新失败不点亮 ready。
- 短路 guard 仅在 `ready` 为真时启用（见下方改动）。ready 为假时保持旧行为——照常 SCAN，绝不短路。

TTL 自然过期导致的残留（Redis key 被动过期、未触发 discard）只会让集合**偏大**，符合安全方向，无需处理。

### 改动
确认前提成立后，在 `_dispatch_userbot_session_message` 开头（`chat_id` 解析出来之后、调用 `_load_userbot_sessions_for_chat` 之前）加**带 ready 闸的**短路：

```python
chat_id = _int_or_none(getattr(event, "chat_id", None))
if chat_id is None:
    return False
# ... 现有的 worker command / interaction_bot sender 早退判断保留 ...
if state.userbot_session_chats_ready and chat_id not in state.userbot_session_chats:
    return False   # 缓存已就绪且该 chat 无活跃 userbot 会话，跳过 Redis SCAN
# 缓存未就绪（ready=False）时不短路，照常走 _load_userbot_sessions_for_chat（SCAN）
```

> 放置位置注意：必须在现有的 `_looks_like_worker_command_text` 和 `interaction_bot_sender_ids` 早退**之后或之前**均可（那两个本就是纯内存判断），但**必须在 `_load_userbot_sessions_for_chat` 之前**。确认不影响现有早退语义。
>
> 双闸叠加后，只有"缓存成功建过 且 该 chat 不在集合"两条件同时满足才短路——两个假阴性来源都被堵住。

### 验收
- 新增回归测试（`test_plugin_loader.py`）：
  - **短路生效**：`ready=True` 且 chat 不在 `userbot_session_chats` 时，`_dispatch_userbot_session_message` 返回 False 且**未调用** Redis SCAN（mock `_redis_keys` / redis，断言 call_count == 0）。
  - **命中不回归**：`ready=True` 且 chat 在集合中时，正常走 SCAN + 会话派发（现有行为不回归）。
  - **假阴性防护 A（创建路径）**：模拟"创建了 session 且 add 了 chat_id"后，该 chat 消息能正常命中会话（防止误加短路把真实会话挡掉）。
  - **假阴性防护 B（cache 未就绪，codex 补强）**：`ready=False`（模拟刷新未成功/Redis 抖动）且 `userbot_session_chats` 为空集合时，喂一条该 chat 实际有会话的消息，断言**仍执行了 SCAN**（call_count > 0）并正常命中会话——即 ready 闸确实在缓存未就绪时禁用了短路。
  - **ready 置位时机**：`_refresh_userbot_session_chat_cache` 全量成功后 `ready` 为 True；模拟其内部 Redis 抛错走 except 分支时 `ready` 保持 False（不被点亮）。
- 现有会话派发测试全部不回归。

---

## 4. 执行方式与并行度

三条任务**文件重叠情况**：
- F1 → 仅 `event_bus.py` + `test_event_bus.py`
- F2 → 仅 `loader.py` + `test_plugin_loader.py`
- F3 → 仅 `loader.py` + `test_plugin_loader.py`

F1 与 F2/F3 **无文件重叠，可并行**（两个子 Agent）。F2 与 F3 **同改 `loader.py`**，建议**同一个子 Agent 串行**完成（F2 先、F3 后），避免 loader.py 合并冲突。

推荐分工：
- **Agent-A**：F1（event_bus 崩溃修复 + 测试）
- **Agent-B**：F2 → F3（loader.py：先 ctx 隔离/泄漏，再 SCAN 短路；共用一份 test_plugin_loader.py 改动）

两个 Agent 完成后统一跑：
```
pytest backend/app/tests/test_event_bus.py backend/app/tests/test_plugin_loader.py
```
**不要**跑改动 test_account_bot.py 的操作。若需全量回归，`pytest backend/app/tests/` 只读运行、不修改 test_account_bot.py。

---

## 5. 收尾

- 三条修复完成、对应回归测试通过后，本轮结束。
- **不 bump 版本号，但要写 `CHANGELOG.md` 的 `Unreleased` 段**（codex 校准）：把 F1/F2/F3 三条作为 patch 级条目记入 `Unreleased`，不动 `__version__`。等第二轮 offset/session 定案、准备发版时再统一 bump patch 并把 `Unreleased` 收敛成正式版本段。这符合项目 CHANGELOG 约定（先积累在 Unreleased，发布时才 bump）。
- **不合并、不触碰 `account_bot_runtime.py` / `test_account_bot.py`**。
- 完成后向用户汇报：三条各自的改法、测试结果、以及 F3 前提核对结论（创建路径是否全部 add chat_id + `cache_ready` 闸是否落地）。

## 6. 第二轮预告（本轮不做，仅备忘）

- offset 失败策略：DLQ 持久化 + 可重放/跳过（等 codex 推送后基于其最新代码设计）。
- session 是否独占 chat：重定义语义 + 改 `test_userbot_observed_interaction_session_consumes_even_without_actions`。
- 未知 filter key：安装期 lint / manifest 校验告警（非硬失败）。
- 整理债（不急）：math10 兼容层清理、payout_mode 恒 auto、信封重复 normalize 收敛为单一真相源、runtime/loader 两套 userbot action 执行合并。

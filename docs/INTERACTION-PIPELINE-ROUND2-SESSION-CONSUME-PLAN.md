# 交互链路修复 · 第二轮 #2 —— session 独占 chat 语义收窄

> 状态：已执行（codex，0.49.26）｜ 目标版本：0.49.26（patch）｜ 前置基线：0.49.25
> 类型：**改契约**（收窄"会话消费整个 chat"的语义 + 改一条固化测试）
> 作者：Claude（源自 `INTERACTION-PIPELINE-BUGFIX-ROUND1-PLAN.md` §6 第二轮预告 #2）

---

## 0. 这份计划要解决的真实问题（一句话）

只要某个 chat 里存在**一个** `channel="interaction_bot"` 的活跃会话，该 chat 内**所有** userbot 消息就会被判为"已消费"、直接 `return`，导致同 chat 的 Event Bus 订阅和 legacy `on_message` 插件**全部被静音** —— 哪怕这条消息跟那个会话毫无关系、哪怕插件这次没产生任何动作。

## 0.1 现状锚点（0.49.25 实测，供 codex 核对）

全部在 `backend/app/worker/plugins/loader.py`：

- **问题核心** `_dispatch_userbot_session_message`（3800）末尾：
  ```python
  consumes_userbot_observer_event = any(
      session_channel != _SESSION_CHANNEL_USERBOT     # 3865：只要有一个非 userbot（即 interaction_bot）会话
      for ... in candidates
  )
  consumed = consumes_userbot_observer_event           # 3868：consumed 初值即为 True
  for ... in candidates:
      result = await _run_userbot_session_entry(...)   # 逐个跑会话入口
      invoked_count += 1
      ...
  if invoked_count == 0:
      return False
  return consumed                                       # 3939：返回这个"是否独占"标记
  ```
- **主派发处** loader.py:4261：`session_consumed = await _dispatch_userbot_session_message(...)` → `if session_consumed: return`（4262-4263）→ **一旦为 True，Event Bus 匹配（4278 `_dispatch_userbot_event_bus_matches`）和 legacy on_message 循环（4285+）全部被跳过**。
- **通道常量**（145-148）：`_SESSION_CHANNEL_USERBOT="userbot"` / `_SESSION_CHANNEL_INTERACTION_BOT="interaction_bot"` / `_SESSION_CHANNELS_OBSERVED_BY_USERBOT={两者}`。
- **固化测试** `test_userbot_observed_interaction_session_consumes_even_without_actions`（`backend/app/tests/test_plugin_loader.py:3605`）：构造一个 `channel="interaction_bot"` 会话、`_run_userbot_session_entry` 返回 False（无动作），断言 `consumed is True`。**这条正是把"无动作也独占整个 chat"钉死的契约，本计划要改它。**
- **相邻固化测试（本计划不破坏，需回归）**：
  - `test_userbot_observed_interaction_session_keeps_logical_interaction_channel`（3517）：interaction_bot 会话的信封 channel 语义保持。
  - `test_userbot_observed_interaction_session_skips_platform_bot_sender`（3647）：interaction bot 自己发的消息不进会话。
  - `test_userbot_update_session_accepts_observed_interaction_session`（4452）。

---

## 1. 语义决策（先读，决定改成什么）

### 1.1 问题的本质

当前"消费"用了**通道**作为判据（`session_channel != userbot` → 独占），这是错的维度。通道只决定"消息由谁收发"，不该决定"这条消息是否属于这个会话"。真正该决定独占的是：**这条消息是不是这个会话在等的消息**。

### 1.2 新语义（本计划采用）

**把"是否消费/独占"从"通道判据"改成"会话是否真正接手了这条消息"判据。**

一条 userbot 观察到的消息，只有在**至少一个会话入口真正处理了它**时，才消费（return、不再下放给 Event Bus / legacy）。具体：

- `_run_userbot_session_entry` 的返回值需要能表达"这个会话到底有没有接手这条消息"，而不只是"有没有失败"。
- **消费的充分条件**：至少一个会话入口返回了"我处理了这条消息"（handled）。
- **不消费**：所有候选会话都表示"这条消息不关我事"（not handled）——此时消息继续正常下放给 Event Bus 和 legacy，**同 chat 的其它 userbot 插件不再被无辜静音**。

### 1.3 "handled" 如何判定（关键，codex 需按实际入口返回值确认）

当前 `_run_userbot_session_entry`（3800 区域调用）返回的是**布尔"是否失败"**（result 为 True 计入 failed_count）。这不足以表达 handled。有两个层次的方案，**优先方案 A**：

**方案 A（推荐，语义最准）**：让会话入口的执行结果携带"是否产生了动作 / 是否声明消费"。
- 插件通过 `on_interaction` 返回的动作列表非空，或显式返回一个 `consume`/`handled` 标记 → 视为 handled。
- `_run_userbot_session_entry` 改成返回一个结构（或三态：handled / not_handled / failed），`_dispatch_userbot_session_message` 据此决定 `consumed`。
- 消费判据：`consumed = any(entry 结果为 handled)`。

**方案 B（退路，改动更小）**：若入口返回值改造成本过高，则至少把"独占"从"通道"改成"该会话是否匹配这条消息"——即在 candidates 收集阶段，只有真正匹配当前消息的会话才纳入消费判定，通道不再单独触发独占。

> codex 按 `_run_userbot_session_entry` 与 `invoke_interaction_entry` 的实际返回值形态选 A 或 B，并在 PR 说明选了哪个及原因。**核心目标不变：一个与当前消息无关的 interaction_bot 会话，不能静音整个 chat 的其它 userbot 插件。**

### 1.4 边界：userbot 通道会话的行为保持

`channel="userbot"` 的会话（命令触发的那种）语义不变——它本来就该接手该 chat 的后续消息。本计划只收窄 `interaction_bot` 观察会话"无条件独占"的行为，不动 userbot 通道会话。

---

## 2. 改动点

`backend/app/worker/plugins/loader.py`：

1. **`_dispatch_userbot_session_message`（3800）末尾的 consumed 判定**：从 `consumes_userbot_observer_event = any(channel != userbot)`（3864-3868）改为基于"会话是否真正 handled"（§1.3 方案 A 或 B）。
2. **`_run_userbot_session_entry`（若走方案 A）**：扩展返回值以携带 handled 信息。注意它当前被 `invoked_count`/`failed_count` 逻辑使用（3930+），改返回值要同步更新这些计数逻辑，别破坏 trace 统计。
3. **确保不回归**：`channel="userbot"` 会话仍正常消费；interaction bot 自己发的消息仍被 skip（3647 测试）；信封 channel 语义仍保持（3517 测试）。

---

## 3. 测试要求

改 / 加在 `backend/app/tests/test_plugin_loader.py`：

1. **改造固化测试** `test_userbot_observed_interaction_session_consumes_even_without_actions`（3605）：
   - 语义反转为新契约。重命名或改断言：一个 interaction_bot 会话在**入口未处理这条消息（无动作 / not handled）** 时，`_dispatch_userbot_session_message` 应返回 **False**（不独占），让消息继续下放。
   - 若采用方案 A：mock `_run_userbot_session_entry` 返回 "not handled"，断言 `consumed is False`；再补一条 mock 返回 "handled"，断言 `consumed is True`。
2. **新增** `test_unrelated_interaction_session_does_not_mute_other_userbot_plugins`：chat 内有一个 interaction_bot 会话但它不处理当前消息，断言主派发**没有** early-return、Event Bus / legacy 仍被调用（mock `_dispatch_userbot_event_bus_matches`，断言它被调用）。
3. **新增** `test_userbot_channel_session_still_consumes`：`channel="userbot"` 会话正常接手消息、`consumed is True`（保证边界 §1.4 不回归）。
4. **回归**：3517 / 3647 / 4452 三条相邻固化测试必须仍通过（不改它们）。

验证：
```
cd backend && .venv/bin/python -m pytest app/tests/test_plugin_loader.py -q
```
全量：
```
cd backend && .venv/bin/python -m pytest app/tests/ -q
```

---

## 4. 文件归属

本计划集中改 `backend/app/worker/plugins/loader.py` + `backend/app/tests/test_plugin_loader.py`。这两个文件第一轮 F2/F3 由 Claude 改过、已发布 0.49.24，当前无在途占用。交由 codex 执行本轮 #2。

---

## 5. 收尾（发布 0.49.26）

按项目规则四处版本号同步 + CHANGELOG：

1. 四处 bump 到 **0.49.26**（缺一不可）：
   - `backend/app/__init__.py`（第 14 行 `__version__`）← **易漏，重点核对**
   - `backend/pyproject.toml`
   - `frontend/package.json`
   - `frontend/src/lib/version.ts`
2. `CHANGELOG.md` 的 `Unreleased` 转 `## [0.49.26] — <date> · patch（补丁版本） · 交互会话消费语义补丁` 段，中文。建议条目：
   - **Fixed**：修复 chat 内只要存在一个交互 Bot 观察会话，该 chat 的全部 userbot 消息就被判为已消费、导致同群其它 Event Bus / on_message 插件被静音的问题；现在只有会话真正接手消息时才消费，无关消息继续正常下放。
3. 单独一个提交，线性推进。

---

## 6. 给 codex 的执行提示

- **§1.2 是核心**：消费判据从"通道"改成"会话是否真正 handled"。不要保留"channel != userbot 就独占"的任何残留。
- 方案 A / B 二选一，按 `_run_userbot_session_entry` 实际返回值形态定，PR 说明理由。
- 改返回值时注意同步 `invoked_count` / `failed_count` / trace 统计，别破坏观测。
- userbot 通道会话行为不动（§1.4）。
- 完成后向用户汇报：选了 A 还是 B、固化测试如何改的、四处版本号是否一致、相邻三条测试是否回归通过。

执行备注（codex）：0.49.26 实际代码没有独立 `_run_userbot_session_entry` 函数，入口调用内联在 `_dispatch_userbot_session_message` 中，因此采用方案 A 的轻量形态：`channel="userbot"` 会话视为已接手；`channel="interaction_bot"` 观察会话只有返回非空动作（或入口异常，表示已尝试接手）才消费，正常返回空动作不再静音后续 Event Bus / legacy 链路。

## 7. 本计划范围外（后续，仅备忘）

- #3 未知 filter key 安装期 lint
- #4 整理债：math10 清理、payout_mode 恒 auto、信封 normalize 收敛、runtime/loader 两套 action 执行合并

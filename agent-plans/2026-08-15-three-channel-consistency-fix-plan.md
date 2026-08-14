# 三通道一致性修复轮

> 状态：草案，待人审确认；本文件只立项，不代表修复轮开工。
>
> 前置审计：agent-plans/2026-08-15-consistency-audit-plan.md WP-A 已完成核销表与 parity 矩阵确认。WP-A 在草案获确认前保持 in_progress。

## 目标与范围

收敛 E1 Loader 直执行、E2 Delivery、E3 Worker RPC 三执行体在错误语义、批处理结果、边缘输入和可观测字段上的真实分叉；把已确认的有意架构分工写成可查契约。

本轮只做以下工作：

- 共享错误码词表、failure_result 单点构造器及其迁移；
- P1 行为语义统一；
- P2 纯空白 payout 文案统一；
- P3 三执行体独立 parity 与审计字段测试补强；
- P4 插件 API 文档补充“动作 × 执行体分工表”。

本轮不恢复、引用或推测已经散佚的 2026-07-09 A1 原文；Event Bus update_session 方案按本草案重新推导。

## 设计中枢与 P1：结果/错误语义收口

工作量：约 2～3 人日。

### 1. action_core 共享层

在 backend/app/services/interaction/action_core.py 建立：

1. canonical 错误码词表，至少覆盖：invalid_payout_amount、empty_message_text、session_not_found、interaction_session_error、rate_limited、unsupported_action、action_limit_exceeded、send_channel_deprecated。
2. 单一 failure_result 构造器，替代以下三份复制体：
   - backend/app/worker/plugins/loader.py:2881
   - backend/app/services/interaction/delivery.py:2415
   - backend/app/worker/runtime.py:1600
3. 统一错误码到 status、actual_send_via、worker_offline、reply_anchor_missing、审计附加字段的映射；适配器只提供通道特有上下文，不再自行复制结果结构。

目标语义：

- session_not_found 表示会话不存在或缺少会话键；interaction_session_error 只保留给 Redis/会话存储等基础设施异常。
- invalid_payout_amount 作为三执行体的非法金额错误码；T3 资金闸拒绝码保持现有契约，不在本轮重做。
- 所有失败结果键集合与派生布尔字段由共享构造器生成。

理由：错误码族分叉（核销分叉 1/3）和第 8 项第 8 维的复制漂移风险，可随单点构造机械消失；未来新增字段只需修改一处。

### 2. 限流拒绝状态：统一为 FAILED

目标：E1 与 E2/E3 对 rate_limited 均记录 TRACE_STATUS_FAILED，保留 error_code=rate_limited，并在结果/审计上下文中携带可选的等待秒数。

理由与影响：动作已被请求但未执行，使用 FAILED 才不会把“拒绝”统计成成功处理的 skipped；插件可按错误码决定延迟重试。为避免盲重试，应将等待信息作为明确元数据，重试策略仍由调用方控制，不由执行体隐式重放。

验收标准：

- E1、E2、E3 同一限流输入的 status/error_code 完全一致；
- batch failed 计数和 action tap 状态一致；
- 既有 rate_limited 错误码不改名，T3 拒绝面测试继续通过。

### 3. 未知动作批聚合：统一为失败

目标：未知 action type 统一记录 TRACE_STATUS_FAILED/error_code=unsupported_action，并使 run_action_batch 的 failed 计数增加；不再由 E2 以 skipped+成功吞掉。

理由：未知动作没有执行，标记成功会让插件误以为整批完成，也会掩盖插件/框架契约不匹配。向前兼容应通过 canonical 词表和能力协商实现，而不是静默成功。

验收标准：

- E1、E2、E3 的单条记录、batch failed、审计 tap 三者一致；
- 未知动作不会触发外部平台调用；
- deprecated send_via 仍使用既有 send_channel_deprecated，不与未知动作混淆。

### 4. Event Bus 裸 update_session：订阅入口必须先 start_session

目标契约：Event Bus 订阅入口若没有已有会话，必须先发出 start_session；直接裸 update_session 不自动创建会话，E1/E2 均返回统一的 session_not_found。

重新推导理由：订阅入口缺少稳定的 entry/session 元数据时，框架无法安全推导 channel、rule、expiry 和声明归属。强制显式 start_session 能把会话生命周期责任放回插件，并避免隐式创建错误会话。

与已修路径的衔接：

- 关键词交互与付款路径继续保留当前“应用动作前预建会话”行为；
- 事件订阅路径仅补齐明确的 start_session→update_session 合同，不回退已修的关键词/付款顺序；
- E1/E2 的缺会话错误通过共享错误码/结果构造器统一；E3 不承接会话动作。

验收标准：

- Event Bus 裸 update_session 在 E1/E2 均失败且 error_code=session_not_found；
- 显式 start_session 后 update_session 在 E1/E2 均成功并 merge data；
- 关键词、付款、显式 start 的既有回归测试保持通过。

## P2：纯空白 payout 文案统一回退

工作量：约 0.5～1 人日。

目标：选择“回退到 +amount”，而不是报错。三执行体对缺失值和空串本来已经回退；本次只补齐显式传入纯空白字符串这一边缘输入。E1 当前 loader.py:3344-3352 在 strip 后为空时仍报 empty_message_text，E2/E3 已回退。

理由：文案不是资金数值的权威来源；对空白文案采用与缺失/空串相同的确定性归一，可避免付款已完成后仅因展示字符串无效而失败，并缩小爆炸半径。

验收标准：

- text 缺失、空串、纯空白三种输入在 E1/E2/E3 均发送 +amount；
- 非空显式文案原样（按既有 trim 规则）保留；
- 非法金额、T3 闸、补偿/幂等语义不改变。

## P3：三方独立 parity 与可观测字段补强

工作量：约 2～3 人日。

### 1. 拆分执行链

把现有 test_interaction_executor_parity.py 中 E2+E3 组合驱动拆成 E1、E2、E3 三个独立 driver；保留现有组合测试作为端到端回归，但不再把组合结果当作三方独立证明。

### 2. 纳入核销矩阵缺口

为第 8 项维度 15/16/17 增加逐执行体断言：

- 回复锚点查找、缺失提示、错误码和 reply_anchor_missing；
- message-id 保存/替换，包括普通消息、媒体、payout；
- audit/action tap 的 status、error_code、channel、审计状态、deny reasons、上下文标识。

### 3. 加宽 audit tap 抽取

现有抽取只比较 status/error_code/actual_send_via；扩展为完整的 canonical 审计字段集合，并对缺失字段使用明确的 None/空集合规范，避免“抽取器过窄导致假一致”。

验收标准：

- 19 个 canonical actions 均有 E1/E2/E3 独立代表行；
- 维度 15/16/17 每项至少有成功、缺失/失败、替换或补偿相关用例；
- failure_result、限流、未知动作、payout 空白文案和 Event Bus 裸 update 的新语义均被锁定；
- 组合链回归与三方独立矩阵同时通过。

## P4：文档随动

工作量：约 1～1.5 人日。

在 docs/PLUGIN-API-REFERENCE.md 增加“动作 × 执行体分工表”，至少说明：

- click_callback_button 为 E1 UserBot 专属能力；
- answer_callback / answer_inline_query 走 Bot API，不进入 E3 Worker RPC；
- start_session、result、end_session、close_session、no_session 的内联/外层会话分工；
- 富消息 reply_markup 不属于 E3 富消息能力；普通 send_message 的 userbot 按钮降级规则；
- payout 的 userbot 路径、T3 资金闸和补偿语义；
- Event Bus 必须显式 start_session 后才能 update_session。

验收标准：

- 表格中的每一格都能链接到当前代码或 parity 测试；
- 插件作者无需阅读执行体源码即可判断动作是否支持、由谁落地、失败如何记录；
- 不把有意分工误写成三执行体均等能力。

## 明确不做

- 不“修复”已确认的有意分工：click_callback_button E1 专属、answer_* 走 Bot API、会话控制外层分工、富消息 markup 能力边界；
- 不重做 T3 已覆盖的 payout 拒绝面；
- 保持 message_id=None 时无法安全跨管道关联而放行的设计边界；
- 保持按 rule_id 隔离 dedupe key 的设计边界；
- 不在本草案阶段修改代码、测试、版本号、CHANGELOG 或执行状态。

## 顺序、总量与完成条件

执行顺序固定为：P1（含 action_core 中枢）→ P2 → P3 → P4。预计总工作量约 6～8.5 人日，不含发布与线上验收。

修复轮完成条件：

1. P1～P4 的验收标准全部满足；
2. 后端 focused parity、会话、payout compensation 测试通过，并补跑项目要求的相关验证；
3. PLUGIN-API-REFERENCE.md 分工表合入；
4. P1 的三处插件可见行为变化（限流状态 SKIPPED→FAILED、未知动作计入 failed、Event Bus 裸 update_session 改为强制 start_session 契约）必须在 CHANGELOG 中文条目中逐条明示“行为变化”，并由 P4 分工表同步覆盖；两者缺一不得合入；
5. 另行提交中文变更日志与版本发布材料（不属于本草案）；
6. 修复轮是否开工、优先级或目标语义若需改变，另行裁定。

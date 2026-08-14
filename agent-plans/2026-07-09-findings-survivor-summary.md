# 2026-07-09 交互框架 review 结论·幸存摘要

> ⚠️ **本文不是原文**。三份原始设计文档（session-timing-design / dedup-sessiongrab-executor-audit / payout-compensation-design）已散佚（agent-plans 属 gitignore，工作区副本被清理，git 对象中无踪）。本文由 advisor 的跨会话记忆转写，内容为 2026-07-09 全面 review 时**经主会话逐环亲自复核确认**的结论，基线 0.53.5 / codex-0.33 分支 @8658aee，@296fbed(0.53.6) 复测仍成立。
> ⚠️ 文中所有函数名、行号、症状均为 **0.53.x 时代快照**，只作定位线索与判定基准，一切以当前代码复核为准。

## 一、文件映射勘误（当年踩过的坑，先记住）

- 文档与设计稿里说的 "runtime(bot)" 的**真实实现文件是 `services/account_bot_runtime.py`**；`services/interaction_bot_runtime.py` 只是兼容外观（re-export）。定位交互 bot 侧逻辑一律去 account_bot_runtime.py。

## 二、三种消息模式当时的主链路（复核时均正确）

1. **直通**：loader 的 `_dispatch_userbot_direct_passthrough`；manifest `capabilities.telegram_direct_passthrough` + 账号配置 `direct_passthrough.enabled` 双开关；消费后 return 阻断普通链路。
2. **userbot 通道**：前缀命令 → `_wrap_manifest_interaction_command` **先建会话**（`_create_manifest_command_userbot_session`）再调入口；会话 channel 硬编码 userbot。
3. **交互 bot 通道**：关键词触发 → `_run_interaction_module` **先应用动作后落会话**（apply → `_save_interaction_session`）；会话硬编码 interaction_bot 且 payload 不含 data 字段；guard 在 `interaction/contracts.py` 注入默认通道（白名单排除 payout / update_session）。

**资金铁律**：收付款永远走 userbot——delivery 的 payout 无条件 `_apply_payout`→worker；userbot 离线返回 `userbot_offline` 不降级；当时已有回归测试锁定。

## 三、确认的框架 bug（WP-A 核销对象，编号与审计计划 §3 对应）

1. **开局 `update_session` 悬空**：delivery 的 `_apply_update_session` 要求会话已存在（当年症状 KeyError "session not found"），而关键词/付款/事件订阅三条路**动作应用先于建会话** → 开局用 update_session 存状态的插件（guess_number 型）经关键词开局丢状态；userbot 命令通道因框架先建会话而正常 → **同插件跨通道行为不一致**。当年已装插件零 `start_session` 使用；契约测试手工传 data，**无端到端开局持久化测试**。修复设计曾定稿（"方案 A1 + 两伴随修"），原文散佚——若判定未修，修复方案需重新推导。
2. **信封嵌套不一致**：account_bot_runtime 的 `_interaction_session_envelope` 把整条 Redis 记录塞进信封 `"data": dict(session_data)`，而 loader 的 `_userbot_session_envelope_from_session` 正确解包 `session["data"]` → 交互 bot 续会话/过期路径插件读 `session.data.get(...)` 拿到记录外壳而非状态。当年可见症状：guess_number 超时文案 target=0。
3. **`_save_interaction_session` 重建抹 data**：只继承 created_at / started_by / paid 集合，不继承 `data` → paid_pool 第二笔付款重触发抹掉已攒状态（dice_grid_hunt 当年靠每次发全量状态侥幸存活）。
4. **payout 失败无补偿闭环**：action 调用路径单次 publish 无在线等待（对比 entry 调用路径有 5s 等待）；delivery 失败仅 record FAILED + 日志，无重试/待发队列——收款成功→结算失败只能人工翻日志。**注**：此后版本引入了补偿系统（payout_compensation、E4 扫描重放、人工核销）与 T3 资金闸，本项大概率已修，销账需给完整证据链。
5. **跨管道双分发竞态**：同一群消息可同时经 userbot 观测（loader 路）与 bot 直收（account_bot_runtime 路）喂会话，两路**无共享 message 级去重** → bot group privacy off 时可能双结算。**注**：0.95.0 "修复重复发奖"可能相关，需查证覆盖面。
6. **双通道抢会话**：bot 侧 `_save_interaction_session` 无条件覆写 `channel=interaction_bot` → userbot 会话可被翻转且 data 丢失。
7. **过期扫描声明检查漂移**：worker 侧过期扫描不检查 entry 是否声明 `session_expired`，bot 侧检查 → 行为漂移。
8. **执行体三体漂移**：动作执行实为三体——E1 loader 直执行 / E2 delivery / E3 worker RPC 动作面。当年整理过 **10 项漂移清单 + 防漂移 parity 测试设计（原文散佚）**；幸存的最重两项：**E3 不读 reply_markup**（userbot 按钮经 bot 路静默丢失，当年 grep 复核插件零使用）；**错误码族 / 限流拒绝行为跨执行体分叉**。**注**：T3 已统一 payout 拒绝面（四类面 + 统一错误码），核销时区分"T3 已覆盖"与"仍分叉"。

## 四、当年记录的相关技术债（非 bug，审计时顺带盘点现状）

- math10 兼容层散布在 account_bot_runtime 多处。
- `payout_mode` 是 chat 粒度**文案语义**，不控路由。
- 信封三层冗余：平铺字段 + envelope + tp_event 并存。

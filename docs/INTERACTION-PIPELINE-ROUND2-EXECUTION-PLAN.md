# 第二轮执行计划(改契约 / 结构轮)

> 状态:已通过双方(Claude + Codex)基于 `codex/0.33-interaction-framework` 分支源码交叉复核。
> 原则:从"4 track 全并行大手术"改成"几个可独立回滚的小刀口"。
> 纪律:先锁后改(改契约前先补/确认测试锁住现有行为),每刀口独立 worktree + 独立 PR,
> PR 描述显式列出"契约变更点"与"有意的取舍"。

---

## 背景校正(避免重复劳动 / 踩已知坑)

第一轮已完成、**第二轮不要重复**的事项:

- ZIP 上传:API `upload_plugin_package`(`backend/app/api/plugins_install.py:116`)+ 前端 `ZipUploadCard`(`frontend/src/pages/Extensions.tsx:554`)**已落地**。
- 账号级启停 UI:管理页展开面板**已接线**。
- 卸载清理:`AccountFeature` / 非内置 `Feature` / `PluginGlobalConfig` 残留**已清**(`plugin_install_service.py`)。
- 直通模式 trace:`receive/route/plugin_invoke` span + `last_trace_id` 回写**已补**(`loader.py`)。
- 日志"四段检查":`Logs.tsx` **已加**。

所以 D2 的工作是**插件详情页/抽屉一页闭环**,不是再补上面这些。

---

## 执行顺序(串行为主,每步可独立回滚)

顺序即优先级。除非明确标注,**不要多个刀口并行改同一文件域**。

1. **C — AI 账号预算 Redis 原子预扣**(价值最实,测试先行)
2. **D1 — 插件聚合只读接口**(不迁移、不删表)
3. **D2 — 插件详情页/抽屉一页闭环**(消费 D1)
4. **B1 — 拆 `ai_runtime.invoke`**(纯重构,行为零变化)
5. **B2 — 新增 streaming 接口**(不改 `complete()` 行为)
6. **A — 信封 normalize 收敛**(执行器合并延后,本轮只产差异表)

可并行的组合(文件域不重叠时):C 与 D1 可同时开;B1 与 A 的"差异表"部分可同时开。
D2 必须等 D1 的聚合接口 merge 后再开。

---

## Track C — AI 账号预算改 Redis 原子预扣

**问题:** `services/llm_runtime.py:371` `_check_budget` 现在查历史 `LLMUsage` 做阈值判断,不是并发安全的原子预扣。并发请求可能同时过检查再一起超限。

**关键约束(Codex 复核补充):** `_check_budget` 管**4 个维度**,不是单一 token:
1. 每分钟请求数
2. 每日请求数
3. 每日 token 数
4. 高价 provider 每日调用次数

当前实现集中在 `backend/app/services/llm_runtime.py:394-435`。

**所以不能直接照搬** `backend/app/services/plugin_ai_quota.py:114` 的单一 token 预扣。需要为这几个维度各设计 reservation 键。

**步骤:**
1. 先补测试:并发请求打同一账号预算,断言超限后被拒数量正确;Redis 失败时的 fail-open 行为;调用失败/fallback 时预扣额度被释放。(现在这些测试会挂)
2. 实现账号级 reservation:事前预扣 + 失败释放(参考 plugin_ai_quota 的 acquire/release 配对),覆盖上述 4 维度。
3. **Redis 异常继续 fail-open**——这是有意取舍(自用项目不希望 Redis 抖动就锁死 AI),在代码注释和 PR 里标明。

**验证:** `pytest -k "budget or quota or llm_runtime"` + 全量。

---

## Track D1 — 插件聚合只读接口(不动 schema)

**问题:** builtin/zip/git/repo/local 数据层已统一到 `installed_plugin`,但 API 有三套并行(`/api/plugins`、`/api/remote-plugins`、`/api/plugin-repos`)。前端一页闭环需要一个聚合视图。

**本轮只做只读聚合,严禁动 schema。** 提供一个"列出所有已安装插件"的统一视图,一次返回:来源 / 版本 / 更新状态 / 全局启停 / 各账号启停矩阵 / 最近加载错误 / 最近 trace 引用。

**明确 defer(不要同轮做):清死表 + 魔法键迁移。** Codex 复核确认:
- `PluginInstall`(`db/models/plugin.py:29`)、`RemotePlugin`(`db/models/remote_plugin.py:17`)仍是兼容模型。
- `_telepilot_remote` 魔法键(`remote_plugin_service.py:86`)**仍在活跃读写**——更新状态(latest_version/update_available)全靠它:写入在 `remote_plugin_service.py:339` / `remote_plugin_service.py:349`,读取在 `remote_plugin_service.py:311` / `remote_plugin_service.py:391`,Feature schema 也在 `backend/app/schemas/feature.py:118` 读取它。
- 清这个 = 动更新检查链路,风险中等,**必须单独开一轮**,先 grep 确认无读路径依赖 + Alembic 可回滚迁移。

**验证:** `pytest -k "plugin"` + 全量。因为不动写路径,风险低。

---

## Track D2 — 插件详情页/抽屉一页闭环(依赖 D1)

**目标(你 D 的核心诉求):** 一个插件详情页/抽屉里一页看全:来源和版本、更新风险、全局配置、账号矩阵、每账号配置、最近加载错误、最近 trace。消除"包级安装/全局开关/账号启停/账号配置/运行状态散在不同视图"。

**范围:** `frontend/src/pages/Extensions.tsx`、`frontend/src/api/plugins.ts`,消费 D1 聚合接口。

**不要重复第一轮已做的 zip 上传卡片和账号启停面板**——那些已在;D2 是把它们和其余信息整合进一页详情。

**依赖:** 必须等 D1 聚合接口 merge,否则前端要 mock 两次。

**验证:** `pnpm --dir frontend typecheck` + `build` + 浏览器冒烟(桌面 1280 / 移动 390 无横向溢出,详情页各区块可见可展开)。

---

## Track B1 — 拆 `ai_runtime.invoke`(纯重构)

**问题:** `worker/ai_runtime.py:137` `invoke` 约 958 行,塞了 mode 分派、图片/音频下载、模板渲染、生图、TG 发送、send_mode 降级,靠函数内惰性 import 规避循环依赖。

**注意:** 它在标准调用处已经走 `services.llm_invoke.invoke`(`ai_runtime.py:609`)——所以拆的是**编排层**,不是 LLM 调用核心。

**步骤:** 拆成 `resolve_provider` / `prepare_inputs`(图片/音频) / `call_llm` / `render_and_deliver` 四段。**纯重构,对外契约零变化。**

**先锁后改:** 拆前确认 `test_ai_*` 覆盖 mode 分派 / 图片 / 模板 / send_mode 降级 / inline override 五条路径,缺的先补。拆完这些测试必须全绿且无新增行为差异。

**验证:** `pytest -k "ai_runtime or ai_facade or llm"` + 全量。

---

## Track B2 — 新增 streaming 接口(不改 `complete()`)

**校正(Codex 复核):streaming 不是"底层都有只差 yield"。**
- `AnthropicClient` 内部用 SSE 但拼成整段返回。
- `ResponsesClient.complete` 默认 `stream: bool = False`(`llm_client.py:989`)——默认压根不开流式。
- `ctx.ai.stream_complete`(`ai_facade.py:220`)明确 `NotImplementedError`。

**所以这是真·新增能力,不是薄封装。** 在 `services/llm_client.py` 把 SSE 增量真正 yield 出来,在 `ai_facade` 落地 `stream_complete`。**绝不改动现有 `complete()` 的行为**(它继续返回整段)。

**验证:** 新增 streaming 测试 + 确认 `complete()` 旧测试全绿未变。

---

## Track A — 信封 normalize 收敛(执行器合并延后)

**问题:** 信封 / actions 在多处重复归一化:交互 payload 在 `account_bot_runtime.py:5385` `_interaction_module_payload` 叠很多字段,actions 又在 `loader.py` 里归一化。字段变多时可能覆盖出不一致。

**本轮只做 normalize 收敛,不合并执行器。**

步骤:
1. 枚举两处现在各归一化了哪些字段,列成清单。
2. 补快照测试锁住现有 normalize 输出。
3. 小范围收敛到单点,逐条核对收敛后覆盖原两处全部字段(否则静默丢字段)。

**执行器合并本轮只产差异表,不动代码。** Codex 复核确认这是核心约束区:
- `InteractionDeliveryExecutor`(`account_bot_runtime.py:6315`)
- payout 强制 userbot(`interaction/delivery.py:647`)
- loader 内 userbot 路径

让 Codex 先产出"`InteractionDeliveryExecutor`(进程内)vs worker RPC 发送路径"的行为差异对照表。看完再决定是否单开一轮合并。payout 强制 userbot / 离线不降级 / send_via 注入规则这些硬约束在差异表里要单独标注为"合并时绝不可稀释"。

**验证:** 全量 pytest + 消息/payout 专项用例全绿(如 `test_interaction_contract_does_not_inject_send_via_for_payout`)。

---

## 每个刀口的收尾四件套

1. 后端全量 `cd backend && .venv/bin/python -m pytest`
2. 触达文件 `ruff`(不顺手改无关旧文件的 import 排序问题)
3. 前端 `pnpm --dir frontend typecheck` + `build`
4. `git diff --check`

沿用第一轮习惯:写 CHANGELOG `Unreleased`,不 bump 版本、不自动 commit,留给人合。

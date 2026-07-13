# TelePilot 插件开发者工具链

本文只记录已经对码验证的插件开发工具。命令示例默认在仓库根目录执行；如果没有安装同名 console script，可用源码脚本形式运行：

```bash
cd backend
python scripts/tp_plugin.py --help
python scripts/tp_replay.py --help
```

## 1. 脚手架 CLI：`tp_plugin`

`tp_plugin` 面向本地插件开发，当前有三条主命令：

```bash
tp_plugin new my_game --profile session_game
tp_plugin check plugins/local_imports/my_game
tp_plugin register plugins/local_imports/my_game
```

源码脚本等价写法：

```bash
cd backend
python scripts/tp_plugin.py new my_game --profile session_game
python scripts/tp_plugin.py check ../plugins/local_imports/my_game
python scripts/tp_plugin.py register ../plugins/local_imports/my_game
```

### `new`

`new` 生成一个可运行插件骨架，支持 `--profile session_game|command|passthrough`，默认生成到 `plugins/local_imports/<name>`，也可以用 `--dir <父目录>` 指定父目录。

常用命令：

```bash
tp_plugin new my_game --profile session_game
tp_plugin new my_command --profile command --dir /tmp/tp-plugins
tp_plugin new my_game --profile session_game --dry-run
tp_plugin new my_game --profile session_game --force
```

骨架包含 `plugin.json`、`manifest.py`、`plugin.py`、`__init__.py`、`README.md`、`CHANGELOG.md`、`test_plugin.py`。`session_game` 模板演示开局建会话、消息抢答、命中奖励和 `payout` action；模板默认权限按 profile 填入。

### `check`

`check` 本地校验插件目录：

```bash
tp_plugin check plugins/local_imports/my_game
```

它会检查必备运行期文件、`plugin.json` 结构、`manifest.py` 语法、事件订阅白名单，并做权限推导审计。`check` 只打印错误/警告，不修改 `plugin.json` 或 `manifest.py`。

### `register`

`register` 把本地目录登记进 `installed_plugin` 台账，让 loader 能按安装插件加载：

```bash
tp_plugin register plugins/local_imports/my_game
tp_plugin register /tmp/tp-plugins/my_game
tp_plugin register /tmp/tp-plugins/my_game --force
```

当源目录不在 `plugins/local_imports` 时，工具会先复制到本地导入目录再登记。如果目标已存在且内容不同，默认报 `STALE_LOCAL_IMPORT_COPY`，防止旧副本被静默覆盖；确认要用外部目录覆盖旧副本时再加 `--force`。`--enable` 会登记后默认启用，开发时通常先不加，回到插件中心按账号启用更可控。

代码入口：`backend/scripts/tp_plugin.py`。查看 `PROFILES`、`scaffold_plugin()`、`check_plugin()`、`register_plugin()` 和 `build_parser()`，分别对应 profile、生成、校验、登记和 CLI 参数。命令行为有变化时，以 `tp_plugin <subcommand> --help` 为准，不依赖文档中的源码行号。

## 2. 权限自动推导

`tp_plugin check` 会静态扫描 `plugin.py`，从可识别的 `ctx.messages.*`、`ctx.http.*` 和标准 action dict 推导权限草案，再和 manifest / `plugin.json` 里的 `permissions` 对比。

会推导的典型权限：

- `ctx.messages.send()` / `reply()` 或 action `send_message`、`send_photo`、`send_file` -> `send_message`
- `ctx.messages.read()` / `get()` / `history()` -> `read_chat`
- `ctx.messages.edit()` 或 action `edit_message`、`edit_caption` -> `edit_message`
- `ctx.messages.delete()` 或 action `delete_message` -> `delete_message`
- `ctx.messages.payout()` 或 action `payout` -> `payout`
- `ctx.http.*()` -> `external_http`

`check` 会报告：

- `permissions 草案`：静态扫描推导出的权限集合。
- `permissions 声明漏了`：代码用到了但 manifest 没声明。
- `permissions 声明多了`：manifest 声明了但静态扫描没看到。
- `permissions 高风险已显式声明`：如 `payout`，需要开发者确认用途确实应保留。
- `external_http 域名草案`：字面量 URL 能解析出的域名。
- 动态 URL 警告：无法静态确认域名白名单，需要人工核对。

审计边界：

- 它是审计模式，只报 diff，不改写 manifest / `plugin.json`。
- 它基于 AST 和字面量识别；如果把 `ctx.messages` 赋给别名、封装在 helper 里、动态构造 action type 或 URL，可能漏报，需要人工复核。
- `payout` 属于高风险权限，必须显式声明并在 PR / 发布说明里写清用途。

代码入口：`backend/scripts/tp_plugin.py` 的 `_infer_permissions_from_plugin_py()` 和 `_collect_permission_issues()`。前者负责 AST 推导，后者比较声明与实际使用并生成错误或警告。

## 3. 录制回放

录制用于把真实入站事件保存为 JSONL，之后用离线 dry-run 回放做插件回归。

账号配置打开录制：

```json
{
  "dev_mode": {
    "recording": true
  }
}
```

打开后，入站信封会按账号写入 `data/recordings/<account_id>/<YYYY-MM-DD>.jsonl`。录制入口只在 `dev_mode.recording` 为 true 时写入。

离线回放：

```bash
tp_replay run data/recordings/123/2026-07-10.jsonl
tp_replay run data/recordings/123/2026-07-10.jsonl --account-id 123
tp_replay run data/recordings/123/2026-07-10.jsonl --account-id 123 --compact
```

源码脚本等价写法：

```bash
cd backend
python scripts/tp_replay.py run ../data/recordings/123/2026-07-10.jsonl --account-id 123
```

`tp_replay` 会强制 dry-run：回放期间禁用再次录制，mock Telegram Bot API / Telethon client，把 action event 捕获到内存并输出 JSON。注意：默认分发器仍会通过 `AsyncSessionLocal` 读取真实数据库里的账号和插件状态，只允许连接 dev DB 运行。

代码入口：`backend/app/services/action_tap.py` 的 `dev_mode_recording_enabled()` 与 `emit_inbound_event()`，`backend/app/worker/replay.py` 的 `load_recording()`、`replay_recording()` 和 `_force_envelope_dry_run()`，以及 `backend/scripts/tp_replay.py` 的 CLI parser。

## 4. 命中调试器

命中调试器用于贴一条消息和账号上下文，看当前 worker 会怎样判定入口：直通、前缀命令、关键词、Event Bus 订阅是否命中，以及未命中原因。

请求端点：

```http
POST /api/dispatch/simulate
```

请求体字段：

```json
{
  "account_id": 123,
  "chat_type": "group",
  "chat_id": -100123456,
  "sender_id": 777,
  "text": "开始游戏",
  "via": "userbot"
}
```

返回值是 worker 内存态评估出的 trace，核心字段包括 `account_id`、`via`、`chat`、`text` 和 `stages`。`stages` 会列出各阶段的 matched / skipped 结果和 reason code；如果账号 worker 不在线，API 返回 `WORKER_OFFLINE`。

代码入口：`backend/app/api/dispatch_debug.py` 的 `DispatchSimulateRequest` 与 `simulate_dispatch()`，以及 `backend/app/worker/plugins/loader.py` 的 `evaluate_dispatch()`。接口 schema 以 FastAPI `/docs` 为准。

## 5. `dry_run` 干跑

`dry_run` 是账号级开发开关，用来做安全测试。账号配置打开：

```json
{
  "dev_mode": {
    "dry_run": true
  }
}
```

打开后，插件产生的发送、编辑、删除、媒体和 `payout` 等出口会记录 action / action_event，但不真实发送、不真实派奖。建议玩法插件开发时先开启 `dry_run`，配合命中调试器验证入口，再观察 action event / trace 里的参数摘要。

代码入口：`backend/app/services/action_tap.py` 的 `dev_mode_dry_run_enabled()` 与 `action_context_dry_run_enabled()`，`backend/app/worker/plugins/loader.py` 的 `_plugin_dev_mode_dry_run_enabled()` 和 `_record_userbot_dry_run()`，以及 `backend/app/services/interaction/delivery.py` 的 Delivery Executor。

## 6. 分级 trace

插件调试有三层 trace：

- 默认轻量：路由层默认只保留轻量 delivery stats，避免所有消息都写完整 trace。
- `strict_trace` 常驻：资金类、`payout` 类或高风险插件可在 manifest / `plugin.json` 声明 `strict_trace: true`，命中相关交互路由时启动完整 router trace。
- router-debug 临时开关：临时排查某账号、某插件或某聊天时，用短 TTL 打开完整 router trace。

临时打开 router-debug：

```http
POST /api/dispatch/router-debug-trace
```

请求体：

```json
{
  "account_id": 123,
  "plugin_key": "my_game",
  "chat_id": -100123456,
  "ttl_seconds": 300
}
```

`plugin_key` 和 `chat_id` 都可省略；省略时按账号范围打开。TTL 最小 1 秒，最大 3600 秒。

代码入口：`backend/app/services/account_bot_runtime.py` 的 `set_router_debug_trace()`、`_router_debug_trace_enabled()`、`_plugin_declares_strict_trace()` 与 `_start_router_trace()`，`backend/app/api/dispatch_debug.py` 的 `RouterDebugTraceRequest` 和 `enable_router_debug_trace()`，以及 `backend/app/worker/plugins/manifest.py` 的 `strict_trace` 字段。

## 7. 从零开发一个玩法插件的推荐流程

1. 生成骨架：

```bash
tp_plugin new guess_number --profile session_game
```

2. 写玩法逻辑：

- 在 `plugin.py` 里优先实现 `on_interaction(ctx, entry_key, payload)`。
- 用 `start_session`、`update_session`、`end_session` 管单局状态。
- 发送用 `send_message` / `edit_message` / `edit_caption` / `payout` 等标准 action 或 `ctx.messages` facade。
- 涉及派奖时，在 `permissions` 里显式声明 `payout`，并考虑加 `strict_trace: true`。

3. 本地审计：

```bash
tp_plugin check plugins/local_imports/guess_number
```

处理 `check` 的错误和警告，尤其是权限漏声明、权限多声明、动态 HTTP 域名和事件订阅未命中。

4. 登记插件：

```bash
tp_plugin register plugins/local_imports/guess_number
```

如果插件在外部目录开发，第一次可直接 register；之后如果 `plugins/local_imports` 已有旧副本且内容不同，确认覆盖后再用：

```bash
tp_plugin register /tmp/tp-plugins/guess_number --force
```

5. 账号启用并开启 dry-run：

```json
{
  "dev_mode": {
    "dry_run": true
  }
}
```

在插件中心给目标账号启用插件，先用 dry-run 验证开局、回复、结算和 payout 参数，不真实发消息或派奖。

6. 命中调试：

```http
POST /api/dispatch/simulate
```

贴账号、聊天、发送者和消息文本，确认命中预期规则、插件和入口；如果未命中，根据 `stages` 里的 reason code 查入口、关键词、事件订阅或账号 worker 状态。

7. 临时打开完整 trace：

```http
POST /api/dispatch/router-debug-trace
```

对某账号、插件或聊天开 300 秒左右短 TTL，复现一次真实输入，然后看 trace span 和 action_event。资金/派奖玩法稳定后，在 manifest / `plugin.json` 保留 `strict_trace: true`。

8. 录制回归样本：

```json
{
  "dev_mode": {
    "dry_run": true,
    "recording": true
  }
}
```

用真实入口跑一局，拿到 `data/recordings/<account_id>/<YYYY-MM-DD>.jsonl`。

9. 离线回放：

```bash
tp_replay run data/recordings/123/2026-07-10.jsonl --account-id 123
```

比较输出的 `action_events` 是否符合预期。修玩法逻辑后重复 `check -> register -> dry_run -> replay`，直到入口命中、动作参数和结算行为都稳定。

## 8. 风险和边界

- `tp_plugin check` 是静态审计，不等于运行时安全证明；别名、helper 封装、动态 action / URL 需要人工复核。
- `register --force` 会覆盖 `plugins/local_imports` 旧副本，只在确认外部目录是权威版本时使用。
- `dry_run` 只保证已接入 dry-run 判断的发送 / payout 出口不真实投递；绕过平台 facade 直接调用外部网络或原生 Telegram API 的代码不受它保护。
- `tp_replay` 默认会读取 dev DB 中的账号和插件状态；不要指向生产数据库运行。
- `recording` 会保存入站信封 JSONL，可能含聊天 ID、发送者 ID 和消息文本；只用于本地开发和最小必要样本。
- `strict_trace` 会增加 trace 写入量；只给资金、高风险或需要常驻审计的插件开启，临时排查优先用短 TTL router-debug。

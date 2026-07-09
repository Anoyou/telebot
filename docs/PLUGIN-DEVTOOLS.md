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

对码依据：

- `backend/scripts/tp_plugin.py:1`-`21`：CLI 设计和三条子命令说明。
- `backend/scripts/tp_plugin.py:45`：`--profile` 可选值 `session_game|command|passthrough`。
- `backend/scripts/tp_plugin.py:857`-`884`：`new` 生成文件并落盘。
- `backend/scripts/tp_plugin.py:1064`-`1121`：`check` 校验流程。
- `backend/scripts/tp_plugin.py:1159`-`1209`：`register` 复制、陈旧副本保护和登记逻辑。
- `backend/scripts/tp_plugin.py:1239`-`1306`：CLI 参数，包括 `--profile`、`--dir`、`--dry-run`、`--force`、`--enable`。

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

对码依据：

- `backend/scripts/tp_plugin.py:52`-`60`：权限风险分级，`payout` / `modify_identity` 为高风险。
- `backend/scripts/tp_plugin.py:61`-`79`：`ctx.messages.*` 和 action type 到权限的映射。
- `backend/scripts/tp_plugin.py:956`-`986`：AST 静态扫描 `plugin.py`，推导权限、HTTP 域名和动态 URL 次数。
- `backend/scripts/tp_plugin.py:989`-`1022`：生成草案、漏声明、多声明、高风险和 HTTP 域名警告。
- `backend/scripts/tp_plugin.py:1109`-`1111`：权限推导审计只报 diff，不改写文件。

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

对码依据：

- `backend/app/services/action_tap.py:76`-`84`：`dev_mode.recording` 开关识别。
- `backend/app/services/action_tap.py:164`-`186`：入站信封写入账号 JSONL。
- `backend/app/worker/plugins/loader.py:1523`-`1528`：从账号插件上下文找 recording 配置。
- `backend/app/worker/plugins/loader.py:5611`-`5616`：userbot 入站事件写录制信封。
- `backend/app/worker/replay.py:1`-`10`：回放隔离边界和 dev DB 警告。
- `backend/app/worker/replay.py:41`-`74`：读取 JSONL 并 replay。
- `backend/app/worker/replay.py:77`-`104`：强制 dry-run 回放并捕获 action events。
- `backend/app/worker/replay.py:494`-`507`：回放信封和配置强制 `dry_run=true`、`recording=false`。
- `backend/scripts/tp_replay.py:25`-`36`：`tp_replay run` 参数。
- `backend/scripts/tp_replay.py:39`-`55`：dev DB 警告和 JSON 输出。

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

对码依据：

- `backend/app/api/dispatch_debug.py:25`：路由前缀 `/api/dispatch`。
- `backend/app/api/dispatch_debug.py:28`-`35`：simulate 请求体字段。
- `backend/app/api/dispatch_debug.py:102`-`114`：`POST /simulate` 端点和 `WORKER_OFFLINE` 返回。
- `backend/app/worker/ipc.py:59`：IPC 命令 `dispatch_simulate`。
- `backend/app/worker/runtime.py:948`-`980`：worker 接收模拟命令并调用 `evaluate_dispatch`。
- `backend/app/worker/plugins/loader.py:1001`-`1084`：无副作用评估 direct passthrough、prefix command、keyword、event subscription。

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

对码依据：

- `backend/app/services/action_tap.py:65`-`73`：`dev_mode.dry_run` 开关识别。
- `backend/app/services/action_tap.py:92`-`97`：从 action context / account_config 读取 dry-run。
- `backend/app/worker/plugins/loader.py:1516`-`1538`：插件上下文和 action 层 dry-run 判断。
- `backend/app/worker/plugins/loader.py:1563`-`1583`：userbot 出口 dry-run 记录 action 和 action_event。
- `backend/app/worker/plugins/loader.py:2537`-`2548`：userbot `payout` dry-run 只记录。
- `backend/app/worker/plugins/loader.py:2727`-`2738`：userbot `send_message` dry-run 只记录。
- `backend/app/services/interaction/delivery.py:76`-`98`：interaction delivery dry-run 判断和记录。
- `backend/app/services/interaction/delivery.py:745`-`754`：interaction `payout` dry-run 只记录。
- `backend/app/services/interaction/delivery.py:873`-`884`：interaction `send_message` dry-run 只记录。

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

对码依据：

- `backend/app/services/account_bot_runtime.py:167`-`199`：默认轻量 router delivery stats。
- `backend/app/services/account_bot_runtime.py:240`-`310`：router-debug trace Redis key、TTL、启用和读取。
- `backend/app/api/dispatch_debug.py:37`-`42`：router-debug 请求体字段。
- `backend/app/api/dispatch_debug.py:117`-`124`：`POST /api/dispatch/router-debug-trace`。
- `backend/app/worker/plugins/manifest.py:23`-`24`、`71`-`72`、`109`：manifest `strict_trace` 字段。
- `backend/app/services/account_bot_runtime.py:313`-`330`：读取 builtin / installed 插件的 `strict_trace`。
- `backend/app/services/account_bot_runtime.py:346`-`377`：资金路由、规则插件和 Event Bus 候选触发 strict trace。
- `backend/app/services/account_bot_runtime.py:380`-`392`：启动 router trace 并记录原因。
- `backend/app/services/account_bot_runtime.py:1986`-`2076`：account bot debug trace 和轻量 stats。
- `backend/app/services/account_bot_runtime.py:2086`-`2225`：interaction bot debug / strict trace、路由 span 和轻量 stats。

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

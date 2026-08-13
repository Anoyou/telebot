# 能力接线协议

本文定义 TelePilot 新增注册面的静态接线契约。它统一的是命名、依赖、生命周期、所有权与失效语义，不成立统一注册机构，也不要求插件 loader、System Agent `ToolRegistry` 或平台能力控制面改用同一套运行时实现。

## 适用范围

新增注册面必须遵守本协议；现有三套注册表按下文记录其符合性映射与客观差距。`backend/app/services/capability_protocol.py` 仅提供结构类型，不能据此推断对象已经注册、能力已经就绪或依赖已经满足。

本协议不改变插件的直通、userbot、interaction bot 三种接入语义，不新增 registry、adapter、调度、状态存储或运行时校验。

## 命名约定

标识符一经持久化或对外暴露即视为稳定接口；重命名必须按兼容性变更处理。展示名称和描述可以本地化，不能作为标识符。

| 名称 | 规范 | 当前映射 |
| --- | --- | --- |
| plugin key | 新增 key 使用小写 snake_case，建议满足 `^[a-z][a-z0-9_]{0,62}$`；全局唯一且不含版本号 | `Manifest.key` 与 `Plugin.key` |
| feature key | 插件叶的 feature key 必须与 plugin key 完全相同；不能使用展示名或工具名代替 | `AccountFeature.feature_key`、`PluginContext.feature_key` |
| module key | 使用稳定的小写 snake_case；表示粗粒度平台能力，不表示某个插件或通道实例 | 当前固定为 `ai`、`interaction_bot`、`webhooks`、`ledger`、`dispatch_debug` |
| tool name | 注册表中的完整名称必须全局唯一。内置工具使用 `domain.action`，两段均使用小写 snake_case；插件本地名使用小写 snake_case，暴露名为 `plugin_{plugin_key}.{local_tool_name}` | `ToolSpec.name` |

上述格式是新增标识符的规范。存量标识符由现有兼容规则继续接纳；本协议不要求迁移或收紧现有校验器。

## 依赖声明

依赖只引用稳定 key，不引用展示名、Python 导入路径或工具名。声明能力需求不等于获得权限，也不能越过运行预设、安全闸或管理员开关。

### 叶到枝：`requires_platform_capabilities`

插件通过 manifest 的 `requires_platform_capabilities` 声明运行所需的平台 module key。每一项都是该叶提供相应入口的必要条件：依赖未知、状态不可读、generation 过期或模块未处于 `ready` 时，新增消费路径必须 fail-closed。

存量插件缺少声明时继续按既有兼容语义加载，不能据此推断它不需要任何平台能力；声明补齐与按叶点枝不属于本协议的实施范围。

### 叶到叶：`requires_features`

插件通过 manifest 的 `requires_features` 声明依赖的其它 feature key。依赖项使用被依赖插件的 plugin key，因为插件叶的 feature key 与 plugin key 相同。依赖声明表达运行前置条件，不表达所有权，也不会把被依赖叶的卸载责任转移给调用方。

对新增消费路径，依赖不存在、未启用、未 `ready` 或代际已失效时必须拒绝使用依赖能力。存量 loader 的实际检查粒度见“符合性映射与差距”。

## 生命周期

能力生命周期沿用以下五态：

| 状态 | 含义 | 是否可接收新工作 |
| --- | --- | --- |
| `starting` | 目标为开启，正在初始化或等待收敛 | 否 |
| `ready` | 初始化和依赖检查已经收敛，可以提供能力 | 是 |
| `quiescing` | 已停止接收新工作，正在排空并释放资源 | 否 |
| `stopped` | 已停止且不提供能力 | 否 |
| `failed` | 启动、停止或收敛失败，错误需要显式可见 | 否 |

标准启停路径为 `stopped → starting → ready → quiescing → stopped`。启动、停止或收敛失败进入 `failed`；后续恢复必须重新进入对应的收敛过程，不能把旧的 `ready` 当作仍然有效。目标开关表示期望状态，不能替代运行态判断。

`ready` 是唯一允许新增调用的状态。状态缺失、未知、读取失败或超出当前 generation 均按不可用处理。

## 注册所有权与 generation

- 谁执行注册，谁就是该条目的 owner，并负责在 disable、reload、unload、启动失败和部分注册失败时清理自己登记的条目及关联资源。清理应可重复调用；不能依赖进程退出兜底。
- 新注册面应让所有权可追踪，并向注册者提供等价于 `Disposable` 的清理入口。generation 不能代替显式 unload；旧条目仍须由 owner 移除。
- 可见条目集合或能力语义发生变化时推进 generation。消费方捕获注册时或读取时的 generation，并在执行副作用前与当前 generation 比对。
- generation 不一致表示引用陈旧：立即拒绝执行并重新发现；不能尝试用旧对象、旧上下文或旧缓存继续工作。generation 只用于失效判断，不是权限凭证。

## Fail-closed 原则

新增注册面只有在标识符有效、依赖满足、生命周期为 `ready` 且 generation 为当前代时才可提供能力。任何状态未知、缓存未就绪、依赖无法确认、owner 已卸载或 generation 过期的情况都按不可用处理，并返回可诊断的拒绝原因。

安全检查必须落在副作用之前；列举或展示信息不能被解释为能力已经可调用。为兼容存量路径而保留的例外必须留在原实现中，不能扩散为新注册面的默认行为。

## 插件 loader：符合性映射与差距

### 符合性映射

- `Manifest.key`、`Plugin.key`、`AccountFeature.feature_key` 与 `PluginContext.feature_key` 形成插件叶的身份映射。manifest 已提供 `requires_features` 与 `requires_platform_capabilities`。
- loader 的账号运行态持有 generation；命令注册记录 owner plugin key 与 generation，旧 `PluginContext` 会因 generation 不一致被拒绝。
- reload/disable 路径会注销插件命令和 scheduler owner，调用 `on_shutdown`，并移除实例与上下文，符合“注册者负责清理”的方向。

### 客观差距

- loader 不是 `Registrable` 的通用实现，也不返回统一 `Disposable`；各资源类型沿用各自的追踪和清理路径。
- `requires_features` 当前只用 `all_plugins()` 检查依赖插件是否已被发现，不证明依赖已启用或处于 `ready`。
- loader 与远程安装服务接受的存量 plugin key 范围比本协议的新 key 规范宽，且 System Agent 插件工具暴露使用更严格的范围。
- 存量插件允许缺少平台能力声明，运行入口继续由现有兼容裁剪逻辑处理。

这些差距是现状描述，不是本轮修改清单。

## System Agent `ToolRegistry`：符合性映射与差距

### 符合性映射

- `ToolSpec.name` 是唯一工具名；内置工具普遍使用 `domain.action`，插件工具使用 `plugin_{plugin_key}.{tool_name}`。
- `register()` 和成功的 `unregister()` 都会推进 generation 并清空只读列表缓存。
- 动态插件工具由外围集合记录名称，刷新前逐项 `unregister()`，具备明确的动态清理路径。

### 客观差距

- `ToolSpec` 没有通用 owner 字段，`register()` 不返回清理句柄；动态工具所有权由 `plugin_tools.py` 的外围集合维护。
- generation 当前用于注册表列表缓存失效，不构成统一的调用前陈旧引用检查。
- 工具注册表不建模 `starting/ready/quiescing/stopped/failed` 生命周期；工具可用性沿用 `ToolSpec.available` 与现有角色、通道过滤。

这些差距是现状描述，不是本轮修改清单。

## 平台能力模块：符合性映射与差距

### 符合性映射

- 控制面使用五个精确 module key，并已采用 `starting/ready/quiescing/stopped/failed` 五态。开启路径为 `starting → ready`，关闭路径为 `quiescing → stopped`，异常进入 `failed`。
- desired 与 generation 持久化，进程内维护 generation 和 runtime 快照；Worker reload ACK 会验证加载代际不早于目标代。
- 通用缓存查询支持在快照未就绪时 fail-closed，公开能力入口已有相应使用。

### 客观差距

- 该文件是固定五模块的控制与收敛面，不是可动态 `register/unregister` 的通用模块目录，也不返回统一 `Disposable`。
- 部分存量展示或兼容入口有各自的默认开启语义，并非所有调用点都统一执行本协议面向新增注册面的 fail-closed 规则。
- 本文件已有的 `RuntimeState` 与静态协议的 `LifecycleState` 值域一致，但两者没有运行时继承或适配关系。

这些差距是现状描述，不是本轮修改清单。

# TelePilot Agent Rules

## 版本与发布

- `main` 是唯一发布分支。`Agent-beta` 和其它开发分支不得独立迭代正式版本号或发布；功能必须先合入 `main`，再由 `main` 统一 bump、写更新日志和推送。
- 发布前必须执行 `git fetch origin --prune`，同时检查 `origin/main` 与所有仍活跃的发布/开发分支版本，不能只依据当前工作树选择下一个版本号。
- 不要为每个微小提交单独迭代版本号。版本号只在准备发布、推送稳定检查点、创建 release/PR，或用户明确要求“推一版/发一版”时统一迭代。
- 一批相关改动只对应一个版本号；开发过程中先积累到 `CHANGELOG.md` 的 `Unreleased`，发布前再决定版本号并移动到正式版本段落。
- 版本 bump 必须按 SemVer 判断：
  - `MAJOR（主版本）`：破坏兼容的数据库迁移、配置格式变更、API 路径或语义不兼容、老版本无法平滑升级。
  - `MINOR（次版本）`：用户可感知的新能力、主要入口/信息架构重组、后端能力完整前端化、新插件或重要工作流变化。
  - `PATCH（补丁版本）`：bug 修复、文案、小 UI、错误提示、测试补充、兼容性补丁和不改变主要用户路径的小调整。
- 0.x 阶段额外约定：`0.X.0` 表示阶段性能力版本，`0.X.Y` 表示同一阶段内的补丁；不要把第三位当作日常流水号。
- 发布时版本号必须同步更新：`backend/app/__init__.py`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/src/lib/version.ts`，并用中文写入 `CHANGELOG.md`。
- commit / PR / release 文案使用中文。

## 工作区安全

- 可能存在用户或其他 agent 的未提交改动。不要 revert、checkout 或 reset 你没有明确负责的改动。
- 手工编辑文件使用 `apply_patch`。

## 日志安全

- 主进程或独立 worker 子进程调用 `logging.basicConfig()` 后，必须安装 `install_sensitive_log_filter()`；新增进程入口时要用回归测试确认 Telegram Bot API URL、Authorization 和代理凭据不会以明文进入日志。
- `SystemSetting` 中保存的 Token、密码或其他可复用凭据必须使用 `*_enc` 字段经 `MASTER_KEY` 加密，并同步加入 `app.scripts.rekey` 覆盖；公开鉴权入口不得在凭据校验前创建或修改持久化配置。

## 项目级 Agent Playbook

- 处理代码、文档、排障、UI、部署或发布任务时，先阅读并按需使用 `docs/AGENT-PLAYBOOKS.md`。
- 复杂需求先走基础进入流程；Bug/异常优先使用 Plugin Hunt 的根因定位口径；UI 改动使用 UI Check；部署/远端操作使用 Deploy Check；推版、PR、release 使用 Release Check。
- Playbook 只用于约束执行和验收，不得覆盖用户当前指令、版本发布规则或工作区安全规则。

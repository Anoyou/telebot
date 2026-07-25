# TelePilot Agent Rules

## 版本与发布

- `main` 是稳定发布线，只接收已经验证稳定的 beta；开发分支是 beta 发布线，必须独立维护带 prerelease 后缀的版本号（例如 `0.81.0-beta.1`），稳定后再合入 `main` 发布对应正式版（例如 `0.81.0`）。
- 发布前必须执行 `git fetch origin --prune`，同时检查 `origin/main` 与所有仍活跃的发布/开发分支版本，不能只依据当前工作树选择下一个版本号。
- 不要为每个微小提交单独迭代版本号。版本号只在准备 beta 检查点、稳定发布、推送稳定检查点、创建 release/PR，或用户明确要求“推一版/发一版”时统一迭代。
- 一批相关改动只对应一个版本号；开发过程中先积累到 `CHANGELOG.md` 的 `Unreleased`，准备 beta 检查点或稳定发布时再决定版本号并移动到对应版本段落。
- 版本 bump 必须按 SemVer 判断：
  - `MAJOR（主版本）`：破坏兼容的数据库迁移、配置格式变更、API 路径或语义不兼容、老版本无法平滑升级。
  - `MINOR（次版本）`：用户可感知的新能力、主要入口/信息架构重组、后端能力完整前端化、新插件或重要工作流变化。
  - `PATCH（补丁版本）`：bug 修复、文案、小 UI、错误提示、测试补充、兼容性补丁和不改变主要用户路径的小调整。
- 0.x 阶段额外约定：`0.X.0` 表示阶段性能力版本，`0.X.Y` 表示同一阶段内的补丁；不要把第三位当作日常流水号。
- prerelease 的基础版本决定 SemVer 级别，`beta.N` 只表示同一候选版本的迭代次数：
  - 新阶段能力从新的基础版本 `-beta.1` 开始，例如 `0.81.0-beta.1`。
  - 同一候选版本继续修复和收口时只递增 beta 序号，例如 `0.81.0-beta.2`。
  - 同阶段补丁版本从新的补丁基础版本开始，例如 `0.81.1-beta.1`。
  - beta 期间若加入新的 minor 或 major 级变化，必须提升基础版本并从 `-beta.1` 重新开始，不能只递增 beta 序号。
  - beta 稳定并合入 `main` 后去掉 prerelease 后缀，发布相同基础版本的正式版。
- beta 检查点与稳定发布都必须同步更新：`backend/app/__init__.py`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/src/lib/version.ts`，并用中文写入 `CHANGELOG.md`。
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

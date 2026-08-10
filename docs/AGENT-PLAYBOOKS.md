# TelePilot Agent Playbooks

这些 playbook 借鉴 Waza 的 skills 思路：不要把「想清楚」「定位根因」「发布检查」「视觉验收」停留在口头习惯里，而是拆成可重复执行的小流程。它们不是替代 `AGENTS.md` 的硬规则，而是把 TelePilot 的高风险链路变成每次都能复用的执行清单。

## 使用方式

- 每轮代码任务先执行基础进入流程，再按任务类型选择一个或多个 playbook。
- 如果用户明确限制范围，以用户当前指令和 `AGENTS.md` 为准；playbook 只能收窄和校验，不得扩大任务。
- 如果工作树已有未提交改动，先识别是否属于当前任务；不属于本轮的改动只观察、不回滚、不格式化。
- 如果任务很多，可以拆子 Agent，但每个子 Agent 必须有写入范围、禁区、验证命令和交付格式。

## 基础进入流程

适用：所有代码、文档、发布和排障任务。

1. 确认真实仓库根目录：`git rev-parse --show-toplevel`。
2. 查看分支和工作树：`git status --short --branch`。
3. 读取项目规则：`AGENTS.md`，以及全局经验文档。
4. 明确本轮范围：必须做、可以暂缓、明确不做。
5. 找到最近相关事实，而不是靠记忆：版本文件、changelog、测试脚本、CI、运行日志、实际 diff。

交付要求：

- 汇报中说明改了哪些文件、验证了什么、哪些失败属于环境限制。
- 不把子 Agent 自述、历史聊天或文件夹名当成事实证据。

## Release Check

适用：用户要求推更新、发版、创建 PR/release、推送稳定检查点，或本轮需要准备可合并版本。

触发词示例：`推一版`、`发版`、`release`、`创建 PR`、`稳定检查点`。

执行清单：

1. 用 `git diff --stat` 和关键 diff 确认本批实际变更，不写愿景式发布说明。
2. 按 SemVer 判断版本级别：
   - `MAJOR（主版本）`：破坏兼容的数据库迁移、配置格式、API 路径或语义变化。
   - `MINOR（次版本）`：用户可感知的新能力、主入口重组、新插件或重要工作流变化。
   - `PATCH（补丁版本）`：修复、文案、小 UI、测试、兼容性补丁。
   - 0.x 阶段：`0.X.0` 是阶段性能力版本，`0.X.Y` 是同阶段补丁，不把第三位当流水号。
3. 按分支选择版本线：
   - 开发分支发布 beta 检查点，使用目标正式版本的 prerelease 版本，例如 `0.81.0-beta.1`；同一候选版本继续收口只递增 `beta.N`。
   - beta 期间加入新的 minor 或 major 级变化时，提升基础版本并从 `-beta.1` 重新开始；补丁候选则提升 PATCH 后从 `-beta.1` 开始。
   - `main` 只发布稳定版；beta 验证稳定并合入 `main` 后，去掉后缀发布相同基础版本，例如 `0.81.0-beta.3` 对应 `0.81.0`。
4. 同步更新五处版本信息：
   - `backend/app/__init__.py`
   - `backend/pyproject.toml`
   - `frontend/package.json`
   - `frontend/src/lib/version.ts`
   - 运行 `make codegen`，由后端版本生成 `openapi/telepilot.openapi.json` 的 `info.version`
5. 用中文把 `CHANGELOG.md` 的 `Unreleased` 移到 beta 或稳定版本段落，只记录实际落地内容。
6. Commit、PR、release 标题和正文使用中文。
7. 发 tag 或 release 前，确认 tag 指向正确提交，不能指向旧 HEAD。
8. Beta 开发提交必须保留 prerelease 版本；正式版检查点完成后，允许把
   `Beta` 快进到与 `main` 完全相同的稳定版提交，但 `Beta` 的下一次开发
   提交必须先切换到新的 prerelease 版本。面向 `main` 的 PR 和 `main` push
   必须先切换为稳定版本，并在 `CHANGELOG.md` 中存在当前精确版本段。
9. 等待 `CI Gate` 成功后再核对镜像；Web、Frontend、Updater 的发布标签都
   必须来自代码版本校验，且清单同时包含 `linux/amd64` 与 `linux/arm64`。

推荐验证：

```bash
pnpm --dir frontend typecheck
pnpm --dir frontend build
cd backend && . .venv/bin/activate && ruff check app
cd backend && . .venv/bin/activate && pytest
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
git diff --check
```

如果验证命令因本地环境失败，要写清命令、解释器/依赖/服务状态和第一条可行动错误。

## Plugin Hunt

适用：远程插件、内置插件、插件安装/更新/启停、沙箱 facade、AI/HTTP 权限、worker 热加载相关问题。

进入实现前必须能写出一句根因假设：

> 因为 A 数据/状态在 B 链路没有同步，导致 C 页面/worker/API 表现为 D。

排查链路：

1. 先区分问题发生在安装记录、feature matrix、前端展示、API 写入、worker reload、运行时权限还是 Telegram 事件链路。
2. 对远程/installed 插件，优先查：
   - `InstalledPlugin` 当前安装状态
   - `AccountFeature` 账号启用状态和 `last_error`
   - `feature_matrix` 返回字段
   - `plugins/installed/<key>/plugin.json` 与 `manifest.py`
   - loader 授权、缓存清理和热更新日志
3. 对 `ctx.http` / `ctx.ai`，确认 manifest 权限、平台 facade、quota 预扣/结算、provider fallback 和日志脱敏。
4. 对 UI 状态异常，先确认 API 响应真实字段，再决定前端展示逻辑；不要让前端猜测磁盘状态。
5. 修复后补最小回归测试，覆盖数据链路中出错的那一层。

交付要求：

- 报告根因、改动点、验证命令和仍需观察的运行时风险。
- 不用“可能是缓存”这类泛因收尾；如果没有证据，继续查。

## UI Check

适用：前端页面、工作台布局、插件中心、AI 设置、日志中心、配置表单、移动端/PWA 相关变更。

设计原则：

- TelePilot 是工作台，不是营销页。优先状态清楚、信息可扫、入口一致。
- 同类页面的标题、图标、tablist、按钮、禁用状态和错误提示应保持一致。
- 模板、只读字段和预览要区分：可编辑内容用输入控件，只读结果用展示组件或预览。
- 按钮状态要解释得通：已安装、可更新、更新中、禁用、失败原因都要可见。
- 移动端和窄视口下，文字、徽章、按钮不能重叠或被挤成不可读状态。

推荐验证：

1. 先运行类型检查：`pnpm --dir frontend typecheck`。
2. 能启动前端时，打开关键页面做桌面和移动宽度截图。
3. 检查 loading、empty、error、disabled、success 状态。
4. 检查长中文、长英文、长插件名、长错误消息是否撑破布局。
5. 如果改动涉及实时状态或后台任务，确认停止、完成、失败、取消后的文案和按钮状态。

交付要求：

- 明确说明看过哪些页面和视口。
- 如果无法启动浏览器或 dev server，说明原因，并至少完成类型/构建层面的替代验证。

## Deploy Check

适用：生产部署、Docker、Nginx、systemd、远端服务器、数据库迁移、安装脚本、`make prod-update` 相关变更。

执行清单：

1. 先确认变更是否会影响数据、配置格式、环境变量、volume、反代或迁移顺序。
2. 远端或生产配置修改前必须备份，并记录备份路径。
3. 迁移类改动必须说明升级路径、回滚限制和 downgrade 是否安全。
4. 部署脚本要区分文档变更、前端变更、后端变更、依赖变更和数据库变更。
5. 修改后验证服务状态、端口、HTTP 健康检查和关键日志。

推荐验证：

```bash
make status
# 源码开发模式
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz

# Docker Compose 生产模式：web:8000 不暴露到宿主机
PUBLISH_PORT="$(sed -n 's/^WEB_PORT_PUBLISH=//p' .env | tail -n1 | tr -d '"')"
PUBLISH_PORT="${PUBLISH_PORT##*:}"
curl -fsS "http://127.0.0.1:${PUBLISH_PORT:-80}/healthz"
docker compose exec -T web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5).read().decode())"
docker compose ps
docker compose logs --tail=100 web
```

交付要求：

- 汇报备份、修改、验证和回滚路径。
- 不把“命令退出 0”当成唯一成功标准。

## Project Health

适用：每隔几个版本、CI 或文档反复失效、Agent 经常踩同一类坑、项目规则需要刷新时。

检查内容：

1. `AGENTS.md`、本文件和全局经验文档是否冲突；冲突时以更具体、更新的规则为准。
2. `README.md`、`docs/*.md` 与真实 API、Makefile、CI 是否一致。
3. 版本文件、changelog、CI version-sync 是否仍覆盖发布风险。
4. 前端关键页面是否有可复用的验收路径和截图更新口径。
5. 插件开发文档是否写清数据链路、兼容边界、排查顺序和最小示例。

文档更新门禁：

- 新增或修改公开 API、环境变量、默认开关、错误码、fail-open / fail-closed 语义、资金状态机或插件契约时，同一批改动必须更新对应 README、部署、安全或插件文档。
- “当前版本”从四个源版本文件与 OpenAPI 快照的 `info.version` 核对；不要把旧版本号写进长期有效的架构和开发说明，版本历史留在 `CHANGELOG.md`。
- 源码证据优先引用文件和稳定函数/类名，不引用容易漂移的行号。接口请求体以 FastAPI `/docs` 和 Pydantic schema 为准，CLI 参数以 `--help` 为准。
- `/healthz` 只用于 liveness，生产就绪和更新完成使用 `/readyz`。文档必须明确两者差异。
- 示例命令要在仓库当前 Makefile、脚本或 package scripts 中真实存在；相对路径要写清从仓库根目录还是子目录执行。

交付要求：

- 输出问题清单，按 blocker / should-fix / later 分组。
- 只修本轮授权范围内的问题；其余记录为后续项。

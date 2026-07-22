# Contributing to TelePilot

> 个人项目 + 单租户 self-hosted 定位。fork 自由用，PR 暂缓接大改。
> 提 PR 前先看完本文档（**5 分钟**）和 [docs/AGENT-PLAYBOOKS.md](docs/AGENT-PLAYBOOKS.md)。

---

## 适合 PR 的事

✅ 值得提：
- Bug 修复（先开 issue 复现，再 PR）
- 文档错字 / 翻译 / 例子补充
- 测试覆盖率提升（pytest）
- 移植样例插件到 `examples/plugins/`（参考 `docs/PLUGIN-DEV-GUIDE.md`）
- 小幅 UX 改进（< 200 行 diff）

❌ 不适合 PR（先开 issue 讨论）：
- 大重构（> 500 行 diff）
- 引入新依赖（pyproject.toml / package.json 加包）
- 改数据库 schema（涉及迁移）
- 任何破坏 SemVer 兼容性的改动

❌ 不接的方向（不要浪费时间）：
- 多用户 / RBAC / 团队协作功能
- 邮件 / Slack / Discord 通知（TG-self 已够；fork 自己加）
- 备份云端化 / Prometheus 集成

---

## 开发环境（5 分钟搭起来）

### 前置

- Python 3.12+
- Node 20+ / pnpm 10.23.0+
- Docker（OrbStack / Docker Desktop 都行）
- macOS / Linux（Windows 没测过）

### 起项目

```bash
# 1. clone + 装依赖
git clone https://github.com/<yourusername>/telebot.git telepilot
cd telepilot
cp .env.example .env  # 改 MASTER_KEY / JWT_SECRET 为强随机串
chmod 600 .env

cd backend && python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
cd ../frontend && pnpm install
cd ..

# 2. 一键起（PG + Redis + alembic + uvicorn + vite + 清孤儿 worker）
make up

# 3. 浏览器开 http://localhost:5173 → 注册第一个超管账号
```

改完代码后建议使用 `make restart`；涉及 worker 的改动不要只依赖后端热重载，以免旧子进程继续运行。

### 跑测试 / Lint / Build

```bash
cd backend && pytest -q && ruff check .
cd frontend && pnpm run typecheck && pnpm test && pnpm run build
```

提 PR 前这三条**必须**全绿，CI 也会跑同样的检查。

---

## 版本与 CHANGELOG

- 不要为每个微小提交单独迭代版本号。开发过程中先把变更记录到 `CHANGELOG.md` 的 `Unreleased`。
- 准备发布、推送稳定检查点、创建 release/PR，或维护者明确要求“推一版/发一版”时，再按本批改动的最高影响级别统一 bump 一次版本号。
- `MAJOR` 用于破坏兼容；`MINOR` 用于用户可感知的新能力、主要入口/信息架构重组、重要工作流变化；`PATCH` 用于 bug 修复、文案、小 UI、错误提示、测试补充和兼容性补丁。
- 发布时同步更新 `backend/app/__init__.py`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/src/lib/version.ts`，并把 `Unreleased` 内容移动到新的中文版本段落。

---

## 提 PR 流程

```bash
# 1. fork 后克隆你的 fork
git clone git@github.com:<yourname>/telebot.git telepilot
cd telepilot
git remote add upstream https://github.com/<projectowner>/telebot.git

# 2. 从 main 拉最新
git fetch upstream
git checkout -b feat/my-thing upstream/main

# 3. 写代码 + 跑测试

# 4. commit，提交说明使用中文
git commit -m "修复：补齐流式响应终态校验"

# 5. push 到自己 fork
git push origin feat/my-thing

# 6. GitHub UI 上开 PR，模板会自动加载
```

---

## 代码规范

### Python（后端）

- 风格：`ruff check .`（pyproject.toml 里有完整 rule 列表）
- 类型注解：函数签名都加，`mypy` 不强制但建议
- 异步：worker 里**不要**写阻塞调用（用 `httpx.AsyncClient` / `aiofiles` / `asyncio.to_thread`）
- 日志：用 `_log(redis, account_id, level, message, source="event/system")`，不要 `print`
- 不要在 GET 接口返回明文密钥（约定 D）

### TypeScript（前端）

- `pnpm run build` 不能有 TS 错（CI 跑 `tsc -b`）
- 不引入新 UI 库（已用 shadcn-style + Radix + lucide-react，够了）
- 不引入新 state library（用 TanStack Query + useState 就够）
- 改 `frontend/src/api/types.ts` **只追加**自己的块，不改他人块

### 提交信息

```
新增：系统助手接入原生流式输出
修复：流式响应提前结束时拒绝伪成功
文档：同步插件 AI facade 契约
重构：提取协议适配器（不改外部行为）
测试：补充 Responses 失败终态回归
维护：更新 CI 与开发依赖
性能：合并同一动画帧内的增量渲染
```

主题必须使用中文，并尽量说明实际影响。例如：`修复（调度器）：支持秒级间隔并补齐边界校验`。

---

## 数据库迁移（**最容易翻车的部分**）

历史迁移分叉信息以 `backend/alembic/versions/` 和当前 `alembic heads` 输出为准。规矩：

```bash
# 写迁移前先看当前 head
cd backend && alembic heads

# 编号 = head + 1
# 文件名形如 backend/alembic/versions/0015_add_xxx.py
# 文件内 down_revision 必须 = head（你刚才 alembic heads 看到的）

# 写完跑一遍
alembic upgrade head
alembic heads  # 必须只有一个 head

# downgrade 也要能跑（除非本质不可逆，迁移注释里说明）
alembic downgrade -1
alembic upgrade head
```

PR 模板里有专门的迁移自查项 —— **每条都勾上**才合并。

---

## 测试要求

- 后端新功能：至少 1 个 pytest 用例覆盖 happy path
- 修 bug：先写一个失败的测试（reproduce），再修代码让它过
- 前端：至少执行 typecheck、单测和生产构建；涉及页面状态时再执行 `test:a11y` 与 `test:visual`
- 安全相关：必须有正面 + 反面用例（如改密码：旧密码错该拒、新密码弱该拒、改成功后旧 token 失效）

---

## 安全漏洞

**不要**公开提 issue。走 GitHub Security Advisories：

`Repo → Security → Report a vulnerability`

详细可以看 [docs/SECURITY-OPS.md](docs/SECURITY-OPS.md)。

---

## 行为准则

简短版：

- **对人友好，对代码严格**
- 提 issue / PR 是协作不是索取，作者没义务马上回
- 不要因为别人没采纳建议就拉踩
- 不接受人身攻击 / 歧视性言论 / 钓鱼引战
- 不在 PR / issue 里推销其它项目 / 服务

违反 → 删评论 + block，不解释。

---

## 致谢

参考 / 启发：

- [Telethon](https://codeberg.org/Lonami/Telethon) — 整个 TG 协议层靠它
- [shadcn/ui](https://ui.shadcn.com/) — 前端 UI 风格

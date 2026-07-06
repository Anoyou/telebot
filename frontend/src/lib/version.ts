// 应用版本号 — 前端单点定义。
//
// 版本号只在准备发布、推送稳定检查点、创建 release/PR，或用户明确要求
// “推一版/发一版”时统一迭代；不要为每个微小提交单独 bump。
//
// 每次正式 bump 同时改 4 处（缺一不可）：
//   1) frontend/src/lib/version.ts          ← 本文件（前端 UI 显示）
//   2) frontend/package.json                ← npm/pnpm 包元数据
//   3) backend/app/__init__.py              ← Python 包 __version__（main.py + ,version 都读它）
//   4) backend/pyproject.toml               ← pip install / 打包发布元数据
//
// 同步处理：把 CHANGELOG.md 的 Unreleased 内容移动到新的
// `## [x.y.z] — yyyy-mm-dd` 段，并用中文说明。
//
// 命名约定（SemVer：MAJOR.MINOR.PATCH）：
//   - MAJOR  破坏兼容的数据库迁移、配置/API 不兼容、无法平滑升级
//   - MINOR  用户可感知的新能力、主要入口/信息架构重组、重要工作流变化
//   - PATCH  bug 修复、文案、小 UI、错误提示、测试补充、兼容性补丁
//
// APP_STAGE 是非正式标签：
//   - 路线图阶段："MVP"、"Sprint 2"、"RC1"
//   - 生产稳定时设为 null（达到 1.0.0 通常就摘掉）

export const APP_VERSION = "0.49.23";
export const APP_STAGE: string | null = null;

/** Sidebar / About 等 UI 处使用的展示串。例："v0.2.0 · Sprint 2" 或 "v0.2.0"（STAGE 为 null 时）。 */
export const APP_VERSION_LABEL = APP_STAGE
  ? `v${APP_VERSION} · ${APP_STAGE}`
  : `v${APP_VERSION}`;

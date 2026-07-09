import React, { Suspense, lazy } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { RequireAuth } from "@/components/layout/RequireAuth";

import { Login } from "@/pages/Login";
import { Dashboard } from "@/pages/Dashboard";
import { Spinner } from "@/components/ui/misc";

// 把不影响首屏的页面拆成 lazy chunk：
//   - 用户最常进入的是 Dashboard 与账号列表，这些保持 eager；
//   - 插件中心、Logs（拖大 echarts）、设置子页、AI、模板、账号详情 / 向导 / 各
//     feature 配置页都按需加载。
//   - vite.config.ts 里另有 manualChunks 把 echarts / highlight.js / react-markdown
//     单独拆 chunk，大依赖只在用到的页面拉一次。
const AccountWizard = lazy(() => import("@/pages/Accounts/Wizard").then(m => ({ default: m.AccountWizard })));
const AccountDetail = lazy(() => import("@/pages/Accounts/Detail").then(m => ({ default: m.AccountDetail })));
const AutoReplyConfig = lazy(() => import("@/pages/Plugins/configs/AutoReply").then(m => ({ default: m.AutoReplyConfig })));
const AutorepeatConfig = lazy(() => import("@/pages/Plugins/configs/Autorepeat").then(m => ({ default: m.AutorepeatConfig })));
const CodexImageConfigPage = lazy(() => import("@/pages/Plugins/configs/CodexImageConfig").then(m => ({ default: m.CodexImageConfigPage })));
const ChatGPTImageConfigPage = lazy(() => import("@/pages/Plugins/configs/ChatGPTImageConfig").then(m => ({ default: m.ChatGPTImageConfigPage })));
const SchedulerConfig = lazy(() => import("@/pages/Plugins/configs/Scheduler").then(m => ({ default: m.SchedulerConfig })));
const Game24ConfigPage = lazy(() => import("@/pages/Plugins/configs/Game24Config").then(m => ({ default: m.Game24ConfigPage })));
const GenericPluginConfigPage = lazy(() => import("@/pages/Plugins/configs/GenericPluginConfig").then(m => ({ default: m.GenericPluginConfigPage })));
const Logs = lazy(() => import("@/pages/Logs").then(m => ({ default: m.Logs })));
const SettingsIndex = lazy(() => import("@/pages/Settings/Index").then(m => ({ default: m.SettingsIndex })));
const PluginsHome = lazy(() => import("@/pages/Plugins").then(m => ({ default: m.PluginsHome })));
const PluginsTemplatesPage = lazy(() => import("@/pages/Plugins").then(m => ({ default: m.PluginsTemplatesPage })));
const PluginsSchedulerPage = lazy(() => import("@/pages/Plugins").then(m => ({ default: m.PluginsSchedulerPage })));
const PluginsAutoCommandWhitelistPage = lazy(() => import("@/pages/Plugins").then(m => ({ default: m.PluginsAutoCommandWhitelistPage })));
const MessageTemplateLabPage = lazy(() => import("@/pages/Plugins").then(m => ({ default: m.MessageTemplateLabPage })));
const PluginsManagePage = lazy(() => import("@/pages/Extensions").then(m => ({ default: m.Extensions })));
const InteractionIndex = lazy(() => import("@/pages/Interaction/Index").then(m => ({ default: m.InteractionIndex })));
const LedgerPage = lazy(() => import("@/pages/Ledger").then(m => ({ default: m.LedgerPage })));
const DispatchDebugPage = lazy(() => import("@/pages/DispatchDebug").then(m => ({ default: m.DispatchDebugPage })));
const WebhooksPage = lazy(() => import("@/pages/Webhooks").then(m => ({ default: m.WebhooksPage })));
const AIIndex = lazy(() => import("@/pages/AI/Index").then(m => ({ default: m.AIIndex })));

type AppErrorBoundaryState = { hasError: boolean };

function PageFallback() {
  return (
    <div className="flex h-[40vh] items-center justify-center">
      <Spinner className="text-primary" />
    </div>
  );
}

function AIProvidersRedirect() {
  const location = useLocation();
  const targetParams = new URLSearchParams(location.search);
  targetParams.set("tab", "providers");
  if (targetParams.get("new") === "1") {
    targetParams.set("newProvider", "1");
  }
  targetParams.delete("new");
  return <Navigate to={`/ai?${targetParams.toString()}`} replace />;
}

export class AppErrorBoundary extends React.Component<
  React.PropsWithChildren,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    console.error("App crashed:", error);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex min-h-screen items-center justify-center p-6">
          <div className="w-full max-w-md rounded-lg border bg-card p-6 shadow-sm">
            <h1 className="text-lg font-semibold">页面发生错误</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              应用遇到未处理异常，请刷新页面重试。
            </p>
            <button
              type="button"
              className="mt-4 rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
              onClick={() => window.location.reload()}
            >
              刷新页面
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route index element={<Dashboard />} />
          <Route path="accounts">
            <Route index element={<Navigate to="/?accounts=1" replace />} />
            <Route
              path="new"
              element={
                <Suspense fallback={<PageFallback />}>
                  <AccountWizard />
                </Suspense>
              }
            />
            <Route
              path=":aid"
              element={
                <Suspense fallback={<PageFallback />}>
                  <AccountDetail />
                </Suspense>
              }
            />
            <Route
              path=":aid/features/auto_reply"
              element={
                <Suspense fallback={<PageFallback />}>
                  <AutoReplyConfig />
                </Suspense>
              }
            />
            <Route
              path=":aid/features/autorepeat"
              element={
                <Suspense fallback={<PageFallback />}>
                  <AutorepeatConfig />
                </Suspense>
              }
            />
            <Route
              path=":aid/features/codex_image"
              element={
                <Suspense fallback={<PageFallback />}>
                  <CodexImageConfigPage />
                </Suspense>
              }
            />
            <Route
              path=":aid/features/chatgpt_image"
              element={
                <Suspense fallback={<PageFallback />}>
                  <ChatGPTImageConfigPage />
                </Suspense>
              }
            />
            <Route
              path=":aid/features/scheduler"
              element={
                <Suspense fallback={<PageFallback />}>
                  <SchedulerConfig />
                </Suspense>
              }
            />
            <Route
              path=":aid/features/game24"
              element={
                <Suspense fallback={<PageFallback />}>
                  <Game24ConfigPage />
                </Suspense>
              }
            />
            <Route
              path=":aid/features/:featureKey"
              element={
                <Suspense fallback={<PageFallback />}>
                  <GenericPluginConfigPage />
                </Suspense>
              }
            />
          </Route>
          <Route
            path="plugins"
            element={
              <Suspense fallback={<PageFallback />}>
                <PluginsHome />
              </Suspense>
            }
          />
          <Route
            path="plugins/templates"
            element={
              <Suspense fallback={<PageFallback />}>
                <PluginsTemplatesPage />
              </Suspense>
            }
          />
          <Route
            path="plugins/scheduler"
            element={
              <Suspense fallback={<PageFallback />}>
                <PluginsSchedulerPage />
              </Suspense>
            }
          />
          <Route
            path="plugins/auto-command-whitelist"
            element={
              <Suspense fallback={<PageFallback />}>
                <PluginsAutoCommandWhitelistPage />
              </Suspense>
            }
          />
          <Route
            path="plugins/message-template-lab"
            element={
              <Suspense fallback={<PageFallback />}>
                <MessageTemplateLabPage />
              </Suspense>
            }
          />
          <Route
            path="plugins/manage"
            element={
              <Suspense fallback={<PageFallback />}>
                <PluginsManagePage />
              </Suspense>
            }
          />
          <Route
            path="interaction"
            element={
              <Suspense fallback={<PageFallback />}>
                <InteractionIndex />
              </Suspense>
            }
          />
          <Route
            path="ledger"
            element={
              <Suspense fallback={<PageFallback />}>
                <LedgerPage />
              </Suspense>
            }
          />
          <Route
            path="dispatch-debug"
            element={
              <Suspense fallback={<PageFallback />}>
                <DispatchDebugPage />
              </Suspense>
            }
          />
          <Route
            path="webhooks"
            element={
              <Suspense fallback={<PageFallback />}>
                <WebhooksPage />
              </Suspense>
            }
          />
          <Route
            path="logs"
            element={
              <Suspense fallback={<PageFallback />}>
                <Logs />
              </Suspense>
            }
          />
          <Route
            path="settings"
            element={
              <Suspense fallback={<PageFallback />}>
                <SettingsIndex />
              </Suspense>
            }
          />
          <Route
            path="ai"
            element={
              <Suspense fallback={<PageFallback />}>
                <AIIndex />
              </Suspense>
            }
          />
          <Route
            path="ai/providers"
            element={<AIProvidersRedirect />}
          />
          <Route
            path="ai/chat"
            element={<Navigate to="/plugins/templates?type=ai" replace />}
          />
          <Route
            path="ai/routing"
            element={<Navigate to="/plugins/templates?aiCapability=routing" replace />}
          />
          <Route
            path="ai/search"
            element={<Navigate to="/plugins/templates?aiCapability=search" replace />}
          />
          <Route
            path="ai/vision"
            element={<Navigate to="/ai?tab=providers&filter=modality:vision" replace />}
          />
          <Route
            path="ai/images"
            element={<Navigate to="/plugins?highlight=codex_image" replace />}
          />
          <Route
            path="ai/output"
            element={<Navigate to="/plugins/templates?aiCapability=output" replace />}
          />
          <Route
            path="ai/help"
            element={<Navigate to="/ai?help=1" replace />}
          />
          <Route
            path="ai/usage"
            element={<Navigate to="/ai?tab=usage" replace />}
          />
          <Route path="ai/*" element={<Navigate to="/ai" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}

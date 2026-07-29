import React, { Suspense, lazy, type ReactNode } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { AppShell } from "@/components/layout/AppShell";
import { RequireAuth } from "@/components/layout/RequireAuth";

import { Login } from "@/pages/Login";
import { Dashboard } from "@/pages/Dashboard";
import { Skeleton } from "@/components/ui/misc";
import { PageShell } from "@/components/layout/PageScaffold";
import { getPlatformCapabilities, getSystemSettings } from "@/api/system";
import type { PlatformModuleKey } from "@/api/types";
import {
  capabilityEnabledMap,
  moduleLabel,
} from "@/lib/navigation";

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
const OperationsWorkspaceRoutes = lazy(() => import("@/pages/Operations/Index").then(m => ({ default: m.OperationsWorkspaceRoutes })));
const MessageTemplateLabPage = lazy(() => import("@/pages/Plugins").then(m => ({ default: m.MessageTemplateLabPage })));
const PluginsManagePage = lazy(() => import("@/pages/Extensions").then(m => ({ default: m.Extensions })));
const InteractionIndex = lazy(() => import("@/pages/Interaction/Index").then(m => ({ default: m.InteractionIndex })));
const LedgerPage = lazy(() => import("@/pages/Ledger").then(m => ({ default: m.LedgerPage })));
const DispatchDebugPage = lazy(() => import("@/pages/DispatchDebug").then(m => ({ default: m.DispatchDebugPage })));
const WebhooksPage = lazy(() => import("@/pages/Webhooks").then(m => ({ default: m.WebhooksPage })));
const AIIndex = lazy(() => import("@/pages/AI/Index").then(m => ({ default: m.AIIndex })));
const AILivenessPage = lazy(() => import("@/pages/AI/Liveness").then(m => ({ default: m.LLMLivenessPage })));
const ActionsInboxPage = lazy(() =>
  import("@/pages/Assistant/ActionsInbox").then((m) => ({ default: m.ActionsInboxPage })),
);

type AppErrorBoundaryState = { hasError: boolean };

function PageFallback() {
  return (
    <PageShell>
      <div role="status" aria-label="页面加载中" className="space-y-5">
        <div className="flex items-center gap-3">
          <Skeleton className="h-10 w-10 shrink-0 rounded-lg" />
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-5 w-32" />
            <Skeleton className="h-3 w-[min(24rem,78%)]" />
          </div>
        </div>
        <div className="flex gap-2">
          <Skeleton className="h-9 w-24 rounded-md" />
          <Skeleton className="h-9 w-32 rounded-md" />
          <Skeleton className="hidden h-9 w-24 rounded-md sm:block" />
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((item) => (
            <div key={item} className="space-y-3 rounded-lg border border-border/70 p-4">
              <div className="flex items-center gap-3">
                <Skeleton className="h-9 w-9 shrink-0 rounded-full" />
                <div className="min-w-0 flex-1 space-y-2">
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-3 w-3/4" />
                </div>
              </div>
              <Skeleton className="h-20 rounded-md" />
            </div>
          ))}
        </div>
      </div>
    </PageShell>
  );
}

function RemovedOperationsRoute() {
  return (
    <PageShell>
      <section className="mx-auto flex min-h-[45vh] max-w-lg flex-col items-center justify-center text-center">
        <p className="font-mono text-sm text-muted-foreground">404</p>
        <h1 className="mt-2 text-2xl font-semibold">页面已移除</h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          旧插件路径不再提供页面或兼容跳转，请从新的一级工作台进入。
        </p>
        <Link className="mt-5 rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground" to="/operations/templates">
          打开指令与任务
        </Link>
      </section>
    </PageShell>
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

/** 直达已关闭模块时保留 URL，显示暂停页而不是白屏。 */
function CapabilityGate({
  moduleKey,
  children,
}: {
  moduleKey: PlatformModuleKey;
  children: ReactNode;
}) {
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
    staleTime: 30_000,
  });
  const capsQ = useQuery({
    queryKey: ["system", "capabilities"],
    queryFn: getPlatformCapabilities,
    staleTime: 15_000,
  });
  const enabled = capabilityEnabledMap(capsQ.data, settingsQ.data?.ai_enabled ?? true);
  if (capsQ.isLoading && settingsQ.isLoading) {
    return <PageFallback />;
  }
  if (enabled[moduleKey] === false) {
    const label = moduleLabel(moduleKey);
    return (
      <PageShell>
        <div className="mx-auto flex max-w-lg flex-col items-start gap-4 rounded-lg border bg-card p-6 shadow-sm">
          <div>
            <h1 className="text-lg font-semibold">模块已暂停</h1>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              {label} 平台能力当前已关闭。配置、Token 与历史数据均保留；重新启用后即可继续使用。
              页面地址保持不变，便于书签与深链接。
            </p>
          </div>
          <Link
            to="/settings?tab=platform"
            className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
          >
            前往平台能力设置
          </Link>
        </div>
      </PageShell>
    );
  }
  return <>{children}</>;
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
          <Route index element={<Navigate to="/plugins" replace />} />
          <Route path="overview" element={<Dashboard />} />
          <Route path="accounts">
            <Route index element={<Navigate to="/overview?accounts=1" replace />} />
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
            path="operations/*"
            element={
              <Suspense fallback={<PageFallback />}>
                <OperationsWorkspaceRoutes />
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
                <CapabilityGate moduleKey="interaction_bot">
                  <InteractionIndex />
                </CapabilityGate>
              </Suspense>
            }
          />
          <Route
            path="ledger"
            element={
              <Suspense fallback={<PageFallback />}>
                <CapabilityGate moduleKey="ledger">
                  <LedgerPage />
                </CapabilityGate>
              </Suspense>
            }
          />
          <Route
            path="dispatch-debug"
            element={
              <Suspense fallback={<PageFallback />}>
                <CapabilityGate moduleKey="dispatch_debug">
                  <DispatchDebugPage />
                </CapabilityGate>
              </Suspense>
            }
          />
          <Route
            path="webhooks"
            element={
              <Suspense fallback={<PageFallback />}>
                <CapabilityGate moduleKey="webhooks">
                  <WebhooksPage />
                </CapabilityGate>
              </Suspense>
            }
          />
          <Route
            path="ai/liveness"
            element={
              <Suspense fallback={<PageFallback />}>
                <CapabilityGate moduleKey="ai">
                  <AILivenessPage />
                </CapabilityGate>
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
            path="assistant"
            element={
              <Suspense fallback={<PageFallback />}>
                <CapabilityGate moduleKey="ai">
                  <AIIndex />
                </CapabilityGate>
              </Suspense>
            }
          />
          <Route
            path="assistant/inbox"
            element={
              <Suspense fallback={<PageFallback />}>
                <CapabilityGate moduleKey="ai">
                  <ActionsInboxPage />
                </CapabilityGate>
              </Suspense>
            }
          />
          <Route
            path="ai"
            element={
              <Suspense fallback={<PageFallback />}>
                <CapabilityGate moduleKey="ai">
                  <AIIndex />
                </CapabilityGate>
              </Suspense>
            }
          />
          <Route
            path="ai/providers"
            element={<AIProvidersRedirect />}
          />
          <Route
            path="ai/chat"
            element={<Navigate to="/operations/templates?type=ai" replace />}
          />
          <Route
            path="ai/routing"
            element={<Navigate to="/operations/templates?aiCapability=routing" replace />}
          />
          <Route
            path="ai/search"
            element={<Navigate to="/operations/templates?aiCapability=search" replace />}
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
            element={<Navigate to="/operations/templates?aiCapability=output" replace />}
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
          <Route path="plugins/templates" element={<RemovedOperationsRoute />} />
          <Route path="plugins/scheduler" element={<RemovedOperationsRoute />} />
          <Route path="plugins/auto-command-whitelist" element={<RemovedOperationsRoute />} />
          <Route path="*" element={<Navigate to="/plugins" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}

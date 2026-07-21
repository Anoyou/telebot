import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Menu, MessageCircle, Minimize2, Server, Settings2 } from "lucide-react";
import { toast } from "sonner";

import {
  cancelSystemAgentRun,
  createSystemAgentSession,
  deleteSystemAgentSession,
  getSystemAgentRun,
  getSystemAgentCapabilities,
  getSystemAgentConfig,
  listSystemAgentActions,
  listSystemAgentMessages,
  listSystemAgentSessions,
  patchSystemAgentConfig,
  startSystemAgentRetryRun,
  startSystemAgentRun,
  streamSystemAgentRun,
  type SystemAgentAction,
  type SystemAgentMessage,
  type SystemAgentRun,
  type SystemAgentSession,
  type SystemAgentStreamEvent,
} from "@/api/systemAgent";
import { listLLMProviders } from "@/api/commands";
import { listAccounts } from "@/api/accounts";
import type { LLMProviderOut } from "@/api/types";
import { Composer } from "@/components/assistant/Composer";
import { useAssistantDock } from "@/components/assistant/AssistantDock";
import { Conversation, type LiveBubble } from "@/components/assistant/Conversation";
import { SessionDrawer } from "@/components/assistant/SessionDrawer";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/misc";
import { getErrMsg } from "@/lib/api";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";
import { removeSessionAndChooseNext } from "./sessionState";

const ACTIVE_RUNS_KEY = "telepilot.system-agent.active-runs.v1";

type StoredRun = { runId: string; lastSeq: number };

function readStoredRuns(): Record<string, StoredRun> {
  try {
    const value = JSON.parse(window.localStorage.getItem(ACTIVE_RUNS_KEY) || "{}") as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return value as Record<string, StoredRun>;
  } catch {
    return {};
  }
}

function storedRun(sessionId: string): StoredRun | null {
  const value = readStoredRuns()[sessionId];
  if (!value || typeof value.runId !== "string") return null;
  return { runId: value.runId, lastSeq: Math.max(0, Number(value.lastSeq) || 0) };
}

function rememberRun(sessionId: string, value: StoredRun): void {
  try {
    window.localStorage.setItem(
      ACTIVE_RUNS_KEY,
      JSON.stringify({ ...readStoredRuns(), [sessionId]: value }),
    );
  } catch {
    // localStorage 不可用时当前页面仍可继续订阅。
  }
}

function forgetRun(sessionId: string, runId?: string): void {
  try {
    const runs = readStoredRuns();
    if (runId && runs[sessionId]?.runId !== runId) return;
    delete runs[sessionId];
    window.localStorage.setItem(ACTIVE_RUNS_KEY, JSON.stringify(runs));
  } catch {
    // 无持久存储时无需额外清理。
  }
}

function requestId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function terminalRun(run: SystemAgentRun): boolean {
  return ["succeeded", "failed", "cancelled"].includes(run.status);
}

function toolsModels(provider?: LLMProviderOut): string[] {
  if (!provider) return [];
  const models = provider.models || [];
  const enabled = models
    .filter((model) => model.enabled && model.supports_tools !== false)
    .map((model) => model.id)
    .filter(Boolean);
  if (enabled.length > 0) return Array.from(new Set(enabled));
  if (models.length === 0 && provider.default_model) return [provider.default_model];
  return [];
}

export function AssistantIndex() {
  const qc = useQueryClient();
  const {
    collapsed: assistantCollapsed,
    setCollapsed: setAssistantCollapsed,
    setStreaming: setDockStreaming,
  } = useAssistantDock();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [accountId, setAccountId] = useState<number | "">("");
  const [live, setLive] = useState<LiveBubble[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [retryingMessageId, setRetryingMessageId] = useState<number | null>(null);
  const [activeRun, setActiveRun] = useState<StoredRun | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [runtimeSelection, setRuntimeSelection] = useState<{
    providerName: string;
    model: string;
  } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setDockStreaming(streaming);
  }, [setDockStreaming, streaming]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const configQ = useQuery({
    queryKey: ["system-agent", "config"],
    queryFn: getSystemAgentConfig,
  });
  const capsQ = useQuery({
    queryKey: ["system-agent", "capabilities"],
    queryFn: getSystemAgentCapabilities,
  });
  const sessionsQ = useQuery({
    queryKey: ["system-agent", "sessions"],
    queryFn: () => listSystemAgentSessions({ status: "active", limit: 50 }),
  });
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const providersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
  });

  const messagesQ = useQuery({
    queryKey: ["system-agent", "messages", activeId],
    queryFn: () => listSystemAgentMessages(activeId!, { limit: 100 }),
    enabled: !!activeId,
  });
  const pendingActionsQ = useQuery({
    queryKey: ["system-agent", "actions", activeId, "pending"],
    queryFn: () =>
      listSystemAgentActions({ session_id: activeId!, status: "pending", limit: 50 }),
    enabled: !!activeId,
    refetchInterval: assistantCollapsed && !streaming ? false : 15_000,
  });

  // 恢复最后一个 active 会话
  useEffect(() => {
    if (activeId || !sessionsQ.data?.length) return;
    setActiveId(sessionsQ.data[0].id);
    if (sessionsQ.data[0].account_id != null) {
      setAccountId(sessionsQ.data[0].account_id);
    }
  }, [sessionsQ.data, activeId]);

  useEffect(() => {
    setRuntimeSelection(null);
  }, [activeId, configQ.data?.provider_id, configQ.data?.model]);

  const createMut = useMutation({
    mutationFn: () =>
      createSystemAgentSession({
        account_id: accountId === "" ? null : Number(accountId),
      }),
    onSuccess: (session) => {
      void qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
      abortRef.current?.abort();
      abortRef.current = null;
      setStreaming(false);
      setActiveId(session.id);
      setLive([]);
      toast.success("已新建会话");
    },
    onError: (e) => toast.error(getErrMsg(e)),
  });

  const deleteMut = useMutation({
    mutationFn: async (id: string) => {
      const run = storedRun(id);
      if (run) await cancelSystemAgentRun(run.runId).catch(() => undefined);
      return deleteSystemAgentSession(id);
    },
    onSuccess: (_data, id) => {
      forgetRun(id);
      const sessionQueryKey = ["system-agent", "sessions"];
      const currentSessions = qc.getQueryData<SystemAgentSession[]>(sessionQueryKey) || [];
      const nextState = removeSessionAndChooseNext(currentSessions, activeId, id);
      qc.setQueryData(sessionQueryKey, nextState.sessions);
      void qc.removeQueries({ queryKey: ["system-agent", "messages", id] });
      void qc.removeQueries({ queryKey: ["system-agent", "actions", id] });
      void qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
      if (activeId === id) {
        abortRef.current?.abort();
        abortRef.current = null;
        setStreaming(false);
        setActiveId(nextState.activeId);
        const nextSession = nextState.sessions.find((session) => session.id === nextState.activeId);
        setAccountId(nextSession?.account_id ?? "");
        setLive([]);
      }
      toast.success("会话已删除");
    },
    onError: (e) => toast.error(getErrMsg(e)),
  });

  const saveConfigMut = useMutation({
    mutationFn: patchSystemAgentConfig,
    onSuccess: (config) => {
      qc.setQueryData(["system-agent", "config"], config);
      void qc.invalidateQueries({ queryKey: ["system-agent"] });
      toast.success("助手配置已保存");
    },
    onError: (e) => toast.error(getErrMsg(e)),
  });

  const enabled = configQ.data?.enabled ?? false;
  const messages: SystemAgentMessage[] = messagesQ.data || [];
  const configuredProvider = useMemo(
    () =>
      (providersQ.data || []).find(
        (provider) => provider.id === configQ.data?.provider_id,
      ),
    [configQ.data?.provider_id, providersQ.data],
  );
  const configuredToolsModels = useMemo(
    () => toolsModels(configuredProvider),
    [configuredProvider],
  );
  const latestMessageSelection = useMemo(() => {
    const message = [...messages]
      .reverse()
      .find((item) => item.role === "assistant" && item.usage);
    const providerName = message?.usage?.provider_name;
    const model = message?.usage?.model;
    if (typeof providerName !== "string" || typeof model !== "string") return null;
    return { providerName, model };
  }, [messages]);
  const displayedSelection =
    runtimeSelection ||
    latestMessageSelection ||
    (capsQ.data?.provider_name
      ? {
          providerName: capsQ.data.provider_name,
          model: capsQ.data.resolved_model || configQ.data?.model || "未选择",
        }
      : configuredProvider
        ? {
            providerName: configuredProvider.name,
            model:
              configQ.data?.model ||
              configuredToolsModels[0] ||
              configuredProvider.default_model,
          }
        : null);

  const selectableProviders = useMemo(
    () => (providersQ.data || []).filter((provider) => provider.has_api_key && toolsModels(provider).length > 0),
    [providersQ.data],
  );
  const selectorDisabled = streaming || saveConfigMut.isPending || providersQ.isLoading;
  const configuredModel = configQ.data?.model || configuredToolsModels[0] || "";
  const actualSelectionDiffers = Boolean(
    displayedSelection &&
      (displayedSelection.providerName !== configuredProvider?.name ||
        displayedSelection.model !== configuredModel),
  );

  const selectProvider = (providerId: string) => {
    const provider = (providersQ.data || []).find((item) => item.id === Number(providerId));
    if (!provider) return;
    saveConfigMut.mutate({
      provider_id: provider.id,
      model: toolsModels(provider)[0] || null,
      enabled: true,
    });
  };

  const ensureSession = async (): Promise<string> => {
    if (activeId) return activeId;
    const session = await createSystemAgentSession({
      account_id: accountId === "" ? null : Number(accountId),
    });
    await qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
    setActiveId(session.id);
    return session.id;
  };

  const updatePendingText = (value: string) => {
    setLive((prev) =>
      prev.map((bubble) =>
        bubble.role === "assistant" && bubble.pending ? { ...bubble, text: value } : bubble,
      ),
    );
  };

  const upsertToolProgress = (event: SystemAgentStreamEvent, finished: boolean) => {
    const id = `tool-${event.call_id || event.seq}`;
    const toolLabel = systemAgentToolLabel(
      String(event.tool_description || ""),
      String(event.tool_name || "系统能力"),
    );
    setLive((prev) => {
      const bubble: LiveBubble = {
        id,
        role: "tool",
        text: `${finished ? (event.is_error ? "调用失败" : "调用完成") : "正在调用"} ${toolLabel}${finished ? "" : "…"}`,
        pending: !finished,
      };
      const withoutCurrent = prev.filter((item) => item.id !== id);
      const pendingBubbles = withoutCurrent.filter(
        (item) => item.pending && item.role === "assistant",
      );
      const stableBubbles = withoutCurrent.filter(
        (item) => !(item.pending && item.role === "assistant"),
      );
      return [...stableBubbles, bubble, ...pendingBubbles];
    });
  };

  const handleRunEvent = (event: SystemAgentStreamEvent) => {
    if (event.type === "model_capability_check") {
      updatePendingText(
        `正在验证 ${event.provider_name || "Provider"} · ${event.model || "模型"} 的工具调用能力…`,
      );
    }
    if (event.type === "model_attempt") {
      const providerName = String(event.provider_name || "");
      const model = String(event.model || "");
      if (providerName && model) setRuntimeSelection({ providerName, model });
      const attempt = Number(event.attempt || 1);
      updatePendingText(
        `正在调用 ${providerName || "Provider"} · ${model || "模型"}${attempt > 1 ? `（第 ${attempt} 次尝试）` : "…"}`,
      );
    }
    if (event.type === "retry_scheduled") {
      const retryNumber = Number(event.retry_number || 1);
      const maxRetries = Number(event.max_retries || 5);
      const delay = Number(event.delay_seconds || 3);
      updatePendingText(
        `${event.provider_name || "Provider"} · ${event.model || "模型"} 暂时失败，${delay} 秒后进行第 ${retryNumber}/${maxRetries} 次重试…`,
      );
    }
    if (event.type === "model_exhausted") {
      updatePendingText(
        `${event.provider_name || "当前 Provider"} · ${event.model || "当前模型"} 未能完成，正在尝试同 Provider 的其它模型…`,
      );
    }
    if (event.type === "skill_selected") {
      const summary = String(event.understanding_summary || "").trim();
      updatePendingText(summary ? `已理解：${summary}，正在处理…` : "已理解你的需求，正在处理…");
    }
    if (event.type === "provider_selected") {
      const providerName = String(event.provider_name || "");
      const model = String(event.model || "");
      if (providerName && model) {
        setRuntimeSelection({ providerName, model });
        if (event.reason === "provider_fallback") {
          toast.message(`已改用 ${providerName} · ${model}`);
        } else if (event.reason === "model_fallback") {
          updatePendingText(`已切换到 ${providerName} 的 ${model}，正在继续…`);
        }
      }
    }
    if (event.type === "tool_started") {
      upsertToolProgress(event, false);
      const toolLabel = systemAgentToolLabel(
        String(event.tool_description || ""),
        String(event.tool_name || "系统能力"),
      );
      updatePendingText(`正在等待 ${toolLabel} 返回…`);
    }
    if (event.type === "tool_finished") upsertToolProgress(event, true);
    if (event.type === "action_proposed" && event.action) {
      const action = event.action as SystemAgentAction;
      setLive((prev) => [
        ...prev.filter((bubble) => !bubble.pending),
        {
          id: `action-${action.id}`,
          role: "action" as const,
          text: action.summary || action.tool_name,
          action,
        },
      ]);
    }
    if (event.type === "assistant_message") {
      const providerName = event.usage?.provider_name;
      const model = event.usage?.model;
      if (typeof providerName === "string" && typeof model === "string") {
        setRuntimeSelection({ providerName, model });
      }
      setLive((prev) => [
        ...prev.filter((bubble) => !bubble.pending),
        {
          id: "live-assistant-final",
          role: "assistant",
          text: String(event.content || ""),
        },
      ]);
    }
    if (event.type === "error") {
      if (event.code === "RUN_STREAM_FAILED") {
        updatePendingText("进度连接中断，正在恢复…");
      } else if (
        event.code === "AGENT_PROVIDER_SWITCH_REQUIRED" ||
        event.code === "AGENT_TOOL_APPROVAL_REQUIRED"
      ) {
        toast.message(event.message || "当前 Provider 内模型均不可用，请确认是否切换");
      } else {
        toast.error(event.message || "助手运行失败");
      }
      if (event.hint?.web_path) {
        toast.message(event.hint.message || "请检查模型配置", {
          action: {
            label: "去配置",
            onClick: () => {
              window.location.href = event.hint?.web_path || "/ai?tab=providers";
            },
          },
        });
      }
    }
  };

  const refreshRunData = async (sessionId: string) => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["system-agent", "messages", sessionId] }),
      qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] }),
      qc.invalidateQueries({ queryKey: ["system-agent", "actions", sessionId] }),
    ]);
  };

  const followRun = async (
    sessionId: string,
    runId: string,
    initialSeq: number,
    controller: AbortController,
  ) => {
    let cursor = Math.max(0, initialSeq);
    let doneReceived = false;
    let reconnectAttempt = 0;
    try {
      while (!controller.signal.aborted && !doneReceived) {
        let streamFailed = false;
        try {
          await streamSystemAgentRun(
            runId,
            cursor,
            (event) => {
              const seq = Number(event.seq || 0);
              if (seq > cursor) {
                cursor = seq;
                const next = { runId, lastSeq: cursor };
                rememberRun(sessionId, next);
                setActiveRun(next);
              }
              handleRunEvent(event);
              if (event.type === "error" && event.code === "RUN_STREAM_FAILED") {
                streamFailed = true;
              }
              if (event.type === "done") doneReceived = true;
            },
            { signal: controller.signal },
          );
          if (streamFailed && !doneReceived) {
            reconnectAttempt += 1;
            await new Promise((resolve) =>
              window.setTimeout(resolve, Math.min(2000, 250 * reconnectAttempt)),
            );
          } else {
            reconnectAttempt = 0;
          }
        } catch (error) {
          if (controller.signal.aborted) throw error;
          const snapshot = await getSystemAgentRun(runId).catch(() => null);
          if (snapshot && terminalRun(snapshot)) {
            // 终态事件也在同一事件表中；继续按游标读取，直到真正收到 done。
            cursor = Math.min(cursor, Math.max(0, snapshot.last_seq - 1));
          }
          reconnectAttempt += 1;
          updatePendingText("进度连接中断，正在恢复…");
          await new Promise((resolve) =>
            window.setTimeout(resolve, Math.min(2000, 250 * reconnectAttempt)),
          );
        }
      }
      if (!doneReceived) return;
      forgetRun(sessionId, runId);
      setActiveRun((current) => (current?.runId === runId ? null : current));
      await refreshRunData(sessionId);
      if (abortRef.current === controller) setLive([]);
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setStreaming(false);
        setRetryingMessageId(null);
      }
    }
  };

  const runTurn = async ({
    text,
    retryMessageId,
    fallbackProviderId,
    approvedTools,
  }: {
    text?: string;
    retryMessageId?: number;
    fallbackProviderId?: number;
    approvedTools?: string[];
  }) => {
    if (!enabled) {
      toast.error("请先在右上角开启系统助手并选择支持 tools 的 Provider");
      setConfigOpen(true);
      return;
    }
    setStreaming(true);
    setRetryingMessageId(retryMessageId ?? null);
    const controller = new AbortController();
    abortRef.current = controller;
    const userBubble: LiveBubble | null = text
      ? { id: `live-user-${Date.now()}`, role: "user", text }
      : null;
    const pending: LiveBubble = {
      id: `live-assistant-${Date.now()}`,
      role: "assistant",
      text: "正在理解你的需求…",
      pending: true,
    };
    setLive(userBubble ? [userBubble, pending] : [pending]);
    try {
      const sessionId = await ensureSession();
      const accountPayload = { account_id: accountId === "" ? null : Number(accountId) };
      const run = retryMessageId != null
        ? await startSystemAgentRetryRun(sessionId, retryMessageId, {
            ...accountPayload,
            fallback_provider_id: fallbackProviderId ?? null,
            approved_tools: approvedTools || [],
            client_request_id: requestId(),
          })
        : await startSystemAgentRun(sessionId, {
            content: text || "",
            ...accountPayload,
            client_request_id: requestId(),
          });
      const saved = { runId: run.id, lastSeq: 0 };
      rememberRun(sessionId, saved);
      setActiveRun(saved);
      await followRun(sessionId, run.id, 0, controller);
    } catch (error) {
      if (!controller.signal.aborted) {
        toast.error(getErrMsg(error));
        setLive((prev) => prev.filter((bubble) => bubble.role === "user" || bubble.role === "action"));
      }
      if (abortRef.current === controller) {
        abortRef.current = null;
        setStreaming(false);
        setRetryingMessageId(null);
      }
    }
  };

  const onSend = async (text: string) => runTurn({ text });

  const onRetryMessage = async (
    messageId: number,
    fallbackProviderId?: number,
    approvedTools?: string[],
  ) => runTurn({ retryMessageId: messageId, fallbackProviderId, approvedTools });

  const onStop = () => {
    const run = activeRun || (activeId ? storedRun(activeId) : null);
    if (!run) return;
    updatePendingText("正在停止本轮请求…");
    void cancelSystemAgentRun(run.runId).catch((error) => toast.error(getErrMsg(error)));
  };

  useEffect(() => {
    if (!activeId || abortRef.current) return;
    const saved = storedRun(activeId);
    if (!saved) {
      setActiveRun(null);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setActiveRun(saved);
    setStreaming(true);
    setLive([
      {
        id: `live-resume-${saved.runId}`,
        role: "assistant",
        text: "正在恢复本轮进度…",
        pending: true,
      },
    ]);
    void followRun(activeId, saved.runId, Math.max(0, saved.lastSeq - 1), controller).catch(
      (error) => {
        if (!controller.signal.aborted) toast.error(getErrMsg(error));
      },
    );
    return () => {
      controller.abort();
      if (abortRef.current === controller) abortRef.current = null;
    };
    // followRun 只读取本轮 activeId 对应的持久游标；切换会话时重新建立订阅。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const pendingActionBubbles: LiveBubble[] = useMemo(() => {
    const rows = pendingActionsQ.data || [];
    // 流过程中 live 已有的 action 避免重复
    const liveIds = new Set(
      live.filter((b) => b.role === "action" && b.action).map((b) => b.action!.id),
    );
    return rows
      .filter((a) => !liveIds.has(a.id))
      .map((a) => ({
        id: `pending-action-${a.id}`,
        role: "action" as const,
        text: a.summary || a.tool_name,
        action: a,
      }));
  }, [pendingActionsQ.data, live]);

  const conversationLive = useMemo(
    () => [...live, ...pendingActionBubbles],
    [live, pendingActionBubbles],
  );

  return (
    <PageShell>
      <PageHeader
        title="系统助手"
        description="用自然语言查询并操作系统能力；写操作需内联确认。"
        icon={MessageCircle}
        actions={(
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-10 rounded-full border-primary/35 bg-card/95 px-3 text-foreground shadow-md shadow-black/10 hover:bg-muted"
            onClick={() => setAssistantCollapsed(true)}
            aria-label="收起系统助手"
            title="收起为悬浮助手"
          >
            <span className="grid h-6 w-6 place-items-center rounded-full bg-primary/15 text-primary">
              <MessageCircle className="h-3.5 w-3.5" />
            </span>
            <span>收起助手</span>
            <Minimize2 className="h-3.5 w-3.5 text-muted-foreground" />
          </Button>
        )}
      />

      <div className="mb-3 rounded-lg border border-border/70 bg-muted/20 p-2.5">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 truncate text-xs text-muted-foreground">
              状态：
              <span className="font-medium text-foreground">
                {configQ.isLoading ? "加载中" : enabled ? (streaming ? "调用中" : "已启用") : "未启用"}
              </span>
            </span>
            <Link to="/ai?tab=providers" className="shrink-0 text-xs text-primary hover:underline">
              配置模型提供商
            </Link>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 px-2.5 md:hidden"
              onClick={() => setDrawerOpen(true)}
            >
              <Menu className="h-4 w-4" />
              会话
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 px-2.5"
              onClick={() => setConfigOpen((v) => !v)}
              aria-expanded={configOpen}
            >
              <Settings2 className="h-4 w-4" />
              配置
            </Button>
          </div>
        </div>
        <div className="mt-2 grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center">
          <label className="min-w-0 sm:flex sm:items-center sm:gap-1.5">
            <span className="mb-1 flex items-center gap-1 text-[11px] text-muted-foreground sm:mb-0 sm:shrink-0 sm:text-sm">
              <Server className="h-3.5 w-3.5 shrink-0" />
              Provider
            </span>
          <Select
            aria-label="切换 Agent Provider"
            value={configQ.data?.provider_id == null ? "" : String(configQ.data.provider_id)}
            disabled={selectorDisabled}
            onChange={(event) => selectProvider(event.target.value)}
            className="h-8 w-full min-w-0 text-xs sm:w-44"
          >
            <option value="">未配置</option>
            {(providersQ.data || []).map((provider) => {
              const eligible = selectableProviders.some((item) => item.id === provider.id);
              const unavailableLabel = !provider.has_api_key
                ? "（缺少 Key）"
                : "（无可用 Tools 模型）";
              return (
                <option key={provider.id} value={provider.id} disabled={!eligible}>
                  {provider.name}{!eligible ? unavailableLabel : ""}
                </option>
              );
            })}
          </Select>
          </label>
          <label className="min-w-0 sm:flex sm:items-center sm:gap-1.5">
            <span className="mb-1 block text-[11px] text-muted-foreground sm:mb-0 sm:shrink-0 sm:text-sm">账号上下文</span>
          <Select
            value={accountId === "" ? "" : String(accountId)}
            onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : "")}
            className="h-8 w-full min-w-0 text-xs sm:w-44"
          >
            <option value="">系统级</option>
            {(accountsQ.data || []).map((a) => (
              <option key={a.id} value={a.id}>
                #{a.id} {a.display_name || a.phone || a.tg_username || ""}
              </option>
            ))}
          </Select>
          </label>
          {(streaming || actualSelectionDiffers) && displayedSelection ? (
            <span className="col-span-2 min-w-0 truncate text-xs text-muted-foreground sm:col-span-1">
              实际调用：{displayedSelection.providerName} · {displayedSelection.model}
            </span>
          ) : null}
        </div>
      </div>

      {configOpen ? (
        <div className="mb-3 rounded-lg border bg-card p-4 text-sm">
          <div className="mb-3 font-medium">系统助手模型</div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={!!configQ.data?.enabled}
                onChange={(e) =>
                  saveConfigMut.mutate({ enabled: e.target.checked })
                }
              />
              启用系统助手
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={!!configQ.data?.require_tool_approval}
                disabled={saveConfigMut.isPending}
                onChange={(e) =>
                  saveConfigMut.mutate({ require_tool_approval: e.target.checked })
                }
              />
              调用工具前需要批准
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">固定 Provider</span>
              <Select
                value={configQ.data?.provider_id != null ? String(configQ.data.provider_id) : ""}
                onChange={(e) => {
                  const v = e.target.value;
                  const provider = (providersQ.data || []).find(
                    (item) => item.id === Number(v),
                  );
                  saveConfigMut.mutate({
                    provider_id: v ? Number(v) : null,
                    model: toolsModels(provider)[0] || null,
                    fallback_provider_ids: (
                      configQ.data?.fallback_provider_ids || []
                    ).filter((id) => id !== Number(v)),
                    enabled: true,
                  });
                }}
              >
                <option value="">请选择</option>
                {(providersQ.data || []).map((p) => (
                  <option
                    key={p.id}
                    value={p.id}
                    disabled={!p.has_api_key || toolsModels(p).length === 0}
                  >
                    {p.name}
                    {!p.has_api_key
                      ? "（缺少 Key）"
                      : toolsModels(p).length === 0
                        ? "（无 Tools 模型）"
                        : ""}
                  </option>
                ))}
              </Select>
            </label>
            <label className="flex flex-col gap-1 sm:col-start-2">
              <span className="text-muted-foreground">主模型</span>
              <Select
                value={configuredModel}
                disabled={!configuredProvider || configuredToolsModels.length === 0}
                onChange={(e) => saveConfigMut.mutate({ model: e.target.value || null })}
              >
                {configuredToolsModels.length === 0 ? (
                  <option value="">没有可用的 Tools 模型</option>
                ) : (
                  configuredToolsModels.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))
                )}
              </Select>
            </label>
          </div>
          <div className="mt-4 border-t pt-3">
            <div className="font-medium">跨 Provider fallback 范围</div>
            <p className="mt-1 text-xs text-muted-foreground">
              当前 Provider 会先静默尝试其它 Tools 模型；只有下列白名单 Provider 可在你确认后接管。
            </p>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              {(providersQ.data || [])
                .filter((provider) => provider.id !== configQ.data?.provider_id)
                .map((provider) => {
                  const models = toolsModels(provider);
                  const eligible = provider.has_api_key && models.length > 0;
                  const checked = (configQ.data?.fallback_provider_ids || []).includes(
                    provider.id,
                  );
                  return (
                    <label
                      key={provider.id}
                      className="flex min-w-0 items-start gap-2 rounded-md border px-3 py-2"
                    >
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={checked}
                        disabled={!eligible || saveConfigMut.isPending}
                        onChange={(event) => {
                          const current = configQ.data?.fallback_provider_ids || [];
                          const next = event.target.checked
                            ? [...current, provider.id]
                            : current.filter((id) => id !== provider.id);
                          saveConfigMut.mutate({ fallback_provider_ids: next });
                        }}
                      />
                      <span className="min-w-0">
                        <span className="block truncate text-foreground">{provider.name}</span>
                        <span className="block truncate text-xs text-muted-foreground">
                          {eligible ? `${models.length} 个 Tools 模型` : "不可用于 Agent fallback"}
                        </span>
                      </span>
                    </label>
                  );
                })}
            </div>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            仅允许声明支持 tools 的模型。写操作会生成待确认卡片；未配置时助手会给出 AI 中心入口。
            {capsQ.data ? ` · 已注册 ${capsQ.data.tools.filter((t) => t.available).length} 个可用工具` : null}
          </p>
        </div>
      ) : null}

      <div className="flex min-h-[60vh] overflow-hidden rounded-xl border bg-card">
        <SessionDrawer
          sessions={sessionsQ.data || []}
          activeId={activeId}
          onSelect={(id) => {
            abortRef.current?.abort();
            abortRef.current = null;
            setStreaming(false);
            setRetryingMessageId(null);
            setLive([]);
            setActiveId(id);
          }}
          onCreate={() => createMut.mutate()}
          onDelete={(id) => deleteMut.mutate(id)}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
        />
        <div className="flex min-w-0 flex-1 flex-col">
          {!activeId ? (
            <div className="flex flex-1 items-center justify-center p-6">
              <div className="max-w-sm text-center">
                <MessageCircle className="mx-auto h-10 w-10 text-muted-foreground/50" />
                <p className="mt-3 text-sm text-muted-foreground">请选择一个会话，或新建一个 Agent 会话。</p>
                <Button type="button" className="mt-4" onClick={() => createMut.mutate()} disabled={createMut.isPending}>
                  新建会话
                </Button>
              </div>
            </div>
          ) : messagesQ.isLoading ? (
            <div role="status" aria-label="会话消息加载中" className="flex flex-1 flex-col gap-5 overflow-hidden p-4">
              <div className="flex items-end gap-3"><Skeleton className="h-12 w-3/5 rounded-2xl" /></div>
              <div className="flex items-end justify-end gap-3"><Skeleton className="h-10 w-2/5 rounded-2xl" /></div>
              <div className="flex items-end gap-3"><Skeleton className="h-20 w-4/5 rounded-2xl" /></div>
              <div className="mt-auto space-y-2 rounded-xl border p-2">
                <Skeleton className="h-16 w-full rounded-lg" />
                <div className="flex justify-end"><Skeleton className="h-8 w-8 rounded-md" /></div>
              </div>
            </div>
          ) : (
            <>
              <Conversation
                messages={messages}
                live={conversationLive}
                onRetryMessage={onRetryMessage}
                retryingMessageId={retryingMessageId}
                onActionUpdated={() => {
                  void qc.invalidateQueries({ queryKey: ["system-agent", "actions", activeId] });
                }}
              />
              <Composer
                disabled={configQ.isLoading}
                streaming={streaming}
                onSend={onSend}
                onStop={onStop}
                modelOptions={configuredToolsModels}
                modelValue={configQ.data?.model || configuredToolsModels[0] || ""}
                onModelChange={(model) => saveConfigMut.mutate({ model: model || null })}
                modelDisabled={selectorDisabled || configuredToolsModels.length === 0}
              />
            </>
          )}
        </div>
      </div>
    </PageShell>
  );
}

export default AssistantIndex;

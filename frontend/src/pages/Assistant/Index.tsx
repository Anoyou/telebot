import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  Bell,
  BellOff,
  BellRing,
  ChevronDown,
  MessageCircle,
  Server,
  Settings2,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import {
  approveSystemAgentRun,
  cancelSystemAgentRun,
  clearSystemAgentQueue,
  createSystemAgentSession,
  deleteSystemAgentQueueItem,
  deleteSystemAgentSession,
  getSystemAgentRun,
  getSystemAgentCapabilities,
  getSystemAgentConfig,
  listSystemAgentActions,
  listSystemAgentMessages,
  listSystemAgentQueue,
  listSystemAgentRunEvents,
  listSystemAgentRuns,
  listSystemAgentSessions,
  createSystemAgentUserMemory,
  deleteSystemAgentUserMemory,
  listSystemAgentUserMemory,
  patchSystemAgentConfig,
  patchSystemAgentUserMemory,
  reorderSystemAgentQueue,
  resumeSystemAgentQueue,
  startSystemAgentRegenerateRun,
  startSystemAgentRetryRun,
  startSystemAgentRun,
  steerSystemAgentRun,
  stopAndReplaceSystemAgentRun,
  streamSystemAgentRun,
  submitSystemAgentRunInput,
  updateSystemAgentQueueItem,
  type SystemAgentAction,
  type SystemAgentMessage,
  type SystemAgentQueueItem,
  type SystemAgentRun,
  type SystemAgentSession,
  type SystemAgentStreamEvent,
  type SystemAgentUserMemory,
} from "@/api/systemAgent";
import { listLLMProviders } from "@/api/commands";
import { listAccounts } from "@/api/accounts";
import type { LLMProviderOut } from "@/api/types";
import { matrixToPickerItems, type ModelPickerValue } from "@/components/ai/ModelPicker";
import { Composer, type ComposerAction } from "@/components/assistant/Composer";
import { AgentMark } from "@/components/assistant/AgentMark";
import { useAssistantDock } from "@/components/assistant/AssistantDock";
import { Conversation, type LiveBubble } from "@/components/assistant/Conversation";
import { TaskCenter } from "@/components/assistant/TaskCenter";
import {
  classifySystemAgentRunSettlement,
  sortSystemAgentQueue,
  sortSystemAgentRuns,
} from "@/components/assistant/taskCenterState";
import { upstreamErrorRequestIds } from "@/lib/upstreamErrorFacts";
import {
  SessionDrawer,
  type SessionOriginFilter,
} from "@/components/assistant/SessionDrawer";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/misc";
import { useStreamingText } from "@/hooks/useStreamingText";
import { getErrMsg } from "@/lib/api";
import { fetchMe } from "@/lib/auth";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";
import { removeSessionAndChooseNext } from "./sessionState";
import {
  loadSessionModelSelection,
  DEFAULT_SESSION_MODEL_SELECTION,
  saveSessionModelSelection,
  toApiModelSelection,
  type SessionModelSelection,
} from "./sessionModelSelection";

const ACTIVE_RUNS_KEY = "telepilot.system-agent.active-runs.v1";
const DRAFTS_KEY = "telepilot.system-agent.drafts.v1";

type StoredRun = {
  runId: string;
  lastSeq: number;
  targetUserMessageId?: number;
  targetAssistantMessageId?: number;
  editedUserText?: string;
};

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
  return {
    runId: value.runId,
    lastSeq: Math.max(0, Number(value.lastSeq) || 0),
    targetUserMessageId:
      Number(value.targetUserMessageId) > 0 ? Number(value.targetUserMessageId) : undefined,
    targetAssistantMessageId:
      Number(value.targetAssistantMessageId) > 0
        ? Number(value.targetAssistantMessageId)
        : undefined,
    editedUserText:
      typeof value.editedUserText === "string" ? value.editedUserText : undefined,
  };
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

function readDrafts(): Record<string, string> {
  try {
    const value = JSON.parse(window.localStorage.getItem(DRAFTS_KEY) || "{}") as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return value as Record<string, string>;
  } catch {
    return {};
  }
}

function draftKey(userId: number | null | undefined, sessionId: string | null): string {
  return `${userId ?? "anonymous"}:${sessionId || "__new__"}`;
}

function readDraft(
  userId: number | null | undefined,
  sessionId: string | null,
): string {
  const drafts = readDrafts();
  const scoped = drafts[draftKey(userId, sessionId)];
  return scoped || "";
}

function writeDraft(
  userId: number | null | undefined,
  sessionId: string | null,
  value: string,
): void {
  try {
    const key = draftKey(userId, sessionId);
    const drafts = readDrafts();
    if (value) drafts[key] = value;
    else delete drafts[key];
    delete drafts[sessionId || "__new__"];
    window.localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
  } catch {
    // localStorage 不可用时仅保留当前页面草稿。
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

function openRun(run: SystemAgentRun): boolean {
  return ["queued", "running", "waiting_input", "waiting_approval"].includes(run.status);
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
  const [searchParams, setSearchParams] = useSearchParams();
  const {
    collapsed: assistantCollapsed,
    setStreaming: setDockStreaming,
    notifyOutcome,
  } = useAssistantDock();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [originFilter, setOriginFilter] = useState<SessionOriginFilter>("all");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sessionSidebarCollapsed, setSessionSidebarCollapsed] = useState(false);
  const [accountId, setAccountId] = useState<number | "">("");
  const [live, setLive] = useState<LiveBubble[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [retryingMessageId, setRetryingMessageId] = useState<number | null>(null);
  const [activeRun, setActiveRun] = useState<StoredRun | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [mobileHeaderExpanded, setMobileHeaderExpanded] = useState(false);
  const [memoryDraft, setMemoryDraft] = useState("");
  const [runtimeSelection, setRuntimeSelection] = useState<{
    providerName: string;
    model: string;
  } | null>(null);
  const [streamNotice, setStreamNotice] = useState("");
  const [composerValue, setComposerValue] = useState("");
  const [composerAction, setComposerAction] = useState<ComposerAction>("queue");
  const [waitingInput, setWaitingInput] = useState("");
  const [activeRunSnapshot, setActiveRunSnapshot] = useState<SystemAgentRun | null>(null);
  const [notificationPermission, setNotificationPermission] = useState<
    NotificationPermission | "unsupported"
  >(() =>
    "Notification" in window ? Notification.permission : "unsupported"
  );
  /** 本轮模型选择：仅会话本地，默认自动路由 */
  const [sessionModel, setSessionModel] = useState<SessionModelSelection>(
    DEFAULT_SESSION_MODEL_SELECTION,
  );
  const abortRef = useRef<AbortController | null>(null);
  const streamingBubbleCreatedRef = useRef(false);
  const liveAssistantMessageIdRef = useRef<number | null>(null);
  const skipNextDraftWriteRef = useRef(false);
  const liveText = useStreamingText();
  const liveReasoning = useStreamingText();

  const clearLiveStreamingState = useCallback(() => {
    streamingBubbleCreatedRef.current = false;
    liveText.clear();
    liveReasoning.clear();
    setStreamNotice("");
  }, [liveReasoning.clear, liveText.clear]);

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
    refetchOnMount: "always",
  });
  const sessionsQ = useQuery({
    queryKey: ["system-agent", "sessions"],
    queryFn: () => listSystemAgentSessions({ status: "active", include_bot: true, limit: 100 }),
    refetchInterval: assistantCollapsed ? false : 3_000,
    refetchOnWindowFocus: "always",
  });
  const meQ = useQuery({
    queryKey: ["auth", "me"],
    queryFn: fetchMe,
    staleTime: 60_000,
  });
  const runsQ = useQuery({
    queryKey: ["system-agent", "runs", "task-center"],
    queryFn: () => listSystemAgentRuns({ limit: 100, include_bot: true }),
    refetchInterval: assistantCollapsed ? 5_000 : 2_000,
    refetchOnWindowFocus: "always",
  });
  const queueQ = useQuery({
    queryKey: ["system-agent", "queue"],
    queryFn: () => listSystemAgentQueue({ include_bot: true }),
    refetchInterval: assistantCollapsed ? 5_000 : 2_000,
    refetchOnWindowFocus: "always",
  });
  const sessionOptions = useMemo(
    () => (Array.isArray(sessionsQ.data) ? sessionsQ.data : []),
    [sessionsQ.data],
  );
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const providersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
    refetchOnMount: "always",
  });
  const accountOptions = Array.isArray(accountsQ.data) ? accountsQ.data : [];
  const activeSession = sessionOptions.find((session) => session.id === activeId) ?? null;
  const viewingBotSession = activeSession?.channel === "bot";
  const allRuns = useMemo(
    () => sortSystemAgentRuns(Array.isArray(runsQ.data) ? runsQ.data : []),
    [runsQ.data],
  );
  const allQueue = useMemo(
    () => sortSystemAgentQueue(Array.isArray(queueQ.data) ? queueQ.data : []),
    [queueQ.data],
  );
  const runStatusBySession = useMemo(() => {
    const statuses: Record<string, string> = {};
    for (const run of allRuns) {
      if (
        statuses[run.session_id] === undefined &&
        ["queued", "running", "waiting_input", "waiting_approval", "failed"].includes(
          run.status,
        )
      ) {
        statuses[run.session_id] = run.status;
      }
    }
    return statuses;
  }, [allRuns]);
  const queueCountBySession = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of allQueue) {
      counts[item.session_id] = (counts[item.session_id] || 0) + 1;
    }
    return counts;
  }, [allQueue]);
  const sessionRuns = useMemo(
    () => allRuns.filter((run) => run.session_id === activeId),
    [activeId, allRuns],
  );
  const currentRun = useMemo(
    () =>
      sessionRuns.find((run) => openRun(run)) ||
      (activeRunSnapshot?.session_id === activeId && openRun(activeRunSnapshot)
        ? activeRunSnapshot
        : null),
    [activeId, activeRunSnapshot, sessionRuns],
  );
  const currentQueue = useMemo(
    () => allQueue.filter((item) => item.session_id === activeId),
    [activeId, allQueue],
  );
  const hasOpenRun = Boolean(currentRun || streaming);
  const memoryQ = useQuery({
    queryKey: ["system-agent", "user-memory"],
    queryFn: listSystemAgentUserMemory,
    enabled: configOpen,
  });

  const messagesQ = useQuery({
    queryKey: ["system-agent", "messages", activeId],
    queryFn: () => listSystemAgentMessages(activeId!, { limit: 100 }),
    enabled: !!activeId,
    refetchInterval: assistantCollapsed || streaming ? false : 3_000,
    refetchOnWindowFocus: "always",
  });
  const pendingActionsQ = useQuery({
    queryKey: ["system-agent", "actions", activeId, "pending"],
    queryFn: () =>
      listSystemAgentActions({ session_id: activeId!, status: "pending", limit: 50 }),
    enabled: !!activeId && !viewingBotSession,
    refetchInterval: assistantCollapsed && !streaming ? false : 15_000,
  });
  const waitingEventsQ = useQuery({
    queryKey: ["system-agent", "run-events", currentRun?.id],
    queryFn: () => listSystemAgentRunEvents(currentRun!.id, 0, 500),
    enabled: currentRun?.status === "waiting_approval",
    refetchOnMount: "always",
  });
  const waitingApproval = useMemo(() => {
    const events = waitingEventsQ.data || [];
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const approval = events[index].tool_approval;
      if (approval?.tools?.length) return approval;
    }
    return null;
  }, [waitingEventsQ.data]);

  // 深链 /assistant?session=… 优先打开指定会话
  useEffect(() => {
    const deepSession = searchParams.get("session");
    if (!deepSession) return;
    setActiveId(deepSession);
    // 消费一次 query，避免刷新后反复抢焦点；保留 path 便于分享
    const next = new URLSearchParams(searchParams);
    next.delete("session");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  // 深链/选中会话后同步账号与 origin 筛选
  useEffect(() => {
    if (!activeId || !sessionOptions.length) return;
    const match = sessionOptions.find((s) => s.id === activeId);
    if (!match) return;
    if (match.account_id != null) {
      setAccountId(match.account_id);
    }
    if (match.origin === "scheduled") {
      setOriginFilter((prev) => (prev === "all" ? "scheduled" : prev));
    }
  }, [activeId, sessionOptions]);

  // 恢复最后一个 active 会话
  useEffect(() => {
    if (activeId || !sessionOptions.length) return;
    setActiveId(sessionOptions[0].id);
    if (sessionOptions[0].account_id != null) {
      setAccountId(sessionOptions[0].account_id);
    }
  }, [sessionOptions, activeId]);

  useEffect(() => {
    setRuntimeSelection(null);
    clearLiveStreamingState();
  }, [activeId, configQ.data?.provider_id, configQ.data?.model]);

  useEffect(() => {
    if (!meQ.data?.id) return;
    skipNextDraftWriteRef.current = true;
    setComposerValue(readDraft(meQ.data.id, activeId));
    setWaitingInput("");
    setComposerAction("queue");
    setActiveRunSnapshot(null);
  }, [activeId, meQ.data?.id]);

  useEffect(() => {
    if (!meQ.data?.id) return;
    if (skipNextDraftWriteRef.current) {
      skipNextDraftWriteRef.current = false;
      return;
    }
    writeDraft(meQ.data.id, activeId, composerValue);
  }, [activeId, composerValue, meQ.data?.id]);

  useEffect(() => {
    if (currentRun) setActiveRunSnapshot(currentRun);
    else if (activeRunSnapshot?.session_id === activeId && terminalRun(activeRunSnapshot)) {
      setActiveRunSnapshot(null);
    }
  }, [activeId, activeRunSnapshot, currentRun]);

  const createMut = useMutation({
    mutationFn: () =>
      createSystemAgentSession({
        account_id: accountId === "" ? null : Number(accountId),
      }),
    onSuccess: (session) => {
      void qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
      abortRef.current?.abort();
      abortRef.current = null;
      clearLiveStreamingState();
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
        clearLiveStreamingState();
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

  const createMemoryMut = useMutation({
    mutationFn: (content: string) => createSystemAgentUserMemory({ content }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["system-agent", "user-memory"] });
      setMemoryDraft("");
      toast.success("已添加长期记忆");
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const patchMemoryMut = useMutation({
    mutationFn: (vars: { id: number; content?: string; enabled?: boolean }) =>
      patchSystemAgentUserMemory(vars.id, { content: vars.content, enabled: vars.enabled }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["system-agent", "user-memory"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const deleteMemoryMut = useMutation({
    mutationFn: (id: number) => deleteSystemAgentUserMemory(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["system-agent", "user-memory"] });
      toast.success("已删除记忆");
    },
    onError: (err) => toast.error(getErrMsg(err)),
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
  const selectorDisabled = saveConfigMut.isPending || providersQ.isLoading;
  const configuredModel = configQ.data?.model || configuredToolsModels[0] || "";
  const actualSelectionDiffers = Boolean(
    displayedSelection &&
      (displayedSelection.providerName !== configuredProvider?.name ||
        displayedSelection.model !== configuredModel),
  );

  const modelPickerItems = useMemo(
    () => matrixToPickerItems(capsQ.data?.model_matrix || [], { requireTools: true }),
    [capsQ.data?.model_matrix],
  );
  const gatewayModelPickerItems = useMemo(
    () => modelPickerItems.filter(
      (item) => item.executionBackend === "codex_gateway" && item.agentEligible !== false,
    ),
    [modelPickerItems],
  );
  const visibleModelPickerItems = sessionModel.executionBackend === "codex_gateway"
    ? gatewayModelPickerItems
    : modelPickerItems;

  const pickerValue: ModelPickerValue =
    sessionModel.mode === "pinned"
      ? { mode: "pinned", providerId: sessionModel.providerId, model: sessionModel.model }
      : { mode: "auto" };

  const expectedSelectionLabel = useMemo(() => {
    if (sessionModel.mode === "pinned") {
      const name =
        modelPickerItems.find(
          (item) =>
            item.providerId === sessionModel.providerId && item.model === sessionModel.model,
        )?.providerName || `Provider #${sessionModel.providerId}`;
      return `${name} · ${sessionModel.model}`;
    }
    if (configuredProvider) {
      return `自动 · ${configuredProvider.name} · ${configuredModel || "未选模型"}`;
    }
    return "自动路由";
  }, [sessionModel, modelPickerItems, configuredProvider, configuredModel]);

  // 切换会话时恢复 localStorage 中的本轮选择
  useEffect(() => {
    setSessionModel(loadSessionModelSelection(activeId));
  }, [activeId]);

  const onSessionModelChange = (next: ModelPickerValue) => {
    const selection: SessionModelSelection =
      next.mode === "pinned"
        ? {
            mode: "pinned",
            providerId: next.providerId,
            model: next.model,
            executionBackend: sessionModel.executionBackend,
            clientIdentityProfile: sessionModel.clientIdentityProfile,
          }
        : {
            mode: "auto",
            executionBackend: sessionModel.executionBackend,
            clientIdentityProfile: sessionModel.clientIdentityProfile,
          };
    setSessionModel(selection);
    if (activeId) saveSessionModelSelection(activeId, selection);
  };

  const onSessionClientChange = (next: {
    executionBackend: SessionModelSelection["executionBackend"];
    clientIdentityProfile?: SessionModelSelection["clientIdentityProfile"];
  }) => {
    const pinnedGatewayCompatible = sessionModel.mode === "pinned"
      && gatewayModelPickerItems.some(
        (item) => item.providerId === sessionModel.providerId && item.model === sessionModel.model,
      );
    const firstGatewayModel = gatewayModelPickerItems[0];
    const selection = next.executionBackend === "codex_gateway" && !pinnedGatewayCompatible
      && firstGatewayModel
      ? {
          mode: "pinned" as const,
          providerId: firstGatewayModel.providerId,
          model: firstGatewayModel.model,
          executionBackend: next.executionBackend,
          clientIdentityProfile: next.clientIdentityProfile,
        }
      : { ...sessionModel, ...next } as SessionModelSelection;
    setSessionModel(selection);
    if (activeId) saveSessionModelSelection(activeId, selection);
  };

  const onSetDefaultModel = (providerId: number, model: string) => {
    saveConfigMut.mutate({
      provider_id: providerId,
      model: model || null,
      enabled: true,
    });
  };

  const selectProvider = (providerId: string) => {
    const provider = (providersQ.data || []).find((item) => item.id === Number(providerId));
    if (!provider) return;
    // 顶部配置区：显式改全局默认 Provider
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

  const liveAssistantIdentity = () => {
    const messageId = liveAssistantMessageIdRef.current;
    return {
      id: messageId == null ? "live-assistant-stream" : `m-${messageId}`,
      messageId: messageId ?? undefined,
    };
  };

  const showStreamingReply = (value: string, options?: { fallback?: boolean }) => {
    setLive((prev) => {
      const pending = prev.find((bubble) => bubble.role === "assistant" && bubble.pending);
      const identity = liveAssistantIdentity();
      const assistant = prev.find((bubble) => bubble.id === identity.id);
      const nextBubble: LiveBubble = {
        ...identity,
        role: "assistant",
        text: value,
        reasoning: liveReasoning.textRef.current,
        streaming: true,
        streamFallback: options?.fallback,
      };
      const rest = prev.filter(
        (bubble) => bubble.id !== identity.id && bubble !== pending,
      );
      return [...rest, assistant ? { ...assistant, ...nextBubble } : nextBubble];
    });
  };

  const appendStreamingDelta = (delta: string) => {
    liveText.append(delta);
    if (!streamingBubbleCreatedRef.current) {
      streamingBubbleCreatedRef.current = true;
      showStreamingReply(liveText.textRef.current);
    }
  };

  const appendReasoningDelta = (delta: string) => {
    liveReasoning.append(delta);
    if (!streamingBubbleCreatedRef.current) {
      streamingBubbleCreatedRef.current = true;
      showStreamingReply(liveText.textRef.current);
    }
  };

  const resetStreamingReply = () => {
    // 过渡语不整泡删除：截断并入状态行，流式气泡清空复用
    const preview = liveText.textRef.current.trim().slice(0, 60);
    if (preview) {
      updatePendingText(`已理解：${preview}${liveText.textRef.current.trim().length > 60 ? "…" : ""}，正在调用工具`);
    } else {
      updatePendingText("已理解你的需求，正在调用工具…");
    }
    liveText.clear();
    setStreamNotice("");
    streamingBubbleCreatedRef.current = false;
    setLive((prev) =>
      prev.map((bubble) =>
        bubble.id === liveAssistantIdentity().id
          ? { ...bubble, text: "", streaming: true, streamFallback: false }
          : bubble,
      ),
    );
  };

  useEffect(() => {
    const current = liveText.text;
    if (!current || !streamingBubbleCreatedRef.current) return;
    setLive((prev) =>
      prev.map((bubble) =>
        bubble.id === liveAssistantIdentity().id ? { ...bubble, text: current } : bubble,
      ),
    );
  }, [liveText.text]);

  useEffect(() => {
    const current = liveReasoning.text;
    if (!current || !streamingBubbleCreatedRef.current) return;
    setLive((prev) =>
      prev.map((bubble) =>
        bubble.id === liveAssistantIdentity().id ? { ...bubble, reasoning: current } : bubble,
      ),
    );
  }, [liveReasoning.text]);

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
    if (event.type === "assistant_delta_reset") {
      resetStreamingReply();
    }
    if (event.type === "assistant_delta") {
      setStreamNotice("");
      appendStreamingDelta(String(event.delta || ""));
    }
    if (event.type === "assistant_reasoning_delta") {
      setStreamNotice("");
      appendReasoningDelta(String(event.delta || ""));
    }
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
      const finalText = String(event.content || "");
      const finalReasoning = String(event.reasoning || "");
      liveText.syncSnapshot(finalText);
      liveReasoning.syncSnapshot(finalReasoning);
      streamingBubbleCreatedRef.current = false;
      const usage =
        event.usage && typeof event.usage === "object"
          ? (event.usage as Record<string, unknown>)
          : null;
      // 同 id 翻转 streaming，避免 remount 闪动
      setLive((prev) => {
        const identity = liveAssistantIdentity();
        const withoutPending = prev.filter((bubble) => !bubble.pending);
        const hasStream = withoutPending.some((b) => b.id === identity.id);
        if (hasStream) {
          return withoutPending.map((bubble) =>
            bubble.id === identity.id
              ? {
                  ...bubble,
                  text: finalText,
                  reasoning: finalReasoning,
                  createdAt: typeof event.ts === "string" ? event.ts : new Date().toISOString(),
                  streaming: false,
                  streamFallback: Boolean(event.stream_fallback || usage?.stream_fallback),
                  usage,
                }
              : bubble,
          );
        }
        return [
          ...withoutPending,
          {
            ...identity,
            role: "assistant" as const,
            text: finalText,
            reasoning: finalReasoning,
            createdAt: typeof event.ts === "string" ? event.ts : new Date().toISOString(),
            streaming: false,
            streamFallback: Boolean(event.stream_fallback || usage?.stream_fallback),
            usage,
          },
        ];
      });
    }
    if (event.type === "error") {
      const requestIds = upstreamErrorRequestIds(event);
      const errorDescription = [
        event.upstream_error_detail
          ? `详细信息：${event.upstream_error_detail}`
          : null,
        requestIds,
        event.suggestion ? `建议：${event.suggestion}` : null,
      ].filter(Boolean).join("\n");
      if (event.code === "RUN_STREAM_FAILED") {
        setStreamNotice("进度连接中断，正在恢复…");
        updatePendingText("进度连接中断，正在恢复…");
      } else if (
        event.code === "AGENT_PROVIDER_SWITCH_REQUIRED" ||
        event.code === "AGENT_TOOL_APPROVAL_REQUIRED"
      ) {
        toast.message(event.message || "当前 Provider 内模型均不可用，请确认是否切换", {
          description: errorDescription || undefined,
        });
      } else {
        toast.error(event.message || "助手运行失败", {
          description: errorDescription || undefined,
        });
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
    if (event.type === "done") {
      setStreamNotice("");
    }
  };

  const refreshRunData = async (sessionId: string) => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["system-agent", "messages", sessionId] }),
      qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] }),
      qc.invalidateQueries({ queryKey: ["system-agent", "actions", sessionId] }),
      qc.invalidateQueries({ queryKey: ["system-agent", "runs", "task-center"] }),
      qc.invalidateQueries({ queryKey: ["system-agent", "queue"] }),
    ]);
  };

  const followRun = async (
    sessionId: string,
    stored: StoredRun,
    controller: AbortController,
  ) => {
    const runId = stored.runId;
    let cursor = Math.max(0, stored.lastSeq);
    let doneReceived = false;
    let doneOk = false;
    let reconnectAttempt = 0;
    let pausedForInput = false;
    try {
      while (!controller.signal.aborted && !doneReceived) {
        let streamFailed = false;
        try {
          await streamSystemAgentRun(
            runId,
            cursor,
            (event) => {
              const seq = Number(event.seq || 0);
              // durable stream 重连时可能重放游标边界事件，不重复追加文本。
              if (seq > 0 && seq <= cursor) return;
              if (seq > cursor) cursor = seq;
              const next = { ...stored, runId, lastSeq: cursor };
              rememberRun(sessionId, next);
              setActiveRun(next);
              handleRunEvent(event);
              if (event.type === "error" && event.code === "RUN_STREAM_FAILED") {
                streamFailed = true;
              }
              if (event.type === "done") {
                doneReceived = true;
                doneOk = event.ok === true;
              }
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
          if (!snapshot) {
            forgetRun(sessionId, runId);
            setActiveRun((current) => (current?.runId === runId ? null : current));
            setStreamNotice("本轮进度已失效，请重新发送消息。");
            setStreaming(false);
            return;
          }
          setActiveRunSnapshot(snapshot);
          if (snapshot.status === "waiting_input" || snapshot.status === "waiting_approval") {
            pausedForInput = true;
            setStreamNotice(
              snapshot.status === "waiting_input"
                ? "任务正在等待补充信息；填写后会从持久队列继续。"
                : "任务正在等待工具审批；处理后会从持久队列继续。",
            );
            await refreshRunData(sessionId);
            return;
          }
          if (snapshot && terminalRun(snapshot)) {
            // 终态事件也在同一事件表中；继续按游标读取，直到真正收到 done。
            cursor = Math.min(cursor, Math.max(0, snapshot.last_seq - 1));
          }
          reconnectAttempt += 1;
          setStreamNotice("进度连接中断，正在恢复…");
          updatePendingText("进度连接中断，正在恢复…");
          await new Promise((resolve) =>
            window.setTimeout(resolve, Math.min(2000, 250 * reconnectAttempt)),
          );
        }
      }
      if (!doneReceived) return;
      const finalSnapshot = await getSystemAgentRun(runId).catch(() => null);
      if (finalSnapshot) {
        setActiveRunSnapshot(finalSnapshot);
        const settlement = classifySystemAgentRunSettlement(finalSnapshot.status);
        if (settlement === "waiting") {
          pausedForInput = true;
          setStreamNotice(
            finalSnapshot.status === "waiting_input"
              ? "任务正在等待补充信息；填写后会从持久队列继续。"
              : "任务正在等待工具审批；处理后会从持久队列继续。",
          );
          await refreshRunData(sessionId);
          return;
        }
      }
      forgetRun(sessionId, runId);
      setActiveRun((current) => (current?.runId === runId ? null : current));
      setActiveRunSnapshot(null);
      await refreshRunData(sessionId);
      const settlement = finalSnapshot
        ? classifySystemAgentRunSettlement(finalSnapshot.status)
        : doneOk
          ? "complete"
          : "failed";
      if (settlement === "complete" || settlement === "failed") {
        notifyOutcome(settlement);
      }
      if (
        (settlement === "complete" || settlement === "failed") &&
        document.hidden &&
        "Notification" in window &&
        Notification.permission === "granted"
      ) {
        new Notification(settlement === "complete" ? "系统助手任务已完成" : "系统助手任务失败", {
          body:
            settlement === "complete"
              ? "点击返回查看结果。"
              : "点击返回查看错误和重试选项。",
        });
      }
      if (abortRef.current === controller) {
        streamingBubbleCreatedRef.current = false;
        clearLiveStreamingState();
        setLive([]);
        liveAssistantMessageIdRef.current = null;
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setStreaming(false);
        setRetryingMessageId(null);
        if (!pausedForInput) setStreamNotice("");
      }
    }
  };

  const runTurn = async ({
    text,
    retryMessageId,
    regenerate,
    fallbackProviderId,
    approvedTools,
  }: {
    text?: string;
    retryMessageId?: number;
    regenerate?: {
      userMessageId: number;
      assistantMessageId: number;
      editedText?: string;
    };
    fallbackProviderId?: number;
    approvedTools?: string[];
  }) => {
    if (streaming || abortRef.current) return;
    if (!enabled) {
      toast.error("请先在右上角开启系统助手并选择支持 tools 的 Provider");
      setConfigOpen(true);
      return;
    }
    setStreaming(true);
    setRetryingMessageId(retryMessageId ?? regenerate?.userMessageId ?? null);
    const controller = new AbortController();
    abortRef.current = controller;
    liveAssistantMessageIdRef.current = regenerate?.assistantMessageId ?? null;
    const userBubble: LiveBubble | null = regenerate?.editedText
      ? {
          id: `m-${regenerate.userMessageId}`,
          messageId: regenerate.userMessageId,
          role: "user",
          text: regenerate.editedText,
          createdAt: new Date().toISOString(),
        }
      : text
        ? {
            id: `live-user-${Date.now()}`,
            role: "user",
            text,
            createdAt: new Date().toISOString(),
          }
        : null;
    const pending: LiveBubble = {
      id: regenerate
        ? `m-${regenerate.assistantMessageId}`
        : `live-assistant-${Date.now()}`,
      messageId: regenerate?.assistantMessageId,
      role: "assistant",
      text: "正在理解你的需求…",
      pending: true,
      createdAt: new Date().toISOString(),
    };
    clearLiveStreamingState();
    setLive(userBubble ? [userBubble, pending] : [pending]);
    try {
      const sessionId = await ensureSession();
      if (activeId !== sessionId) {
        // 新建会话：把当前选择绑到新 session key
        saveSessionModelSelection(sessionId, sessionModel);
      }
      const accountPayload = { account_id: accountId === "" ? null : Number(accountId) };
      const model_selection = toApiModelSelection(sessionModel);
      const run = regenerate
        ? await startSystemAgentRegenerateRun(sessionId, regenerate.userMessageId, {
            assistant_message_id: regenerate.assistantMessageId,
            content: regenerate.editedText,
            ...accountPayload,
            client_request_id: requestId(),
            model_selection,
          })
        : retryMessageId != null
        ? await startSystemAgentRetryRun(sessionId, retryMessageId, {
            ...accountPayload,
            fallback_provider_id: fallbackProviderId ?? null,
            approved_tools: approvedTools || [],
            client_request_id: requestId(),
            model_selection,
          })
        : await startSystemAgentRun(sessionId, {
            content: text || "",
            ...accountPayload,
            client_request_id: requestId(),
            model_selection,
          });
      const saved: StoredRun = {
        runId: run.id,
        lastSeq: 0,
        targetUserMessageId: regenerate?.userMessageId,
        targetAssistantMessageId: regenerate?.assistantMessageId,
        editedUserText: regenerate?.editedText,
      };
      rememberRun(sessionId, saved);
      setActiveRun(saved);
      await followRun(sessionId, saved, controller);
    } catch (error) {
      if (!controller.signal.aborted) {
        toast.error(getErrMsg(error));
        clearLiveStreamingState();
        if (regenerate) {
          if (activeId) await refreshRunData(activeId).catch(() => undefined);
          setLive([]);
          liveAssistantMessageIdRef.current = null;
        } else {
          setLive((prev) =>
            prev.filter((bubble) => bubble.role === "user" || bubble.role === "action"),
          );
        }
      }
      if (abortRef.current === controller) {
        abortRef.current = null;
        setStreaming(false);
        setRetryingMessageId(null);
      }
    }
  };

  const onSend = async (text: string) => {
    try {
      if (!hasOpenRun) {
        await runTurn({ text });
        return;
      }
      if (!enabled) {
        toast.error("请先开启系统助手");
        return;
      }
      const sessionId = await ensureSession();
      const target = currentRun || activeRunSnapshot;
      if (!target) {
        await runTurn({ text });
        return;
      }
      if (composerAction === "steer") {
        if (target.status !== "running") {
          toast.error("只有正在运行的任务可以调整；等待态请使用下方补充或审批操作");
          return;
        }
        await steerSystemAgentRun(target.id, {
          content: text,
          client_request_id: requestId(),
        });
        toast.success("已提交调整，会在下一个安全边界应用");
        return;
      }
      if (composerAction === "replace") {
        if (!["running", "waiting_input", "waiting_approval"].includes(target.status)) {
          toast.error("当前任务尚未进入可替换状态");
          return;
        }
        const replacement = await stopAndReplaceSystemAgentRun(target.id, {
          content: text,
          client_request_id: requestId(),
          model_selection: toApiModelSelection(sessionModel),
        });
        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;
        const saved: StoredRun = { runId: replacement.id, lastSeq: 0 };
        rememberRun(sessionId, saved);
        setActiveRun(saved);
        setActiveRunSnapshot(replacement);
        setStreaming(true);
        clearLiveStreamingState();
        setLive([
          {
            id: `live-user-${Date.now()}`,
            role: "user",
            text,
            createdAt: new Date().toISOString(),
          },
          {
            id: `live-assistant-${Date.now()}`,
            role: "assistant",
            text: "正在停止上一任务并切换…",
            pending: true,
          },
        ]);
        void refreshRunData(sessionId);
        await followRun(sessionId, saved, controller);
        return;
      }
      const queued = await startSystemAgentRun(sessionId, {
        content: text,
        account_id: accountId === "" ? null : Number(accountId),
        client_request_id: requestId(),
        model_selection: toApiModelSelection(sessionModel),
      });
      await refreshRunData(sessionId);
      const pendingAhead = currentQueue.filter((item) => item.status === "pending").length;
      toast.success(
        queued.status === "running"
          ? "已开始执行"
          : `已加入队列${pendingAhead ? `，前面有 ${pendingAhead} 条` : ""}`,
      );
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

  const onEditMessage = async (
    userMessageId: number,
    assistantMessageId: number,
    editedText: string,
  ) => runTurn({ regenerate: { userMessageId, assistantMessageId, editedText } });

  const onRegenerateMessage = async (
    userMessageId: number,
    assistantMessageId: number,
  ) => runTurn({ regenerate: { userMessageId, assistantMessageId } });

  const onRetryMessage = async (
    messageId: number,
    fallbackProviderId?: number,
    approvedTools?: string[],
  ) => runTurn({ retryMessageId: messageId, fallbackProviderId, approvedTools });

  const onStop = () => {
    const stored = activeRun || (activeId ? storedRun(activeId) : null);
    const runId = currentRun?.id || stored?.runId;
    if (!runId) return;
    updatePendingText("正在停止本轮请求…");
    void cancelSystemAgentRun(runId)
      .then((run) => {
        if (activeId && terminalRun(run)) {
          forgetRun(activeId, run.id);
          setActiveRun((current) => (current?.runId === run.id ? null : current));
          setActiveRunSnapshot(null);
        }
        if (activeId) void refreshRunData(activeId);
      })
      .catch((error) => toast.error(getErrMsg(error)));
  };

  const submitWaitingInput = async () => {
    if (!currentRun || currentRun.status !== "waiting_input") return;
    const content = waitingInput.trim();
    if (!content) return;
    try {
      await submitSystemAgentRunInput(currentRun.id, {
        content,
        client_request_id: requestId(),
      });
      setWaitingInput("");
      setStreamNotice("补充信息已提交，任务即将继续…");
      await refreshRunData(currentRun.session_id);
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

  const decideApproval = async (approved: boolean) => {
    if (!currentRun || currentRun.status !== "waiting_approval") return;
    const approvedTools = (waitingApproval?.tools || []).map((tool) => tool.name);
    try {
      await approveSystemAgentRun(currentRun.id, {
        approved,
        approved_tools: approved ? approvedTools : [],
        client_request_id: requestId(),
      });
      if (!approved) {
        forgetRun(currentRun.session_id, currentRun.id);
        setActiveRun((current) =>
          current?.runId === currentRun.id ? null : current,
        );
        setActiveRunSnapshot(null);
      }
      setStreamNotice(approved ? "审批已通过，任务即将继续…" : "已拒绝工具调用并结束本任务。");
      await refreshRunData(currentRun.session_id);
    } catch (error) {
      toast.error(getErrMsg(error));
    }
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
    liveAssistantMessageIdRef.current = saved.targetAssistantMessageId ?? null;
    const resumed: LiveBubble[] = [];
    if (saved.targetUserMessageId && saved.editedUserText) {
      resumed.push({
        id: `m-${saved.targetUserMessageId}`,
        messageId: saved.targetUserMessageId,
        role: "user",
        text: saved.editedUserText,
      });
    }
    resumed.push({
      id: saved.targetAssistantMessageId
        ? `m-${saved.targetAssistantMessageId}`
        : `live-resume-${saved.runId}`,
      messageId: saved.targetAssistantMessageId,
      role: "assistant",
      text: "正在恢复本轮进度…",
      pending: true,
    });
    setLive(resumed);
    clearLiveStreamingState();
    void followRun(activeId, saved, controller).catch(
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

  useEffect(() => {
    if (
      !activeId ||
      abortRef.current ||
      !currentRun ||
      !["queued", "running"].includes(currentRun.status)
    ) {
      return;
    }
    const existing = storedRun(activeId);
    const saved =
      existing?.runId === currentRun.id
        ? existing
        : { runId: currentRun.id, lastSeq: 0 };
    rememberRun(activeId, saved);
    setActiveRun(saved);
    setActiveRunSnapshot(currentRun);
    setStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;
    if (live.length === 0) {
      setLive([
        {
          id: `live-resume-${currentRun.id}`,
          role: "assistant",
          text: currentRun.status === "queued" ? "任务正在排队…" : "正在恢复任务进度…",
          pending: true,
        },
      ]);
    }
    void followRun(activeId, saved, controller).catch((error) => {
      if (!controller.signal.aborted) toast.error(getErrMsg(error));
    });
    return () => {
      controller.abort();
      if (abortRef.current === controller) abortRef.current = null;
    };
    // currentRun 的状态由任务中心轮询推进；只在 queued/running 时重新订阅。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, currentRun?.id, currentRun?.status]);

  const editQueueItem = async (item: SystemAgentQueueItem, content: string) => {
    try {
      await updateSystemAgentQueueItem(item.id, { content });
      await qc.invalidateQueries({ queryKey: ["system-agent", "queue"] });
      toast.success("排队消息已更新");
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

  const deleteQueueItem = async (item: SystemAgentQueueItem) => {
    try {
      await deleteSystemAgentQueueItem(item.id);
      await refreshRunData(item.session_id);
      toast.success("已移出队列");
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

  const moveQueueItem = async (item: SystemAgentQueueItem, direction: -1 | 1) => {
    const rows = allQueue
      .filter(
        (row) =>
          row.session_id === item.session_id &&
          ["pending", "paused"].includes(row.status),
      )
      .sort((left, right) => left.position - right.position);
    const index = rows.findIndex((row) => row.id === item.id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= rows.length) return;
    const ids = rows.map((row) => row.id);
    [ids[index], ids[target]] = [ids[target], ids[index]];
    try {
      await reorderSystemAgentQueue(item.session_id, ids);
      await qc.invalidateQueries({ queryKey: ["system-agent", "queue"] });
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

  const clearQueue = async (sessionId: string) => {
    try {
      const count = await clearSystemAgentQueue(sessionId);
      await refreshRunData(sessionId);
      toast.success(`已清空 ${count} 条排队消息`);
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

  const resumeQueue = async (sessionId: string) => {
    try {
      const count = await resumeSystemAgentQueue(sessionId);
      await refreshRunData(sessionId);
      toast.success(`已恢复 ${count} 条排队消息`);
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

  const enableTaskNotifications = async () => {
    if (!("Notification" in window)) {
      setNotificationPermission("unsupported");
      toast.error("当前浏览器不支持系统通知");
      return;
    }
    try {
      const permission = await Notification.requestPermission();
      setNotificationPermission(permission);
      if (permission === "granted") {
        toast.success("任务完成通知已开启");
      } else if (permission === "denied") {
        toast.error("浏览器已拒绝通知，请在站点权限中重新开启");
      }
    } catch (error) {
      toast.error(getErrMsg(error));
    }
  };

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

  const renderAgentContextControls = () => {
    return (
      <div
        data-assistant-context-controls="header"
        className="hidden min-w-0 flex-wrap items-center justify-end gap-2 sm:flex"
      >
        <label className="flex min-w-0 items-center gap-1.5">
          <span className="flex shrink-0 items-center gap-1 text-sm text-muted-foreground">
            <Server className="h-3.5 w-3.5 shrink-0" />
            Provider
          </span>
          <Select
            aria-label="切换 Agent Provider"
            value={configQ.data?.provider_id == null ? "" : String(configQ.data.provider_id)}
            disabled={selectorDisabled}
            onChange={(event) => selectProvider(event.target.value)}
            className="h-8 w-44 min-w-0 text-xs"
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
        <label className="flex min-w-0 items-center gap-1.5">
          <span className="shrink-0 text-sm text-muted-foreground">账号上下文</span>
          <Select
            value={accountId === "" ? "" : String(accountId)}
            onChange={(event) => setAccountId(event.target.value ? Number(event.target.value) : "")}
            className="h-8 w-44 min-w-0 text-xs"
          >
            <option value="">系统级</option>
            {accountOptions.map((account) => (
              <option key={account.id} value={account.id}>
                #{account.id} {account.display_name || account.phone || account.tg_username || ""}
              </option>
            ))}
          </Select>
        </label>
        {(streaming || actualSelectionDiffers) && displayedSelection ? (
          <span className="min-w-0 max-w-56 truncate text-xs text-muted-foreground">
            实际调用：{displayedSelection.providerName} · {displayedSelection.model}
          </span>
        ) : null}
      </div>
    );
  };

  return (
    <PageShell className="flex h-full min-h-0 flex-col gap-3 space-y-0">
      <div className="hidden shrink-0 sm:block">
        <PageHeader
          title="系统助手"
          description="用自然语言查询并操作系统能力；写操作需内联确认。"
          icon={AgentMark}
          actions={renderAgentContextControls()}
        />
      </div>

      <div className="shrink-0 overflow-hidden rounded-lg border border-border/70 bg-muted/20 sm:overflow-visible sm:p-2.5">
        <button
          type="button"
          data-assistant-mobile-summary
          className="flex h-12 w-full items-center gap-2 px-3 text-left sm:hidden"
          aria-expanded={mobileHeaderExpanded}
          onClick={() => setMobileHeaderExpanded((value) => !value)}
        >
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
            <AgentMark className="h-4 w-4" />
          </span>
          <span className="shrink-0 text-sm font-semibold">系统助手</span>
          <span className="min-w-0 truncate text-[11px] text-muted-foreground">
            {configQ.isLoading ? "加载中" : enabled ? (streaming ? "调用中" : "已启用") : "未启用"}
          </span>
          <span className="ml-auto shrink-0 text-[11px] text-primary">展开后可配置</span>
          <ChevronDown className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${mobileHeaderExpanded ? "rotate-180" : ""}`} />
        </button>
        <div data-assistant-mobile-settings className={`${mobileHeaderExpanded && !configOpen ? "block" : "hidden"} border-t p-2.5 sm:block sm:border-t-0 sm:p-0`}>
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
              className="h-8 px-2.5"
              onClick={() => setConfigOpen((v) => !v)}
              aria-expanded={configOpen}
            >
              <Settings2 className="h-4 w-4" />
              配置
            </Button>
          </div>
        </div>
        </div>
      </div>

      {configOpen ? (
        <>
        <button
          type="button"
          className="fixed inset-0 z-[69] bg-black/20 animate-in fade-in duration-200 sm:hidden"
          aria-label="关闭助手配置"
          onClick={() => setConfigOpen(false)}
        />
        <div
          data-assistant-config-panel
          className="fixed bottom-[calc(4.75rem+env(safe-area-inset-bottom))] right-0 top-[calc(5rem+env(safe-area-inset-top))] z-[70] w-[min(320px,88vw)] animate-in overflow-y-auto overscroll-contain rounded-l-2xl border-l border-border/70 bg-card p-4 text-sm shadow-[0_6px_18px_rgba(15,23,42,0.10)] slide-in-from-right-3 duration-200 sm:static sm:z-auto sm:mt-0 sm:max-h-96 sm:w-auto sm:shrink sm:rounded-lg sm:border sm:shadow-none sm:animate-none"
        >
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="font-medium">系统助手模型</div>
            <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs sm:hidden" onClick={() => setConfigOpen(false)}>
              收起配置
            </Button>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="flex min-h-11 items-center justify-between gap-3 rounded-md border px-3 py-2">
              <span>启用系统助手</span>
              <Switch
                checked={!!configQ.data?.enabled}
                disabled={saveConfigMut.isPending}
                onCheckedChange={(checked) => saveConfigMut.mutate({ enabled: checked })}
                aria-label="启用系统助手"
              />
            </div>
            <div className="flex min-h-11 items-center justify-between gap-3 rounded-md border px-3 py-2">
              <span>调用工具前需要批准</span>
              <Switch
                checked={!!configQ.data?.require_tool_approval}
                disabled={saveConfigMut.isPending}
                onCheckedChange={(checked) => saveConfigMut.mutate({ require_tool_approval: checked })}
                aria-label="调用工具前需要批准"
              />
            </div>
            <div className="flex min-h-11 items-center justify-between gap-3 rounded-md border px-3 py-2 sm:col-span-2">
              <span className="min-w-0">
                <span className="flex items-center gap-1.5">
                  {notificationPermission === "granted" ? (
                    <BellRing className="h-4 w-4 text-emerald-600" />
                  ) : notificationPermission === "denied" ? (
                    <BellOff className="h-4 w-4 text-destructive" />
                  ) : (
                    <Bell className="h-4 w-4 text-muted-foreground" />
                  )}
                  <span>任务完成通知</span>
                </span>
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  {notificationPermission === "granted"
                    ? "页面在后台时，完成或失败会发送系统通知。"
                    : notificationPermission === "denied"
                      ? "浏览器已拒绝通知，请在站点权限中重新开启。"
                      : notificationPermission === "unsupported"
                        ? "当前浏览器不支持系统通知。"
                        : "仅在你主动开启后请求浏览器通知权限。"}
                </span>
              </span>
              {notificationPermission === "default" ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="min-h-9 shrink-0 active:scale-95"
                  onClick={() => void enableTaskNotifications()}
                >
                  开启
                </Button>
              ) : null}
            </div>
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
            <label className="flex flex-col gap-1 sm:col-span-2">
              <span className="text-muted-foreground">本轮 token 预算</span>
              <Input
                key={configQ.data?.session_token_limit ?? 16_384}
                type="number"
                min={0}
                step={1024}
                defaultValue={configQ.data?.session_token_limit ?? 16_384}
                placeholder="0 表示无上限"
                disabled={saveConfigMut.isPending}
                onBlur={(event) => {
                  const raw = Number(event.target.value);
                  const next = raw <= 0 || Number.isNaN(raw) ? 0 : Math.max(1024, Math.floor(raw));
                  if (next !== configQ.data?.session_token_limit) {
                    saveConfigMut.mutate({ session_token_limit: next });
                  }
                }}
              />
              <span className="text-xs text-muted-foreground">
                填 0 表示无上限；非 0 时只限制本轮新增输出和工具结果增长。
              </span>
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
                    <div
                      key={provider.id}
                      className="flex min-w-0 items-center justify-between gap-3 rounded-md border px-3 py-2"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-foreground">{provider.name}</span>
                        <span className="block truncate text-xs text-muted-foreground">
                          {eligible ? `${models.length} 个 Tools 模型` : "不可用于 Agent fallback"}
                        </span>
                      </span>
                      <Switch
                        checked={checked}
                        disabled={!eligible || saveConfigMut.isPending}
                        onCheckedChange={(nextChecked) => {
                          const current = configQ.data?.fallback_provider_ids || [];
                          const next = nextChecked
                            ? [...current, provider.id]
                            : current.filter((id) => id !== provider.id);
                          saveConfigMut.mutate({ fallback_provider_ids: next });
                        }}
                        aria-label={`${provider.name} fallback`}
                      />
                    </div>
                  );
                })}
            </div>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            仅允许声明支持 tools 的模型。写操作会生成待确认卡片；未配置时助手会给出 AI 中心入口。
            {capsQ.data ? ` · 已注册 ${capsQ.data.tools.filter((t) => t.available).length} 个可用工具` : null}
          </p>
          {capsQ.data?.tools?.length ? (
            <div className="mt-4 border-t pt-3">
              <div className="font-medium">能力矩阵 · 工具来源</div>
              <p className="mt-1 text-xs text-muted-foreground">
                插件贡献的工具带「插件」徽标；第一期仅只读暴露。
              </p>
              <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto text-xs">
                {capsQ.data.tools
                  .filter((t) => t.available)
                  .slice(0, 80)
                  .map((tool) => (
                    <li
                      key={tool.name}
                      className="flex flex-wrap items-center gap-1.5 rounded border border-border/60 px-2 py-1"
                    >
                      {tool.source === "plugin" ? (
                        <span className="rounded bg-violet-500/15 px-1 py-0.5 text-[10px] text-violet-700 dark:text-violet-300">
                          插件{tool.plugin_key ? ` · ${tool.plugin_key}` : ""}
                        </span>
                      ) : (
                        <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
                          内置
                        </span>
                      )}
                      <code className="text-[11px]">{tool.name}</code>
                      {tool.read_only ? (
                        <span className="text-[10px] text-muted-foreground">只读</span>
                      ) : (
                        <span className="text-[10px] text-amber-700 dark:text-amber-300">写</span>
                      )}
                    </li>
                  ))}
              </ul>
            </div>
          ) : null}
          <div className="mt-4 border-t pt-3">
            <div className="font-medium">长期记忆</div>
            <p className="mt-1 text-xs text-muted-foreground">
              跨会话保留的偏好（最多 20 条）。也可对助手说「记住…」经确认后写入。
            </p>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              <input
                className="min-w-0 flex-1 rounded-md border bg-background px-3 py-2 text-sm"
                placeholder="例如：回复请尽量简短"
                value={memoryDraft}
                maxLength={200}
                onChange={(e) => setMemoryDraft(e.target.value)}
              />
              <Button
                type="button"
                size="sm"
                disabled={!memoryDraft.trim() || createMemoryMut.isPending}
                onClick={() => createMemoryMut.mutate(memoryDraft.trim())}
              >
                添加
              </Button>
            </div>
            <div className="mt-3 space-y-2">
              {memoryQ.isLoading ? (
                <Skeleton className="h-10 w-full" />
              ) : (memoryQ.data || []).length === 0 ? (
                <p className="text-xs text-muted-foreground">暂无长期记忆</p>
              ) : (
                (memoryQ.data as SystemAgentUserMemory[]).map((item) => (
                  <div
                    key={item.id}
                    className="rounded-md border border-border/70 bg-background/45 px-3 py-2.5"
                  >
                    <p className="whitespace-pre-wrap break-words text-sm leading-6 text-foreground">
                      {item.content}
                    </p>
                    <div className="mt-2 flex min-h-8 items-center justify-between gap-3 border-t border-border/45 pt-2">
                      <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                        <Switch
                          className="scale-90"
                          checked={item.enabled}
                          disabled={patchMemoryMut.isPending}
                          onCheckedChange={(checked) => patchMemoryMut.mutate({ id: item.id, enabled: checked })}
                          aria-label={`切换记忆：${item.content}`}
                        />
                        <span>{item.enabled ? "已启用" : "已停用"}</span>
                        <span className="truncate text-[10px] text-muted-foreground/65">
                          {item.source === "agent_learned" ? "助手记录" : "手动添加"}
                        </span>
                      </div>
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        className="h-7 w-7 shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                        disabled={deleteMemoryMut.isPending}
                        onClick={() => {
                          if (confirm("确认删除这条记忆？")) deleteMemoryMut.mutate(item.id);
                        }}
                        aria-label="删除长期记忆"
                        title="删除长期记忆"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
        </>
      ) : null}

      <div data-assistant-chat-window className="flex min-h-[min(20rem,40dvh)] flex-1 overflow-hidden rounded-xl border bg-card sm:min-h-[min(22rem,48dvh)]">
        <SessionDrawer
          sessions={sessionOptions}
          activeId={activeId}
          runStatusBySession={runStatusBySession}
          queueCountBySession={queueCountBySession}
          originFilter={originFilter}
          onOriginFilterChange={setOriginFilter}
          onSelect={(id) => {
            abortRef.current?.abort();
            abortRef.current = null;
            clearLiveStreamingState();
            setStreaming(false);
            setRetryingMessageId(null);
            setLive([]);
            setActiveId(id);
          }}
          onCreate={() => createMut.mutate()}
          onDelete={(id) => deleteMut.mutate(id)}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          desktopCollapsed={sessionSidebarCollapsed}
          onDesktopCollapse={() => setSessionSidebarCollapsed(true)}
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <TaskCenter
            sessions={sessionOptions}
            runs={allRuns}
            queue={allQueue}
            activeSessionId={activeId}
            onSelectSession={(id) => {
              if (id === activeId) return;
              abortRef.current?.abort();
              abortRef.current = null;
              clearLiveStreamingState();
              setStreaming(false);
              setRetryingMessageId(null);
              setLive([]);
              setActiveId(id);
            }}
            onEditQueueItem={(item, content) => void editQueueItem(item, content)}
            onDeleteQueueItem={(item) => void deleteQueueItem(item)}
            onMoveQueueItem={(item, direction) => void moveQueueItem(item, direction)}
            onClearQueue={(sessionId) => void clearQueue(sessionId)}
            onResumeQueue={(sessionId) => void resumeQueue(sessionId)}
          />
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
                onRetryMessage={viewingBotSession ? undefined : onRetryMessage}
                onEditMessage={viewingBotSession ? undefined : onEditMessage}
                onRegenerateMessage={viewingBotSession ? undefined : onRegenerateMessage}
                retryingMessageId={retryingMessageId}
                busy={streaming}
                expectedSelection={
                  sessionModel.mode === "pinned"
                    ? {
                        providerName:
                          modelPickerItems.find(
                            (item) =>
                              item.providerId === sessionModel.providerId &&
                              item.model === sessionModel.model,
                          )?.providerName || undefined,
                        model: sessionModel.model,
                      }
                    : configuredProvider
                      ? {
                          providerName: configuredProvider.name,
                          model: configuredModel || undefined,
                        }
                      : null
                }
                onActionUpdated={() => {
                  void qc.invalidateQueries({ queryKey: ["system-agent", "actions", activeId] });
                }}
              />
              {streamNotice ? (
                <div role="status" aria-live="polite" className="mx-auto w-full max-w-3xl px-4 pb-2 text-xs text-muted-foreground xl:max-w-5xl 2xl:max-w-6xl">
                  {streamNotice}
                </div>
              ) : null}
              {currentRun?.status === "waiting_input" ? (
                <div className="shrink-0 border-t border-amber-500/20 bg-amber-500/5 px-2 py-2.5 sm:px-3">
                  <div className="mx-auto flex max-w-3xl flex-col gap-2 rounded-lg border border-amber-500/25 bg-background/85 p-3 pr-[4.75rem] sm:flex-row sm:items-end sm:pr-3 xl:max-w-5xl 2xl:max-w-6xl">
                    <label className="min-w-0 flex-1">
                      <span className="mb-1 block text-xs font-medium text-amber-800 dark:text-amber-200">
                        当前任务需要补充信息
                      </span>
                      <Input
                        value={waitingInput}
                        onChange={(event) => setWaitingInput(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                            event.preventDefault();
                            void submitWaitingInput();
                          }
                        }}
                        placeholder="输入补充信息后继续执行"
                        maxLength={10_000}
                      />
                    </label>
                    <Button
                      type="button"
                      className="min-h-9 shrink-0"
                      disabled={!waitingInput.trim()}
                      onClick={() => void submitWaitingInput()}
                    >
                      提交并继续
                    </Button>
                  </div>
                </div>
              ) : null}
              {currentRun?.status === "waiting_approval" ? (
                <div className="shrink-0 border-t border-amber-500/20 bg-amber-500/5 px-2 py-2.5 sm:px-3">
                  <div className="mx-auto max-w-3xl rounded-lg border border-amber-500/25 bg-background/85 p-3 pr-[4.75rem] sm:pr-3 xl:max-w-5xl 2xl:max-w-6xl">
                    <div className="text-xs font-medium text-amber-800 dark:text-amber-200">
                      当前任务等待工具调用审批
                    </div>
                    <div className="mt-2 space-y-1.5">
                      {(waitingApproval?.tools || []).map((tool) => (
                        <div
                          key={`${tool.call_id || ""}-${tool.name}`}
                          className="flex min-w-0 items-start justify-between gap-3 rounded-md border border-border/70 bg-muted/20 px-2.5 py-2"
                        >
                          <span className="min-w-0">
                            <span className="block break-all text-xs font-medium">
                              {systemAgentToolLabel(tool.name)}
                            </span>
                            <span className="mt-0.5 block text-[10px] leading-4 text-muted-foreground">
                              {tool.description || tool.name}
                            </span>
                          </span>
                          <span className="shrink-0 rounded border border-amber-500/25 bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
                            {tool.risk || "需确认"}
                          </span>
                        </div>
                      ))}
                      {!waitingApproval?.tools?.length ? (
                        <p className="text-xs text-muted-foreground">
                          审批详情正在同步；你仍可拒绝并结束本任务。
                        </p>
                      ) : null}
                    </div>
                    <div className="mt-3 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                      <Button
                        type="button"
                        variant="outline"
                        className="min-h-9"
                        onClick={() => void decideApproval(false)}
                      >
                        拒绝并结束
                      </Button>
                      <Button
                        type="button"
                        className="min-h-9"
                        disabled={!waitingApproval?.tools?.length}
                        onClick={() => void decideApproval(true)}
                      >
                        批准并继续
                      </Button>
                    </div>
                  </div>
                </div>
              ) : null}
              <Composer
                disabled={configQ.isLoading || viewingBotSession}
                streaming={hasOpenRun}
                onSend={onSend}
                value={composerValue}
                onValueChange={setComposerValue}
                onStop={onStop}
                actionMode={composerAction}
                onActionModeChange={setComposerAction}
                queueCount={currentQueue.length}
                runStatus={currentRun?.status}
                placeholder={viewingBotSession ? "Telegram 会话仅供查看" : undefined}
                modelItems={visibleModelPickerItems}
                modelSelection={pickerValue}
                onModelSelectionChange={onSessionModelChange}
                clientSelection={sessionModel}
                onClientSelectionChange={onSessionClientChange}
                clientDisabled={selectorDisabled}
                gatewayAvailable={gatewayModelPickerItems.length > 0}
                onSetDefaultModel={onSetDefaultModel}
                modelDisabled={selectorDisabled || visibleModelPickerItems.length === 0}
                expectedLabel={expectedSelectionLabel}
                onOpenSessions={() => {
                  if (window.matchMedia("(min-width: 768px)").matches) {
                    setSessionSidebarCollapsed(false);
                    return;
                  }
                  setDrawerOpen(true);
                }}
                showSessionButtonOnDesktop={sessionSidebarCollapsed}
              />
            </>
          )}
        </div>
      </div>
    </PageShell>
  );
}

export default AssistantIndex;

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Filter,
  RotateCcw,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { cancelFullLiveness, fullLivenessPreview, fullLivenessRun, fullLivenessStatus } from "@/api/commands";
import type {
  FullLivenessPreviewResponse,
  FullLivenessRunResponse,
  LLMProviderOut,
} from "@/api/types";
import { ModelRunMeta } from "@/components/ai/ModelRunMeta";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { getErrMsg } from "@/lib/api";
import {
  classifyFullLivenessStatus,
  extractHttpStatusCode,
  livenessResultToUsage,
  livenessStatusLabel as sharedStatusLabel,
  livenessStatusTone as sharedStatusTone,
} from "@/lib/livenessStatus";
import { cn } from "@/lib/utils";

const DEFAULT_CHAT_TEST_SYSTEM_PROMPT =
  "你是一个自然、简洁的中文聊天助手。请像真实聊天一样直接回复用户，不要只返回 ping/pong。";

const CLIENT_IDENTITY_LABELS: Record<string, string> = {
  auto: "自动选择",
  minimal: "最小身份",
  openai_sdk: "OpenAI SDK（标准 API）",
  codex_cli: "Codex CLI",
  codex_desktop: "Codex CLI（旧 Desktop 配置）",
  claude_code: "Claude Code CLI",
  claude_desktop: "Claude Code CLI（旧 Desktop 配置）",
  grok_cli: "Grok CLI",
};

const FULL_LIVENESS_STORAGE_KEY = "telepilot:llm-full-liveness-result";

type FullLivenessPersistedState = {
  preview: FullLivenessPreviewResponse | null;
  result: FullLivenessRunResponse | null;
  selectedProviderIds?: number[];
  selectedModelsByProvider?: Record<number, string[]>;
  activeRunId?: string | null;
  previewInputKey?: string | null;
};

function readFullLivenessState(): FullLivenessPersistedState {
  try {
    const raw = window.sessionStorage.getItem(FULL_LIVENESS_STORAGE_KEY);
    if (!raw) return { preview: null, result: null };
    const parsed = JSON.parse(raw) as Partial<FullLivenessPersistedState>;
    const result = parsed.result ?? null;
    const activeRunId = typeof parsed.activeRunId === "string"
      ? parsed.activeRunId
      : result && (result.status === "queued" || result.status === "running")
        ? result.run_id
        : null;
    return {
      preview: parsed.preview ?? null,
      result,
      selectedProviderIds: Array.isArray(parsed.selectedProviderIds)
        ? parsed.selectedProviderIds.filter((value): value is number => typeof value === "number")
        : undefined,
      selectedModelsByProvider:
        parsed.selectedModelsByProvider && typeof parsed.selectedModelsByProvider === "object"
          ? Object.fromEntries(
              Object.entries(parsed.selectedModelsByProvider).map(([providerId, models]) => [
                Number(providerId),
                Array.isArray(models) ? models.filter((model): model is string => typeof model === "string") : [],
              ]),
            )
          : undefined,
      activeRunId: activeRunId || null,
      previewInputKey: typeof parsed.previewInputKey === "string" ? parsed.previewInputKey : null,
    };
  } catch {
    return { preview: null, result: null };
  }
}

function writeFullLivenessState(state: FullLivenessPersistedState): void {
  try {
    window.sessionStorage.setItem(FULL_LIVENESS_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // sessionStorage 不可用时仍允许测活，只是不在当前标签页保留结果。
  }
}

function isRunActive(result: FullLivenessRunResponse | null | undefined): boolean {
  return result?.status === "queued" || result?.status === "running";
}

function startedRunResult(
  runId: string,
  status: "queued" | "running",
  taskTotal: number,
): FullLivenessRunResponse {
  return {
    run_id: runId,
    status,
    task_total: taskTotal,
    completed: 0,
    healthy: 0,
    failed: 0,
    skipped: 0,
    cancelled: 0,
    results: [],
  };
}

function responseStatus(error: unknown): number | null {
  if (!error || typeof error !== "object") return null;
  const response = (error as { response?: { status?: unknown } }).response;
  return typeof response?.status === "number" ? response.status : null;
}

function livenessStatusTone(status: string): "success" | "warn" | "danger" | "neutral" | "info" | undefined {
  return sharedStatusTone(classifyFullLivenessStatus(status));
}

function livenessStatusLabel(status: string): string {
  return sharedStatusLabel(classifyFullLivenessStatus(status));
}

type LivenessResultFilter = "all" | "healthy" | "failed" | "skipped" | "cancelled";

function livenessResultCategory(result: FullLivenessRunResponse["results"][number]): LivenessResultFilter {
  if (result.status === "healthy") return "healthy";
  if (result.status === "cancelled") return "cancelled";
  if (result.skipped) return "skipped";
  return "failed";
}

function livenessProtocolLabel(value?: string | null): string {
  if (value === "chat_completions") return "Chat Completions";
  if (value === "responses") return "Responses";
  if (value === "anthropic_messages") return "Anthropic Messages";
  return value || "未知协议";
}

function livenessIdentityLabel(value?: string | null): string {
  return CLIENT_IDENTITY_LABELS[value || ""] || value || "未知客户端";
}

function enabledModelIds(provider: LLMProviderOut): string[] {
  return (provider.models || []).filter((model) => model.enabled).map((model) => model.id);
}

function resultStatusText(item: FullLivenessRunResponse["results"][number]): string {
  const statusCode = extractHttpStatusCode(item.status_code, item.error);
  return [
    livenessStatusLabel(item.status),
    statusCode,
    item.latency_ms ? `${item.latency_ms}ms` : null,
  ].filter(Boolean).join(" · ");
}

interface FullLivenessPanelProps {
  providers: LLMProviderOut[];
  systemPrompt: string;
  onSystemPromptChange: (value: string) => void;
  message: string;
  onMessageChange: (value: string) => void;
  onBusyChange: (busy: boolean) => void;
}

export function FullLivenessPanel({
  providers,
  systemPrompt,
  onSystemPromptChange,
  message,
  onMessageChange,
  onBusyChange,
}: FullLivenessPanelProps) {
  const retainedState = useRef(readFullLivenessState()).current;
  const [preview, setPreview] = useState<FullLivenessPreviewResponse | null>(retainedState.preview);
  const [result, setResult] = useState<FullLivenessRunResponse | null>(retainedState.result);
  const [maxTokens, setMaxTokens] = useState(256);
  const [timeoutSeconds, setTimeoutSeconds] = useState(90);
  const [globalConcurrency, setGlobalConcurrency] = useState(8);
  const [selectedProviderIds, setSelectedProviderIds] = useState<number[]>(
    retainedState.selectedProviderIds ?? providers.map((provider) => provider.id),
  );
  const [selectedModelsByProvider, setSelectedModelsByProvider] = useState<Record<number, string[]>>(
    retainedState.selectedModelsByProvider
      ?? Object.fromEntries(providers.map((provider) => [provider.id, enabledModelIds(provider)])),
  );
  const [providerQuery, setProviderQuery] = useState("");
  const [scopeOpen, setScopeOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [resultExpanded, setResultExpanded] = useState(true);
  const [resultFilter, setResultFilter] = useState<LivenessResultFilter>("all");
  const [expandedScopeProviders, setExpandedScopeProviders] = useState<Record<number, boolean>>({});
  const [collapsedResultProviders, setCollapsedResultProviders] = useState<Record<number, boolean>>({});
  const [activeRunId, setActiveRunId] = useState<string | null>(retainedState.activeRunId || null);
  const [previewInputKey, setPreviewInputKey] = useState<string | null>(retainedState.previewInputKey || null);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [pollingError, setPollingError] = useState<string | null>(null);
  const [pollRetryDelayMs, setPollRetryDelayMs] = useState<number | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelPendingStart, setCancelPendingStart] = useState(false);
  const activeRunIdRef = useRef<string | null>(retainedState.activeRunId || null);
  const resultRef = useRef<FullLivenessRunResponse | null>(retainedState.result);
  const pollRef = useRef<number | null>(null);
  const pollRunRef = useRef<(runId: string) => Promise<void>>(async () => undefined);
  const pollFailureCountRef = useRef(0);
  const mountedRef = useRef(false);
  const cancelRequestedRef = useRef(false);
  const deferredUnmountCancelRef = useRef<number | null>(null);

  resultRef.current = result;

  const currentPreviewInputKey = useMemo(() => {
    const selectedIds = [...selectedProviderIds].sort((a, b) => a - b);
    const modelScope = providers
      .filter((provider) => selectedIds.includes(provider.id))
      .map((provider) => ({
        providerId: provider.id,
        models: [...(selectedModelsByProvider[provider.id] || [])].sort(),
      }))
      .sort((a, b) => a.providerId - b.providerId);
    return JSON.stringify({
      selectedProviderIds: selectedIds,
      modelScope,
      maxTokens,
      globalConcurrency,
    });
  }, [globalConcurrency, maxTokens, providers, selectedModelsByProvider, selectedProviderIds]);
  const currentPreviewInputKeyRef = useRef(currentPreviewInputKey);
  currentPreviewInputKeyRef.current = currentPreviewInputKey;

  const clearPollTimer = useCallback(() => {
    if (pollRef.current != null) window.clearTimeout(pollRef.current);
    pollRef.current = null;
  }, []);

  const trackActiveRun = useCallback((runId: string | null) => {
    activeRunIdRef.current = runId;
    setActiveRunId(runId);
  }, []);

  const pollRun = useCallback(async (runId: string) => {
    if (!mountedRef.current || activeRunIdRef.current !== runId) return;
    try {
      const next = await fullLivenessStatus(runId);
      if (!mountedRef.current || activeRunIdRef.current !== runId) return;
      setResult(next);
      setPollingError(null);
      setPollRetryDelayMs(null);
      pollFailureCountRef.current = 0;
      if (isRunActive(next)) {
        clearPollTimer();
        pollRef.current = window.setTimeout(() => {
          void pollRunRef.current(runId);
        }, 500);
      } else {
        clearPollTimer();
        trackActiveRun(null);
      }
    } catch (error) {
      if (!mountedRef.current || activeRunIdRef.current !== runId) return;
      if (responseStatus(error) === 404) {
        const previous = resultRef.current;
        setResult({
          ...(previous || startedRunResult(runId, "running", 0)),
          status: "cancelled",
          error: "测活运行不存在或已过期，已停止恢复轮询。",
        });
        clearPollTimer();
        trackActiveRun(null);
        return;
      }
      pollFailureCountRef.current += 1;
      const retryDelay = Math.min(1000 * (2 ** (pollFailureCountRef.current - 1)), 15_000);
      const messageText = getErrMsg(error);
      setPollingError(messageText);
      setPollRetryDelayMs(retryDelay);
      if (pollFailureCountRef.current === 1) {
        toast.error(`巡检状态暂时读取失败，将自动重试：${messageText}`);
      }
      clearPollTimer();
      pollRef.current = window.setTimeout(() => {
        void pollRunRef.current(runId);
      }, retryDelay);
    }
  }, [clearPollTimer, trackActiveRun]);
  pollRunRef.current = pollRun;

  const startPolling = useCallback((runId: string, immediate = true) => {
    clearPollTimer();
    pollFailureCountRef.current = 0;
    setPollingError(null);
    setPollRetryDelayMs(null);
    trackActiveRun(runId);
    if (immediate) {
      void pollRunRef.current(runId);
    } else {
      pollRef.current = window.setTimeout(() => {
        void pollRunRef.current(runId);
      }, 500);
    }
  }, [clearPollTimer, trackActiveRun]);

  useEffect(() => {
    writeFullLivenessState({
      preview,
      result,
      selectedProviderIds,
      selectedModelsByProvider,
      activeRunId,
      previewInputKey,
    });
  }, [activeRunId, preview, previewInputKey, result, selectedModelsByProvider, selectedProviderIds]);

  useEffect(() => {
    const validIds = new Set(providers.map((provider) => provider.id));
    setSelectedProviderIds((current) => current.filter((id) => validIds.has(id)));
    setSelectedModelsByProvider((current) => Object.fromEntries(
      providers.map((provider) => {
        const enabled = enabledModelIds(provider);
        const existing = current[provider.id];
        return [
          provider.id,
          existing == null ? enabled : existing.filter((model) => enabled.includes(model)),
        ];
      }),
    ));
  }, [providers]);

  useEffect(() => {
    setSelectedProviderIds((current) => current.filter(
      (providerId) => (selectedModelsByProvider[providerId] || []).length > 0,
    ));
  }, [selectedModelsByProvider]);

  useEffect(() => {
    if (!preview || previewInputKey === currentPreviewInputKey) return;
    setPreview(null);
    setPreviewInputKey(null);
    setConfirmationOpen(false);
  }, [currentPreviewInputKey, preview, previewInputKey]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setScopeOpen(false);
      setSettingsOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const previewMut = useMutation({
    mutationFn: (variables: { inputKey: string; providerIds: number[]; modelsByProvider: Record<number, string[]> }) => fullLivenessPreview({
      max_tokens: maxTokens,
      global_concurrency: globalConcurrency,
      only_provider_ids: variables.providerIds,
      models_by_provider: variables.modelsByProvider,
    }),
    onSuccess: (resp, variables) => {
      if (variables.inputKey !== currentPreviewInputKeyRef.current) return;
      setPreview(resp);
      setPreviewInputKey(variables.inputKey);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const runMut = useMutation({
    mutationFn: async (variables: { confirmLargeRun: boolean }) => {
      const response = await fullLivenessRun({
        system_prompt: systemPrompt.trim() || DEFAULT_CHAT_TEST_SYSTEM_PROMPT,
        message: message.trim(),
        max_tokens: maxTokens,
        timeout_seconds: timeoutSeconds,
        global_concurrency: globalConcurrency,
        confirm_large_run: variables.confirmLargeRun,
        only_provider_ids: selectedProviderIds,
        models_by_provider: selectedModelsByProvider,
      });
      if (mountedRef.current && !cancelRequestedRef.current) return response;

      const started = startedRunResult(response.run_id, response.status, response.task_total);
      writeFullLivenessState({
        ...readFullLivenessState(),
        result: started,
        activeRunId: response.run_id,
      });
      try {
        const cancelled = await cancelFullLiveness(response.run_id);
        writeFullLivenessState({
          ...readFullLivenessState(),
          result: cancelled,
          activeRunId: isRunActive(cancelled) ? response.run_id : null,
        });
      } catch {
        // 保留 run_id；若页面仍在或稍后重新进入，可继续轮询并再次停止。
      }
      return null;
    },
    onSuccess: (resp) => {
      cancelRequestedRef.current = false;
      if (!mountedRef.current) return;
      setCancelPendingStart(false);
      setCancelling(false);
      if (!resp) {
        const retained = readFullLivenessState();
        setResult(retained.result);
        if (retained.activeRunId) startPolling(retained.activeRunId);
        else trackActiveRun(null);
        return;
      }
      const started = startedRunResult(resp.run_id, resp.status, resp.task_total);
      writeFullLivenessState({
        ...readFullLivenessState(),
        result: started,
        activeRunId: resp.run_id,
      });
      setResult(started);
      setResultExpanded(true);
      setResultFilter("all");
      setCollapsedResultProviders({});
      startPolling(resp.run_id);
    },
    onError: (err) => {
      cancelRequestedRef.current = false;
      if (!mountedRef.current) return;
      setCancelPendingStart(false);
      setCancelling(false);
      toast.error(getErrMsg(err));
    },
  });

  const running = runMut.isPending || activeRunId !== null || isRunActive(result);

  useEffect(() => {
    onBusyChange(running);
    return () => onBusyChange(false);
  }, [onBusyChange, running]);

  useEffect(() => {
    mountedRef.current = true;
    if (deferredUnmountCancelRef.current != null) {
      window.clearTimeout(deferredUnmountCancelRef.current);
      deferredUnmountCancelRef.current = null;
    }
    const retainedRunId = activeRunIdRef.current;
    if (retainedRunId) startPolling(retainedRunId);

    return () => {
      mountedRef.current = false;
      clearPollTimer();
      const runId = activeRunIdRef.current;
      if (!runId) return;
      // StrictMode 会立刻重新挂载 effect；延后一拍可避免开发模式误取消真实任务。
      deferredUnmountCancelRef.current = window.setTimeout(() => {
        if (mountedRef.current) return;
        void cancelFullLiveness(runId)
          .then((next) => {
            writeFullLivenessState({
              ...readFullLivenessState(),
              result: next,
              activeRunId: isRunActive(next) ? runId : null,
            });
          })
          .catch(() => undefined);
      }, 0);
    };
  }, [clearPollTimer, startPolling]);
  const visibleProviders = useMemo(() => {
    const query = providerQuery.trim().toLowerCase();
    if (!query) return providers;
    return providers.filter((provider) => (
      provider.name.toLowerCase().includes(query)
      || provider.provider.toLowerCase().includes(query)
    ));
  }, [providerQuery, providers]);
  const selectedModelCount = providers
    .filter((provider) => selectedProviderIds.includes(provider.id))
    .reduce((count, provider) => count + (selectedModelsByProvider[provider.id]?.length || 0), 0);

  const setProviderSelection = (next: number[]) => {
    if (running) return;
    const selected = [...new Set(next)];
    setSelectedProviderIds(selected);
    setSelectedModelsByProvider((current) => Object.fromEntries(
      providers.map((provider) => [
        provider.id,
        selected.includes(provider.id)
          ? current[provider.id]?.length
            ? current[provider.id]
            : enabledModelIds(provider)
          : [],
      ]),
    ));
    setPreview(null);
    setPreviewInputKey(null);
    setConfirmationOpen(false);
  };

  const selectAllProviders = () => {
    if (running) return;
    setSelectedProviderIds(providers.filter((provider) => enabledModelIds(provider).length > 0).map((provider) => provider.id));
    setSelectedModelsByProvider(Object.fromEntries(
      providers.map((provider) => [provider.id, enabledModelIds(provider)]),
    ));
    setPreview(null);
    setPreviewInputKey(null);
    setConfirmationOpen(false);
  };

  const toggleProvider = (providerId: number) => {
    setProviderSelection(
      selectedProviderIds.includes(providerId)
        ? selectedProviderIds.filter((id) => id !== providerId)
        : [...selectedProviderIds, providerId],
    );
  };

  const toggleModel = (providerId: number, modelId: string) => {
    if (running) return;
    const current = selectedModelsByProvider[providerId] || [];
    const next = current.includes(modelId)
      ? current.filter((model) => model !== modelId)
      : [...current, modelId];
    setSelectedModelsByProvider((value) => ({ ...value, [providerId]: next }));
    setSelectedProviderIds((value) => (
      next.length > 0
        ? [...new Set([...value, providerId])]
        : value.filter((id) => id !== providerId)
    ));
    setPreview(null);
    setPreviewInputKey(null);
    setConfirmationOpen(false);
  };

  const beginRun = (targetPreview = preview, targetInputKey = previewInputKey) => {
    if (!targetPreview || targetInputKey !== currentPreviewInputKey) {
      setConfirmationOpen(false);
      setPreview(null);
      setPreviewInputKey(null);
      toast.error("巡检范围或请求设置已经变化，请重新开始测活。");
      return;
    }
    cancelRequestedRef.current = false;
    setCancelPendingStart(false);
    setConfirmationOpen(false);
    onBusyChange(true);
    runMut.mutate({ confirmLargeRun: targetPreview.needs_confirmation });
  };

  const requestRun = async () => {
    const inputKey = currentPreviewInputKey;
    const providerIds = [...selectedProviderIds];
    const modelsByProvider = Object.fromEntries(
      providerIds.map((providerId) => [providerId, selectedModelsByProvider[providerId] || []]),
    );
    let refreshed: FullLivenessPreviewResponse;
    try {
      refreshed = await previewMut.mutateAsync({ inputKey, providerIds, modelsByProvider });
    } catch {
      return;
    }
    if (inputKey !== currentPreviewInputKeyRef.current) {
      toast.error("刷新期间巡检范围发生变化，请重新开始。");
      return;
    }
    setPreview(refreshed);
    setPreviewInputKey(inputKey);
    if (refreshed.task_total === 0) {
      toast.error("当前所选范围没有可测活模型。");
      return;
    }
    if (refreshed.needs_confirmation) {
      setConfirmationOpen(true);
      return;
    }
    beginRun(refreshed, inputKey);
  };

  const stopRun = async () => {
    cancelRequestedRef.current = true;
    const runId = activeRunIdRef.current;
    if (!runId) {
      if (runMut.isPending) {
        setCancelPendingStart(true);
        setCancelling(true);
      }
      return;
    }
    setCancelling(true);
    try {
      const next = await cancelFullLiveness(runId);
      if (!mountedRef.current || activeRunIdRef.current !== runId) return;
      setResult(next);
      if (isRunActive(next)) {
        startPolling(runId, false);
      } else {
        clearPollTimer();
        trackActiveRun(null);
      }
    } catch (error) {
      if (mountedRef.current) toast.error(getErrMsg(error));
    } finally {
      if (mountedRef.current) {
        setCancelling(false);
        setCancelPendingStart(false);
      }
    }
  };

  const clearResults = () => {
    if (running) return;
    setResult(null);
    setResultExpanded(true);
    setResultFilter("all");
    setCollapsedResultProviders({});
    writeFullLivenessState({
      preview,
      result: null,
      selectedProviderIds,
      selectedModelsByProvider,
      activeRunId: null,
      previewInputKey,
    });
  };

  const filteredResults = result?.results.filter((item) => (
    resultFilter === "all" || livenessResultCategory(item) === resultFilter
  )) ?? [];
  const groupedResults = filteredResults.reduce<Array<{
    providerId: number;
    providerName: string;
    items: FullLivenessRunResponse["results"];
  }>>((groups, item) => {
    let group = groups.find((candidate) => candidate.providerId === item.provider_id);
    if (!group) {
      group = { providerId: item.provider_id, providerName: item.provider_name, items: [] };
      groups.push(group);
    }
    group.items.push(item);
    return groups;
  }, []);
  const resultCounts: Record<LivenessResultFilter, number> = {
    all: result?.results.length ?? 0,
    healthy: result?.results.filter((item) => livenessResultCategory(item) === "healthy").length ?? 0,
    failed: result?.results.filter((item) => livenessResultCategory(item) === "failed").length ?? 0,
    skipped: result?.results.filter((item) => livenessResultCategory(item) === "skipped").length ?? 0,
    cancelled: result?.results.filter((item) => livenessResultCategory(item) === "cancelled").length ?? 0,
  };
  const filterOptions: Array<{ value: LivenessResultFilter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "healthy", label: "正常" },
    { value: "failed", label: "异常" },
    { value: "skipped", label: "跳过" },
    { value: "cancelled", label: "取消" },
  ];

  return (
    <div className="relative grid min-h-0 flex-1 gap-4 overflow-hidden xl:grid-cols-[280px_minmax(0,1fr)] 2xl:grid-cols-[280px_minmax(0,1fr)_280px]">
      {scopeOpen ? (
        <button
          type="button"
          className="fixed inset-0 z-[69] bg-black/20 xl:hidden"
          aria-label="关闭 Provider 范围"
          onClick={() => setScopeOpen(false)}
        />
      ) : null}
      <aside
        className={cn(
          "fixed bottom-[calc(4.75rem+env(safe-area-inset-bottom))] left-0 top-[calc(5rem+env(safe-area-inset-top))] z-[70] w-[min(320px,88vw)] overflow-y-auto rounded-r-2xl border-r border-border/70 bg-card p-4 shadow-[0_6px_18px_rgba(15,23,42,0.10)] transition-transform duration-200 sm:bottom-0 xl:static xl:z-auto xl:w-auto xl:translate-x-0 xl:rounded-lg xl:border xl:shadow-sm",
          scopeOpen
            ? "visible translate-x-0"
            : "invisible -translate-x-full xl:visible xl:translate-x-0",
        )}
        aria-label="全局巡检 Provider 范围"
      >
        <div className="flex items-start justify-between gap-3 border-b pb-3">
          <div>
            <div className="text-sm font-semibold">Provider 范围</div>
            <div className="mt-1 text-xs text-muted-foreground">开启多个 LLM Provider 并发测活。</div>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-9 w-9 p-0 xl:hidden"
            aria-label="关闭 Provider 范围"
            onClick={() => setScopeOpen(false)}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="mt-4 flex items-center justify-between gap-2">
          <Label>LLM Provider</Label>
          <MetaBadge tone={selectedProviderIds.length > 0 ? "success" : "warn"}>
            已选 {selectedProviderIds.length}/{providers.length}
          </MetaBadge>
        </div>
        <div className="relative mt-2">
          <Filter className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={providerQuery}
            onChange={(event) => setProviderQuery(event.target.value)}
            placeholder="搜索 Provider"
            className="pl-9"
          />
        </div>
        <div className="mt-2 flex gap-1">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={running}
            onClick={selectAllProviders}
          >
            全选
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={running}
            onClick={() => setProviderSelection([])}
          >
            清空
          </Button>
        </div>
        <div className="mt-2 max-h-[62vh] space-y-1 overflow-y-auto rounded-md border bg-background p-1 xl:max-h-[560px]">
          {visibleProviders.map((provider) => {
            const enabledModels = (provider.models || []).filter((model) => model.enabled);
            const selectedModels = selectedModelsByProvider[provider.id] || [];
            const expanded = expandedScopeProviders[provider.id] === true;
            const selected = selectedProviderIds.includes(provider.id) && selectedModels.length > 0;
            return (
              <div key={provider.id} className="overflow-hidden rounded border bg-card">
                <div className="flex min-h-11 items-center gap-2 px-2 py-1.5 text-xs">
                  <Switch
                    checked={selected}
                    disabled={running || enabledModels.length === 0}
                    aria-label={`${provider.name} 参与巡检`}
                    onCheckedChange={() => toggleProvider(provider.id)}
                  />
                  <button
                    type="button"
                    className="flex min-w-0 flex-1 items-center gap-2 rounded text-left hover:text-foreground disabled:cursor-not-allowed"
                    disabled={enabledModels.length === 0}
                    onClick={() => setExpandedScopeProviders((current) => ({ ...current, [provider.id]: !expanded }))}
                    aria-expanded={expanded}
                  >
                    {expanded ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium">{provider.name}</span>
                      <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground">{provider.provider}</span>
                    </span>
                    <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                      {selectedModels.length}/{enabledModels.length}
                    </span>
                  </button>
                </div>
                {expanded ? (
                  <div className="space-y-1 border-t bg-muted/20 p-1.5">
                    {enabledModels.map((model) => (
                      <div key={model.id} className="flex min-h-9 items-center gap-2 rounded px-2 py-1 hover:bg-background">
                        <Switch
                          checked={selectedModels.includes(model.id)}
                          disabled={running}
                          aria-label={`${provider.name} 的 ${model.id} 参与巡检`}
                          onCheckedChange={() => toggleModel(provider.id, model.id)}
                          className="scale-90"
                        />
                        <span className="min-w-0 flex-1 break-all font-mono text-[10px]">{model.id}</span>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </aside>

      <div className="flex h-[calc(100dvh-12rem)] min-h-[420px] min-w-0 flex-col overflow-hidden rounded-lg border bg-card shadow-sm xl:min-h-[650px]">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b px-3 py-2">
          <div className="min-w-0">
            <div className="text-sm font-medium">多 Provider 并发巡检</div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              已选择 {selectedProviderIds.length} 个 Provider、{selectedModelCount} 个模型。
            </div>
            <div className="mt-1 truncate text-xs text-muted-foreground" title={message}>
              测活词：{message || "未填写"}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 px-2 text-xs xl:hidden"
              onClick={() => { setSettingsOpen(false); setScopeOpen(true); }}
            >
              <Filter className="h-4 w-4" />范围
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 px-2 text-xs 2xl:hidden"
              onClick={() => { setScopeOpen(false); setSettingsOpen(true); }}
            >
              <SlidersHorizontal className="h-4 w-4" />设置
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 px-2 text-xs"
              disabled={running || !result}
              title="清空本次巡检结果"
              onClick={clearResults}
            >
              <RotateCcw className="mr-1 h-3.5 w-3.5" />清空对话
            </Button>
          </div>
          {pollingError && activeRunId ? (
            <div className="w-full flex flex-wrap items-center gap-1.5 text-[11px] text-amber-600 dark:text-amber-400">
              <span>状态读取失败，后台任务仍受控。</span>
              {pollRetryDelayMs ? <span>将在 {Math.ceil(pollRetryDelayMs / 1000)} 秒内重试。</span> : null}
            </div>
          ) : null}
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-muted/20 p-3 text-xs">
          {result ? (
            <section className="overflow-hidden rounded-md border bg-background">
              <button
                type="button"
                className="flex min-h-10 w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/35"
                onClick={() => setResultExpanded((value) => !value)}
                aria-expanded={resultExpanded}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {resultExpanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
                  <span className="font-medium">测活结果</span>
                </span>
                <span className="flex flex-wrap justify-end gap-1.5">
                  <MetaBadge>{result.completed}/{result.task_total}</MetaBadge>
                  <MetaBadge tone="success">正常 {result.healthy}</MetaBadge>
                  <MetaBadge tone="warn">异常 {result.failed}</MetaBadge>
                  {result.skipped ? <MetaBadge>跳过 {result.skipped}</MetaBadge> : null}
                  {result.cancelled ? <MetaBadge>取消 {result.cancelled}</MetaBadge> : null}
                </span>
              </button>
              {resultExpanded ? (
                <div className="border-t">
                  <div className="flex flex-col gap-2 border-b bg-muted/15 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex items-center gap-1.5 text-muted-foreground">
                      <Filter className="h-3.5 w-3.5" />
                      <span>按结果筛选</span>
                    </div>
                    <div className="flex max-w-full gap-1 overflow-x-auto rounded-md bg-muted/60 p-1">
                      {filterOptions.map((option) => (
                        <Button
                          key={option.value}
                          type="button"
                          size="sm"
                          variant={resultFilter === option.value ? "secondary" : "ghost"}
                          className="h-7 shrink-0 px-2.5"
                          onClick={() => setResultFilter(option.value)}
                        >
                          {option.label} {resultCounts[option.value]}
                        </Button>
                      ))}
                    </div>
                  </div>
                  {result.error ? <div className="border-b px-3 py-2 break-words text-amber-600 dark:text-amber-400">{result.error}</div> : null}
                  {groupedResults.length > 0 ? (
                    <div className="space-y-3 bg-muted/20 p-3">
                      {groupedResults.map((group) => {
                        const collapsed = collapsedResultProviders[group.providerId] === true;
                        const allProviderResults = result.results.filter((item) => item.provider_id === group.providerId);
                        const healthyCount = allProviderResults.filter((item) => item.status === "healthy").length;
                        const statusCodes = Array.from(new Set(
                          allProviderResults
                            .map((item) => extractHttpStatusCode(item.status_code, item.error))
                            .filter((code): code is number => code !== null),
                        ));
                        return (
                          <div key={group.providerId} className="overflow-hidden rounded-lg border bg-background shadow-sm">
                            <button
                              type="button"
                              className="flex min-h-11 w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/30"
                              onClick={() => setCollapsedResultProviders((current) => ({
                                ...current,
                                [group.providerId]: !collapsed,
                              }))}
                              aria-expanded={!collapsed}
                            >
                              <span className="flex min-w-0 items-center gap-2">
                                {collapsed ? <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
                                <span className="truncate text-sm font-semibold">{group.providerName}</span>
                              </span>
                              <span className="flex items-center gap-1.5">
                                {resultFilter !== "all" ? <MetaBadge>{group.items.length} 条匹配</MetaBadge> : null}
                                {statusCodes.map((code) => (
                                  <MetaBadge
                                    key={code}
                                    mono
                                    tone={code === 429 ? "warn" : "danger"}
                                    title={`HTTP 状态码 ${code}`}
                                  >
                                    {code}
                                  </MetaBadge>
                                ))}
                                <MetaBadge tone={healthyCount === allProviderResults.length ? "success" : "warn"}>
                                  正常 {healthyCount}/{allProviderResults.length}
                                </MetaBadge>
                              </span>
                            </button>
                            {!collapsed ? (
                              <div className="divide-y border-t bg-muted/10">
                                {group.items.map((item, index) => (
                                  <div key={`${item.provider_id}-${item.model_id}-${index}`} className="px-3 py-2.5">
                                    <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
                                      <div className="min-w-0">
                                        <div className="break-all font-mono text-xs font-semibold">{item.model_id}</div>
                                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                                          {item.effective_api_format ? (
                                            <MetaBadge tone="outline">协议 {livenessProtocolLabel(item.effective_api_format)}</MetaBadge>
                                          ) : null}
                                          {item.client_identity_profile ? (
                                            <MetaBadge tone="info">客户端 {livenessIdentityLabel(item.client_identity_profile)}</MetaBadge>
                                          ) : null}
                                          {item.skipped && !item.effective_api_format && !item.client_identity_profile ? (
                                            <MetaBadge>未发起请求</MetaBadge>
                                          ) : null}
                                        </div>
                                        {!item.skipped ? (
                                          <ModelRunMeta
                                            className="mt-1.5"
                                            compact
                                            usage={livenessResultToUsage({
                                              requested_model: item.model_id,
                                              model: item.model_id,
                                              provider_name: item.provider_name,
                                              input_tokens: item.input_tokens,
                                              output_tokens: item.output_tokens,
                                              latency_ms: item.latency_ms,
                                              effective_api_format: item.effective_api_format,
                                            })}
                                          />
                                        ) : null}
                                      </div>
                                      <MetaBadge mono tone={livenessStatusTone(item.status)}>
                                        {resultStatusText(item)}
                                      </MetaBadge>
                                    </div>
                                    {item.preview ? <div className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">{item.preview}</div> : null}
                                    {item.error ? <div className="mt-2 break-words text-amber-600 dark:text-amber-400">{item.error}</div> : null}
                                    {item.suggestion && item.suggestion !== item.error ? (
                                      <div className="mt-1 break-words text-muted-foreground">建议：{item.suggestion}</div>
                                    ) : null}
                                  </div>
                                ))}
                              </div>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="px-4 py-8 text-center text-sm text-muted-foreground">
                      当前筛选条件下没有结果。
                    </div>
                  )}
                </div>
              ) : null}
            </section>
          ) : (
            <div className="flex min-h-48 items-center justify-center rounded-md border border-dashed bg-background px-4 text-center text-sm text-muted-foreground">
              选择参与巡检的 Provider 与模型，然后开始全量测活。
            </div>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t p-3">
          {running ? (
            <Button
              type="button"
              variant="outline"
              loading={cancelling}
              onClick={() => void stopRun()}
            >
              {cancelPendingStart ? "等待任务编号后停止" : cancelling ? "停止中…" : "停止"}
            </Button>
          ) : null}
          <Button
            type="button"
            onClick={() => void requestRun()}
            loading={running || previewMut.isPending}
            disabled={running || previewMut.isPending || selectedProviderIds.length === 0 || !message.trim() || !systemPrompt.trim()}
          >
            {running ? "测活中…" : previewMut.isPending ? "刷新范围…" : "开始全量测活"}
          </Button>
        </div>
      </div>

      {settingsOpen ? (
        <button
          type="button"
          className="fixed inset-0 z-[69] bg-black/20 2xl:hidden"
          aria-label="关闭请求设置"
          onClick={() => setSettingsOpen(false)}
        />
      ) : null}
      <aside
        className={cn(
          "fixed bottom-[calc(4.75rem+env(safe-area-inset-bottom))] right-0 top-[calc(5rem+env(safe-area-inset-top))] z-[70] w-[min(320px,88vw)] overflow-y-auto rounded-l-2xl border-l border-border/70 bg-card p-4 shadow-[0_6px_18px_rgba(15,23,42,0.10)] transition-transform duration-200 sm:bottom-0 2xl:static 2xl:z-auto 2xl:w-auto 2xl:translate-x-0 2xl:rounded-lg 2xl:border 2xl:shadow-sm",
          settingsOpen
            ? "visible translate-x-0"
            : "invisible translate-x-full 2xl:visible 2xl:translate-x-0",
        )}
        aria-label="全局巡检请求设置"
      >
        <div className="flex items-start justify-between gap-3 border-b pb-3">
          <div>
            <div className="text-sm font-semibold">请求设置</div>
            <div className="mt-1 text-xs text-muted-foreground">两种测活模式共用系统提示词与测活词。</div>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-9 w-9 p-0 2xl:hidden"
            aria-label="关闭请求设置"
            onClick={() => setSettingsOpen(false)}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2">
          <div className="space-y-1.5">
            <Label className="text-xs">最大输出 Token</Label>
            <Input
              type="number"
              min={64}
              max={8000}
              value={maxTokens}
              onChange={(event) => setMaxTokens(Math.max(64, Math.min(8000, Number(event.target.value) || 256)))}
              disabled={running || previewMut.isPending}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">超时秒数</Label>
            <Input
              type="number"
              min={10}
              max={600}
              value={timeoutSeconds}
              onChange={(event) => setTimeoutSeconds(Math.max(10, Math.min(600, Number(event.target.value) || 90)))}
              disabled={running}
            />
          </div>
        </div>
        <div className="mt-4 space-y-1.5">
          <Label className="text-xs">全局并发</Label>
          <Select
            value={String(globalConcurrency)}
            onChange={(event) => setGlobalConcurrency(Number(event.target.value))}
            disabled={running || previewMut.isPending}
          >
            {[2, 4, 8, 12].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </Select>
        </div>
        <div className="mt-4 space-y-1.5">
          <Label className="text-xs">系统提示词</Label>
          <Textarea
            value={systemPrompt}
            rows={6}
            maxLength={2000}
            onChange={(event) => onSystemPromptChange(event.target.value)}
            disabled={running}
          />
        </div>
        <div className="mt-4 space-y-1.5">
          <Label className="text-xs">测活词</Label>
          <Textarea
            value={message}
            rows={4}
            maxLength={2000}
            onChange={(event) => onMessageChange(event.target.value)}
            disabled={running}
          />
        </div>
      </aside>

      <Dialog open={confirmationOpen} onOpenChange={setConfirmationOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>确认执行大规模巡检</DialogTitle>
            <DialogDescription>
              本次将并发测试 {preview?.task_total || 0} 个模型，最大可能输出约 {preview?.max_output_tokens || 0} Token，会消耗真实上游额度。
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm leading-6 text-amber-700 dark:text-amber-300">
            请确认 Provider 范围和 Token 上限无误后再继续。
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setConfirmationOpen(false)}>
              取消
            </Button>
            <Button type="button" onClick={() => beginRun()}>
              确认并开始
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

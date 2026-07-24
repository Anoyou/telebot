import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Filter,
  LockKeyhole,
  MessageSquare,
  RotateCcw,
  Send,
  SlidersHorizontal,
  X,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";

import {
  listLLMProviders,
  streamChatTestProviderModels,
} from "@/api/commands";
import type {
  ChatTestModelResult,
  ChatTestTurn,
  LLMApiFormat,
  LLMClientIdentityProfile,
  LLMProviderOut,
  ProviderModel,
} from "@/api/types";
import { ModelRunMeta } from "@/components/ai/ModelRunMeta";
import { FullLivenessPanel } from "@/components/ai/FullLivenessPanel";
import { RuntimeHealthBar } from "@/components/ai/RuntimeHealthBar";
import { StreamingText } from "@/components/ai/StreamingText";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/feedback/EmptyState";
import { ErrorState } from "@/components/feedback/ErrorState";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Skeleton, Spinner } from "@/components/ui/misc";
import { Textarea } from "@/components/ui/textarea";
import { useStreamingText } from "@/hooks/useStreamingText";
import { getErrMsg } from "@/lib/api";
import {
  classifyChatResult,
  extractHttpStatusCode,
  livenessResultToUsage,
  livenessStatusLabel,
  livenessStatusTone,
} from "@/lib/livenessStatus";
import { cn } from "@/lib/utils";

const DEFAULT_MESSAGE = "你怎么又不行啦？";
const DEFAULT_SYSTEM_PROMPT =
  "你是一个自然、简洁的中文聊天助手。请像真实聊天一样直接回复用户，不要只返回 ping/pong。";
const DEFAULT_SELECTED_MODEL_LIMIT = 8;
const MAX_CHAT_MODELS = 20;
const CHAT_STORAGE_KEY = "telepilot:llm-conversation";
const CHAT_PERSIST_DEBOUNCE_MS = 250;

interface ChatDisplayResult extends ChatTestModelResult {
  pending?: boolean;
}

interface ChatRound {
  id: string;
  providerId: number;
  providerName: string;
  message: string;
  createdAt: number;
  results: ChatDisplayResult[];
  histories: Record<string, ChatTestTurn[]>;
  systemPrompt: string;
  maxTokens: number;
  timeoutSeconds: number;
}

interface PersistedConversation {
  providerId: number | null;
  selectedModels: string[];
  message: string;
  systemPrompt: string;
  rounds: ChatRound[];
  histories: Record<string, ChatTestTurn[]>;
}

const EMPTY_CONVERSATION: PersistedConversation = {
  providerId: null,
  selectedModels: [],
  message: DEFAULT_MESSAGE,
  systemPrompt: DEFAULT_SYSTEM_PROMPT,
  rounds: [],
  histories: {},
};

function readConversation(): PersistedConversation {
  try {
    const raw = window.localStorage.getItem(CHAT_STORAGE_KEY);
    if (!raw) return EMPTY_CONVERSATION;
    const parsed = JSON.parse(raw) as Partial<PersistedConversation>;
    const restoredSystemPrompt = typeof parsed.systemPrompt === "string" && parsed.systemPrompt.trim()
      ? parsed.systemPrompt
      : DEFAULT_SYSTEM_PROMPT;
    return {
      providerId: typeof parsed.providerId === "number" ? parsed.providerId : null,
      selectedModels: Array.isArray(parsed.selectedModels) ? parsed.selectedModels : [],
      message: typeof parsed.message === "string" ? parsed.message : DEFAULT_MESSAGE,
      systemPrompt: restoredSystemPrompt,
      rounds: Array.isArray(parsed.rounds)
        ? parsed.rounds.map((round) => ({
            ...round,
            histories: round.histories || {},
            systemPrompt: typeof round.systemPrompt === "string" && round.systemPrompt.trim()
              ? round.systemPrompt
              : restoredSystemPrompt,
            maxTokens: Number.isFinite(round.maxTokens)
              && round.maxTokens >= 64
              && round.maxTokens <= 8000
              ? round.maxTokens
              : 1200,
            timeoutSeconds: Number.isFinite(round.timeoutSeconds)
              && round.timeoutSeconds >= 10
              && round.timeoutSeconds <= 600
              ? round.timeoutSeconds
              : 90,
            results: (round.results || []).map((result) => (
              result.pending
                ? { ...result, pending: false, ok: false, error: "页面刷新，请求已取消" }
                : result
            )),
          }))
        : [],
      histories: parsed.histories && typeof parsed.histories === "object" ? parsed.histories : {},
    };
  } catch {
    return EMPTY_CONVERSATION;
  }
}

function writeConversation(state: PersistedConversation): boolean {
  try {
    window.localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(state));
    return true;
  } catch {
    return false;
  }
}

const IDENTITY_LABELS: Record<string, string> = {
  auto: "自动选择",
  minimal: "最小身份",
  openai_sdk: "OpenAI SDK",
  codex_cli: "Codex CLI",
  codex_desktop: "Codex Desktop",
  claude_code: "Claude Code",
  claude_desktop: "Claude Desktop",
  grok_cli: "Grok CLI",
};

function identityLabel(value?: string | null): string {
  return IDENTITY_LABELS[value || ""] || value || "未知客户端";
}

function protocolLabel(value?: string | null): string {
  if (value === "chat_completions") return "Chat Completions";
  if (value === "responses") return "Responses";
  if (value === "anthropic_messages") return "Anthropic Messages";
  return value || "未知协议";
}

function providerModels(provider: LLMProviderOut | null): ProviderModel[] {
  if (!provider) return [];
  const seen = new Set<string>();
  const items: ProviderModel[] = [];
  const add = (
    id: string,
    enabled = true,
    custom = false,
    label: string | null = null,
    extra?: Partial<ProviderModel>,
  ) => {
    const modelId = String(id || "").trim();
    if (!modelId || seen.has(modelId)) return;
    seen.add(modelId);
    items.push({ id: modelId, enabled, custom, label, ...extra });
  };
  for (const item of provider.models || []) {
    add(item.id, !!item.enabled, !!item.custom, item.label ?? null, {
      supports_tools: item.supports_tools,
      supports_images: item.supports_images,
    });
  }
  add(provider.default_model, true, false, "默认模型");
  return items.sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.id.localeCompare(b.id));
}

function selectedDefaults(models: ProviderModel[]): string[] {
  const enabled = models.filter((item) => item.enabled).map((item) => item.id);
  const source = enabled.length > 0 ? enabled : models.map((item) => item.id);
  return source.slice(0, DEFAULT_SELECTED_MODEL_LIMIT);
}

function formatTime(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

const API_FORMAT_OPTIONS: Array<{ value: LLMApiFormat; label: string }> = [
  { value: "chat_completions", label: "Chat Completions" },
  { value: "responses", label: "Responses" },
  { value: "anthropic_messages", label: "Anthropic Messages" },
];

const IDENTITY_OPTIONS: Array<{ value: LLMClientIdentityProfile; label: string }> = [
  { value: "auto", label: "自动选择" },
  { value: "minimal", label: "最小身份" },
  { value: "openai_sdk", label: "OpenAI SDK" },
  { value: "codex_cli", label: "Codex CLI" },
  { value: "codex_desktop", label: "Codex Desktop" },
  { value: "claude_code", label: "Claude Code" },
  { value: "claude_desktop", label: "Claude Desktop" },
  { value: "grok_cli", label: "Grok CLI" },
];

function ChatResponseBranch({
  result,
  onRetry,
}: {
  result: ChatDisplayResult;
  onRetry: (
    apiFormat: LLMApiFormat,
    identity: LLMClientIdentityProfile,
  ) => Promise<void>;
}) {
  const streamed = useStreamingText(String(result.response || ""));
  useEffect(() => {
    streamed.syncSnapshot(String(result.response || ""));
  }, [result.response]);
  const [expanded, setExpanded] = useState(result.pending || result.ok);
  const [showOverrides, setShowOverrides] = useState(false);
  const [apiFormat, setApiFormat] = useState<LLMApiFormat>(
    (result.effective_api_format as LLMApiFormat) || "chat_completions",
  );
  const [identity, setIdentity] = useState<LLMClientIdentityProfile>(
    (result.client_identity_profile as LLMClientIdentityProfile) || "auto",
  );

  useEffect(() => {
    if (result.pending || result.ok) setExpanded(true);
    else setExpanded(false);
  }, [result.pending, result.ok]);

  useEffect(() => {
    if (result.pending) return;
    if (result.effective_api_format) {
      setApiFormat(result.effective_api_format as LLMApiFormat);
    }
    if (result.client_identity_profile) {
      setIdentity(result.client_identity_profile as LLMClientIdentityProfile);
    }
  }, [result.pending, result.effective_api_format, result.client_identity_profile]);

  const statusKey = classifyChatResult(result);
  const statusTone = livenessStatusTone(statusKey);
  const httpStatus = extractHttpStatusCode(result.status_code, result.error);
  const statusLabel =
    statusKey === "pending"
      ? result.streaming
        ? "流式回复中"
        : livenessStatusLabel("pending")
      : livenessStatusLabel(statusKey);
  const usage = !result.pending
    ? livenessResultToUsage({
        requested_model: result.requested_model,
        model: result.model,
        input_tokens: result.input_tokens,
        output_tokens: result.output_tokens,
        latency_ms: result.latency_ms,
        effective_api_format: result.effective_api_format,
        stream_fallback: result.stream_fallback,
      })
    : null;

  return (
    <article className="py-4 first:pt-2 [&+article]:border-t">
      <button
        type="button"
        className="flex min-h-10 w-full min-w-0 items-start justify-between gap-3 rounded-md px-2 py-1.5 text-left focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/30 enabled:hover:bg-muted/40"
        aria-expanded={expanded}
        disabled={result.pending}
        onClick={() => setExpanded((value) => !value)}
      >
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-all font-mono text-xs font-semibold">
              {result.requested_model}
            </span>
            <MetaBadge tone={statusTone}>{statusLabel}</MetaBadge>
            {httpStatus ? (
              <MetaBadge
                mono
                tone={httpStatus === 429 ? "warn" : "danger"}
                title={`HTTP 状态码 ${httpStatus}`}
              >
                {httpStatus}
              </MetaBadge>
            ) : null}
          </div>
          {usage ? (
            <ModelRunMeta
              className="mt-1"
              compact
              usage={usage}
              expected={
                result.requested_model
                  ? { model: result.requested_model }
                  : null
              }
            />
          ) : null}
        </div>
        {!result.pending ? (
          expanded
            ? <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            : <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        ) : null}
      </button>

      <div className="mt-2 flex flex-wrap gap-1.5">
        {result.pending && result.streaming ? (
          <MetaBadge tone="info">流式</MetaBadge>
        ) : result.ok && result.streaming ? (
          <MetaBadge tone="success">流式完成</MetaBadge>
        ) : result.stream_fallback ? (
          <MetaBadge tone="outline">已回退完整响应</MetaBadge>
        ) : null}
        {result.effective_api_format ? (
          result.ok || result.pending ? (
            <MetaBadge tone="outline">协议 {protocolLabel(result.effective_api_format)}</MetaBadge>
          ) : (
            <button
              type="button"
              className="min-h-8 rounded-md focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/30"
              title="临时切换协议后重试"
              onClick={() => { setExpanded(true); setShowOverrides(true); }}
            >
              <MetaBadge tone="outline">协议 {protocolLabel(result.effective_api_format)}</MetaBadge>
            </button>
          )
        ) : null}
        {result.client_identity_profile ? (
          result.ok || result.pending ? (
            <MetaBadge tone="info">客户端 {identityLabel(result.client_identity_profile)}</MetaBadge>
          ) : (
            <button
              type="button"
              className="min-h-8 rounded-md focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/30"
              title="临时切换客户端身份后重试"
              onClick={() => { setExpanded(true); setShowOverrides(true); }}
            >
              <MetaBadge tone="info">客户端 {identityLabel(result.client_identity_profile)}</MetaBadge>
            </button>
          )
        ) : null}
      </div>

      {expanded && result.pending ? (
        result.response ? (
          <StreamingText
            text={streamed.text}
            active={Boolean(result.streaming)}
            fallback={Boolean(result.stream_fallback)}
            className="mt-3 text-sm leading-7 text-foreground"
          />
        ) : (
          <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner className="h-4 w-4 animate-spin text-primary" />
            正在等待上游返回首段内容
          </div>
        )
      ) : expanded && result.ok && result.response ? (
        <StreamingText
          text={streamed.text}
          fallback={Boolean(result.stream_fallback)}
          className="mt-3 text-sm leading-7 text-foreground"
        />
      ) : expanded && !result.ok ? (
        <>
          {result.response ? (
            <div className="mt-3 whitespace-pre-wrap break-words border-l-2 border-destructive/40 pl-3 text-sm leading-7 text-muted-foreground">
              {result.response}
            </div>
          ) : null}
          <div className="mt-3 flex items-start gap-2 rounded-md bg-destructive/10 px-3 py-2 text-sm leading-6 text-destructive">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="min-w-0 break-words">{result.error || "没有拿到可展示文本。"}</span>
          </div>
          <div className="mt-3 border-t pt-3">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 px-2 text-xs"
              onClick={() => setShowOverrides((value) => !value)}
            >
              <SlidersHorizontal className="mr-1 h-3.5 w-3.5" />
              {showOverrides ? "收起临时配置" : "换协议或客户端重试"}
            </Button>
            {showOverrides ? (
              <div className="mt-2 grid gap-2 rounded-md bg-muted/35 p-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label className="text-xs">临时协议</Label>
                  <Select value={apiFormat} onChange={(event) => setApiFormat(event.target.value as LLMApiFormat)}>
                    {API_FORMAT_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label className="text-xs">临时客户端</Label>
                  <Select value={identity} onChange={(event) => setIdentity(event.target.value as LLMClientIdentityProfile)}>
                    {IDENTITY_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </Select>
                </div>
                <Button
                  type="button"
                  size="sm"
                  className="sm:col-span-2"
                  onClick={() => void onRetry(apiFormat, identity)}
                >
                  使用临时配置重试此模型
                </Button>
                <p className="text-[11px] leading-5 text-muted-foreground sm:col-span-2">
                  只影响这次重试，不会保存到 Provider 配置。
                </p>
              </div>
            ) : null}
          </div>
        </>
      ) : null}
    </article>
  );
}

export function LLMLivenessPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const retainedConversation = useRef(readConversation()).current;
  const providersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
  });
  const providers = providersQ.data || [];
  const providerParam = searchParams.get("provider");
  const requestedProviderId = providerParam ? Number(providerParam) : Number.NaN;
  const canRestoreConversation = !Number.isFinite(requestedProviderId)
    || requestedProviderId === retainedConversation.providerId;
  const initialProviderId = Number.isFinite(requestedProviderId)
    ? requestedProviderId
    : retainedConversation.providerId;

  const [mode, setMode] = useState<"conversation" | "all">("conversation");
  const [providerId, setProviderId] = useState<number | null>(initialProviderId);
  const selectedProvider = providers.find((item) => item.id === providerId) || providers[0] || null;
  const modelChoices = useMemo(() => providerModels(selectedProvider), [selectedProvider]);
  const [selectedModels, setSelectedModels] = useState<string[]>(
    canRestoreConversation ? retainedConversation.selectedModels : [],
  );
  const [modelQuery, setModelQuery] = useState("");
  /** lite 能力筛选：仅收窄可选模型，不改批量逻辑 */
  const [capFilter, setCapFilter] = useState<"all" | "tools" | "vision">("all");
  const [message, setMessage] = useState(
    canRestoreConversation ? retainedConversation.message : DEFAULT_MESSAGE,
  );
  const [systemPrompt, setSystemPrompt] = useState(
    canRestoreConversation ? retainedConversation.systemPrompt : DEFAULT_SYSTEM_PROMPT,
  );
  const [maxTokens, setMaxTokens] = useState(1200);
  const [timeoutSeconds, setTimeoutSeconds] = useState(90);
  const [rounds, setRounds] = useState<ChatRound[]>(
    canRestoreConversation ? retainedConversation.rounds : [],
  );
  const [histories, setHistories] = useState<Record<string, ChatTestTurn[]>>(
    canRestoreConversation ? retainedConversation.histories : {},
  );
  const [running, setRunning] = useState(false);
  const [retryingModel, setRetryingModel] = useState<string | null>(null);
  const [fullLivenessBusy, setFullLivenessBusy] = useState(false);
  const [scopeOpen, setScopeOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const persistTimerRef = useRef<number | null>(null);
  const persistenceWarningShownRef = useRef(false);
  const forceConversationFlushRef = useRef(false);
  const latestConversationRef = useRef<PersistedConversation>({
    providerId: initialProviderId,
    selectedModels: canRestoreConversation ? retainedConversation.selectedModels : [],
    message: canRestoreConversation ? retainedConversation.message : DEFAULT_MESSAGE,
    systemPrompt: canRestoreConversation ? retainedConversation.systemPrompt : DEFAULT_SYSTEM_PROMPT,
    rounds: canRestoreConversation ? retainedConversation.rounds : [],
    histories: canRestoreConversation ? retainedConversation.histories : {},
  });
  const modelsLocked = rounds.length > 0;
  const busy = running || retryingModel !== null;
  const modeSwitchBusy = busy || fullLivenessBusy;

  const persistConversationNow = useCallback((state: PersistedConversation) => {
    if (writeConversation(state) || persistenceWarningShownRef.current) return;
    persistenceWarningShownRef.current = true;
    toast.warning("对话记录暂时无法写入浏览器存储；当前页面仍可继续测活。", {
      duration: 6000,
    });
  }, []);

  const flushConversation = useCallback(() => {
    if (persistTimerRef.current != null) {
      window.clearTimeout(persistTimerRef.current);
      persistTimerRef.current = null;
    }
    persistConversationNow(latestConversationRef.current);
  }, [persistConversationNow]);

  const visibleModels = modelChoices.filter((model) => {
    if (!model.id.toLowerCase().includes(modelQuery.trim().toLowerCase())) return false;
    if (capFilter === "tools" && model.supports_tools === false) return false;
    if (capFilter === "vision" && model.supports_images !== true) return false;
    return true;
  });

  // 深链：?models=a,b 或 ?model=x 预选模型（快速单测入口）
  useEffect(() => {
    if (modelsLocked || !selectedProvider) return;
    const modelsParam = searchParams.get("models") || searchParams.get("model");
    if (!modelsParam) return;
    const wanted = modelsParam
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    if (!wanted.length) return;
    const available = new Set(modelChoices.map((item) => item.id));
    const next = wanted.filter((id) => available.has(id)).slice(0, MAX_CHAT_MODELS);
    if (next.length) setSelectedModels(next);
  }, [selectedProvider?.id, modelChoices, modelsLocked, searchParams]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      flushConversation();
    };
  }, [flushConversation]);

  useEffect(() => {
    const onPageHide = () => flushConversation();
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, [flushConversation]);

  useEffect(() => {
    if (providers.length === 0) return;
    const requested = providers.find((item) => item.id === requestedProviderId);
    setProviderId((current) => {
      if (requested) return requested.id;
      return providers.some((item) => item.id === current) ? current : providers[0].id;
    });
  }, [providers, requestedProviderId]);

  useEffect(() => {
    latestConversationRef.current = {
      providerId,
      selectedModels,
      message,
      systemPrompt,
      rounds,
      histories,
    };
    if (persistTimerRef.current != null) window.clearTimeout(persistTimerRef.current);
    if (forceConversationFlushRef.current) {
      forceConversationFlushRef.current = false;
      flushConversation();
      return;
    }
    persistTimerRef.current = window.setTimeout(() => {
      persistTimerRef.current = null;
      persistConversationNow(latestConversationRef.current);
    }, CHAT_PERSIST_DEBOUNCE_MS);
  }, [
    flushConversation,
    histories,
    message,
    persistConversationNow,
    providerId,
    retryingModel,
    rounds,
    running,
    selectedModels,
    systemPrompt,
  ]);

  useEffect(() => {
    if (providers.length === 0 || rounds.length === 0) return;
    if (providers.some((provider) => provider.id === rounds[0].providerId)) return;
    setRounds([]);
    setHistories({});
    setSelectedModels([]);
  }, [providers, rounds]);

  useEffect(() => {
    if (!selectedProvider || modelsLocked) return;
    setSelectedModels((current) => {
      const valid = current.filter((id) => modelChoices.some((item) => item.id === id));
      return valid.length > 0 ? valid : selectedDefaults(modelChoices);
    });
  }, [selectedProvider?.id, modelChoices, modelsLocked]);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [rounds, running]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setScopeOpen(false);
      setSettingsOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const abortInFlight = () => {
    const controller = abortRef.current;
    if (!controller) return;
    controller.abort();
    abortRef.current = null;
    if (!mountedRef.current) return;
    forceConversationFlushRef.current = true;
    setRunning(false);
    setRetryingModel(null);
    setRounds((current) =>
      current.map((round) => ({
        ...round,
        results: round.results.map((item) =>
          item.pending
            ? { ...item, pending: false, ok: false, empty_response: false, error: "已取消" }
            : item,
        ),
      })),
    );
  };

  const selectProvider = (nextId: number) => {
    if (modelsLocked || busy) return;
    abortInFlight();
    setProviderId(nextId);
    setSelectedModels([]);
    setModelQuery("");
    const next = new URLSearchParams(searchParams);
    next.set("provider", String(nextId));
    setSearchParams(next, { replace: true });
  };

  const setModelSelection = (next: string[]) => {
    if (modelsLocked || busy) return;
    const unique = [...new Set(next)];
    if (unique.length > MAX_CHAT_MODELS) {
      toast.error(`单次对话最多选择 ${MAX_CHAT_MODELS} 个模型`);
    }
    setSelectedModels(unique.slice(0, MAX_CHAT_MODELS));
  };

  const toggleModel = (modelId: string) => {
    setModelSelection(
      selectedModels.includes(modelId)
        ? selectedModels.filter((item) => item !== modelId)
        : [...selectedModels, modelId],
    );
  };

  const resetConversation = () => {
    abortInFlight();
    const emptyState: PersistedConversation = {
      providerId,
      selectedModels,
      message,
      systemPrompt,
      rounds: [],
      histories: {},
    };
    latestConversationRef.current = emptyState;
    persistConversationNow(emptyState);
    setRounds([]);
    setHistories({});
  };

  const updateRoundResult = (
    roundId: string,
    modelId: string,
    update: ChatDisplayResult | ((current: ChatDisplayResult) => ChatDisplayResult),
  ) => {
    setRounds((current) =>
      current.map((round) =>
        round.id === roundId
          ? {
              ...round,
              results: round.results.map((item) =>
                item.requested_model === modelId
                  ? typeof update === "function" ? update(item) : update
                  : item,
              ),
            }
          : round,
      ),
    );
  };

  const sendTest = async () => {
    const provider = selectedProvider;
    const text = message.trim();
    if (busy) return;
    if (!provider) return toast.error("请先选择模型提供商");
    if (selectedModels.length === 0) return toast.error("请至少选择一个模型");
    if (!text) return toast.error("测试语不能为空");

    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    const createdAt = Date.now();
    const roundId = `${createdAt}`;
    const modelsToTest = [...selectedModels];
    const roundSystemPrompt = systemPrompt.trim() || DEFAULT_SYSTEM_PROMPT;
    const roundMaxTokens = maxTokens;
    const roundTimeoutSeconds = timeoutSeconds;
    const historiesForRequest = modelsToTest.reduce<Record<string, ChatTestTurn[]>>((acc, modelId) => {
      acc[modelId] = histories[`${provider.id}:${modelId}`] || [];
      return acc;
    }, {});

    setRounds((current) => [
      ...current,
      {
        id: roundId,
        providerId: provider.id,
        providerName: provider.name,
        message: text,
        createdAt,
        histories: historiesForRequest,
        systemPrompt: roundSystemPrompt,
        maxTokens: roundMaxTokens,
        timeoutSeconds: roundTimeoutSeconds,
        results: modelsToTest.map((modelId) => ({
          ok: false,
          requested_model: modelId,
          latency_ms: 0,
          input_tokens: 0,
          output_tokens: 0,
          empty_response: false,
          pending: true,
        })),
      },
    ]);
    try {
      await streamChatTestProviderModels(
        provider.id,
        {
          models: modelsToTest,
          message: text,
          history_by_model: historiesForRequest,
          system_prompt: roundSystemPrompt,
          max_tokens: roundMaxTokens,
          timeout_seconds: roundTimeoutSeconds,
        },
        (event) => {
          if (controller.signal.aborted) return;
          const modelId = event.requested_model;
          if (event.type === "start") {
            updateRoundResult(roundId, modelId, (current) => ({
              ...current,
              pending: true,
              streaming: true,
              effective_api_format: event.effective_api_format,
              client_identity_profile: event.client_identity_profile,
            }));
            return;
          }
          if (event.type === "delta") {
            updateRoundResult(roundId, modelId, (current) => ({
              ...current,
              pending: true,
              streaming: !event.stream_fallback,
              stream_fallback: Boolean(event.stream_fallback),
              model: event.model || current.model,
              response: `${current.response || ""}${event.delta}`,
            }));
            return;
          }
          const result = event.result;
          updateRoundResult(roundId, modelId, result);
          if (event.type === "done" && result.ok && result.response) {
            setHistories((current) => {
              const key = `${provider.id}:${modelId}`;
              return {
                ...current,
                [key]: [
                  ...(historiesForRequest[modelId] || []),
                  { role: "user", content: text },
                  { role: "assistant", content: result.response as string },
                ].slice(-16) as ChatTestTurn[],
              };
            });
          }
        },
        { signal: controller.signal },
      );
    } catch (error) {
      if (!controller.signal.aborted) {
        forceConversationFlushRef.current = true;
        for (const modelId of modelsToTest) {
          updateRoundResult(roundId, modelId, (current) => current.pending ? {
            ...current,
            pending: false,
            ok: false,
            error: getErrMsg(error),
          } : current);
        }
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        if (mountedRef.current) {
          forceConversationFlushRef.current = true;
          setRunning(false);
        }
      }
    }
  };

  const retryModel = async (
    roundId: string,
    modelId: string,
    apiFormat: LLMApiFormat,
    identity: LLMClientIdentityProfile,
  ) => {
    const round = rounds.find((item) => item.id === roundId);
    if (!round || busy) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setRetryingModel(modelId);
    updateRoundResult(roundId, modelId, {
      ok: false,
      requested_model: modelId,
      latency_ms: 0,
      input_tokens: 0,
      output_tokens: 0,
      empty_response: false,
      pending: true,
      effective_api_format: apiFormat,
      client_identity_profile: identity,
    });
    try {
      await streamChatTestProviderModels(
        round.providerId,
        {
          models: [modelId],
          message: round.message,
          history: round.histories[modelId] || [],
          system_prompt: round.systemPrompt,
          max_tokens: round.maxTokens,
          timeout_seconds: round.timeoutSeconds,
          api_format_override: apiFormat,
          client_identity_profile_override: identity,
        },
        (event) => {
          if (controller.signal.aborted || !mountedRef.current) return;
          if (event.type === "start") {
            updateRoundResult(roundId, modelId, (current) => ({
              ...current,
              pending: true,
              streaming: true,
              effective_api_format: event.effective_api_format,
              client_identity_profile: event.client_identity_profile,
            }));
            return;
          }
          if (event.type === "delta") {
            updateRoundResult(roundId, modelId, (current) => ({
              ...current,
              pending: true,
              streaming: !event.stream_fallback,
              stream_fallback: Boolean(event.stream_fallback),
              model: event.model || current.model,
              response: `${current.response || ""}${event.delta}`,
            }));
            return;
          }
          const result = event.result;
          updateRoundResult(roundId, modelId, result);
          const isLatestRound = !rounds.some((item) => item.createdAt > round.createdAt);
          if (event.type === "done" && isLatestRound && result.ok && result.response) {
            const key = `${round.providerId}:${modelId}`;
            setHistories((current) => ({
              ...current,
              [key]: [
                ...(round.histories[modelId] || []),
                { role: "user", content: round.message },
                { role: "assistant", content: result.response as string },
              ].slice(-16) as ChatTestTurn[],
            }));
          }
        },
        { signal: controller.signal },
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      forceConversationFlushRef.current = true;
      updateRoundResult(roundId, modelId, (current) => current.pending ? {
        ...current,
        pending: false,
        ok: false,
        empty_response: false,
        error: getErrMsg(error),
        effective_api_format: apiFormat,
        client_identity_profile: identity,
      } : current);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      if (mountedRef.current) {
        forceConversationFlushRef.current = true;
        setRetryingModel(null);
      }
    }
  };

  if (providersQ.isLoading) {
    return (
      <PageShell>
        <PageHeader
          icon={Activity}
          title="模型测活"
          description="正在读取 Provider、模型能力和最近的对话测活状态。"
        />
        <div role="status" aria-label="模型测活加载中" className="space-y-4">
          <div className="flex gap-2">
            <Skeleton className="h-9 w-28 rounded-md" />
            <Skeleton className="h-9 w-32 rounded-md" />
          </div>
          <div className="grid gap-4 xl:grid-cols-[280px_minmax(0,1fr)] 2xl:grid-cols-[280px_minmax(0,1fr)_280px]">
            <div className="hidden space-y-4 rounded-lg border p-4 xl:block">
              <Skeleton className="h-5 w-24" />
              <Skeleton className="h-9 w-full rounded-md" />
              {[0, 1, 2, 3].map((item) => <Skeleton key={item} className="h-8 w-full rounded-md" />)}
            </div>
            <div className="flex min-h-[420px] flex-col overflow-hidden rounded-lg border bg-card">
              <div className="flex items-center gap-3 border-b p-4">
                <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
                <div className="flex-1 space-y-2"><Skeleton className="h-4 w-36" /><Skeleton className="h-3 w-48" /></div>
              </div>
              <div className="flex flex-1 items-center justify-center p-6">
                <div className="w-full max-w-sm space-y-3"><Skeleton className="mx-auto h-12 w-12 rounded-lg" /><Skeleton className="mx-auto h-5 w-4/5" /><Skeleton className="mx-auto h-3 w-full" /></div>
              </div>
              <div className="space-y-2 border-t p-3"><Skeleton className="h-16 w-full rounded-lg" /><div className="flex justify-between"><Skeleton className="h-3 w-32" /><Skeleton className="h-3 w-24" /></div></div>
            </div>
            <div className="hidden space-y-4 rounded-lg border p-4 2xl:block">
              <Skeleton className="h-5 w-24" />
              {[0, 1, 2].map((item) => <div key={item} className="space-y-2"><Skeleton className="h-3 w-20" /><Skeleton className="h-9 w-full rounded-md" /></div>)}
            </div>
          </div>
        </div>
      </PageShell>
    );
  }

  if (providersQ.isError) {
    return (
      <PageShell>
        <PageHeader
          icon={Activity}
          title="模型测活"
          description="无法加载模型提供商，请返回供应商页面检查配置。"
          actions={<Button asChild variant="outline"><Link to="/ai?tab=providers">返回模型提供商</Link></Button>}
        />
        <ErrorState error={providersQ.error} onRetry={() => void providersQ.refetch()} />
      </PageShell>
    );
  }

  if (providers.length === 0 || !selectedProvider) {
    return (
      <PageShell>
        <PageHeader
          icon={Activity}
          title="模型测活"
          description="至少配置一个 LLM Provider 后才能发起真实对话测活。"
          actions={<Button asChild><Link to="/ai?tab=providers">配置模型提供商</Link></Button>}
        />
        <EmptyState
          icon={Activity}
          title="暂无可测活的模型提供商"
          description="先添加 Provider 并启用至少一个模型，再回来发起真实对话测活。"
          action={<Button asChild size="touch"><Link to="/ai?tab=providers">配置模型提供商</Link></Button>}
        />
      </PageShell>
    );
  }

  const completedResults = rounds.reduce(
    (count, round) => count + round.results.filter((item) => !item.pending).length,
    0,
  );
  const healthyResults = rounds.reduce(
    (count, round) => count + round.results.filter((item) => !item.pending && item.ok).length,
    0,
  );

  const scopePanel = (
    <>
      <div className="flex items-start justify-between gap-3 border-b pb-3">
        <div>
          <div className="text-sm font-semibold">测试范围</div>
          <div className="mt-1 text-xs text-muted-foreground">先选 Provider，再选择要比较的模型。</div>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-9 w-9 p-0 xl:hidden"
          aria-label="关闭测试范围"
          onClick={() => setScopeOpen(false)}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="mt-4 space-y-1.5">
        <Label htmlFor="liveness-provider">LLM Provider</Label>
        <Select
          id="liveness-provider"
          value={String(selectedProvider.id)}
          disabled={modelsLocked || busy}
          onChange={(event) => selectProvider(Number(event.target.value))}
        >
          {providers.map((provider) => (
            <option key={provider.id} value={String(provider.id)}>
              {provider.name} · {provider.provider}
            </option>
          ))}
        </Select>
      </div>

      <div className="mt-4 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <Label>模型</Label>
          <MetaBadge mono tone={selectedModels.length > 0 ? "success" : "warn"}>
            已选 {selectedModels.length}/{modelChoices.length}
          </MetaBadge>
        </div>
        <div className="relative">
          <Filter className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={modelQuery}
            onChange={(event) => setModelQuery(event.target.value)}
            placeholder="搜索当前 Provider 的模型"
            className="pl-9"
          />
        </div>
        <div className="flex flex-wrap gap-1">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={modelsLocked || busy}
            onClick={() => setModelSelection(modelChoices.filter((item) => item.enabled).map((item) => item.id))}
          >
            已启用
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={modelsLocked || busy}
            onClick={() => setModelSelection(modelChoices.map((item) => item.id))}
          >
            全选
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={modelsLocked || busy}
            onClick={() => setModelSelection([])}
          >
            清空
          </Button>
          <Button
            type="button"
            variant={capFilter === "all" ? "secondary" : "ghost"}
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={modelsLocked || busy}
            onClick={() => setCapFilter("all")}
          >
            全部能力
          </Button>
          <Button
            type="button"
            variant={capFilter === "tools" ? "secondary" : "ghost"}
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={modelsLocked || busy}
            onClick={() => setCapFilter("tools")}
            title="仅显示声明支持 Tools 的模型"
          >
            Tools
          </Button>
          <Button
            type="button"
            variant={capFilter === "vision" ? "secondary" : "ghost"}
            size="sm"
            className="h-7 px-2 text-xs"
            disabled={modelsLocked || busy}
            onClick={() => setCapFilter("vision")}
            title="仅显示声明支持 Vision 的模型"
          >
            Vision
          </Button>
        </div>
        <div className="max-h-[48vh] space-y-1 overflow-y-auto rounded-md border bg-background p-1 xl:max-h-[520px]">
          {visibleModels.length > 0 ? visibleModels.map((model) => (
            <div
              key={model.id}
              className={cn(
                "flex min-h-10 items-start gap-2 rounded px-2 py-1.5 text-xs",
                modelsLocked || busy ? "cursor-not-allowed opacity-70" : "cursor-pointer hover:bg-muted/60",
              )}
            >
              <Switch
                checked={selectedModels.includes(model.id)}
                disabled={modelsLocked || busy}
                aria-label={`${model.id} 参与多模型测活`}
                onCheckedChange={() => toggleModel(model.id)}
                className="mt-0.5 shrink-0 scale-90"
              />
              <div className="min-w-0 flex-1">
                <span className="block truncate font-mono" title={model.id}>{model.id}</span>
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1">
                  {model.id === selectedProvider.default_model ? <MetaBadge tone="success">默认</MetaBadge> : null}
                  {model.supports_tools === true ? <MetaBadge tone="outline">Tools</MetaBadge> : null}
                  {model.supports_images === true ? <MetaBadge tone="outline">Vision</MetaBadge> : null}
                  {model.enabled ? (
                    <span className="shrink-0 rounded bg-emerald-500/10 px-1.5 py-0.5 text-[9px] font-medium text-emerald-700 dark:text-emerald-300">
                      已被启用
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
          )) : (
            <EmptyState className="min-h-0 rounded-none border-0 px-3" size="sm" title="没有匹配的模型" />
          )}
        </div>
      </div>

      {modelsLocked ? (
        <div className="mt-3 flex items-start gap-2 rounded-md bg-muted/50 px-3 py-2 text-xs leading-5 text-muted-foreground">
          <LockKeyhole className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          为保证每个模型拥有相同上下文，本会话已锁定 Provider 与模型集合。清空对话后可重新选择。
        </div>
      ) : null}
    </>
  );

  const settingsPanel = (
    <>
      <div className="flex items-start justify-between gap-3 border-b pb-3">
        <div>
          <div className="text-sm font-semibold">请求设置</div>
          <div className="mt-1 text-xs text-muted-foreground">协议和客户端是诊断信息，不作为页面分类。</div>
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

      <dl className="mt-4 space-y-3 text-xs">
        <div className="flex items-start justify-between gap-3">
          <dt className="text-muted-foreground">Provider</dt>
          <dd className="text-right font-medium">{selectedProvider.name}</dd>
        </div>
        <div className="flex items-start justify-between gap-3">
          <dt className="text-muted-foreground">配置协议</dt>
          <dd className="text-right font-mono">{protocolLabel(selectedProvider.api_format)}</dd>
        </div>
        <div className="flex items-start justify-between gap-3">
          <dt className="text-muted-foreground">客户端身份</dt>
          <dd className="text-right">{identityLabel(selectedProvider.client_identity_profile)}</dd>
        </div>
        <div className="flex items-start justify-between gap-3">
          <dt className="text-muted-foreground">Base URL</dt>
          <dd className="max-w-[170px] break-all text-right font-mono">{selectedProvider.base_url || "默认地址"}</dd>
        </div>
      </dl>

      <div className="mt-4 grid grid-cols-2 gap-2 border-t pt-4">
        <div className="space-y-1.5">
          <Label htmlFor="liveness-max-tokens">最大输出</Label>
          <Input
            id="liveness-max-tokens"
            type="number"
            min={64}
            max={8000}
            value={maxTokens}
            onChange={(event) => setMaxTokens(Number(event.target.value) || 1200)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="liveness-timeout">超时秒数</Label>
          <Input
            id="liveness-timeout"
            type="number"
            min={10}
            max={600}
            value={timeoutSeconds}
            onChange={(event) => setTimeoutSeconds(Number(event.target.value) || 90)}
          />
        </div>
      </div>

      <div className="mt-4 space-y-1.5">
        <Label htmlFor="liveness-system-prompt">系统提示词</Label>
        <Textarea
          id="liveness-system-prompt"
          value={systemPrompt}
          rows={7}
          maxLength={2000}
          onChange={(event) => setSystemPrompt(event.target.value)}
        />
      </div>

      <div className="mt-4 rounded-md bg-muted/50 px-3 py-2 text-xs leading-5 text-muted-foreground">
        每条回复会显示后端实际采用的协议与客户端身份。固定身份与协议不兼容时，后端仍按安全规则回落。
      </div>
    </>
  );

  return (
    <PageShell className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button asChild size="sm" className="gap-1.5 shadow-sm">
          <Link to="/ai?tab=providers">
            <ArrowLeft className="h-4 w-4" />返回模型提供商
          </Link>
        </Button>
      </div>
      <PageHeader
        icon={Activity}
        title="模型测活"
        description={mode === "conversation"
          ? "在同一个 LLM Provider 内向多个模型发送真实对话，比较模型回复、实际协议、客户端身份与上游耗时。"
          : "开启多个 LLM Provider，对其已启用模型发送同一条真实测试语并并发比较结果。"}
        signals={
          mode === "conversation" ? (
            <>
              <MetaBadge tone="success">{selectedProvider.name}</MetaBadge>
              <MetaBadge>{selectedModels.length} 个模型</MetaBadge>
              {rounds.length > 0 ? <MetaBadge mono>{healthyResults}/{completedResults} 正常</MetaBadge> : null}
            </>
          ) : (
            <>
              <MetaBadge tone="success">全局巡检</MetaBadge>
              <MetaBadge>{providers.length} 个 Provider 可选</MetaBadge>
            </>
          )
        }
      />

      <RuntimeHealthBar />

      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card p-2 shadow-sm">
        <Tabs value={mode} onValueChange={(value) => !modeSwitchBusy && setMode(value as "conversation" | "all")}>
          <TabsList className="grid h-auto w-full grid-cols-2 sm:w-auto">
            <TabsTrigger value="conversation" disabled={modeSwitchBusy} className="gap-1.5 px-3 py-2 text-xs sm:text-sm">
              <MessageSquare className="h-4 w-4" />Provider 多模型对话
            </TabsTrigger>
            <TabsTrigger value="all" disabled={modeSwitchBusy} className="gap-1.5 px-3 py-2 text-xs sm:text-sm">
              <Activity className="h-4 w-4" />全部 Provider 巡检
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="text-xs text-muted-foreground">
          {mode === "conversation" ? "连续对话按模型分别保留上下文" : "所有已启用模型接收同一条无历史测试语"}
        </div>
      </div>

      {mode === "all" ? (
        <FullLivenessPanel
          providers={providers}
          systemPrompt={systemPrompt}
          onSystemPromptChange={setSystemPrompt}
          message={message}
          onMessageChange={setMessage}
          onBusyChange={setFullLivenessBusy}
        />
      ) : (
        <div className="relative grid min-h-0 gap-4 xl:grid-cols-[280px_minmax(0,1fr)] 2xl:grid-cols-[280px_minmax(0,1fr)_280px]">
          {scopeOpen ? (
            <button
              type="button"
              className="fixed inset-0 z-[69] bg-black/20 xl:hidden"
              aria-label="关闭测试范围"
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
            aria-label="Provider 与模型范围"
          >
            {scopePanel}
          </aside>

          <section className="flex h-[calc(100dvh-12rem)] min-h-[420px] min-w-0 flex-col overflow-hidden rounded-lg border bg-card shadow-sm xl:min-h-[650px]">
            <header className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="truncate text-sm font-semibold">{selectedProvider.name}</h2>
                  <MetaBadge mono>{selectedProvider.provider}</MetaBadge>
                  <MetaBadge mono>{protocolLabel(selectedProvider.api_format)}</MetaBadge>
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {selectedModels.length} 个模型并行回复，结果按模型身份逐条展示。
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 gap-1.5 px-2 text-xs xl:hidden"
                  aria-label="打开测试范围"
                  title="打开测试范围"
                  onClick={() => { setSettingsOpen(false); setScopeOpen(true); }}
                >
                  <Filter className="h-4 w-4" />范围
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 gap-1.5 px-2 text-xs 2xl:hidden"
                  aria-label="打开请求设置"
                  title="打开请求设置"
                  onClick={() => { setScopeOpen(false); setSettingsOpen(true); }}
                >
                  <SlidersHorizontal className="h-4 w-4" />设置
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2"
                  disabled={busy || rounds.length === 0}
                  onClick={resetConversation}
                >
                  <RotateCcw className="mr-1 h-4 w-4" />清空对话
                </Button>
              </div>
            </header>

            <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto bg-muted/15 px-4 py-5 sm:px-6">
              {rounds.length === 0 ? (
                <div className="flex h-full min-h-72 items-center justify-center">
                  <div className="max-w-md text-center">
                    <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-lg bg-primary/10 text-primary">
                      <MessageSquare className="h-5 w-5" />
                    </div>
                    <h3 className="mt-4 text-base font-semibold">向多个模型发起同一轮真实对话</h3>
                    <p className="mt-2 text-sm leading-6 text-muted-foreground">
                      首轮发送后会锁定 Provider 与模型集合，确保后续每个模型拥有相同的对话历史。
                    </p>
                  </div>
                </div>
              ) : (
                <div className="mx-auto w-full max-w-3xl space-y-8">
                  {rounds.map((round) => {
                    const completed = round.results.filter((item) => !item.pending).length;
                    return (
                      <div key={round.id}>
                        <div className="flex justify-end">
                          <div className="max-w-[88%] rounded-lg rounded-tr-sm bg-foreground px-4 py-3 text-sm leading-6 text-background shadow-sm">
                            <div className="mb-1 text-[11px] opacity-65">{formatTime(round.createdAt)} · {round.providerName}</div>
                            {round.message}
                          </div>
                        </div>
                        <div className="mt-5">
                          <div className="flex items-center gap-3 border-b pb-2 text-xs text-muted-foreground">
                            <span className="font-semibold text-foreground">模型回复</span>
                            <span className="tabular-nums">{completed}/{round.results.length} 已完成</span>
                            <span className="h-px flex-1 bg-border" />
                          </div>
                          <div>
                            {round.results.map((result) => (
                              <ChatResponseBranch
                                key={`${round.id}:${result.requested_model}`}
                                result={result}
                                onRetry={(apiFormat, identity) => retryModel(
                                  round.id,
                                  result.requested_model,
                                  apiFormat,
                                  identity,
                                )}
                              />
                            ))}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="border-t bg-card p-3">
              <div className="mx-auto w-full max-w-3xl">
                <div className="mb-2 flex gap-1.5 overflow-x-auto pb-1">
                  {selectedModels.map((modelId) => (
                    <span key={modelId} className="shrink-0 rounded-full border bg-muted/40 px-2 py-1 font-mono text-[10px] text-muted-foreground">
                      {modelId}
                    </span>
                  ))}
                </div>
                <div className="flex min-w-0 items-end gap-2 rounded-lg border bg-background p-2 shadow-sm focus-within:ring-[3px] focus-within:ring-ring/20">
                  <Textarea
                    value={message}
                    rows={2}
                    maxLength={2000}
                    disabled={busy}
                    onChange={(event) => setMessage(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                        event.preventDefault();
                        if (!busy) void sendTest();
                      }
                    }}
                    placeholder="输入测试消息，Enter 发送"
                    className="min-h-12 flex-1 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0"
                  />
                  <Button
                    type="button"
                    className="h-11 w-11 shrink-0 p-0"
                    variant={busy ? "outline" : "default"}
                    disabled={!busy && (!message.trim() || selectedModels.length === 0)}
                    onClick={() => busy ? abortInFlight() : void sendTest()}
                  >
                    {busy ? <X className="h-4 w-4" /> : <Send className="h-4 w-4" />}
                    <span className="sr-only">{busy ? "取消测活请求" : "发送测试消息"}</span>
                  </Button>
                </div>
                <div className="mt-1.5 flex items-center justify-between gap-2 px-1 text-[10px] text-muted-foreground">
                  <span>Enter 发送 · Shift + Enter 换行</span>
                  <span>真实请求会消耗上游额度</span>
                </div>
              </div>
            </div>
          </section>

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
            aria-label="请求设置与诊断"
          >
            {settingsPanel}
          </aside>
        </div>
      )}
    </PageShell>
  );
}

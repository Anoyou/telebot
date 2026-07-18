import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Cpu, Menu, MessageCircle, Server, Settings2 } from "lucide-react";
import { toast } from "sonner";

import {
  createSystemAgentSession,
  deleteSystemAgentSession,
  getSystemAgentCapabilities,
  getSystemAgentConfig,
  listSystemAgentActions,
  listSystemAgentMessages,
  listSystemAgentSessions,
  patchSystemAgentConfig,
  retrySystemAgentMessage,
  streamSystemAgentMessage,
  type SystemAgentAction,
  type SystemAgentMessage,
  type SystemAgentSession,
  type SystemAgentStreamEvent,
} from "@/api/systemAgent";
import { listLLMProviders } from "@/api/commands";
import { listAccounts } from "@/api/accounts";
import type { LLMProviderOut } from "@/api/types";
import { Composer } from "@/components/assistant/Composer";
import { Conversation, type LiveBubble } from "@/components/assistant/Conversation";
import { SessionDrawer } from "@/components/assistant/SessionDrawer";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/misc";
import { getErrMsg } from "@/lib/api";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";
import { removeSessionAndChooseNext } from "./sessionState";

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
  const [activeId, setActiveId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [accountId, setAccountId] = useState<number | "">("");
  const [live, setLive] = useState<LiveBubble[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [retryingMessageId, setRetryingMessageId] = useState<number | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [runtimeSelection, setRuntimeSelection] = useState<{
    providerName: string;
    model: string;
  } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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
    refetchInterval: 15_000,
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
      setActiveId(session.id);
      setLive([]);
      toast.success("已新建会话");
    },
    onError: (e) => toast.error(getErrMsg(e)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteSystemAgentSession(id),
    onSuccess: (_data, id) => {
      const sessionQueryKey = ["system-agent", "sessions"];
      const currentSessions = qc.getQueryData<SystemAgentSession[]>(sessionQueryKey) || [];
      const nextState = removeSessionAndChooseNext(currentSessions, activeId, id);
      qc.setQueryData(sessionQueryKey, nextState.sessions);
      void qc.removeQueries({ queryKey: ["system-agent", "messages", id] });
      void qc.removeQueries({ queryKey: ["system-agent", "actions", id] });
      void qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
      if (activeId === id) {
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
    const stoppedBubbleId = `live-stopped-${Date.now()}`;
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
    let sessionId: string | null = null;
    let stopped = false;
    try {
      sessionId = await ensureSession();
      let assistantText = "";
      const updatePendingText = (value: string) => {
        setLive((prev) =>
          prev.map((bubble) =>
            bubble.role === "assistant" && bubble.pending
              ? { ...bubble, text: value }
              : bubble,
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
          const pendingBubbles = withoutCurrent.filter((item) => item.pending && item.role === "assistant");
          const stableBubbles = withoutCurrent.filter(
            (item) => !(item.pending && item.role === "assistant"),
          );
          return [...stableBubbles, bubble, ...pendingBubbles];
        });
      };
      const onEvent = (event: SystemAgentStreamEvent) => {
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
          if (event.type === "tool_finished") {
            upsertToolProgress(event, true);
          }
          if (event.type === "action_proposed" && event.action) {
            const action = event.action as SystemAgentAction;
            setLive((prev) => {
              const withoutPending = prev.filter((b) => !b.pending);
              return [
                ...withoutPending,
                {
                  id: `action-${action.id}`,
                  role: "action" as const,
                  text: action.summary || action.tool_name,
                  action,
                },
              ];
            });
          }
          if (event.type === "assistant_message") {
            assistantText = String(event.content || "");
            const providerName = event.usage?.provider_name;
            const model = event.usage?.model;
            if (typeof providerName === "string" && typeof model === "string") {
              setRuntimeSelection({ providerName, model });
            }
            setLive((prev) => {
              const withoutPending = prev.filter((b) => !b.pending);
              return [
                ...withoutPending,
                {
                  id: `live-assistant-final`,
                  role: "assistant",
                  text: assistantText,
                },
              ];
            });
          }
          if (event.type === "error") {
            if (
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
      const accountPayload = { account_id: accountId === "" ? null : Number(accountId) };
      if (retryMessageId != null) {
        await retrySystemAgentMessage(
          sessionId,
          retryMessageId,
          {
            ...accountPayload,
            fallback_provider_id: fallbackProviderId ?? null,
            approved_tools: approvedTools || [],
          },
          onEvent,
          { signal: controller.signal },
        );
      } else {
        await streamSystemAgentMessage(
          sessionId,
          { content: text || "", ...accountPayload },
          onEvent,
          { signal: controller.signal },
        );
      }
      // 流结束后清空临时气泡；pending Action 由 pendingActionsQ 持久渲染
      setLive([]);
    } catch (e) {
      stopped = controller.signal.aborted || (e instanceof DOMException && e.name === "AbortError");
      if (stopped) {
        toast.message("已停止本轮请求");
        setLive((prev) => [
          ...prev.filter((bubble) => bubble.role === "action"),
          { id: stoppedBubbleId, role: "assistant", text: "已停止本轮请求。" },
        ]);
      } else {
        toast.error(getErrMsg(e));
        setLive((prev) => prev.filter((b) => (text && b.role === "user") || b.role === "action"));
      }
    } finally {
      if (sessionId) {
        await Promise.all([
          qc.invalidateQueries({ queryKey: ["system-agent", "messages", sessionId] }),
          qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] }),
          qc.invalidateQueries({ queryKey: ["system-agent", "actions", sessionId] }),
        ]);
      }
      if (abortRef.current === controller) abortRef.current = null;
      setStreaming(false);
      setRetryingMessageId(null);
      if (stopped && sessionId) {
        window.setTimeout(() => {
          void qc.invalidateQueries({ queryKey: ["system-agent", "messages", sessionId] });
          setLive((prev) => prev.filter((bubble) => bubble.id !== stoppedBubbleId));
        }, 1000);
      }
    }
  };

  const onSend = async (text: string) => runTurn({ text });

  const onRetryMessage = async (
    messageId: number,
    fallbackProviderId?: number,
    approvedTools?: string[],
  ) => runTurn({ retryMessageId: messageId, fallbackProviderId, approvedTools });

  const onStop = () => abortRef.current?.abort();

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
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="md:hidden"
              onClick={() => setDrawerOpen(true)}
            >
              <Menu className="h-4 w-4" />
              会话
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setConfigOpen((v) => !v)}>
              <Settings2 className="mr-1 h-4 w-4" />
              配置
            </Button>
          </div>
        }
      />

      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted-foreground">
        <span>
          状态：
          <span className="text-foreground">
            {configQ.isLoading ? "加载中" : enabled ? (streaming ? "调用中" : "已启用") : "未启用"}
          </span>
        </span>
        <label className="inline-flex min-w-0 items-center gap-1.5">
          <Server className="h-3.5 w-3.5 shrink-0" />
          <span className="shrink-0 whitespace-nowrap">Provider</span>
          <Select
            aria-label="切换 Agent Provider"
            value={configQ.data?.provider_id == null ? "" : String(configQ.data.provider_id)}
            disabled={selectorDisabled}
            onChange={(event) => selectProvider(event.target.value)}
            className="h-8 w-44"
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
        <label className="inline-flex min-w-0 items-center gap-1.5">
          <Cpu className="h-3.5 w-3.5 shrink-0" />
          <span className="shrink-0 whitespace-nowrap">模型</span>
          <Select
            aria-label="切换 Agent 模型"
            value={configQ.data?.model || configuredToolsModels[0] || ""}
            disabled={selectorDisabled || configuredToolsModels.length === 0}
            onChange={(event) => saveConfigMut.mutate({ model: event.target.value || null })}
            className="h-8 max-w-64"
          >
            {configuredToolsModels.length === 0 ? (
              <option value="">未选择</option>
            ) : (
              configuredToolsModels.map((model) => (
                <option key={model} value={model}>
                  {model}
                </option>
              ))
            )}
          </Select>
        </label>
        {(streaming || actualSelectionDiffers) && displayedSelection ? (
          <span className="min-w-0 truncate text-xs text-muted-foreground">
            实际调用：{displayedSelection.providerName} · {displayedSelection.model}
          </span>
        ) : null}
        <label className="flex items-center gap-2">
          <span className="shrink-0 whitespace-nowrap">账号上下文</span>
          <Select
            value={accountId === "" ? "" : String(accountId)}
            onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : "")}
            className="h-8 w-44"
          >
            <option value="">系统级</option>
            {(accountsQ.data || []).map((a) => (
              <option key={a.id} value={a.id}>
                #{a.id} {a.display_name || a.phone || a.tg_username || ""}
              </option>
            ))}
          </Select>
        </label>
        <Link to="/ai?tab=providers" className="text-primary hover:underline">
          配置模型提供商
        </Link>
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
          onSelect={setActiveId}
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
            <div className="flex flex-1 items-center justify-center">
              <Spinner />
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
              />
            </>
          )}
        </div>
      </div>
    </PageShell>
  );
}

export default AssistantIndex;

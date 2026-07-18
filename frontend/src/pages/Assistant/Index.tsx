import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Menu, MessageCircle, Settings2 } from "lucide-react";
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
  type SystemAgentStreamEvent,
} from "@/api/systemAgent";
import { listLLMProviders } from "@/api/commands";
import { listAccounts } from "@/api/accounts";
import { Composer } from "@/components/assistant/Composer";
import { Conversation, type LiveBubble } from "@/components/assistant/Conversation";
import { SessionDrawer } from "@/components/assistant/SessionDrawer";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/misc";
import { getErrMsg } from "@/lib/api";

export function AssistantIndex() {
  const qc = useQueryClient();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [accountId, setAccountId] = useState<number | "">("");
  const [live, setLive] = useState<LiveBubble[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [retryingMessageId, setRetryingMessageId] = useState<number | null>(null);
  const [configOpen, setConfigOpen] = useState(false);

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
    enabled: configOpen,
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
      void qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
      if (activeId === id) {
        setActiveId(null);
        setLive([]);
      }
      toast.success("会话已删除");
    },
    onError: (e) => toast.error(getErrMsg(e)),
  });

  const saveConfigMut = useMutation({
    mutationFn: patchSystemAgentConfig,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["system-agent"] });
      toast.success("助手配置已保存");
      setConfigOpen(false);
    },
    onError: (e) => toast.error(getErrMsg(e)),
  });

  const enabled = configQ.data?.enabled ?? false;
  const statusLabel = useMemo(() => {
    if (configQ.isLoading) return "加载中";
    if (!enabled) return "未启用";
    if (configQ.data?.provider_id) {
      return `Provider #${configQ.data.provider_id}${configQ.data.model ? ` · ${configQ.data.model}` : ""}`;
    }
    return "未配置模型";
  }, [configQ.data, configQ.isLoading, enabled]);

  const messages: SystemAgentMessage[] = messagesQ.data || [];

  const ensureSession = async (): Promise<string> => {
    if (activeId) return activeId;
    const session = await createSystemAgentSession({
      account_id: accountId === "" ? null : Number(accountId),
    });
    await qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
    setActiveId(session.id);
    return session.id;
  };

  const runTurn = async ({ text, retryMessageId }: { text?: string; retryMessageId?: number }) => {
    if (!enabled) {
      toast.error("请先在右上角开启系统助手并选择支持 tools 的 Provider");
      setConfigOpen(true);
      return;
    }
    setStreaming(true);
    setRetryingMessageId(retryMessageId ?? null);
    const userBubble: LiveBubble | null = text
      ? { id: `live-user-${Date.now()}`, role: "user", text }
      : null;
    const pending: LiveBubble = {
      id: `live-assistant-${Date.now()}`,
      role: "assistant",
      text: "",
      pending: true,
    };
    setLive(userBubble ? [userBubble, pending] : [pending]);
    try {
      const sessionId = await ensureSession();
      let assistantText = "";
      const onEvent = (event: SystemAgentStreamEvent) => {
          if (event.type === "tool_finished") {
            setLive((prev) => {
              const tools = prev.filter((b) => b.role === "tool" || b.role === "user");
              const rest = prev.filter((b) => b.role !== "tool" && b.role !== "user");
              return [
                ...tools.filter((b) => b.role === "user"),
                {
                  id: `tool-${event.call_id || event.seq}`,
                  role: "tool" as const,
                  text: `${event.tool_name || "tool"}${event.is_error ? " 失败" : " 完成"}`,
                },
                ...rest,
              ];
            });
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
            toast.error(event.message || "助手运行失败");
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
        await retrySystemAgentMessage(sessionId, retryMessageId, accountPayload, onEvent);
      } else {
        await streamSystemAgentMessage(
          sessionId,
          { content: text || "", ...accountPayload },
          onEvent,
        );
      }
      await qc.invalidateQueries({ queryKey: ["system-agent", "messages", sessionId] });
      await qc.invalidateQueries({ queryKey: ["system-agent", "sessions"] });
      await qc.invalidateQueries({ queryKey: ["system-agent", "actions", sessionId] });
      // 流结束后清空临时气泡；pending Action 由 pendingActionsQ 持久渲染
      setLive([]);
    } catch (e) {
      toast.error(getErrMsg(e));
      setLive((prev) => prev.filter((b) => (text && b.role === "user") || b.role === "action"));
    } finally {
      setStreaming(false);
      setRetryingMessageId(null);
    }
  };

  const onSend = async (text: string) => runTurn({ text });

  const onRetryMessage = async (messageId: number) => runTurn({ retryMessageId: messageId });

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

      <div className="mb-3 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
        <span>
          状态：<span className="text-foreground">{statusLabel}</span>
        </span>
        <label className="flex items-center gap-2">
          <span>账号上下文</span>
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
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">固定 Provider</span>
              <Select
                value={configQ.data?.provider_id != null ? String(configQ.data.provider_id) : ""}
                onChange={(e) => {
                  const v = e.target.value;
                  saveConfigMut.mutate({
                    provider_id: v ? Number(v) : null,
                    enabled: true,
                  });
                }}
              >
                <option value="">请选择</option>
                {(providersQ.data || []).map((p) => (
                  <option key={p.id} value={p.id}>
                    #{p.id} {p.name}
                  </option>
                ))}
              </Select>
            </label>
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
          {messagesQ.isLoading && activeId ? (
            <div className="flex flex-1 items-center justify-center">
              <Spinner />
            </div>
          ) : (
            <Conversation
              messages={messages}
              live={conversationLive}
              onRetryMessage={onRetryMessage}
              retryingMessageId={retryingMessageId}
              onActionUpdated={() => {
                void qc.invalidateQueries({ queryKey: ["system-agent", "actions", activeId] });
              }}
            />
          )}
          <Composer disabled={streaming || configQ.isLoading} onSend={onSend} />
        </div>
      </div>
    </PageShell>
  );
}

export default AssistantIndex;

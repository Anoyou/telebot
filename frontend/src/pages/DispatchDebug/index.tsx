import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bug,
  CheckCircle2,
  ListTree,
  Loader2,
  RefreshCw,
  Route,
  Search,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { listAccounts } from "@/api/accounts";
import { simulateDispatch, type DispatchTrace, type DispatchTraceStage } from "@/api/dispatch";
import { listIgnoredPeers } from "@/api/ignored_peers";
import { getSystemSettings } from "@/api/system";
import type { AccountSummary, IgnoredPeer } from "@/api/types";
import { AccountStatusBadge } from "@/components/AccountStatusBadge";
import { DryRunDetail } from "@/components/DryRunDetail";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { SignalPill } from "@/components/ui/status";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

const STAGE_LABELS: Record<string, string> = {
  direct_passthrough: "直通",
  prefix_command: "命令",
  keyword: "关键词",
  event_subscription: "事件订阅",
};

const CHAT_TYPE_OPTIONS = [
  { value: "group", label: "群组" },
  { value: "supergroup", label: "超级群" },
  { value: "private", label: "私聊" },
  { value: "channel", label: "频道" },
];

const VIA_OPTIONS = [
  { value: "userbot", label: "Userbot" },
  { value: "interaction_bot", label: "交互 Bot" },
  { value: "webhook", label: "Webhook" },
];

function accountOptionLabel(account: AccountSummary): string {
  const name = account.display_name?.trim();
  const username = account.tg_username?.trim();
  return name || (username ? `@${username}` : null) || `账号 #${account.id}`;
}

function peerKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    private: "私聊",
    group: "群组",
    supergroup: "超级群",
    channel: "频道",
  };
  return labels[kind] || kind || "会话";
}

function peerLabel(peer: IgnoredPeer): string {
  return peer.peer_label?.trim() || `${peerKindLabel(String(peer.peer_kind))} ${peer.peer_id}`;
}

function optionalInteger(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function matchedCount(trace?: DispatchTrace | null): number {
  return trace?.stages.filter((stage) => stage.matched).length ?? 0;
}

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] || stage;
}

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "空";
  if (Array.isArray(value)) return value.length ? value.map(valueText).join(", ") : "[]";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

function stageLogs(trace: DispatchTrace | null) {
  if (!trace) return null;
  return {
    logs: trace.stages.map((stage) => ({
      step: stage.stage,
      msg: `${stage.matched ? "命中" : "未命中"}：${stage.message}（${stage.reason_code}）`,
    })),
  };
}

function DetailRows({ item }: { item: Record<string, unknown> }) {
  const entries = Object.entries(item);
  if (entries.length === 0) return null;
  return (
    <dl className="grid gap-1.5 text-xs sm:grid-cols-[124px_minmax(0,1fr)]">
      {entries.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="min-w-0 truncate text-muted-foreground">{key}</dt>
          <dd className="min-w-0 break-words font-medium text-foreground">
            {typeof value === "object" && value !== null ? (
              <code className="break-all rounded bg-muted px-1 py-0.5 text-[11px] font-normal">
                {valueText(value)}
              </code>
            ) : (
              valueText(value)
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function DetailGroup({
  title,
  items,
  emptyText,
}: {
  title: string;
  items?: Array<Record<string, unknown>>;
  emptyText: string;
}) {
  if (!items) return null;
  return (
    <details className="rounded-md border border-border/70 bg-background/70" open={items.some((item) => item.matched)}>
      <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-foreground">
        {title}
        <span className="ml-2 text-muted-foreground">{items.length}</span>
      </summary>
      <div className="space-y-2 border-t px-3 py-3">
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground">{emptyText}</p>
        ) : (
          items.map((item, index) => (
            <div
              key={index}
              className={cn(
                "rounded-md border px-3 py-2",
                item.matched ? "border-success/25 bg-success/10" : "bg-muted/20",
              )}
            >
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge variant={item.matched ? "success" : "secondary"}>
                  {item.matched ? "命中" : "未命中"}
                </Badge>
                {typeof item.reason_code === "string" ? (
                  <code className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    {item.reason_code}
                  </code>
                ) : null}
              </div>
              <DetailRows item={item} />
            </div>
          ))
        )}
      </div>
    </details>
  );
}

function StageNode({ stage, index, isLast }: { stage: DispatchTraceStage; index: number; isLast: boolean }) {
  const Icon = stage.matched ? CheckCircle2 : XCircle;
  const extra = Object.fromEntries(
    Object.entries(stage).filter(
      ([key, value]) =>
        !["stage", "matched", "reason_code", "message", "matches", "candidates", "decisions"].includes(key)
        && value !== undefined
        && value !== null
        && value !== "",
    ),
  );

  return (
    <div className="relative pl-7">
      {!isLast ? <div className="absolute left-2 top-7 h-[calc(100%-1.75rem)] w-px bg-border" /> : null}
      <div
        className={cn(
          "absolute left-0 top-3 flex h-5 w-5 items-center justify-center rounded-full border bg-card",
          stage.matched ? "border-success/40 text-success" : "border-muted-foreground/30 text-muted-foreground",
        )}
      >
        <Icon className="h-3.5 w-3.5" />
      </div>
      <section className="rounded-lg border border-border/70 bg-background/80 p-3 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold text-muted-foreground">#{index + 1}</span>
              <h3 className="text-sm font-semibold">{stageLabel(stage.stage)}</h3>
              <Badge variant={stage.matched ? "success" : "secondary"}>
                {stage.matched ? "命中" : "未命中"}
              </Badge>
            </div>
            <p className="mt-1 break-words text-sm text-muted-foreground">{stage.message}</p>
          </div>
          <code className="rounded bg-muted px-2 py-1 text-xs text-muted-foreground">
            {stage.reason_code}
          </code>
        </div>
        {Object.keys(extra).length > 0 ? (
          <div className="mt-3 rounded-md border bg-muted/20 p-3">
            <DetailRows item={extra} />
          </div>
        ) : null}
        <div className="mt-3 space-y-2">
          <DetailGroup title="命中项" items={stage.matches} emptyText="没有命中项。" />
          <DetailGroup title="候选项" items={stage.candidates} emptyText="没有候选项。" />
          <DetailGroup title="订阅决策" items={stage.decisions} emptyText="没有订阅决策。" />
        </div>
      </section>
    </div>
  );
}

function TracePanel({ trace }: { trace: DispatchTrace | null }) {
  if (!trace) {
    return (
      <div className="flex min-h-[360px] flex-col items-center justify-center rounded-lg border border-dashed bg-muted/20 px-4 py-10 text-center">
        <ListTree className="mb-3 h-8 w-8 text-muted-foreground" />
        <p className="text-sm font-medium">暂无 trace</p>
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">
          提交模拟后，这里会显示每个分发阶段的命中状态和原因码。
        </p>
      </div>
    );
  }

  const logs = stageLogs(trace);
  return (
    <div className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-3">
        <SignalPill tone={matchedCount(trace) > 0 ? "success" : "neutral"} label="命中阶段" value={`${matchedCount(trace)}/${trace.stages.length}`} />
        <SignalPill tone="neutral" label="来源" value={trace.via || "-"} />
        <SignalPill tone="neutral" label="方向" value={trace.chat.direction || "incoming"} />
      </div>
      {logs ? <DryRunDetail detail={logs} /> : null}
      <div className="space-y-3">
        {trace.stages.map((stage, index) => (
          <StageNode
            key={`${stage.stage}-${index}`}
            stage={stage}
            index={index}
            isLast={index === trace.stages.length - 1}
          />
        ))}
      </div>
      <details className="rounded-lg border bg-background/80">
        <summary className="cursor-pointer px-3 py-2 text-sm font-semibold">原始 trace JSON</summary>
        <pre className="max-h-80 overflow-auto border-t p-3 text-xs">
          {JSON.stringify(trace, null, 2)}
        </pre>
      </details>
    </div>
  );
}

export function DispatchDebugPage() {
  const queryClient = useQueryClient();
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(),
  });
  const settingsQ = useQuery({ queryKey: ["system", "settings"], queryFn: getSystemSettings });
  const accounts = accountsQ.data ?? [];
  const [selectedAid, setSelectedAid] = useState<number | null>(null);
  const [chatType, setChatType] = useState("group");
  const [chatId, setChatId] = useState("-100123");
  const [senderId, setSenderId] = useState("");
  const [via, setVia] = useState("userbot");
  const [text, setText] = useState("");
  const [trace, setTrace] = useState<DispatchTrace | null>(null);

  const selectedAccount = useMemo(
    () => accounts.find((account) => account.id === selectedAid) ?? null,
    [accounts, selectedAid],
  );
  const commandPrefix = settingsQ.data?.command_prefix || ",";
  const allowedPeersQ = useQuery({
    queryKey: ["accounts", selectedAid, "ignored-peers"],
    queryFn: () => listIgnoredPeers(selectedAid as number),
    enabled: selectedAid !== null,
  });
  const allowedPeers = allowedPeersQ.data ?? [];

  useEffect(() => {
    if (selectedAid !== null) return;
    if (accounts.length > 0) {
      setSelectedAid(accounts[0].id);
    }
  }, [accounts, selectedAid]);

  const simulateMut = useMutation({
    mutationFn: simulateDispatch,
    onSuccess: (nextTrace) => {
      setTrace(nextTrace);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (selectedAid === null) {
      toast.error("请选择账号");
      return;
    }
    if (!text.trim()) {
      toast.error("请输入要模拟的文本");
      return;
    }
    simulateMut.mutate({
      account_id: selectedAid,
      chat_type: chatType,
      chat_id: optionalInteger(chatId),
      sender_id: optionalInteger(senderId),
      text,
      via,
    });
  };

  if (accountsQ.isLoading) {
    return (
      <PageShell>
        <PageHeader
          icon={Bug}
          title="命中调试"
          description="正在读取账号列表。"
        />
        <div className="flex h-36 items-center justify-center rounded-lg border bg-card">
          <Spinner className="text-primary" />
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell>
      <PageHeader
        icon={Bug}
        title="命中调试"
        description="按账号模拟消息分发，查看直通、命令、关键词和事件订阅的判断结果。"
        actions={
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={refresh}
            disabled={accountsQ.isFetching}
            className="active:scale-95"
          >
            <RefreshCw className={cn("mr-1 h-4 w-4", accountsQ.isFetching && "animate-spin")} />
            刷新账号
          </Button>
        }
        signals={
          <>
            <SignalPill tone={trace ? (matchedCount(trace) > 0 ? "success" : "neutral") : "neutral"} label="最近结果" value={trace ? `${matchedCount(trace)} 个阶段命中` : "未模拟"} />
            <SignalPill tone="neutral" label="阶段" value="4" />
          </>
        }
      />

      {accounts.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <AlertTriangle className="h-4 w-4 text-warning" />
              暂无账号
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            命中调试需要至少一个 Telegram 账号 worker。
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)]">
          <Card className="h-fit xl:sticky xl:top-4">
            <CardHeader className="border-b bg-muted/30">
              <CardTitle className="flex items-center gap-2 text-base">
                <Search className="h-4 w-4 text-primary" />
                模拟输入
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-5">
              <form className="space-y-4" onSubmit={submit}>
                <div className="space-y-2">
                  <Label htmlFor="dispatch-account">账号</Label>
                  <Select
                    id="dispatch-account"
                    value={selectedAid?.toString() ?? ""}
                    onChange={(event) => {
                      setSelectedAid(Number(event.target.value));
                      setChatId("");
                      setTrace(null);
                    }}
                    aria-label="选择账号"
                  >
                    {accounts.map((account) => (
                      <option key={account.id} value={account.id}>
                        {accountOptionLabel(account)}
                      </option>
                    ))}
                  </Select>
                  {selectedAccount ? (
                    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      <AccountStatusBadge status={selectedAccount.status} />
                      <span>ID {selectedAccount.id}</span>
                      {selectedAccount.tg_user_id ? <span>TG {selectedAccount.tg_user_id}</span> : null}
                    </div>
                  ) : null}
                </div>

                <div className="space-y-2">
                  <Label htmlFor="dispatch-peer">会话</Label>
                  <Select
                    id="dispatch-peer"
                    value={allowedPeers.some((peer) => String(peer.peer_id) === chatId.trim()) ? chatId.trim() : ""}
                    onChange={(event) => {
                      const id = event.target.value;
                      const peer = allowedPeers.find((item) => String(item.peer_id) === id);
                      setChatId(id);
                      if (peer?.peer_kind) setChatType(String(peer.peer_kind));
                      setTrace(null);
                    }}
                    disabled={allowedPeersQ.isLoading || allowedPeers.length === 0}
                  >
                    <option value="">
                      {allowedPeersQ.isLoading
                        ? "正在读取已允许会话"
                        : allowedPeers.length
                          ? "从已允许会话选择"
                          : "暂无已允许会话，可手动填写"}
                    </option>
                    {allowedPeers.map((peer) => (
                      <option key={peer.id} value={String(peer.peer_id)}>
                        {peerLabel(peer)} · {peer.peer_id}
                      </option>
                    ))}
                  </Select>
                  <p className="text-xs leading-5 text-muted-foreground">
                    交互规则通常按已允许会话生效；选择会话后会自动同步 Chat ID 和会话类型。
                  </p>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="dispatch-via">来源</Label>
                    <Select id="dispatch-via" value={via} onChange={(event) => setVia(event.target.value)}>
                      {VIA_OPTIONS.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="dispatch-chat-type">会话类型</Label>
                    <Select id="dispatch-chat-type" value={chatType} onChange={(event) => setChatType(event.target.value)}>
                      {CHAT_TYPE_OPTIONS.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </Select>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="dispatch-chat-id">Chat ID</Label>
                    <Input
                      id="dispatch-chat-id"
                      inputMode="numeric"
                      value={chatId}
                      onChange={(event) => setChatId(event.target.value)}
                      placeholder="-100123"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="dispatch-sender-id">Sender ID</Label>
                    <Input
                      id="dispatch-sender-id"
                      inputMode="numeric"
                      value={senderId}
                      onChange={(event) => setSenderId(event.target.value)}
                      placeholder="可选"
                    />
                  </div>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="dispatch-text">文本</Label>
                  <Textarea
                    id="dispatch-text"
                    value={text}
                    onChange={(event) => setText(event.target.value)}
                    placeholder={`${commandPrefix}help 或关键词文本`}
                    className="min-h-36 resize-y"
                  />
                </div>

                {simulateMut.isError ? (
                  <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    {getErrMsg(simulateMut.error)}
                  </div>
                ) : null}

                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <Button
                    type="submit"
                    disabled={simulateMut.isPending || selectedAid === null || !text.trim()}
                    className="w-full active:scale-95 sm:w-auto"
                  >
                    {simulateMut.isPending ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : (
                      <Route className="mr-1 h-4 w-4" />
                    )}
                    模拟命中
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={simulateMut.isPending}
                    onClick={() => {
                      setText("");
                      setTrace(null);
                    }}
                    className="w-full active:scale-95 sm:w-auto"
                  >
                    清空
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>

          <Card className="min-w-0 overflow-hidden">
            <CardHeader className="border-b bg-muted/30">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <CardTitle className="flex items-center gap-2 text-base">
                  <ListTree className="h-4 w-4 text-primary" />
                  命中 trace
                </CardTitle>
                {trace ? (
                  <Badge variant={matchedCount(trace) > 0 ? "success" : "secondary"}>
                    {matchedCount(trace) > 0 ? "有命中" : "未命中"}
                  </Badge>
                ) : null}
              </div>
            </CardHeader>
            <CardContent className="pt-5">
              <TracePanel trace={trace} />
            </CardContent>
          </Card>
        </div>
      )}
    </PageShell>
  );
}

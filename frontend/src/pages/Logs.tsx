import { useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Circle,
  Clock,
  Copy,
  MessageSquareText,
  MousePointerClick,
  Puzzle,
  RefreshCw,
  Search,
  ScrollText,
  SlidersHorizontal,
  Workflow,
  XCircle,
} from "lucide-react";

import { listAccounts } from "@/api/accounts";
import { getFeatureMatrix } from "@/api/features";
import { getEventTrace, getMessageFunel, getSystemSettings } from "@/api/system";
import type {
  EventActionItem,
  EventProbeReport,
  EventProbeRoutingItem,
  EventProbeSuggestion,
  EventSpanItem,
  EventTraceDetail,
  EventTraceSummary,
  MessageFunelItem,
  MessageFunelStage,
  MessageVerdict,
} from "@/api/types";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { Switch } from "@/components/ui/switch";
import { cn, formatDateTime } from "@/lib/utils";

type TimeRange = "15m" | "1h" | "6h" | "24h" | "custom";
type VerdictFilter = "" | MessageVerdict;
type TimelineItem =
  | { kind: "span"; ts: string; span: EventSpanItem }
  | { kind: "action"; ts: string; action: EventActionItem };

const TIME_RANGE_LABELS: Record<TimeRange, string> = {
  "15m": "近 15 分钟",
  "1h": "近 1 小时",
  "6h": "近 6 小时",
  "24h": "近 24 小时",
  custom: "自定义",
};

const STAGE_LABELS: Record<"received" | "routed" | "ran" | "sent", string> = {
  received: "收到",
  routed: "匹配",
  ran: "执行",
  sent: "发送",
};

export function Logs() {
  const [searchParams] = useSearchParams();
  const initialTraceId = searchParams.get("trace_id") || "";
  const [accountId, setAccountId] = useState(() => searchParams.get("account_id") || searchParams.get("aid") || "");
  const [keyword, setKeyword] = useState(() => searchParams.get("keyword") || "");
  const [verdict, setVerdict] = useState<VerdictFilter>(() => parseVerdict(searchParams.get("verdict")));
  const [timeRange, setTimeRange] = useState<TimeRange>("1h");
  const [customSince, setCustomSince] = useState("");
  const [customUntil, setCustomUntil] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [pluginKey, setPluginKey] = useState(() => searchParams.get("plugin_key") || "");
  const [eventType, setEventType] = useState(() => searchParams.get("event_type") || "");
  const [sourceChannel, setSourceChannel] = useState(() => searchParams.get("source_channel") || "");
  const [status, setStatus] = useState(() => searchParams.get("status") || "");
  const [traceId, setTraceId] = useState(() => initialTraceId);
  const [reasonCode, setReasonCode] = useState(() => searchParams.get("reason_code") || searchParams.get("error_code") || "");
  const [chatId, setChatId] = useState(() => searchParams.get("chat_id") || "");
  const [messageId, setMessageId] = useState(() => searchParams.get("message_id") || "");
  const [senderUserId, setSenderUserId] = useState(() => searchParams.get("sender_user_id") || "");
  const [selectedTraceId, setSelectedTraceId] = useState(() => initialTraceId);

  const settingsQ = useQuery({ queryKey: ["system", "settings"], queryFn: getSystemSettings });
  const accountsQ = useQuery({ queryKey: ["accounts"], queryFn: listAccounts });
  const matrixQ = useQuery({ queryKey: ["matrix"], queryFn: getFeatureMatrix });
  const timezone = settingsQ.data?.timezone || "";
  const range = useMemo(() => buildTimeRange(timeRange, customSince, customUntil), [customSince, customUntil, timeRange]);
  const traceIdFilter = traceId.trim();
  const reasonCodeFilter = reasonCode.trim();
  const commonQuery = {
    account_id: accountId || undefined,
    source_channel: sourceChannel || undefined,
    event_type: eventType || undefined,
    status: status || undefined,
    plugin_key: pluginKey || undefined,
    trace_id: traceIdFilter || undefined,
    reason_code: reasonCodeFilter || undefined,
    chat_id: chatId.trim() || undefined,
    message_id: messageId.trim() || undefined,
    sender_user_id: senderUserId.trim() || undefined,
    keyword: keyword.trim() || undefined,
    verdict: verdict || undefined,
    since: range.since,
    until: range.until,
    limit: 100,
  };

  const messagesQ = useQuery({
    queryKey: ["logs", "messages", commonQuery],
    queryFn: () => getMessageFunel(commonQuery),
    refetchInterval: autoRefresh ? 5_000 : false,
  });
  const traceDetailQ = useQuery({
    queryKey: ["logs", "trace", "detail", selectedTraceId],
    queryFn: () => getEventTrace(selectedTraceId),
    enabled: Boolean(selectedTraceId),
  });
  const selectedMessage = (messagesQ.data ?? []).find((item) => item.trace_id === selectedTraceId);
  const counts = countVerdicts(messagesQ.data ?? []);
  const pluginOptions = matrixQ.data?.features.map((item) => item.key) ?? [];

  return (
    <PageShell>
      <PageHeader
        title="日志 · 消息流"
        description="按消息追踪收到、匹配、执行、发送四段状态，直接定位卡点、失败和正常跳过。"
        icon={ScrollText}
        signals={(
          <>
            <SignalPill tone="neutral" label="窗口" value={TIME_RANGE_LABELS[timeRange]} />
            <SignalPill tone={autoRefresh ? "success" : "neutral"} label="刷新" value={autoRefresh ? "5 秒" : "暂停"} />
            <SignalPill tone={counts.failed || counts.stuck ? "warn" : "success"} label="消息" value={`${messagesQ.data?.length ?? 0} 条`} />
          </>
        )}
        actions={(
          <Button type="button" variant="outline" size="sm" onClick={() => messagesQ.refetch()}>
            <RefreshCw className="mr-1.5 h-4 w-4" />
            刷新
          </Button>
        )}
      />

      <Card>
        <CardHeader>
          <SectionHeader
            icon={Search}
            title="筛选"
            description="账号、时间、结果和关键词会直接作用到消息流。"
            meta={(
              <Button type="button" variant="outline" size="sm" onClick={() => setShowFilters((value) => !value)}>
                <SlidersHorizontal className="mr-1.5 h-4 w-4" />
                更多条件
                <ChevronDown className={cn("ml-1.5 h-4 w-4 transition-transform", showFilters ? "rotate-180" : "")} />
              </Button>
            )}
          />
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_180px_minmax(300px,340px)] 2xl:grid-cols-[220px_180px_340px_minmax(260px,1fr)]">
            <Field label="账号">
              <Select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
                <option value="">全部账号</option>
                {accountsQ.data?.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.display_name || account.phone}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="时间">
              <Select value={timeRange} onChange={(event) => setTimeRange(event.target.value as TimeRange)}>
                {Object.entries(TIME_RANGE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </Select>
            </Field>
            <Field label="结果">
              <VerdictSegment value={verdict} onChange={setVerdict} />
            </Field>
            <Field label="搜索">
              <SearchBox value={keyword} onChange={setKeyword} />
            </Field>
          </div>

          {timeRange === "custom" ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="开始时间">
                <Input type="datetime-local" value={customSince} onChange={(event) => setCustomSince(event.target.value)} />
              </Field>
              <Field label="结束时间">
                <Input type="datetime-local" value={customUntil} onChange={(event) => setCustomUntil(event.target.value)} />
              </Field>
            </div>
          ) : null}

          {showFilters ? (
            <div className="grid grid-cols-1 gap-3 border-t pt-4 sm:grid-cols-2 xl:grid-cols-4">
              <Field label="插件">
                <PluginSelect value={pluginKey} onChange={setPluginKey} options={pluginOptions} />
              </Field>
              <Field label="来源">
                <Select value={sourceChannel} onChange={(event) => setSourceChannel(event.target.value)}>
                  <option value="">全部来源</option>
                  <option value="userbot">userbot</option>
                  <option value="interaction_bot">interaction_bot</option>
                  <option value="account_bot">account_bot</option>
                  <option value="external_payment_notice">external_payment_notice</option>
                </Select>
              </Field>
              <Field label="事件类型">
                <Select value={eventType} onChange={(event) => setEventType(event.target.value)}>
                  <option value="">全部事件</option>
                  <option value="message">message</option>
                  <option value="command">command</option>
                  <option value="callback_query">callback_query</option>
                  <option value="inline_query">inline_query</option>
                  <option value="chosen_inline_result">chosen_inline_result</option>
                  <option value="payment_confirmed">payment_confirmed</option>
                  <option value="session_close">session_close</option>
                </Select>
              </Field>
              <Field label="Trace 状态">
                <Select value={status} onChange={(event) => setStatus(event.target.value)}>
                  <option value="">全部状态</option>
                  <option value="ok">ok</option>
                  <option value="running">running</option>
                  <option value="skipped">skipped</option>
                  <option value="warning">warning</option>
                  <option value="failed">failed</option>
                </Select>
              </Field>
              <Field label="Trace ID">
                <Input
                  value={traceId}
                  onChange={(event) => {
                    const nextTraceId = event.target.value.trim();
                    setTraceId(nextTraceId);
                    setSelectedTraceId(nextTraceId);
                  }}
                  placeholder="evt_..."
                />
              </Field>
              <Field label="原因代码">
                <Input value={reasonCode} onChange={(event) => setReasonCode(event.target.value.trim())} placeholder="subscription_not_matched" />
              </Field>
              <Field label="Chat ID">
                <Input value={chatId} onChange={(event) => setChatId(event.target.value)} placeholder="-100..." />
              </Field>
              <Field label="Message ID">
                <Input value={messageId} onChange={(event) => setMessageId(event.target.value)} placeholder="消息 ID" />
              </Field>
              <Field label="用户 ID">
                <Input value={senderUserId} onChange={(event) => setSenderUserId(event.target.value)} placeholder="sender user id" />
              </Field>
              <Field label="自动刷新">
                <div className="flex h-10 items-center gap-2">
                  <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} />
                  <span className="text-sm text-muted-foreground">{autoRefresh ? "开启" : "关闭"}</span>
                </div>
              </Field>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <MessageStream
        messages={messagesQ.data ?? []}
        loading={messagesQ.isLoading}
        error={messagesQ.error}
        timezone={timezone}
        selectedTraceId={selectedTraceId}
        selectedMessage={selectedMessage}
        detail={traceDetailQ.data}
        detailLoading={traceDetailQ.isLoading}
        detailError={traceDetailQ.error}
        keyword={keyword}
        onSelectTrace={(nextTraceId) => {
          setSelectedTraceId((current) => (current === nextTraceId ? "" : nextTraceId));
          setTraceId("");
        }}
      />
    </PageShell>
  );
}

function VerdictSegment({ value, onChange }: { value: VerdictFilter; onChange: (value: VerdictFilter) => void }) {
  const items: { value: VerdictFilter; label: string }[] = [
    { value: "", label: "全部" },
    { value: "responded", label: "已响应" },
    { value: "no_response_normal", label: "未响应" },
    { value: "stuck", label: "卡住" },
    { value: "failed", label: "失败" },
  ];
  return (
    <div className="grid grid-cols-5 gap-1 rounded-lg border bg-muted/30 p-1">
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          onClick={() => onChange(item.value)}
          className={cn(
            "h-8 min-w-0 rounded-md px-2 text-xs font-medium transition",
            value === item.value ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
          )}
        >
          <span className="block truncate">{item.label}</span>
        </button>
      ))}
    </div>
  );
}

function MessageStream({
  messages,
  loading,
  error,
  timezone,
  selectedTraceId,
  selectedMessage,
  detail,
  detailLoading,
  detailError,
  keyword,
  onSelectTrace,
}: {
  messages: MessageFunelItem[];
  loading: boolean;
  error?: unknown;
  timezone?: string;
  selectedTraceId: string;
  selectedMessage?: MessageFunelItem;
  detail?: EventTraceDetail;
  detailLoading: boolean;
  detailError?: unknown;
  keyword: string;
  onSelectTrace: (traceId: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={MessageSquareText}
          title="消息流"
          description="每行直接显示收到、匹配、执行、发送四段状态。"
          meta={<SignalPill tone="neutral" label="返回" value={`${messages.length} 条`} />}
        />
      </CardHeader>
      <CardContent className="space-y-2">
        {loading ? (
          <InlineLoading />
        ) : error ? (
          <ErrorHint text="消息流加载失败" error={error} />
        ) : messages.length ? (
          <>
            <div className="hidden grid-cols-[150px_minmax(180px,1.1fr)_minmax(260px,1.8fr)_300px_112px] gap-3 px-3 pb-1 text-xs font-medium text-muted-foreground 2xl:grid">
              <span>时间</span>
              <span>会话</span>
              <span>消息</span>
              <span>漏斗</span>
              <span className="text-right">结果</span>
            </div>
            {messages.map((message) => (
              <MessageRow
                key={message.trace_id}
                message={message}
                selected={message.trace_id === selectedTraceId}
                selectedMessage={selectedMessage}
                detail={detail}
                detailLoading={detailLoading}
                detailError={detailError}
                timezone={timezone}
                keyword={keyword}
                onSelect={() => onSelectTrace(message.trace_id)}
              />
            ))}
          </>
        ) : (
          <EmptyHint text="当前条件下没有消息 trace" />
        )}
      </CardContent>
    </Card>
  );
}

function MessageRow({
  message,
  selected,
  selectedMessage,
  detail,
  detailLoading,
  detailError,
  timezone,
  keyword,
  onSelect,
}: {
  message: MessageFunelItem;
  selected: boolean;
  selectedMessage?: MessageFunelItem;
  detail?: EventTraceDetail;
  detailLoading: boolean;
  detailError?: unknown;
  timezone?: string;
  keyword: string;
  onSelect: () => void;
}) {
  const meta = verdictMeta(message.verdict);
  const messageText = message.text_preview || message.inline_query || message.chosen_inline_query || message.event_type;
  return (
    <div className={cn("overflow-hidden rounded-lg border bg-background transition", selected ? "border-primary/60 shadow-sm" : "border-border hover:border-primary/40")}>
      <button type="button" className="w-full min-w-0 p-3 text-left" onClick={onSelect}>
        <div className="grid min-w-0 grid-cols-1 gap-3 2xl:grid-cols-[150px_minmax(180px,1.1fr)_minmax(260px,1.8fr)_300px_112px] 2xl:items-center">
          <div className="flex min-w-0 items-center justify-between gap-2 2xl:block">
            <span className="text-xs text-muted-foreground 2xl:hidden">{formatDateTime(message.started_at, timezone)}</span>
            <span className="hidden text-xs text-muted-foreground 2xl:inline">{formatDateTime(message.started_at, timezone)}</span>
            <VerdictBadge verdict={message.verdict} className="2xl:hidden" />
          </div>
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <Badge variant="secondary">{channelLabel(message.source_channel)}</Badge>
              <Badge variant="outline">{message.event_type}</Badge>
              {message.account_id ? <span className="text-xs text-muted-foreground">账号 #{message.account_id}</span> : null}
            </div>
            <div className="mt-1 truncate text-sm font-medium">{conversationLabel(message)}</div>
            <div className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">{message.trace_id}</div>
          </div>
          <div className="min-w-0">
            <p className="line-clamp-2 break-words text-sm leading-6">
              <HighlightedMessage text={messageText} keyword={keyword} />
            </p>
            <div className="mt-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
              {message.sender_name ? <span>{message.sender_name}</span> : null}
              {message.sender_user_id ? <span>user {message.sender_user_id}</span> : null}
              {message.message_id ? <span>msg {message.message_id}</span> : null}
              {message.plugin_count ? <span>插件 {message.plugin_count}</span> : null}
              {message.action_count ? <span>动作 {message.action_count}</span> : null}
            </div>
          </div>
          <FunelStrip message={message} />
          <div className="hidden justify-end 2xl:flex">
            <VerdictBadge verdict={message.verdict} />
          </div>
        </div>
        <div className={cn("mt-3 rounded-md px-3 py-2 text-sm", meta.panelClass)}>
          <div className="flex flex-wrap items-start justify-between gap-2">
            <span className="font-medium">{message.reason_text}</span>
            {message.reason_code ? <span className="font-mono text-xs opacity-80">{message.reason_code}</span> : null}
          </div>
          <p className="mt-1 text-xs leading-5 opacity-90">{message.next_step}</p>
        </div>
      </button>
      {selected ? (
        <TraceDetailPanel
          message={selectedMessage}
          detail={detail}
          loading={detailLoading}
          error={detailError}
          timezone={timezone}
        />
      ) : null}
    </div>
  );
}

function FunelStrip({ message }: { message: MessageFunelItem }) {
  const stages: { key: keyof MessageFunelItem["funel"]; label: string; value: MessageFunelStage }[] = [
    { key: "received", label: STAGE_LABELS.received, value: message.funel.received },
    { key: "routed", label: STAGE_LABELS.routed, value: message.funel.routed },
    { key: "ran", label: STAGE_LABELS.ran, value: message.funel.ran },
    { key: "sent", label: STAGE_LABELS.sent, value: message.funel.sent },
  ];
  return (
    <div className="grid grid-cols-4 gap-1.5">
      {stages.map((stage) => (
        <div key={String(stage.key)} className={cn("min-w-0 rounded-md border px-2 py-1.5", stageClass(stage.value))}>
          <div className="flex items-center justify-center gap-1.5">
            <StageIcon status={stage.value} />
            <span className="truncate text-xs font-medium">{stage.label}</span>
          </div>
          <div className="mt-0.5 truncate text-center text-[11px] opacity-80">{stageLabel(stage.value)}</div>
        </div>
      ))}
    </div>
  );
}

function TraceDetailPanel({
  message,
  detail,
  loading,
  error,
  timezone,
}: {
  message?: MessageFunelItem;
  detail?: EventTraceDetail;
  loading: boolean;
  error?: unknown;
  timezone?: string;
}) {
  if (loading) return <div className="border-t"><InlineLoading /></div>;
  if (error) {
    return (
      <div className="border-t p-3">
        <ErrorHint text="链路详情加载失败" error={error} />
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="border-t p-3">
        <EmptyHint text="尚未读取链路详情" />
      </div>
    );
  }
  const verdict = message ? verdictMeta(message.verdict) : undefined;
  return (
    <div className="space-y-4 border-t bg-muted/20 p-3">
      <section className={cn("rounded-lg border bg-background p-3", verdict?.panelClass)}>
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={detail.status} />
              {message ? <VerdictBadge verdict={message.verdict} /> : <Badge variant="secondary">无判定</Badge>}
              <span className="break-all font-mono text-xs text-muted-foreground">{detail.trace_id}</span>
            </div>
            <p className="mt-2 text-sm font-medium">{message?.reason_text || "后端消息流未返回本条判定"}</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{message?.next_step || "请从消息流列表进入详情，或用 Trace ID 重新筛选。"}</p>
          </div>
          <div className="shrink-0 text-xs text-muted-foreground">{formatDateTime(detail.started_at, timezone)}</div>
        </div>
        <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
          <InfoCell label="来源" value={`${detail.source_channel || "-"} / ${detail.event_type}`} />
          <InfoCell label="会话" value={detail.chat_id ?? "-"} />
          <InfoCell label="消息" value={detail.message_id ?? "-"} />
          <InfoCell label="耗时" value={detail.duration_ms == null ? "-" : `${detail.duration_ms}ms`} />
        </div>
      </section>

      {detail.text_preview ? <p className="rounded-lg border bg-background p-3 text-sm whitespace-pre-wrap">{detail.text_preview}</p> : null}
      <InlineTraceSummary trace={detail} actions={detail.actions} />
      <NativeRawSummary meta={detail.native_raw_meta} />
      <ProbeReportPanel report={detail.probe_report} />
      <Timeline spans={detail.spans} actions={detail.actions} timezone={timezone} />
      <details className="rounded-lg border bg-background p-3">
        <summary className="cursor-pointer text-sm font-medium">高级数据</summary>
        <div className="mt-3 space-y-3">
          <JsonBlock title="native_raw_meta" value={detail.native_raw_meta} />
          <JsonBlock title="raw_summary" value={detail.raw_summary} />
          <JsonBlock title="payload_snapshot" value={detail.payload_snapshot} />
          {detail.related_runtime_logs.length ? <JsonBlock title="related_runtime_logs" value={detail.related_runtime_logs} /> : null}
        </div>
      </details>
    </div>
  );
}

function Timeline({ spans, actions, timezone }: { spans: EventSpanItem[]; actions: EventActionItem[]; timezone?: string }) {
  const [showAll, setShowAll] = useState(false);
  const items = useMemo<TimelineItem[]>(() => [
    ...spans.map((span) => ({ kind: "span" as const, ts: span.started_at, span })),
    ...actions.map((action) => ({ kind: "action" as const, ts: action.created_at, action })),
  ].sort((a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime()), [actions, spans]);
  const visibleItems = showAll ? items : pickDiagnosticTimelineItems(items);
  const hiddenCount = Math.max(0, items.length - visibleItems.length);

  if (!items.length) return <EmptyHint text="该 trace 暂无 span/action 明细" />;
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium">关键时间线</div>
        {hiddenCount || showAll ? (
          <Button type="button" variant="ghost" size="sm" onClick={() => setShowAll((value) => !value)}>
            {showAll ? "只看关键" : `显示全部 ${items.length} 项`}
          </Button>
        ) : null}
      </div>
      <div className="space-y-2">
        {visibleItems.map((item, index) => (
          <div key={`${item.kind}-${index}-${item.ts}`} className={cn("rounded-lg border bg-background p-3", timelineItemClass(item))}>
            {item.kind === "span" ? (
              <div className="space-y-1.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={item.span.status} />
                    <Badge variant="secondary">{item.span.phase}</Badge>
                    {item.span.component ? <Badge variant="secondary">{item.span.component}</Badge> : null}
                  </div>
                  <span className="text-xs text-muted-foreground">{formatDateTime(item.span.started_at, timezone)}</span>
                </div>
                <p className="text-sm">{item.span.message || item.span.reason_code || "阶段完成"}</p>
                <TraceMeta pluginKey={item.span.plugin_key} entryKey={item.span.entry_key} reasonCode={item.span.reason_code} />
              </div>
            ) : (
              <div className="space-y-1.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={item.action.status} />
                    <Badge variant="secondary">{item.action.action_type}</Badge>
                    {item.action.actual_send_via ? <Badge variant="secondary">{item.action.actual_send_via}</Badge> : null}
                  </div>
                  <span className="text-xs text-muted-foreground">{formatDateTime(item.action.created_at, timezone)}</span>
                </div>
                <p className="text-sm">{actionDisplayText(item.action)}</p>
                <TraceMeta pluginKey={item.action.plugin_key} reasonCode={item.action.error_code} />
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function ProbeReportPanel({ report }: { report?: EventProbeReport | null }) {
  if (!report) return null;
  const fieldPaths = report.field_paths ?? [];
  const messageFacts = report.message_facts ?? [];
  const subscriptions = report.subscription_suggestions ?? [];
  const actions = report.action_suggestions ?? [];
  const capabilities = report.capability_hints ?? [];
  const routing = report.routing ?? [];
  const warnings = report.warnings ?? [];
  return (
    <section className="rounded-lg border bg-background p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Workflow className="h-4 w-4 text-primary" />
            <span className="font-medium">开发探针</span>
            <Badge variant="secondary">{report.headline}</Badge>
          </div>
        </div>
        {warnings.length ? <Badge variant="warn">{warnings.length} 条提示</Badge> : null}
      </div>
      {fieldPaths.length || messageFacts.length ? (
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          <ProbeItemGrid title="标准信封路径" items={fieldPaths} />
          <ProbeItemGrid title="消息补充摘要" items={messageFacts} />
        </div>
      ) : null}
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <ProbeSuggestionList title="订阅建议" items={subscriptions} jsonKey="manifest" />
        <ProbeSuggestionList title="动作建议" items={actions} jsonKey="action" />
      </div>
      {capabilities.length ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {capabilities.map((item, index) => (
            <div key={`${item.title}-${index}`} className="rounded-lg border bg-muted/20 p-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <StatusBadge status={item.level || "info"} />
                <span className="text-sm font-medium">{item.title}</span>
                {item.capability ? <Badge variant="secondary">{item.capability}</Badge> : null}
              </div>
              {item.reason ? <p className="mt-1 text-xs text-muted-foreground">{item.reason}</p> : null}
              {item.reason_code ? <p className="mt-1 font-mono text-xs text-muted-foreground">{item.reason_code}</p> : null}
            </div>
          ))}
        </div>
      ) : null}
      {routing.length ? (
        <div className="mt-3 space-y-2">
          <div className="text-xs font-medium text-muted-foreground">路由解释</div>
          {routing.slice(0, 8).map((item, index) => (
            <ProbeRoutingRow key={`${item.phase}-${item.plugin_key}-${index}`} item={item} />
          ))}
        </div>
      ) : null}
      {warnings.length ? (
        <div className="mt-3 rounded-lg border border-amber-300/70 bg-amber-50/70 p-2 text-sm text-amber-950 dark:bg-amber-500/10 dark:text-amber-200">
          {warnings.map((item, index) => (
            <div key={`${item}-${index}`}>{item}</div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function ProbeItemGrid({ title, items }: { title: string; items: EventProbeReport["field_paths"] }) {
  return (
    <div className="rounded-lg border bg-muted/20 p-2">
      <div className="mb-2 text-xs font-medium text-muted-foreground">{title}</div>
      {items.length ? (
        <div className="space-y-2">
          {items.slice(0, 8).map((item, index) => (
            <div key={`${item.path || item.label}-${index}`} className="rounded-md bg-background px-2 py-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[11px] text-muted-foreground">{item.label}</span>
                {item.path ? <span className="break-all font-mono text-[11px] text-muted-foreground">{item.path}</span> : null}
              </div>
              <div className="mt-1 break-all font-mono text-xs">{stringifyShort(item.value)}</div>
              {item.note ? <div className="mt-1 text-xs text-muted-foreground">{item.note}</div> : null}
            </div>
          ))}
        </div>
      ) : (
        <div className="text-sm text-muted-foreground">暂无数据</div>
      )}
    </div>
  );
}

function ProbeSuggestionList({ title, items, jsonKey }: { title: string; items: EventProbeSuggestion[]; jsonKey: "manifest" | "action" }) {
  return (
    <div className="rounded-lg border bg-muted/20 p-2">
      <div className="mb-2 text-xs font-medium text-muted-foreground">{title}</div>
      {items.length ? items.map((item, index) => {
        const jsonValue = item[jsonKey];
        return (
          <div key={`${item.title}-${index}`} className={index ? "mt-3 border-t pt-3" : ""}>
            <div className="text-sm font-medium">{item.title}</div>
            {item.reason ? <p className="mt-1 text-xs text-muted-foreground">{item.reason}</p> : null}
            {jsonValue ? (
              <pre className="mt-2 max-h-52 overflow-auto rounded-md bg-background p-2 text-xs leading-relaxed whitespace-pre-wrap break-all">
                {safeJsonStringify(jsonValue, 2)}
              </pre>
            ) : null}
          </div>
        );
      }) : <div className="text-sm text-muted-foreground">暂无建议</div>}
    </div>
  );
}

function ProbeRoutingRow({ item }: { item: EventProbeRoutingItem }) {
  return (
    <div className={cn("rounded-lg border p-2", item.matched ? "border-emerald-200 bg-emerald-50/50 dark:bg-emerald-500/10" : "bg-muted/20")}>
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge status={item.matched ? "ok" : item.status || "skipped"} />
        {item.phase ? <Badge variant="secondary">{item.phase}</Badge> : null}
        {item.plugin_key ? <Badge variant="secondary">{item.plugin_key}</Badge> : null}
        {item.entry_key ? <Badge variant="secondary">{item.entry_key}</Badge> : null}
      </div>
      <div className="mt-1 text-xs text-muted-foreground">{item.reason_code || item.message || "路由阶段已记录"}</div>
      {item.filters ? <div className="mt-1 break-all font-mono text-xs text-muted-foreground">filters={stringifyShort(item.filters)}</div> : null}
    </div>
  );
}

function InlineTraceSummary({ trace, actions }: { trace: EventTraceSummary | EventTraceDetail; actions?: EventActionItem[] }) {
  const query = trace.inline_query || pickString(trace, ["payload_snapshot.inline_query.query", "raw_summary.inline_query.query", "payload_snapshot.query"]);
  const chosen = trace.chosen_inline_result_id || pickString(trace, ["payload_snapshot.chosen_inline_result.result_id", "raw_summary.chosen_inline_result.result_id"]);
  const choiceQuery = trace.chosen_inline_query || pickString(trace, ["payload_snapshot.chosen_inline_result.query", "raw_summary.chosen_inline_result.query"]);
  const inlineActions = (actions ?? []).filter((action) => action.action_type === "answer_inline_query");
  const resultCount = inlineActions.find((action) => action.inline_result_count != null)?.inline_result_count;
  const failedAction = inlineActions.find((action) => action.status === "failed" || action.error_code || action.error_message);
  if (!query && !chosen && resultCount == null && !failedAction && trace.event_type !== "inline_query" && trace.event_type !== "chosen_inline_result") {
    return null;
  }
  return (
    <section className="rounded-lg border border-primary/20 bg-primary/5 p-3 text-xs">
      <div className="mb-2 font-medium text-foreground">Inline 摘要</div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <InfoCell label="inline_query" value={query || "-"} />
        <InfoCell label="chosen_result" value={chosen || "-"} />
        <InfoCell label="chosen_query" value={choiceQuery || "-"} />
        <InfoCell label="answer 结果数" value={resultCount == null ? "-" : resultCount} />
      </div>
      {failedAction ? (
        <div className="mt-2 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-destructive">
          失败原因：{actionErrorLabel(failedAction)}
        </div>
      ) : null}
    </section>
  );
}

function NativeRawSummary({ meta }: { meta?: EventTraceSummary["native_raw_meta"] }) {
  if (!meta) return null;
  return (
    <section className="rounded-lg border bg-background p-3 text-xs">
      <div className="mb-2 font-medium text-foreground">native_raw_meta 摘要</div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <InfoCell label="声明状态" value={meta.enabled ? "已允许" : "未允许"} />
        <InfoCell label="驱动" value={meta.driver || meta.source || "-"} />
        <InfoCell label="对象" value={meta.object || "-"} />
        <InfoCell label="持久化" value={meta.stored_in_trace ? `已保存 ${meta.size_bytes ?? "-"} bytes` : "未保存"} />
      </div>
      {meta.reason_code ? <div className="mt-2 font-mono text-xs text-muted-foreground">{meta.reason_code}</div> : null}
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0 space-y-1.5">
      <Label>{label}</Label>
      {children}
    </div>
  );
}

function PluginSelect({ value, onChange, options }: { value: string; onChange: (value: string) => void; options: string[] }) {
  const keys = Array.from(new Set([...options, ...(value ? [value] : [])])).sort();
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] xl:grid-cols-1">
      <Select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">全部插件</option>
        {keys.map((key) => (
          <option key={key} value={key}>{key}</option>
        ))}
      </Select>
      <Input value={value} onChange={(event) => onChange(event.target.value.trim())} placeholder="远程插件 key" />
    </div>
  );
}

function SearchBox({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <div className="relative">
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <Input className="pl-9" value={value} onChange={(event) => onChange(event.target.value)} placeholder="chat / 消息 / 发送者 / trace_id" />
    </div>
  );
}

function StatusBadge({ status }: { status?: string | null }) {
  const value = (status || "unknown").toLowerCase();
  const variant = value === "ok" || value === "success" || value === "active"
    ? "success"
    : value === "warning" || value === "warn" || value === "skipped"
      ? "warn"
      : value === "failed" || value === "error"
        ? "destructive"
        : "secondary";
  return <Badge variant={variant}>{status || "unknown"}</Badge>;
}

function VerdictBadge({ verdict, className }: { verdict: MessageVerdict; className?: string }) {
  const meta = verdictMeta(verdict);
  return (
    <Badge variant={meta.badgeVariant} className={className}>
      <meta.icon className="mr-1 h-3 w-3" />
      {meta.label}
    </Badge>
  );
}

function StageIcon({ status }: { status: MessageFunelStage }) {
  if (status === "pass") return <CheckCircle2 className="h-3.5 w-3.5" />;
  if (status === "fail") return <XCircle className="h-3.5 w-3.5" />;
  if (status === "stuck") return <Clock className="h-3.5 w-3.5" />;
  if (status === "skip") return <Circle className="h-3.5 w-3.5" />;
  return <Circle className="h-3.5 w-3.5 opacity-50" />;
}

function TraceMeta({ pluginKey, entryKey, reasonCode }: { pluginKey?: string | null; entryKey?: string | null; reasonCode?: string | null }) {
  if (!pluginKey && !entryKey && !reasonCode) return null;
  return (
    <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
      {pluginKey ? <span>插件 {pluginKey}</span> : null}
      {entryKey ? <span>入口 {entryKey}</span> : null}
      {reasonCode ? <span className="font-mono">{reasonCode}</span> : null}
    </div>
  );
}

function InfoCell({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="rounded-md bg-muted px-2 py-1.5">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="break-all font-mono text-xs text-foreground">{String(value ?? "-")}</div>
    </div>
  );
}

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  if (value == null) return null;
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-muted-foreground">{title}</div>
      <pre className="max-h-72 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-all">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function InlineLoading() {
  return (
    <div className="flex h-28 items-center justify-center">
      <Spinner className="text-primary" />
    </div>
  );
}

function ErrorHint({ text, error }: { text: string; error: unknown }) {
  return (
    <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">
      <div className="font-medium text-destructive">{text}</div>
      <div className="mt-1 break-words text-muted-foreground">{errorMessage(error)}</div>
    </div>
  );
}

function EmptyHint({ text }: { text: string }) {
  return <p className="py-8 text-center text-sm text-muted-foreground">{text}</p>;
}

function HighlightedMessage({ text, keyword }: { text: string; keyword: string }) {
  const q = keyword.trim();
  if (!q) return <>{text}</>;
  const index = text.toLowerCase().indexOf(q.toLowerCase());
  if (index < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, index)}
      <mark className="rounded bg-yellow-200 px-0.5 text-yellow-950">{text.slice(index, index + q.length)}</mark>
      {text.slice(index + q.length)}
    </>
  );
}

function actionDisplayText(action: EventActionItem): string {
  if (action.error_message || action.error_code) return actionErrorLabel(action);
  if (action.action_type === "answer_inline_query") {
    return `Inline 回答已记录，结果 ${action.inline_result_count == null ? "未知" : action.inline_result_count}`;
  }
  return "动作已记录";
}

function actionErrorLabel(action: EventActionItem): string {
  const code = action.error_code ? `${action.error_code}` : "";
  if (code && action.error_message) return `${code}：${action.error_message}`;
  return action.error_message || code || "动作失败";
}

function pickDiagnosticTimelineItems(items: TimelineItem[]): TimelineItem[] {
  const selected = new Set<number>();
  items.forEach((item, index) => {
    if (index === 0 || index === items.length - 1 || isProblemTimelineItem(item) || isImportantTimelineItem(item)) {
      selected.add(index);
    }
  });
  return items.filter((_, index) => selected.has(index)).slice(0, 10);
}

function isImportantTimelineItem(item: TimelineItem): boolean {
  if (item.kind === "action") return true;
  const phase = item.span.phase.toLowerCase();
  return ["subscription", "command", "plugin_invoke", "plugin_return", "contract_guard", "delivery", "settlement"].some((key) => phase.includes(key));
}

function isProblemTimelineItem(item: TimelineItem): boolean {
  if (item.kind === "action") return isFailedStatus(item.action.status) || Boolean(item.action.error_code || item.action.error_message);
  return isFailedStatus(item.span.status) || isWarnStatus(item.span.status) || Boolean(item.span.reason_code && !["matched", "command_matched", "callback_query", "session_control_action"].includes(item.span.reason_code));
}

function timelineItemClass(item: TimelineItem): string {
  if (item.kind === "action") {
    if (isFailedStatus(item.action.status) || item.action.error_code || item.action.error_message) return "border-destructive/30 bg-destructive/5";
    return "";
  }
  if (isFailedStatus(item.span.status)) return "border-destructive/30 bg-destructive/5";
  if (isWarnStatus(item.span.status) || isProblemTimelineItem(item)) return "border-amber-300/60 bg-amber-50/50 dark:bg-amber-500/10";
  return "";
}

function isFailedStatus(status?: string | null): boolean {
  const value = (status || "").toLowerCase();
  return value === "failed" || value === "error";
}

function isWarnStatus(status?: string | null): boolean {
  const value = (status || "").toLowerCase();
  return value === "warning" || value === "warn" || value === "skipped";
}

function parseVerdict(value: string | null): VerdictFilter {
  if (value === "responded" || value === "no_response_normal" || value === "stuck" || value === "failed") return value;
  return "";
}

function buildTimeRange(range: TimeRange, customSince: string, customUntil: string): { since?: string; until?: string } {
  if (range === "custom") {
    return {
      since: localDateTimeToIso(customSince),
      until: localDateTimeToIso(customUntil),
    };
  }
  const minutes: Record<Exclude<TimeRange, "custom">, number> = {
    "15m": 15,
    "1h": 60,
    "6h": 360,
    "24h": 1440,
  };
  return { since: new Date(Date.now() - minutes[range] * 60_000).toISOString() };
}

function localDateTimeToIso(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function countVerdicts(messages: MessageFunelItem[]) {
  return messages.reduce(
    (acc, item) => {
      acc[item.verdict] += 1;
      return acc;
    },
    { responded: 0, no_response_normal: 0, stuck: 0, failed: 0 },
  );
}

function verdictMeta(verdict: MessageVerdict) {
  const map = {
    responded: {
      label: "已响应",
      badgeVariant: "success" as const,
      icon: CheckCircle2,
      panelClass: "border-emerald-500/20 bg-emerald-500/10",
    },
    no_response_normal: {
      label: "未响应正常",
      badgeVariant: "secondary" as const,
      icon: Circle,
      panelClass: "border-border bg-muted/40",
    },
    stuck: {
      label: "卡住",
      badgeVariant: "warn" as const,
      icon: Clock,
      panelClass: "border-amber-500/25 bg-amber-500/10",
    },
    failed: {
      label: "失败",
      badgeVariant: "destructive" as const,
      icon: AlertTriangle,
      panelClass: "border-destructive/30 bg-destructive/5",
    },
  };
  return map[verdict];
}

function stageClass(status: MessageFunelStage): string {
  if (status === "pass") return "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  if (status === "fail") return "border-destructive/30 bg-destructive/5 text-destructive";
  if (status === "stuck") return "border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-300";
  if (status === "skip") return "border-border bg-muted/60 text-muted-foreground";
  return "border-border bg-background text-muted-foreground";
}

function stageLabel(status: MessageFunelStage): string {
  if (status === "pass") return "通过";
  if (status === "fail") return "失败";
  if (status === "stuck") return "卡住";
  if (status === "skip") return "跳过";
  return "未到达";
}

function channelLabel(channel?: string | null): string {
  if (channel === "interaction_bot") return "交互 Bot";
  if (channel === "userbot") return "UserBot";
  if (channel === "account_bot") return "管理 Bot";
  if (channel === "external_payment_notice") return "转账通知";
  return channel || "未知来源";
}

function conversationLabel(trace: EventTraceSummary): string {
  if (trace.chat_id != null) {
    const kind = trace.chat_id < 0 ? "群" : "私";
    return `${kind} ${trace.chat_id}`;
  }
  if (trace.sender_name) return trace.sender_name;
  if (trace.sender_user_id != null) return `user ${trace.sender_user_id}`;
  return "未知会话";
}

function errorMessage(error: unknown): string {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      return String((detail as { message?: unknown }).message || error.message);
    }
    return error.message || `HTTP ${error.response?.status ?? "请求失败"}`;
  }
  if (error instanceof Error) return error.message;
  return String(error || "未知错误");
}

function safeJsonStringify(value: unknown, space?: number): string {
  try {
    return JSON.stringify(value, null, space);
  } catch {
    return String(value);
  }
}

function stringifyShort(value: unknown): string {
  if (value == null) return "-";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function pickString(source: unknown, paths: string[]): string | null {
  for (const path of paths) {
    const value = path.split(".").reduce<unknown>((current, key) => {
      if (!current || typeof current !== "object") return undefined;
      return (current as Record<string, unknown>)[key];
    }, source);
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
  }
  return null;
}

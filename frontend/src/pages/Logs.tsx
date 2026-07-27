import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowDown,
  Bot,
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
  Terminal,
  Workflow,
  XCircle,
} from "lucide-react";

import { listAccounts } from "@/api/accounts";
import { getFeatureMatrix } from "@/api/features";
import { listSystemAgentRuns, type SystemAgentRun } from "@/api/systemAgent";
import { getEventTrace, getMessageFunel, getSystemSettings, listRuntimeLogs, listSystemConsoleLogs } from "@/api/system";
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
  RuntimeLogItem,
  SystemConsoleLogsResponse,
} from "@/api/types";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { RunTrace } from "@/components/assistant/RunTrace";
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
import { EmptyState } from "@/components/feedback/EmptyState";
import { Select } from "@/components/ui/select";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn, formatDateTime } from "@/lib/utils";

type LogView = "messages" | "agent" | "console" | "runtime";
type TimeRange = "15m" | "1h" | "6h" | "24h" | "custom";
type VerdictFilter = "" | MessageVerdict;
type RuntimeLevelFilter = "" | "debug" | "info" | "warn" | "warning" | "error";
type RuntimeSourceFilter = "" | "system" | "event" | "plugin";
type AgentStatusFilter = "" | "queued" | "running" | "succeeded" | "failed" | "cancelled";
type ConsoleLogLevel = "debug" | "info" | "warn" | "error";
type ConsoleLevelFilter = "all" | ConsoleLogLevel;
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
  routed: "路由",
  ran: "处理",
  sent: "发送",
};

const MESSAGE_SOURCE_OPTIONS = [
  { value: "userbot", label: "UserBot 账号" },
  { value: "interaction_bot", label: "交互 Bot" },
  { value: "account_bot", label: "管理 Bot" },
  { value: "scheduler", label: "定时任务" },
  { value: "external_payment_notice", label: "收款通知" },
];

const EVENT_TYPE_OPTIONS = [
  { value: "message", label: "普通消息" },
  { value: "command", label: "命令消息" },
  { value: "callback_query", label: "按钮点击" },
  { value: "inline_query", label: "Inline 查询" },
  { value: "chosen_inline_result", label: "Inline 选择结果" },
  { value: "payment_confirmed", label: "收款确认" },
  { value: "session_close", label: "会话关闭" },
  { value: "session_expired", label: "会话过期" },
  { value: "message_edited", label: "编辑消息" },
  { value: "keyword", label: "关键词触发" },
  { value: "scheduler_fire", label: "定时触发" },
];

const TRACE_STATUS_OPTIONS = [
  { value: "ok", label: "完成" },
  { value: "running", label: "处理中" },
  { value: "skipped", label: "已跳过" },
  { value: "warning", label: "告警" },
  { value: "failed", label: "失败" },
];

const RUNTIME_LEVEL_OPTIONS = [
  { value: "debug", label: "调试" },
  { value: "info", label: "信息" },
  { value: "warn", label: "告警" },
  { value: "error", label: "错误" },
];

const RUNTIME_SOURCE_OPTIONS = [
  { value: "system", label: "系统/Worker" },
  { value: "event", label: "事件链路" },
  { value: "plugin", label: "插件" },
];

const SYSTEM_CONSOLE_SERVICE_OPTIONS = [
  { value: "all", label: "全部服务" },
  { value: "web", label: "后端 Web/Worker" },
  { value: "frontend", label: "前端 Nginx" },
  { value: "postgres", label: "PostgreSQL" },
  { value: "redis", label: "Redis" },
  { value: "updater", label: "更新器" },
];

export function Logs() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialTraceId = searchParams.get("trace_id") || "";
  const [view, setView] = useState<LogView>(() => parseLogView(searchParams.get("view")));
  const [accountId, setAccountId] = useState(() => searchParams.get("account_id") || searchParams.get("aid") || "");
  const [keyword, setKeyword] = useState(() => searchParams.get("keyword") || "");
  const [verdict, setVerdict] = useState<VerdictFilter>(() => parseVerdict(searchParams.get("verdict")));
  const [timeRange, setTimeRange] = useState<TimeRange>(() => parseTimeRange(searchParams.get("range")));
  const [customSince, setCustomSince] = useState(() => searchParams.get("since") || "");
  const [customUntil, setCustomUntil] = useState(() => searchParams.get("until") || "");
  const [showFilters, setShowFilters] = useState(false);
  // 默认关闭自动刷新：进入日志页不再每 5 秒打一轮请求
  const [autoRefresh, setAutoRefresh] = useState(false);
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
  const [runtimeLevel, setRuntimeLevel] = useState<RuntimeLevelFilter>(() => parseRuntimeLevel(searchParams.get("level")));
  const [runtimeSource, setRuntimeSource] = useState<RuntimeSourceFilter>(() => parseRuntimeSource(searchParams.get("source")));
  const [runtimeLimit, setRuntimeLimit] = useState(() => parseRuntimeLimit(searchParams.get("limit")));
  const [agentStatus, setAgentStatus] = useState<AgentStatusFilter>(() => parseAgentStatus(searchParams.get("agent_status")));
  const [consoleService, setConsoleService] = useState(() => parseConsoleService(searchParams.get("service")));
  const debouncedKeyword = useDebouncedValue(keyword.trim(), 350);
  const debouncedTraceId = useDebouncedValue(traceId.trim(), 350);
  const debouncedReasonCode = useDebouncedValue(reasonCode.trim(), 350);
  const debouncedChatId = useDebouncedValue(chatId.trim(), 350);
  const debouncedMessageId = useDebouncedValue(messageId.trim(), 350);
  const debouncedSenderUserId = useDebouncedValue(senderUserId.trim(), 350);

  useEffect(() => {
    const next = new URLSearchParams();
    const setValue = (key: string, value: string | number | undefined) => {
      if (value !== undefined && String(value).trim()) next.set(key, String(value));
    };
    setValue("view", view === "messages" ? undefined : view);
    setValue("account_id", accountId);
    setValue("keyword", keyword.trim());
    setValue("verdict", verdict);
    setValue("range", timeRange === "1h" ? undefined : timeRange);
    if (timeRange === "custom") {
      setValue("since", customSince);
      setValue("until", customUntil);
    }
    setValue("plugin_key", pluginKey);
    setValue("event_type", eventType);
    setValue("source_channel", sourceChannel);
    setValue("status", status);
    setValue("trace_id", traceId.trim() || selectedTraceId);
    setValue("reason_code", reasonCode.trim());
    setValue("chat_id", chatId.trim());
    setValue("message_id", messageId.trim());
    setValue("sender_user_id", senderUserId.trim());
    setValue("level", runtimeLevel);
    setValue("source", runtimeSource);
    setValue("limit", runtimeLimit === 300 ? undefined : runtimeLimit);
    setValue("service", consoleService === "all" ? undefined : consoleService);
    setValue("agent_status", agentStatus);
    if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
  }, [accountId, agentStatus, chatId, consoleService, customSince, customUntil, eventType, keyword, messageId, pluginKey, reasonCode, runtimeLevel, runtimeLimit, runtimeSource, searchParams, selectedTraceId, senderUserId, setSearchParams, sourceChannel, status, timeRange, traceId, verdict, view]);

  const settingsQ = useQuery({ queryKey: ["system", "settings"], queryFn: getSystemSettings });
  const accountsQ = useQuery({ queryKey: ["accounts"], queryFn: listAccounts });
  const matrixQ = useQuery({ queryKey: ["matrix"], queryFn: getFeatureMatrix });
  const timezone = settingsQ.data?.timezone || "";
  const range = useMemo(() => buildTimeRange(timeRange, customSince, customUntil), [customSince, customUntil, timeRange]);
  const traceIdFilter = debouncedTraceId;
  const reasonCodeFilter = debouncedReasonCode;
  const commonQuery = {
    account_id: accountId || undefined,
    source_channel: sourceChannel || undefined,
    event_type: eventType || undefined,
    status: status || undefined,
    plugin_key: pluginKey || undefined,
    trace_id: traceIdFilter || undefined,
    reason_code: reasonCodeFilter || undefined,
    chat_id: safeIntegerFilter(debouncedChatId),
    message_id: safeIntegerFilter(debouncedMessageId, false),
    sender_user_id: safeIntegerFilter(debouncedSenderUserId, false),
    keyword: debouncedKeyword || undefined,
    verdict: verdict || undefined,
    since: range.since,
    until: range.until,
    limit: 100,
  };
  const runtimeQuery = {
    account_id: accountId || undefined,
    level: runtimeLevel || undefined,
    source: runtimeSource || undefined,
    plugin_key: pluginKey || undefined,
    keyword: debouncedKeyword || undefined,
    since: range.since,
    limit: runtimeLimit,
  };
  const consoleQuery = {
    service: consoleService,
    keyword: debouncedKeyword || undefined,
    tail: runtimeLimit,
  };
  const agentQuery = {
    status: agentStatus || undefined,
    since: range.since,
    until: range.until,
    limit: runtimeLimit,
  };

  const messagesQ = useQuery({
    queryKey: ["logs", "messages", commonQuery],
    queryFn: () => getMessageFunel(commonQuery),
    refetchInterval: autoRefresh ? 5_000 : false,
    enabled: view === "messages",
  });
  const runtimeLogsQ = useQuery({
    queryKey: ["logs", "runtime", runtimeQuery],
    queryFn: () => listRuntimeLogs(runtimeQuery),
    refetchInterval: autoRefresh ? 5_000 : false,
    enabled: view === "runtime",
  });
  const systemConsoleQ = useQuery({
    queryKey: ["logs", "system-console", consoleQuery],
    queryFn: () => listSystemConsoleLogs(consoleQuery),
    refetchInterval: autoRefresh ? 5_000 : false,
    enabled: view === "console",
  });
  const agentRunsQ = useQuery({
    queryKey: ["logs", "system-agent-runs", agentQuery],
    queryFn: () => listSystemAgentRuns(agentQuery),
    refetchInterval: autoRefresh ? 5_000 : false,
    enabled: view === "agent",
  });
  const traceDetailQ = useQuery({
    queryKey: ["logs", "trace", "detail", selectedTraceId],
    queryFn: () => getEventTrace(selectedTraceId),
    enabled: view === "messages" && Boolean(selectedTraceId),
    refetchInterval: autoRefresh && selectedTraceId ? 5_000 : false,
  });
  const selectedMessage = (messagesQ.data ?? []).find((item) => item.trace_id === selectedTraceId);
  const counts = countVerdicts(messagesQ.data ?? []);
  const runtimeStats = countRuntimeLogs(runtimeLogsQ.data ?? []);
  const agentStats = countAgentRuns(agentRunsQ.data ?? []);
  const pluginOptions = matrixQ.data?.features.map((item) => item.key) ?? [];
  const activeTitle = view === "messages"
    ? "日志 · 消息流"
    : view === "agent"
      ? "日志 · Agent 运行"
      : view === "console"
        ? "日志 · 系统控制台"
        : "日志 · 运行事件";
  const activeDescription = view === "messages"
    ? "按消息追踪收到、路由、执行、发送四段状态，直接定位卡点、失败和正常跳过。"
    : view === "agent"
      ? "查看系统助手每一轮的状态、错误代码与完整执行轨迹。"
    : view === "console"
      ? "查看 Docker / stdout / stderr 级别的原始系统日志，适合排查服务启动、异常堆栈和部署输出。"
      : "查看 TelePilot 写入数据库的结构化运行事件，适合按插件、账号、等级和 JSON 详情排查。";

  return (
    <PageShell>
      <PageHeader
        title={activeTitle}
        description={activeDescription}
        icon={view === "messages" ? ScrollText : view === "agent" ? Bot : Terminal}
        signals={(
          <>
            <SignalPill tone="neutral" label={view === "console" ? "行数" : "窗口"} value={view === "console" ? `${runtimeLimit} 行` : TIME_RANGE_LABELS[timeRange]} />
            <SignalPill tone={autoRefresh ? "success" : "neutral"} label="刷新" value={autoRefresh ? "5 秒" : "暂停"} />
            {view === "messages" ? (
              <SignalPill tone={counts.failed || counts.stuck ? "warn" : "success"} label="消息" value={`${messagesQ.data?.length ?? 0} 条`} />
            ) : view === "agent" ? (
              <SignalPill tone={agentStats.failed ? "warn" : "success"} label="Agent" value={`${agentRunsQ.data?.length ?? 0} 轮`} />
            ) : view === "console" ? (
              <SignalPill tone={systemConsoleQ.data?.ok === false ? "warn" : "success"} label="控制台" value={`${systemConsoleQ.data?.lines.length ?? 0} 行`} />
            ) : (
              <SignalPill tone={runtimeStats.error || runtimeStats.warn ? "warn" : "success"} label="事件" value={`${runtimeLogsQ.data?.length ?? 0} 条`} />
            )}
          </>
        )}
        actions={(
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              if (view === "messages") messagesQ.refetch();
              else if (view === "agent") agentRunsQ.refetch();
              else if (view === "console") systemConsoleQ.refetch();
              else runtimeLogsQ.refetch();
            }}
          >
            <RefreshCw className="mr-1.5 h-4 w-4" />
            刷新
          </Button>
        )}
      />

      <LogViewSegment value={view} onChange={setView} />

      {view === "messages" ? <Card>
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
          <div className="grid grid-cols-2 gap-3 md:grid-cols-[minmax(0,1fr)_140px] xl:grid-cols-[180px_140px_minmax(0,1fr)] 2xl:grid-cols-[180px_140px_minmax(340px,1fr)_minmax(260px,0.8fr)]">
            <Field label="账号" controlId="message-log-account-filter">
              <Select id="message-log-account-filter" value={accountId} onChange={(event) => setAccountId(event.target.value)}>
                <option value="">全部账号</option>
                {accountsQ.data?.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.display_name || account.phone}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="时间" controlId="message-log-time-filter">
              <Select id="message-log-time-filter" value={timeRange} onChange={(event) => setTimeRange(event.target.value as TimeRange)}>
                {Object.entries(TIME_RANGE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </Select>
            </Field>
            <Field label="结果" className="col-span-2 md:col-span-2 xl:col-span-1">
              <VerdictSegment value={verdict} onChange={setVerdict} />
            </Field>
            <Field label="搜索" className="col-span-2 md:col-span-2 xl:col-span-3 2xl:col-span-1">
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
            <div className="grid grid-cols-2 gap-3 border-t pt-4 min-[560px]:grid-cols-3 xl:grid-cols-4">
              <Field label="插件">
                <PluginSelect value={pluginKey} onChange={setPluginKey} options={pluginOptions} />
              </Field>
              <Field label="来源">
                <Select value={sourceChannel} onChange={(event) => setSourceChannel(event.target.value)}>
                  <option value="">全部来源</option>
                  {MESSAGE_SOURCE_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="事件类型">
                <Select value={eventType} onChange={(event) => setEventType(event.target.value)}>
                  <option value="">全部事件</option>
                  {EVENT_TYPE_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="链路状态">
                <Select value={status} onChange={(event) => setStatus(event.target.value)}>
                  <option value="">全部状态</option>
                  {TRACE_STATUS_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="链路 ID">
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
                <Input value={reasonCode} onChange={(event) => setReasonCode(event.target.value.trim())} placeholder="例如 subscription_not_matched" />
              </Field>
              <Field label="会话 ID">
                <Input value={chatId} onChange={(event) => setChatId(event.target.value)} placeholder="-100..." />
              </Field>
              <Field label="消息 ID">
                <Input value={messageId} onChange={(event) => setMessageId(event.target.value)} placeholder="消息 ID" />
              </Field>
              <Field label="用户 ID">
                <Input value={senderUserId} onChange={(event) => setSenderUserId(event.target.value)} placeholder="发送者用户 ID" />
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
      </Card> : view === "agent" ? (
        <Card>
          <CardHeader>
            <SectionHeader
              icon={Bot}
              title="Agent 运行筛选"
              description="按状态和时间查看系统助手的持久运行记录，失败轮次可直接展开轨迹。"
            />
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-3 min-[560px]:grid-cols-3 xl:grid-cols-4">
              <Field label="状态">
                <Select value={agentStatus} onChange={(event) => setAgentStatus(parseAgentStatus(event.target.value))}>
                  <option value="">全部状态</option>
                  <option value="queued">排队中</option>
                  <option value="running">运行中</option>
                  <option value="succeeded">已完成</option>
                  <option value="failed">失败</option>
                  <option value="cancelled">已取消</option>
                </Select>
              </Field>
              <Field label="时间">
                <Select value={timeRange} onChange={(event) => setTimeRange(event.target.value as TimeRange)}>
                  {Object.entries(TIME_RANGE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="条数">
                <Select value={String(runtimeLimit)} onChange={(event) => setRuntimeLimit(parseRuntimeLimit(event.target.value))}>
                  <option value="100">最近 100 轮</option>
                  <option value="300">最近 300 轮</option>
                  <option value="500">最近 500 轮</option>
                </Select>
              </Field>
              <Field label="自动刷新">
                <div className="flex h-10 items-center gap-2">
                  <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} />
                  <span className="text-sm text-muted-foreground">{autoRefresh ? "开启" : "关闭"}</span>
                </div>
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
          </CardContent>
        </Card>
      ) : view === "console" ? (
        <Card>
          <CardHeader>
            <SectionHeader
              icon={Terminal}
              title="系统控制台筛选"
              description="读取生产容器 stdout/stderr，类似在服务器执行 docker compose logs。"
            />
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-3 min-[560px]:grid-cols-3 md:grid-cols-[220px_160px_minmax(240px,1fr)_160px_180px]">
              <Field label="服务">
                <Select value={consoleService} onChange={(event) => setConsoleService(parseConsoleService(event.target.value))}>
                  {SYSTEM_CONSOLE_SERVICE_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="行数">
                <Select value={String(runtimeLimit)} onChange={(event) => setRuntimeLimit(parseRuntimeLimit(event.target.value))}>
                  <option value="100">最近 100 行</option>
                  <option value="300">最近 300 行</option>
                  <option value="500">最近 500 行</option>
                </Select>
              </Field>
              <Field label="搜索">
                <SearchBox value={keyword} onChange={setKeyword} placeholder="搜索原始日志行" />
              </Field>
              <Field label="自动刷新">
                <div className="flex h-10 items-center gap-2">
                  <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} />
                  <span className="text-sm text-muted-foreground">{autoRefresh ? "开启" : "关闭"}</span>
                </div>
              </Field>
              <Field label="复制">
                <Button
                  type="button"
                  variant="outline"
                  className="w-full justify-start"
                  onClick={() => copySystemConsoleLogs(systemConsoleQ.data, timezone)}
                >
                  <Copy className="mr-1.5 h-4 w-4" />
                  复制已加载日志
                </Button>
              </Field>
            </div>
            <p className="rounded-md border bg-muted/30 px-3 py-2 text-xs leading-5 text-muted-foreground">
              这里展示的是容器控制台输出；如果要按账号、插件或 JSON 字段查结构化事件，请切到“运行事件”。
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <SectionHeader
              icon={Search}
              title="运行事件筛选"
              description="用于排查后台结构化事件，支持按等级、来源、账号、插件和关键词过滤。"
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
            <div className="grid grid-cols-2 gap-3 min-[560px]:grid-cols-3 md:grid-cols-[minmax(0,1fr)_160px_160px_minmax(240px,1fr)]">
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
              <Field label="等级">
                <Select value={runtimeLevel} onChange={(event) => setRuntimeLevel(parseRuntimeLevel(event.target.value))}>
                  <option value="">全部等级</option>
                  {RUNTIME_LEVEL_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="来源">
                <Select value={runtimeSource} onChange={(event) => setRuntimeSource(parseRuntimeSource(event.target.value))}>
                  <option value="">全部来源</option>
                  {RUNTIME_SOURCE_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="搜索">
                <SearchBox value={keyword} onChange={setKeyword} />
              </Field>
            </div>

            <div className="grid grid-cols-2 gap-3 min-[560px]:grid-cols-3 xl:grid-cols-4">
              <Field label="时间">
                <Select value={timeRange} onChange={(event) => setTimeRange(event.target.value as TimeRange)}>
                  {Object.entries(TIME_RANGE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="条数">
                <Select value={String(runtimeLimit)} onChange={(event) => setRuntimeLimit(parseRuntimeLimit(event.target.value))}>
                  <option value="100">最近 100 条</option>
                  <option value="300">最近 300 条</option>
                  <option value="500">最近 500 条</option>
                </Select>
              </Field>
              <Field label="自动刷新">
                <div className="flex h-10 items-center gap-2">
                  <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} />
                  <span className="text-sm text-muted-foreground">{autoRefresh ? "开启" : "关闭"}</span>
                </div>
              </Field>
              <Field label="复制">
                <Button
                  type="button"
                  variant="outline"
                  className="w-full justify-start"
                  onClick={() => copyRuntimeLogs(runtimeLogsQ.data ?? [], timezone)}
                >
                  <Copy className="mr-1.5 h-4 w-4" />
                  复制当前结果
                </Button>
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
              <div className="grid grid-cols-2 gap-3 border-t pt-4 min-[560px]:grid-cols-3 xl:grid-cols-4">
                <Field label="插件">
                  <PluginSelect value={pluginKey} onChange={setPluginKey} options={pluginOptions} />
                </Field>
              </div>
            ) : null}
          </CardContent>
        </Card>
      )}

      {view === "messages" ? (
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
      ) : view === "agent" ? (
        <AgentRunStream
          runs={agentRunsQ.data ?? []}
          loading={agentRunsQ.isLoading}
          error={agentRunsQ.error}
          timezone={timezone}
          stats={agentStats}
        />
      ) : view === "console" ? (
        <SystemConsoleStream
          data={systemConsoleQ.data}
          loading={systemConsoleQ.isLoading}
          error={systemConsoleQ.error}
          service={consoleService}
          keyword={keyword}
          timezone={timezone}
        />
      ) : (
        <RuntimeEventStream
          logs={runtimeLogsQ.data ?? []}
          loading={runtimeLogsQ.isLoading}
          error={runtimeLogsQ.error}
          timezone={timezone}
          keyword={keyword}
          stats={runtimeStats}
        />
      )}
    </PageShell>
  );
}

function LogViewSegment({ value, onChange }: { value: LogView; onChange: (value: LogView) => void }) {
  const items: { value: LogView; label: string; icon: typeof ScrollText }[] = [
    { value: "messages", label: "消息流", icon: ScrollText },
    { value: "agent", label: "Agent 运行", icon: Bot },
    { value: "console", label: "系统控制台", icon: Terminal },
    { value: "runtime", label: "运行事件", icon: Workflow },
  ];
  return (
    <Tabs className="min-w-0" value={value} onValueChange={(next) => onChange(next as LogView)}>
      <TabsList className="grid w-full grid-cols-2 sm:grid-cols-4">
        {items.map((item) => {
          const Icon = item.icon;
          return (
            <TabsTrigger
              key={item.value}
              value={item.value}
              className="min-w-0 gap-1.5 px-2 sm:gap-2"
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="truncate">{item.label}</span>
            </TabsTrigger>
          );
        })}
      </TabsList>
    </Tabs>
  );
}

function VerdictSegment({ value, onChange }: { value: VerdictFilter; onChange: (value: VerdictFilter) => void }) {
  const items: { value: VerdictFilter; label: string }[] = [
    { value: "", label: "全部" },
    { value: "responded", label: "已响应" },
    { value: "no_response_normal", label: "正常跳过" },
    { value: "stuck", label: "卡住" },
    { value: "failed", label: "失败" },
  ];
  return (
    <Tabs className="w-full min-w-0" value={value || "all"} onValueChange={(next) => onChange(next === "all" ? "" : (next as VerdictFilter))}>
      <TabsList className="w-full flex-nowrap justify-stretch sm:w-full">
        {items.map((item) => (
          <TabsTrigger key={item.label} value={item.value || "all"} className="min-w-0 flex-1 px-1 sm:min-w-0 sm:px-2">
            <span>{item.label}</span>
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
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
          description="每行直接显示收到、路由、执行、发送四段状态。"
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

function SystemConsoleStream({
  data,
  loading,
  error,
  service,
  keyword,
  timezone,
}: {
  data?: SystemConsoleLogsResponse;
  loading: boolean;
  error?: unknown;
  service: string;
  keyword: string;
  timezone?: string;
}) {
  const lines = data?.lines ?? [];
  const services = data?.services?.length ? data.services.map(systemConsoleServiceLabel) : [systemConsoleServiceLabel(service)];
  const [levelFilter, setLevelFilter] = useState<ConsoleLevelFilter>("all");
  const [followLatest, setFollowLatest] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const parsedLines = useMemo(
    () => lines.map((line) => parseSystemConsoleLine(line, timezone)),
    [lines, timezone],
  );
  const levelCounts = useMemo(
    () => parsedLines.reduce(
      (counts, line) => ({ ...counts, [line.level]: counts[line.level] + 1 }),
      { debug: 0, info: 0, warn: 0, error: 0 },
    ),
    [parsedLines],
  );
  const records = useMemo(
    () => groupSystemConsoleLines(
      levelFilter === "all" ? parsedLines : parsedLines.filter((line) => line.level === levelFilter),
    ),
    [levelFilter, parsedLines],
  );
  const visibleLineCount = records.reduce((count, record) => count + record.lines.length, 0);

  useEffect(() => {
    if (!followLatest) return;
    const frame = window.requestAnimationFrame(() => {
      const node = scrollRef.current;
      if (node) node.scrollTop = node.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [followLatest, lines.length, levelFilter]);

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Terminal}
          title="系统控制台"
          description="原样展示服务 stdout/stderr，排查启动失败、异常堆栈和部署输出时看这里。"
          meta={(
            <div className="flex min-w-0 flex-wrap gap-1.5">
              <SignalPill tone={data?.ok === false ? "warn" : "success"} label="来源" value={systemConsoleSourceLabel(data?.source)} />
              <div className="flex min-w-0 flex-wrap items-center gap-1.5 rounded-full border border-border/70 bg-muted/40 px-3 py-1.5 text-xs">
                <span className="shrink-0 text-muted-foreground">服务</span>
                {services.map((item) => (
                  <span key={item} className="max-w-full break-words font-medium text-foreground">
                    {item}
                  </span>
                ))}
              </div>
            </div>
          )}
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <InlineLoading />
        ) : error ? (
          <ErrorHint text="系统控制台加载失败" error={error} />
        ) : data?.ok === false ? (
          <ErrorHint text="系统控制台暂不可用" error={data.error || "当前环境没有暴露系统级日志源"} />
        ) : lines.length ? (
          <div className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950 text-zinc-100 shadow-inner">
            <div className="space-y-2 border-b border-white/10 px-3 py-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-zinc-400">
                <span className="min-w-0 break-all font-mono">$ docker compose logs --tail={data?.tail ?? 300} {service === "all" ? "" : service}</span>
                <span>{visibleLineCount}/{lines.length} 行</span>
              </div>
              <div className="flex max-w-full gap-1 overflow-x-auto pb-0.5" role="group" aria-label="控制台日志等级">
                {([
                  ["all", "全部", lines.length],
                  ["debug", "DEBUG", levelCounts.debug],
                  ["info", "INFO", levelCounts.info],
                  ["warn", "WARNING", levelCounts.warn],
                  ["error", "ERROR", levelCounts.error],
                ] as const).map(([value, label, count]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={levelFilter === value}
                    onClick={() => setLevelFilter(value)}
                    className={cn(
                      "shrink-0 rounded-md border border-white/10 px-2 py-1 font-mono text-[10px] text-zinc-400 transition-colors hover:bg-white/10 hover:text-zinc-100",
                      levelFilter === value && "border-white/20 bg-white/10 text-zinc-100",
                      value === "info" && levelFilter === value && "text-lime-300",
                      value === "warn" && levelFilter === value && "text-amber-300",
                      value === "error" && levelFilter === value && "text-red-300",
                      value === "debug" && levelFilter === value && "text-violet-300",
                    )}
                  >
                    {label} <span className="opacity-60">{count}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="relative">
              <div
                ref={scrollRef}
                className="max-h-[640px] space-y-2 overflow-auto p-3 font-mono text-[11px] leading-5"
                onScroll={(event) => {
                  const node = event.currentTarget;
                  setFollowLatest(node.scrollHeight - node.scrollTop - node.clientHeight < 40);
                }}
              >
                {records.map((record, index) => (
                  <SystemConsoleRecord key={`${record.key}-${index}`} record={record} keyword={keyword} />
                ))}
              </div>
              {!followLatest ? (
                <Button
                  type="button"
                  size="sm"
                  className="absolute bottom-3 right-3 h-8 rounded-full px-3 text-xs shadow-lg"
                  onClick={() => {
                    const node = scrollRef.current;
                    if (node) node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
                    setFollowLatest(true);
                  }}
                >
                  <ArrowDown className="mr-1 h-3.5 w-3.5" />
                  查看最新
                </Button>
              ) : null}
            </div>
          </div>
        ) : (
          <EmptyHint text="当前条件下没有系统控制台日志" />
        )}
      </CardContent>
    </Card>
  );
}

type ParsedSystemConsoleLine = {
  raw: string;
  service: string;
  timestamp: string;
  timeKey: string;
  inlineTime: string;
  level: ConsoleLogLevel;
  body: string;
};

type SystemConsoleRecordData = {
  key: string;
  service: string;
  timestamp: string;
  level: ConsoleLogLevel;
  lines: ParsedSystemConsoleLine[];
};

function SystemConsoleRecord({ record, keyword }: { record: SystemConsoleRecordData; keyword: string }) {
  const levelLabel = consoleLevelLabel(record.level);
  return (
    <div className="grid min-w-0 gap-1.5 sm:grid-cols-[9.5rem_minmax(0,1fr)] sm:gap-3">
      <div className="hidden pt-2 tabular-nums text-violet-300 sm:block">{record.timestamp || "无时间"}</div>
      <div
        className={cn(
          "relative min-w-0 overflow-hidden rounded-xl border border-white/10 bg-zinc-900/55 py-2 pl-4 pr-3",
          record.level === "error" && "border-red-400/25 bg-red-500/5",
          record.level === "warn" && "border-amber-400/20 bg-amber-500/5",
        )}
        data-console-level={record.level}
      >
        <span className={cn(
          "absolute inset-y-2 left-2 w-1 rounded-full bg-zinc-500",
          record.level === "debug" && "bg-violet-400",
          record.level === "info" && "bg-lime-500",
          record.level === "warn" && "bg-amber-400",
          record.level === "error" && "bg-red-400",
        )} />
        <div className="mb-1 flex flex-wrap items-center gap-2 pl-1 text-[10px] sm:hidden">
          <span className="tabular-nums text-violet-300">{record.timestamp || "无时间"}</span>
          {record.service ? <span className="text-zinc-500">{record.service}</span> : null}
        </div>
        <div className="space-y-0.5 pl-1">
          {record.lines.map((line, index) => (
            <div key={`${index}-${line.raw.slice(0, 24)}`} className="grid min-w-0 grid-cols-[4.6rem_minmax(0,1fr)] gap-2" title={`原始日志：${line.raw}`}>
              <span className={cn(
                "font-semibold",
                line.level === "debug" && "text-violet-300",
                line.level === "info" && "text-lime-400",
                line.level === "warn" && "text-amber-300",
                line.level === "error" && "text-red-300",
              )}>
                {levelLabel}:
              </span>
              <span className="min-w-0 whitespace-pre-wrap break-words text-zinc-300">
                <span className="mr-2 tabular-nums text-zinc-500">{line.inlineTime}</span>
                <HighlightedMessage text={line.body || " "} keyword={keyword} />
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function AgentRunStream({
  runs,
  loading,
  error,
  timezone,
  stats,
}: {
  runs: SystemAgentRun[];
  loading: boolean;
  error?: unknown;
  timezone?: string;
  stats: ReturnType<typeof countAgentRuns>;
}) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Bot}
          title="Agent 运行"
          description="每一轮系统助手请求都会保留状态与事件轨迹，失败时优先查看错误代码和最后几步。"
          meta={(
            <div className="flex flex-wrap gap-1.5">
              <SignalPill tone="neutral" label="运行中" value={String(stats.running)} />
              <SignalPill tone="success" label="完成" value={String(stats.succeeded)} />
              <SignalPill tone={stats.failed ? "warn" : "neutral"} label="失败" value={String(stats.failed)} />
            </div>
          )}
        />
      </CardHeader>
      <CardContent className="space-y-2">
        {loading ? (
          <InlineLoading />
        ) : error ? (
          <ErrorHint text="Agent 运行记录加载失败" error={error} />
        ) : runs.length ? (
          runs.map((run) => {
            const failed = run.status === "failed" || Boolean(run.error_code || run.error_message);
            return (
              <div
                key={run.id}
                className={cn(
                  "rounded-lg border bg-background p-3",
                  failed && "border-destructive/30 bg-destructive/5",
                  run.status === "running" && "border-info/30 bg-info/5",
                )}
              >
                <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant={agentRunBadgeVariant(run.status)}>{agentRunStatusLabel(run.status)}</Badge>
                      <span className="text-xs text-muted-foreground">
                        {run.kind === "retry"
                          ? "重试"
                          : run.kind === "regenerate"
                            ? "重新生成"
                            : "问答"}
                      </span>
                      {run.user_message_id != null ? (
                        <span className="text-xs text-muted-foreground">消息 #{run.user_message_id}</span>
                      ) : null}
                    </div>
                    <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">{run.id}</p>
                    <p className="mt-0.5 break-all text-xs text-muted-foreground">会话 {run.session_id}</p>
                  </div>
                  <div className="shrink-0 text-left text-xs text-muted-foreground sm:text-right">
                    <div>{formatDateTime(run.created_at || run.started_at, timezone)}</div>
                    <div className="mt-0.5">{agentRunElapsed(run)}</div>
                  </div>
                </div>
                {run.error_message || run.error_code ? (
                  <div className="mt-2 rounded-md border border-destructive/20 bg-background/70 px-2.5 py-2 text-xs text-destructive">
                    {run.error_code ? <div className="font-mono text-[11px]">{run.error_code}</div> : null}
                    {run.error_message ? <div className="mt-0.5 break-words">{run.error_message}</div> : null}
                  </div>
                ) : null}
                <RunTrace
                  runId={run.id}
                  runStatus={run.status}
                  lastSeq={run.last_seq}
                  running={run.status === "running"}
                  defaultOpen={false}
                  mode="diagnostic"
                  timezone={timezone}
                  className="mt-2"
                />
              </div>
            );
          })
        ) : (
          <EmptyHint text="当前条件下没有 Agent 运行记录" />
        )}
      </CardContent>
    </Card>
  );
}

function RuntimeEventStream({
  logs,
  loading,
  error,
  timezone,
  keyword,
  stats,
}: {
  logs: RuntimeLogItem[];
  loading: boolean;
  error?: unknown;
  timezone?: string;
  keyword: string;
  stats: ReturnType<typeof countRuntimeLogs>;
}) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Terminal}
          title="运行事件"
          description="TelePilot 写入数据库的结构化运行事件，点开单行可查看完整详情 JSON。"
          meta={(
            <div className="flex flex-wrap gap-1.5">
              <SignalPill tone="neutral" label="调试" value={String(stats.debug)} />
              <SignalPill tone={stats.warn ? "warn" : "neutral"} label="告警" value={String(stats.warn)} />
              <SignalPill tone={stats.error ? "warn" : "neutral"} label="错误" value={String(stats.error)} />
            </div>
          )}
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <InlineLoading />
        ) : error ? (
          <ErrorHint text="运行事件加载失败" error={error} />
        ) : logs.length ? (
          <div className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950 text-zinc-100 shadow-inner">
            <div className="hidden grid-cols-[170px_74px_96px_120px_minmax(0,1fr)_92px] gap-3 border-b border-white/10 px-3 py-2 text-[11px] font-medium tracking-wide text-zinc-400 xl:grid">
              <span>时间</span>
              <span>等级</span>
              <span>来源</span>
              <span>账号</span>
              <span>内容</span>
              <span className="text-right">详情</span>
            </div>
            <div className="divide-y divide-white/10">
              {logs.map((log) => (
                <RuntimeEventRow key={log.id} log={log} timezone={timezone} keyword={keyword} />
              ))}
            </div>
          </div>
        ) : (
          <EmptyHint text="当前条件下没有运行事件" />
        )}
      </CardContent>
    </Card>
  );
}

function RuntimeEventRow({ log, timezone, keyword }: { log: RuntimeLogItem; timezone?: string; keyword: string }) {
  const [expanded, setExpanded] = useState(false);
  const detailText = log.detail ? safeJsonStringify(log.detail, 2) : "";
  const pluginKey = pickString(log.detail, ["plugin_key", "plugin.key"]);
  const traceId = pickString(log.detail, ["trace_id", "trace.id"]);
  const errorCode = pickString(log.detail, ["error_code", "reason_code", "code"]);
  const level = normalizeRuntimeLevel(log.level);
  return (
    <div className={cn("transition", runtimeRowClass(level))}>
      <div
        role="button"
        tabIndex={0}
        className="w-full min-w-0 px-3 py-2 text-left hover:bg-white/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/35"
        onClick={() => detailText && setExpanded((value) => !value)}
        onKeyDown={(event) => {
          if (!detailText || (event.key !== "Enter" && event.key !== " ")) return;
          event.preventDefault();
          setExpanded((value) => !value);
        }}
      >
        <div className="grid min-w-0 grid-cols-1 gap-1.5 xl:grid-cols-[170px_74px_96px_120px_minmax(0,1fr)_92px] xl:items-start xl:gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2 xl:block">
            <span className="font-mono text-[11px] text-zinc-400">{formatRuntimeTime(log.created_at, timezone)}</span>
            <RuntimeLevelBadge level={log.level} className="xl:hidden" />
          </div>
          <div className="hidden xl:block">
            <RuntimeLevelBadge level={log.level} />
          </div>
          <div className="min-w-0 text-xs text-zinc-300">{runtimeSourceLabel(log.source)}</div>
          <div className="min-w-0 font-mono text-xs text-zinc-400">{log.account_id == null ? "-" : `#${log.account_id}`}</div>
          <div className="min-w-0">
            <p className="break-words font-mono text-xs leading-5 text-zinc-100">
              <HighlightedMessage text={log.message || "-"} keyword={keyword} />
            </p>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {pluginKey ? <span className="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[11px] text-zinc-400">插件 {pluginKey}</span> : null}
              {traceId ? <span className="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[11px] text-zinc-400">{traceId}</span> : null}
              {errorCode ? <span className="rounded border border-warning/30 px-1.5 py-0.5 font-mono text-[11px] text-warning">{errorCode}</span> : null}
            </div>
          </div>
          <div className="flex items-center justify-between gap-2 xl:justify-end">
            <div className="flex items-center gap-1.5">
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8 border border-white/20 bg-white/10 text-zinc-100 hover:bg-white hover:text-zinc-950"
                onClick={(event) => {
                  event.stopPropagation();
                  copyRuntimeLog(log, timezone);
                }}
                aria-label="复制日志"
              >
                <Copy className="h-4 w-4" />
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={!detailText}
                className={cn(
                  "h-8 min-w-[88px] border text-xs font-semibold",
                  detailText
                    ? "border-white/30 bg-white text-zinc-950 shadow-sm hover:bg-zinc-200 dark:bg-zinc-100 dark:text-zinc-950 dark:hover:bg-white"
                    : "border-white/10 bg-transparent text-zinc-500 opacity-70",
                )}
                onClick={(event) => {
                  event.stopPropagation();
                  if (detailText) setExpanded((value) => !value);
                }}
              >
                {detailText ? (expanded ? "收起详情" : "查看详情") : "无详情"}
                {detailText ? <ChevronDown className={cn("ml-1 h-4 w-4 transition-transform", expanded ? "rotate-180" : "")} /> : null}
              </Button>
            </div>
          </div>
        </div>
      </div>
      {expanded && detailText ? (
        <div className="border-t border-white/10 bg-black/30 p-3">
          <pre className="max-h-96 overflow-auto rounded-md border border-white/10 bg-black/40 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all text-zinc-200">
            {detailText}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function RuntimeLevelBadge({ level, className }: { level?: string | null; className?: string }) {
  const normalized = normalizeRuntimeLevel(level);
  const variant = normalized === "error" ? "destructive" : normalized === "warn" ? "warn" : normalized === "debug" ? "secondary" : "success";
  return (
    <Badge variant={variant} className={cn("font-mono", className)}>
      {runtimeLevelLabel(level)}
    </Badge>
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
  const messageText = message.text_preview || message.inline_query || message.chosen_inline_query || eventTypeLabel(message.event_type);
  const pluginText = pluginKeysLabel(message.plugin_keys, message.plugin_count);
  const detailLabel = selected ? "收起详情" : "查看详情";
  return (
    <div className={cn("group overflow-hidden rounded-lg border bg-background transition", selected ? "border-primary/60 shadow-sm" : "border-border hover:border-primary/40 hover:shadow-sm")}>
      <button type="button" className="w-full min-w-0 cursor-pointer p-3 text-left" onClick={onSelect} aria-expanded={selected}>
        <div className="grid min-w-0 grid-cols-1 gap-3 2xl:grid-cols-[150px_minmax(180px,1.1fr)_minmax(260px,1.8fr)_300px_112px] 2xl:items-center">
          <div className="flex min-w-0 items-center justify-between gap-2 2xl:block">
            <span className="text-xs text-muted-foreground 2xl:hidden">{formatDateTime(message.started_at, timezone)}</span>
            <span className="hidden text-xs text-muted-foreground 2xl:inline">{formatDateTime(message.started_at, timezone)}</span>
            <VerdictBadge verdict={message.verdict} className="2xl:hidden" />
          </div>
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <Badge variant="secondary">{channelLabel(message.source_channel)}</Badge>
              <Badge variant="outline">{eventTypeLabel(message.event_type)}</Badge>
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
              {message.sender_user_id ? <span>用户 {message.sender_user_id}</span> : null}
              {message.message_id ? <span>消息 {message.message_id}</span> : null}
              {pluginText ? <span>插件 {pluginText}</span> : null}
              {message.action_count ? <span>动作 {message.action_count}</span> : null}
            </div>
          </div>
          <FunelStrip message={message} />
          <div className="hidden flex-col items-end gap-2 2xl:flex">
            <VerdictBadge verdict={message.verdict} />
            <span className="inline-flex items-center gap-1 rounded-full border border-primary bg-primary px-2.5 py-1 text-[11px] font-semibold text-primary-foreground shadow-sm transition group-hover:bg-primary/90">
              <MousePointerClick className="h-3 w-3" />
              {detailLabel}
            </span>
          </div>
        </div>
        <div className={cn("mt-3 rounded-md border px-3 py-2 text-sm transition group-hover:ring-1 group-hover:ring-primary/25", meta.panelClass)}>
          <div className="flex flex-wrap items-start justify-between gap-2">
            <span className="font-medium">{message.reason_text}</span>
            <span className="inline-flex items-center gap-1 rounded-full border border-primary bg-primary px-2.5 py-1 text-[11px] font-semibold text-primary-foreground shadow-sm 2xl:hidden">
              <MousePointerClick className="h-3 w-3" />
              {detailLabel}
            </span>
          </div>
          <p className="mt-1 text-xs leading-5 opacity-90">{message.next_step}</p>
          {message.reason_code ? <p className="mt-1 font-mono text-[11px] opacity-70">{message.reason_code}</p> : null}
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
          <div className="mt-0.5 truncate text-center text-[11px] opacity-80">
            {funnelStageLabel(stage.key, stage.value)}
          </div>
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
      <section className={cn("nested-surface border bg-background", verdict?.panelClass)}>
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={detail.status} />
              {message ? <VerdictBadge verdict={message.verdict} /> : <Badge variant="secondary">无判定</Badge>}
              <span className="break-all font-mono text-xs text-muted-foreground">{detail.trace_id}</span>
            </div>
            <p className="mt-2 text-sm font-medium">{message?.reason_text || "后端消息流未返回本条判定"}</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{message?.next_step || "请从消息流列表进入详情，或用链路 ID 重新筛选。"}</p>
          </div>
          <div className="shrink-0 text-xs text-muted-foreground">{formatDateTime(detail.started_at, timezone)}</div>
        </div>
      </section>

      <OperationalTraceSummary detail={detail} message={message} />
      {detail.text_preview ? <p className="nested-surface border bg-background text-sm whitespace-pre-wrap">{detail.text_preview}</p> : null}
      <Timeline spans={detail.spans} actions={detail.actions} timezone={timezone} />
      <details className="nested-surface border bg-background">
        <summary className="cursor-pointer text-sm font-medium">插件开发详情</summary>
        <div className="mt-3 space-y-3">
          <p className="rounded-md bg-muted px-3 py-2 text-xs leading-5 text-muted-foreground">
            这里是给插件开发和深度排障看的原始材料：触发入口建议、动作建议、标准信封路径和原始 payload。日常排障优先看上面的摘要与关键时间线。
          </p>
          <InlineTraceSummary trace={detail} actions={detail.actions} />
          <NativeRawSummary meta={detail.native_raw_meta} />
          <ProbeReportPanel report={detail.probe_report} />
          <JsonBlock title="原生数据设置 native_raw_meta" value={detail.native_raw_meta} />
          <JsonBlock title="原始摘要 raw_summary" value={detail.raw_summary} />
          <JsonBlock title="消息快照 payload_snapshot" value={detail.payload_snapshot} />
          {detail.related_runtime_logs.length ? <JsonBlock title="关联运行事件 related_runtime_logs" value={detail.related_runtime_logs} /> : null}
        </div>
      </details>
    </div>
  );
}

function OperationalTraceSummary({ detail, message }: { detail: EventTraceDetail; message?: MessageFunelItem }) {
  const pluginText = pluginKeysLabel(detail.plugin_keys, detail.plugin_count);
  const sentText = sendSummary(detail.actions, message);
  return (
    <section className="nested-surface border bg-background">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <MessageSquareText className="h-4 w-4 text-primary" />
        <span className="text-sm font-medium">排障摘要</span>
      </div>
      <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <InfoCell label="会话" value={conversationLabel(detail)} />
        <InfoCell label="发送者" value={actorLabel(detail)} />
        <InfoCell label="处理插件" value={pluginText || "未进入插件"} />
        <InfoCell label="发送结果" value={sentText} />
      </div>
      <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <InfoCell label="入口" value={`${channelLabel(detail.source_channel)} / ${eventTypeLabel(detail.event_type)}`} />
        <InfoCell label="消息 ID" value={detail.message_id ?? "-"} />
        <InfoCell label="耗时" value={detail.duration_ms == null ? "-" : `${detail.duration_ms}ms`} />
        <InfoCell label="链路 ID" value={detail.trace_id} />
      </div>
    </section>
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
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="border-primary/30 bg-primary/5 text-primary hover:bg-primary/10"
            onClick={() => setShowAll((value) => !value)}
          >
            {showAll ? "只看关键" : `显示全部 ${items.length} 项`}
          </Button>
        ) : null}
      </div>
      <div className="space-y-2">
        {visibleItems.map((item, index) => (
          <div key={`${item.kind}-${index}-${item.ts}`} className={cn("nested-surface border bg-background", timelineItemClass(item))}>
            {item.kind === "span" ? (
              <div className="space-y-1.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={item.span.status} />
                    <Badge variant="secondary">{phaseLabel(item.span.phase)}</Badge>
                    {item.span.plugin_key ? <Badge variant="secondary">{item.span.plugin_key}</Badge> : null}
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
                    <Badge variant="secondary">{actionTypeLabel(item.action.action_type)}</Badge>
                    {item.action.actual_send_via ? <Badge variant="secondary">{channelLabel(item.action.actual_send_via)}</Badge> : null}
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
    <section className="nested-surface border bg-background">
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
            <div key={`${item.title}-${index}`} className="nested-surface nested-surface-inset-2 border bg-muted/20">
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
        <div className="nested-surface nested-surface-inset-2 mt-3 border border-warning/40 bg-warning/10 text-sm text-warning">
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
    <div className="nested-surface nested-surface-inset-2 border bg-muted/20">
      <div className="mb-2 text-xs font-medium text-muted-foreground">{title}</div>
      {items.length ? (
        <div className="space-y-2">
          {items.slice(0, 8).map((item, index) => (
            <div key={`${item.path || item.label}-${index}`} className="nested-surface-item bg-background px-2 py-1.5">
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
    <div className="nested-surface nested-surface-inset-2 border bg-muted/20">
      <div className="mb-2 text-xs font-medium text-muted-foreground">{title}</div>
      {items.length ? items.map((item, index) => {
        const jsonValue = item[jsonKey];
        return (
          <div key={`${item.title}-${index}`} className={index ? "mt-3 border-t pt-3" : ""}>
            <div className="text-sm font-medium">{item.title}</div>
            {item.reason ? <p className="mt-1 text-xs text-muted-foreground">{item.reason}</p> : null}
            {jsonValue ? (
              <pre className="nested-surface-item mt-2 max-h-52 overflow-auto bg-background p-2 text-xs leading-relaxed whitespace-pre-wrap break-all">
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
    <div className={cn("rounded-lg border p-2", item.matched ? "border-success/25 bg-success/10" : "bg-muted/20")}>
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge status={item.matched ? "ok" : item.status || "skipped"} />
        {item.phase ? <Badge variant="secondary">{phaseLabel(item.phase)}</Badge> : null}
        {item.plugin_key ? <Badge variant="secondary">{item.plugin_key}</Badge> : null}
        {item.entry_key ? <Badge variant="secondary">{item.entry_key}</Badge> : null}
      </div>
      <div className="mt-1 text-xs text-muted-foreground">{item.reason_code || item.message || "路由阶段已记录"}</div>
      {item.filters ? <div className="mt-1 break-all font-mono text-xs text-muted-foreground">过滤条件={stringifyShort(item.filters)}</div> : null}
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
      <div className="mb-2 font-medium text-foreground">Inline 查询摘要</div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <InfoCell label="查询内容" value={query || "-"} />
        <InfoCell label="选择结果 ID" value={chosen || "-"} />
        <InfoCell label="选择时查询" value={choiceQuery || "-"} />
        <InfoCell label="回答结果数" value={resultCount == null ? "-" : resultCount} />
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
      <div className="mb-2 font-medium text-foreground">原生数据摘要</div>
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

function Field({
  label,
  children,
  className,
  controlId,
}: {
  label: string;
  children: ReactNode;
  className?: string;
  controlId?: string;
}) {
  return (
    <div className={cn("min-w-0 space-y-1.5", className)}>
      <Label htmlFor={controlId}>{label}</Label>
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
      <Input value={value} onChange={(event) => onChange(event.target.value.trim())} placeholder="插件标识" />
    </div>
  );
}

function SearchBox({ value, onChange, placeholder = "会话 / 消息 / 发送者 / 链路 ID" }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  return (
    <div className="relative">
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <Input className="pl-9" value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
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
  return <Badge variant={variant}>{traceStatusLabel(status)}</Badge>;
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
    <div className="nested-surface-item bg-muted px-2 py-1.5">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="break-all text-xs text-foreground">{String(value ?? "-")}</div>
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
  return <EmptyState title={text} size="sm" />;
}

function HighlightedMessage({ text, keyword }: { text: string; keyword: string }) {
  const q = keyword.trim();
  if (!q) return <>{text}</>;
  const index = text.toLowerCase().indexOf(q.toLowerCase());
  if (index < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, index)}
      <mark className="rounded bg-warning/30 px-0.5 text-foreground">{text.slice(index, index + q.length)}</mark>
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
  if (isWarnStatus(item.span.status) || isProblemTimelineItem(item)) return "border-warning/40 bg-warning/10";
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

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

function parseTimeRange(value: string | null): TimeRange {
  if (value === "15m" || value === "6h" || value === "24h" || value === "custom") return value;
  return "1h";
}

function safeIntegerFilter(value: string, allowNegative = true): string | undefined {
  const normalized = value.trim();
  if (!normalized) return undefined;
  if (!(allowNegative ? /^-?\d+$/ : /^\d+$/).test(normalized)) return undefined;
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed) || (!allowNegative && parsed <= 0)) return undefined;
  return normalized;
}

function parseLogView(value: string | null): LogView {
  if (value === "agent" || value === "console" || value === "runtime") return value;
  return "messages";
}

function parseAgentStatus(value: string | null): AgentStatusFilter {
  if (
    value === "queued" ||
    value === "running" ||
    value === "succeeded" ||
    value === "failed" ||
    value === "cancelled"
  ) return value;
  return "";
}

function parseRuntimeLevel(value: string | null): RuntimeLevelFilter {
  const normalized = (value || "").toLowerCase();
  if (normalized === "debug" || normalized === "info" || normalized === "warn" || normalized === "warning" || normalized === "error") return normalized;
  return "";
}

function parseRuntimeSource(value: string | null): RuntimeSourceFilter {
  const normalized = (value || "").toLowerCase();
  if (normalized === "system" || normalized === "event" || normalized === "plugin") return normalized;
  return "";
}

function parseRuntimeLimit(value: string | null): number {
  const parsed = Number(value);
  if (parsed === 100 || parsed === 300 || parsed === 500) return parsed;
  return 300;
}

function parseConsoleService(value: string | null): string {
  const normalized = (value || "all").toLowerCase();
  return SYSTEM_CONSOLE_SERVICE_OPTIONS.some((item) => item.value === normalized) ? normalized : "all";
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

function countRuntimeLogs(logs: RuntimeLogItem[]) {
  return logs.reduce(
    (acc, item) => {
      const level = normalizeRuntimeLevel(item.level);
      acc[level] += 1;
      return acc;
    },
    { debug: 0, info: 0, warn: 0, error: 0 },
  );
}

function countAgentRuns(runs: SystemAgentRun[]) {
  return runs.reduce(
    (acc, run) => {
      if (run.status === "running" || run.status === "queued") acc.running += 1;
      else if (run.status === "succeeded") acc.succeeded += 1;
      else if (run.status === "failed" || run.status === "cancelled") acc.failed += 1;
      return acc;
    },
    { running: 0, succeeded: 0, failed: 0 },
  );
}

function agentRunStatusLabel(status: string): string {
  if (status === "queued") return "排队中";
  if (status === "running") return "运行中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  return status || "未知";
}

function agentRunBadgeVariant(status: string): "secondary" | "success" | "destructive" | "warn" {
  if (status === "succeeded") return "success";
  if (status === "failed") return "destructive";
  if (status === "cancelled") return "warn";
  return "secondary";
}

function agentRunElapsed(run: SystemAgentRun): string {
  const started = Date.parse(run.started_at || run.created_at || "");
  const ended = Date.parse(run.finished_at || run.updated_at || "");
  if (!Number.isFinite(started) || !Number.isFinite(ended) || ended < started) return "耗时 -";
  const ms = ended - started;
  return `耗时 ${ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`}`;
}

function normalizeRuntimeLevel(level?: string | null): "debug" | "info" | "warn" | "error" {
  const value = (level || "info").toLowerCase();
  if (value === "debug") return "debug";
  if (value === "warn" || value === "warning") return "warn";
  if (value === "error" || value === "critical" || value === "fatal") return "error";
  return "info";
}

function runtimeRowClass(level: ReturnType<typeof normalizeRuntimeLevel>): string {
  if (level === "error") return "bg-destructive/10";
  if (level === "warn") return "bg-warning/10";
  if (level === "debug") return "bg-info/5";
  return "";
}

function formatRuntimeTime(iso?: string | null, tz?: string | null): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  try {
    return date.toLocaleString("zh-CN", {
      timeZone: tz || "Asia/Shanghai",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return formatDateTime(iso, tz);
  }
}

async function copyRuntimeLogs(logs: RuntimeLogItem[], timezone?: string) {
  if (!logs.length) {
    toast.info("当前没有可复制的运行事件");
    return;
  }
  await copyText(logs.map((log) => formatRuntimeLog(log, timezone)).join("\n"), `已复制 ${logs.length} 条运行事件`);
}

async function copyRuntimeLog(log: RuntimeLogItem, timezone?: string) {
  await copyText(formatRuntimeLog(log, timezone), "已复制该条日志");
}

async function copySystemConsoleLogs(data?: SystemConsoleLogsResponse, timezone?: string) {
  const lines = data?.lines ?? [];
  if (!lines.length) {
    toast.info("当前没有可复制的系统控制台日志");
    return;
  }
  await copyText(lines.map((line) => formatSystemConsoleLine(line, timezone)).join("\n"), `已复制 ${lines.length} 行系统控制台日志`);
}

const DOCKER_ISO_TIMESTAMP_RE = /\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z\b/;

function formatSystemConsoleTimestamp(rawTimestamp: string, timezone?: string): string {
  const match = DOCKER_ISO_TIMESTAMP_RE.exec(rawTimestamp);
  if (!match) return rawTimestamp;
  const millis = (match[2] || "").slice(0, 3).padEnd(3, "0");
  const normalized = `${match[1]}.${millis}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return rawTimestamp;
  try {
    const local = date.toLocaleString("zh-CN", {
      timeZone: timezone || "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).replaceAll("/", "-");
    return `${local}.${millis}`;
  } catch {
    return rawTimestamp;
  }
}

function formatSystemConsoleLine(line: string, timezone?: string): string {
  const match = DOCKER_ISO_TIMESTAMP_RE.exec(line);
  return match ? line.replace(match[0], formatSystemConsoleTimestamp(match[0], timezone)) : line;
}

function parseSystemConsoleLine(line: string, timezone?: string): ParsedSystemConsoleLine {
  const level = consoleLineLevel(line);
  const serviceMatch = /^([^|]+?)\s*\|\s*/.exec(line);
  const service = serviceMatch?.[1]?.trim() || "";
  const withoutService = serviceMatch ? line.slice(serviceMatch[0].length) : line;
  const timestampMatch = DOCKER_ISO_TIMESTAMP_RE.exec(withoutService);
  const timestamp = timestampMatch ? formatSystemConsoleTimestamp(timestampMatch[0], timezone) : "";
  const bodyWithLevel = (timestampMatch
    ? withoutService.replace(timestampMatch[0], "")
    : withoutService).trim();
  const body = bodyWithLevel.replace(/^(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s*:\s*/i, "");
  return {
    raw: line,
    service,
    timestamp,
    timeKey: timestamp.replace(/\.\d{3}$/, ""),
    inlineTime: timestamp.split(" ").at(-1) || "--:--:--",
    level,
    body,
  };
}

function groupSystemConsoleLines(lines: ParsedSystemConsoleLine[]): SystemConsoleRecordData[] {
  const records: SystemConsoleRecordData[] = [];
  for (const line of lines) {
    const previous = records.at(-1);
    if (previous && previous.level === line.level && previous.service === line.service && previous.key === `${line.timeKey}|${line.service}|${line.level}`) {
      previous.lines.push(line);
      continue;
    }
    records.push({
      key: `${line.timeKey}|${line.service}|${line.level}`,
      service: line.service,
      timestamp: line.timeKey,
      level: line.level,
      lines: [line],
    });
  }
  return records;
}

async function copyText(text: string, message: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(message);
  } catch {
    toast.error("复制失败，请检查浏览器剪贴板权限");
  }
}

function formatRuntimeLog(log: RuntimeLogItem, timezone?: string): string {
  const parts = [
    `[${formatRuntimeTime(log.created_at, timezone)}]`,
    runtimeLevelLabel(log.level),
    runtimeSourceLabel(log.source),
    log.account_id == null ? "" : `账号=#${log.account_id}`,
    log.message || "",
  ].filter(Boolean);
  const detail = log.detail ? ` 详情=${safeJsonStringify(log.detail)}` : "";
  return `${parts.join(" ")}${detail}`;
}

function systemConsoleServiceLabel(service?: string | null): string {
  const value = (service || "all").toLowerCase();
  return SYSTEM_CONSOLE_SERVICE_OPTIONS.find((item) => item.value === value)?.label || value;
}

function systemConsoleSourceLabel(source?: string | null): string {
  const value = (source || "").toLowerCase();
  if (value === "docker_compose") return "Docker";
  if (value === "docker_containers") return "Docker 容器";
  if (value === "local_files") return "本地日志";
  if (value === "unavailable") return "不可用";
  return source || "系统";
}

function consoleLineLevel(line: string): "debug" | "info" | "warn" | "error" {
  const lowered = line.toLowerCase();
  if (/(error|exception|traceback|failed|fatal|\bpanic\b)/i.test(lowered)) return "error";
  if (/(warn|warning|retry|timeout)/i.test(lowered)) return "warn";
  if (/(debug|verbose)/i.test(lowered)) return "debug";
  return "info";
}

function consoleLevelLabel(level: ConsoleLogLevel): string {
  if (level === "warn") return "WARNING";
  return level.toUpperCase();
}

function verdictMeta(verdict: MessageVerdict) {
  const map = {
    responded: {
      label: "已响应",
      badgeVariant: "success" as const,
      icon: CheckCircle2,
      panelClass: "border-success/20 bg-success/10",
    },
    no_response_normal: {
      label: "正常跳过",
      badgeVariant: "secondary" as const,
      icon: Circle,
      panelClass: "border-border bg-muted/40",
    },
    stuck: {
      label: "卡住",
      badgeVariant: "warn" as const,
      icon: Clock,
      panelClass: "border-warning/25 bg-warning/10",
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
  if (status === "pass") return "border-success/20 bg-success/10 text-success";
  if (status === "fail") return "border-destructive/30 bg-destructive/5 text-destructive";
  if (status === "stuck") return "border-warning/25 bg-warning/10 text-warning";
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

function funnelStageLabel(
  stage: keyof MessageFunelItem["funel"],
  status: MessageFunelStage,
): string {
  if (status !== "pass" && status !== "none") return stageLabel(status);
  if (stage === "received" && status === "pass") return "已接收";
  if (stage === "routed" && status === "pass") return "已匹配";
  if (stage === "ran" && status === "pass") return "已处理";
  if (stage === "sent" && status === "pass") return "已发送";
  if (stage === "sent" && status === "none") return "无动作";
  return stageLabel(status);
}

function channelLabel(channel?: string | null): string {
  if (channel === "interaction_bot") return "交互 Bot";
  if (channel === "interaction_bot_reply") return "交互 Bot 回复";
  if (channel === "userbot") return "UserBot";
  if (channel === "userbot_reply") return "UserBot 回复";
  if (channel === "account_bot") return "管理 Bot";
  if (channel === "scheduler") return "定时任务";
  if (channel === "external_payment_notice") return "转账通知";
  return channel || "未知来源";
}

function eventTypeLabel(eventType?: string | null): string {
  const value = (eventType || "").toLowerCase();
  const known = EVENT_TYPE_OPTIONS.find((item) => item.value === value);
  if (known) return known.label;
  if (value === "all_messages") return "全部消息";
  if (value === "all_events") return "全部事件";
  return eventType || "未知事件";
}

function traceStatusLabel(status?: string | null): string {
  const value = (status || "unknown").toLowerCase();
  if (value === "ok" || value === "success") return "完成";
  if (value === "running" || value === "received" || value === "normalized" || value === "matched" || value === "delivered") return "处理中";
  if (value === "skipped") return "已跳过";
  if (value === "warning" || value === "warn") return "告警";
  if (value === "failed" || value === "error") return "失败";
  if (value === "active") return "可用";
  return status || "未知";
}

function runtimeLevelLabel(level?: string | null): string {
  const value = normalizeRuntimeLevel(level);
  if (value === "debug") return "调试";
  if (value === "warn") return "告警";
  if (value === "error") return "错误";
  return "信息";
}

function runtimeSourceLabel(source?: string | null): string {
  const value = (source || "").toLowerCase();
  if (value === "system" || value === "worker" || value === "runtime") return "系统";
  if (value === "event") return "事件链路";
  if (value === "plugin") return "插件";
  return source || "运行时";
}

function phaseLabel(phase?: string | null): string {
  const value = (phase || "").toLowerCase();
  if (value.includes("receive")) return "收到消息";
  if (value.includes("subscription") || value.includes("route")) return "路由判断";
  if (value.includes("plugin_invoke")) return "插件执行";
  if (value.includes("plugin_return")) return "插件返回";
  if (value.includes("contract")) return "契约检查";
  if (value.includes("delivery") || value.includes("send")) return "发送处理";
  if (value.includes("settlement")) return "收付款处理";
  if (value.includes("session")) return "会话处理";
  return phase || "阶段记录";
}

function actionTypeLabel(actionType?: string | null): string {
  const value = (actionType || "").toLowerCase();
  if (value === "send_message") return "发送消息";
  if (value === "edit_message") return "编辑消息";
  if (value === "delete_message") return "删除消息";
  if (value === "answer_inline_query") return "Inline 回答";
  if (value === "payout") return "结算付款";
  if (value === "start_session") return "开启会话";
  if (value === "close_session") return "关闭会话";
  return actionType || "动作记录";
}

function conversationLabel(trace: EventTraceSummary): string {
  if (trace.chat_id != null) {
    const kind = trace.chat_id < 0 ? "群" : "私";
    return trace.chat_title ? `${kind} ${trace.chat_title} / ${trace.chat_id}` : `${kind} ${trace.chat_id}`;
  }
  if (trace.sender_name) return trace.sender_name;
  if (trace.sender_user_id != null) return `用户 ${trace.sender_user_id}`;
  return "未知会话";
}

function actorLabel(trace: EventTraceSummary): string {
  const parts = [
    trace.sender_name || "",
    trace.sender_user_id != null ? `用户 ${trace.sender_user_id}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" / ") : "未知发送者";
}

function pluginKeysLabel(keys?: string[] | null, count?: number): string {
  const clean = Array.from(new Set((keys ?? []).map((item) => String(item).trim()).filter(Boolean)));
  if (clean.length) {
    const visible = clean.slice(0, 3).join("、");
    return clean.length > 3 ? `${visible} 等 ${clean.length} 个` : visible;
  }
  return count ? `${count} 个` : "";
}

function sendSummary(actions: EventActionItem[], message?: MessageFunelItem): string {
  if (!actions.length) {
    return message?.funel.sent === "none" ? "未产生发送动作" : stageLabel(message?.funel.sent || "none");
  }
  const failed = actions.filter((item) => isFailedStatus(item.status) || item.error_code || item.error_message);
  if (failed.length) return `${failed.length} 个发送动作失败`;
  const sent = actions.filter((item) => item.action_type === "send_message" || item.telegram_message_id != null);
  if (sent.length) return `已发送 ${sent.length} 条`;
  return `已记录 ${actions.length} 个动作`;
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

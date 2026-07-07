import { useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Copy,
  MessageSquareText,
  MousePointerClick,
  Puzzle,
  Search,
  ScrollText,
  ServerCog,
  ShieldCheck,
  TerminalSquare,
  Workflow,
} from "lucide-react";

import { listAccounts } from "@/api/accounts";
import { getFeatureMatrix } from "@/api/features";
import {
  getEventTrace,
  getHealthOverview,
  getPluginRuntimeDetail,
  getSystemSettings,
  getTraceOverview,
  listAuditLogs,
  listCommandTraces,
  listEventActions,
  listEventTraces,
  listPluginRuntimeStatus,
  listRuntimeLogs,
} from "@/api/system";
import type {
  AuditLogItem,
  EventActionItem,
  EventProbeReport,
  EventProbeRoutingItem,
  EventProbeSuggestion,
  EventTraceDetail,
  EventSpanItem,
  EventTraceSummary,
  HealthOverview,
  PluginRuntimeDetail,
  PluginRuntimeStatusItem,
  RuntimeLogItem,
  TraceOverview,
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatDateTime } from "@/lib/utils";

type MainTab = "overview" | "events" | "plugins" | "commands" | "actions" | "raw";
type RuntimeSourceFilter = "" | "event" | "plugin" | "system";
type RuntimeLevelFilter = "" | "debug" | "info" | "warn" | "error";
type NormalizedRuntimeLevel = "debug" | "info" | "warn" | "error" | "unknown";
type RawTab = "runtime" | "audit";
type DiagnosisTone = "success" | "warn" | "danger" | "neutral";

interface DiagnosisResult {
  tone: DiagnosisTone;
  title: string;
  message: string;
  nextStep: string;
  reasonCode?: string | null;
  pluginKey?: string | null;
  entryKey?: string | null;
  traceId?: string | null;
}

type TimelineItem =
  | { kind: "span"; ts: string; span: EventSpanItem }
  | { kind: "action"; ts: string; action: EventActionItem };
type QuickCheckStage = {
  key: "receive" | "route" | "plugin" | "send";
  title: string;
  tone: DiagnosisTone;
  status: string;
  detail: string;
};

const RUNTIME_LEVEL_RANK: Record<string, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  warning: 2,
  error: 3,
};

const LOW_VALUE_RUNTIME_PATTERNS = [
  "允许群组名单已热更新",
  "插件配置已热更新",
  "账号 Bot 配置已热更新",
  "配置已热更新",
  "reload_config 完成",
  "reload_config completed",
  "hot reload 完成",
];

const LOW_VALUE_AUDIT_ACTIONS = new Set([
  "auth.login",
  "auth.logout",
]);

const NORMAL_REASON_CODES = new Set([
  "matched",
  "command_matched",
  "callback_query",
  "session_control_action",
]);

function parseMainTab(value: string | null): MainTab {
  if (value === "events" || value === "plugins" || value === "commands" || value === "actions" || value === "raw") {
    return value;
  }
  return "raw";
}

export function Logs() {
  const [searchParams] = useSearchParams();
  const initialTraceId = searchParams.get("trace_id") || "";
  const [mainTab, setMainTab] = useState<MainTab>(() => {
    const tab = parseMainTab(searchParams.get("tab"));
    return initialTraceId && tab === "raw" ? "events" : tab;
  });
  const [accountId, setAccountId] = useState(() => searchParams.get("account_id") || searchParams.get("aid") || "");
  const [keyword, setKeyword] = useState(() => searchParams.get("keyword") || "");
  const [status, setStatus] = useState(() => searchParams.get("status") || "");
  const [pluginKey, setPluginKey] = useState(() => searchParams.get("plugin_key") || "");
  const [eventType, setEventType] = useState(() => searchParams.get("event_type") || "");
  const [traceId, setTraceId] = useState(() => initialTraceId);
  const [reasonCode, setReasonCode] = useState(() => searchParams.get("reason_code") || searchParams.get("error_code") || "");
  const [chatId, setChatId] = useState(() => searchParams.get("chat_id") || "");
  const [messageId, setMessageId] = useState(() => searchParams.get("message_id") || "");
  const [senderUserId, setSenderUserId] = useState(() => searchParams.get("sender_user_id") || "");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selectedTraceId, setSelectedTraceId] = useState(() => initialTraceId);
  const [selectedPluginKey, setSelectedPluginKey] = useState(() => searchParams.get("plugin_key") || "");
  const traceIdFilter = traceId.trim();
  const reasonCodeFilter = reasonCode.trim();

  const accountsQ = useQuery({ queryKey: ["accounts"], queryFn: listAccounts });
  const matrixQ = useQuery({ queryKey: ["matrix"], queryFn: getFeatureMatrix });
  const settingsQ = useQuery({ queryKey: ["system", "settings"], queryFn: getSystemSettings });
  const timezone = settingsQ.data?.timezone || "";
  const commonTraceQuery = {
    account_id: accountId || undefined,
    keyword: keyword.trim() || undefined,
    status: status || undefined,
    plugin_key: pluginKey || undefined,
    event_type: eventType || undefined,
    trace_id: traceIdFilter || undefined,
    reason_code: reasonCodeFilter || undefined,
    chat_id: chatId.trim() || undefined,
    message_id: messageId.trim() || undefined,
    sender_user_id: senderUserId.trim() || undefined,
    since: localDateTimeToIso(since),
    until: localDateTimeToIso(until),
    limit: 100,
  };

  const overviewQ = useQuery({
    queryKey: ["logs", "trace", "overview", accountId],
    queryFn: () => getTraceOverview({ account_id: accountId || undefined }),
    refetchInterval: autoRefresh && mainTab === "overview" ? 5_000 : false,
  });
  const healthQ = useQuery({
    queryKey: ["system", "health-overview"],
    queryFn: getHealthOverview,
    refetchInterval: autoRefresh && mainTab === "overview" ? 10_000 : false,
  });
  const eventsQ = useQuery({
    queryKey: ["logs", "trace", "events", commonTraceQuery],
    queryFn: () => listEventTraces(commonTraceQuery),
    refetchInterval: autoRefresh && mainTab === "events" ? 5_000 : false,
  });
  const traceDetailQ = useQuery({
    queryKey: ["logs", "trace", "detail", selectedTraceId],
    queryFn: () => getEventTrace(selectedTraceId),
    enabled: Boolean(selectedTraceId),
  });
  const pluginsQ = useQuery({
    queryKey: ["logs", "trace", "plugins", accountId, pluginKey, status],
    queryFn: () =>
      listPluginRuntimeStatus({
        account_id: accountId || undefined,
        plugin_key: pluginKey || undefined,
        status: status || undefined,
        limit: 100,
      }),
    refetchInterval: autoRefresh && mainTab === "plugins" ? 5_000 : false,
  });
  const pluginDetailQ = useQuery({
    queryKey: ["logs", "trace", "plugin-detail", selectedPluginKey, accountId],
    queryFn: () => getPluginRuntimeDetail(selectedPluginKey, { account_id: accountId || undefined }),
    enabled: Boolean(selectedPluginKey),
  });
  const commandsQ = useQuery({
    queryKey: ["logs", "trace", "commands", accountId, keyword, since, until, reasonCodeFilter],
    queryFn: () => listCommandTraces({
      account_id: accountId || undefined,
      keyword: keyword || undefined,
      since: localDateTimeToIso(since),
      until: localDateTimeToIso(until),
      reason_code: reasonCodeFilter || undefined,
      limit: 100,
    }),
    refetchInterval: autoRefresh && mainTab === "commands" ? 5_000 : false,
  });
  const actionsQ = useQuery({
    queryKey: ["logs", "trace", "actions", accountId, pluginKey, status, traceIdFilter, reasonCodeFilter],
    queryFn: () =>
      listEventActions({
        account_id: accountId || undefined,
        plugin_key: pluginKey || undefined,
        status: status || undefined,
        trace_id: traceIdFilter || undefined,
        reason_code: reasonCodeFilter || undefined,
        limit: 100,
      }),
    refetchInterval: autoRefresh && mainTab === "actions" ? 5_000 : false,
  });

  return (
    <PageShell>
      <PageHeader
        title="日志中心"
        description="默认先看原始运行日志；需要排查某条消息为什么没触发、插件为什么没响应时，再用 trace_id 进入消息链路。"
        icon={ScrollText}
      />

      <Card>
        <CardHeader>
          <SectionHeader
            title="排查过滤"
            description="按账号、关键词、trace、Chat ID 或用户 ID 缩小范围；这些条件会同步作用到下方日志视图。"
            meta={(
              <SignalPill
                tone={autoRefresh ? "success" : "neutral"}
                label="刷新"
                value={autoRefresh ? "自动" : "暂停"}
              />
            )}
          />
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5 xl:items-end">
            <div className="space-y-1.5">
              <Label>账号</Label>
              <Select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                <option value="">全部账号</option>
                {accountsQ.data?.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.display_name || a.phone}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>状态</Label>
              <Select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">全部状态</option>
                <option value="ok">ok</option>
                <option value="running">running</option>
                <option value="skipped">skipped</option>
                <option value="warning">warning</option>
                <option value="failed">failed</option>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>插件</Label>
              <PluginSelect
                value={pluginKey}
                onChange={setPluginKey}
                options={matrixQ.data?.features.map((item) => item.key) ?? []}
              />
            </div>
            <div className="space-y-1.5">
              <Label>事件类型</Label>
              <Select value={eventType} onChange={(e) => setEventType(e.target.value)}>
                <option value="">全部事件</option>
                <option value="message">message</option>
                <option value="command">command</option>
                <option value="callback_query">callback_query</option>
                <option value="inline_query">inline_query</option>
                <option value="chosen_inline_result">chosen_inline_result</option>
                <option value="payment_confirmed">payment_confirmed</option>
                <option value="session_close">session_close</option>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>关键词</Label>
              <SearchInput value={keyword} onChange={setKeyword} />
            </div>
            <div className="space-y-1.5">
              <Label>Trace ID</Label>
              <div className="flex gap-2">
                <Input
                  className="min-w-0"
                  value={traceId}
                  onChange={(e) => {
                    const nextTraceId = e.target.value.trim();
                    setTraceId(nextTraceId);
                    setSelectedTraceId(nextTraceId);
                  }}
                  placeholder="evt_..."
                />
                {traceIdFilter ? (
                  <Button
                    type="button"
                    variant="outline"
                    className="shrink-0"
                    onClick={() => {
                      setTraceId("");
                      setSelectedTraceId("");
                    }}
                  >
                    清空
                  </Button>
                ) : null}
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>原因代码</Label>
              <Input
                value={reasonCode}
                onChange={(e) => setReasonCode(e.target.value.trim())}
                placeholder="send_channel_deprecated"
              />
            </div>
            <div className="space-y-1.5">
              <Label>Chat ID</Label>
              <Input value={chatId} onChange={(e) => setChatId(e.target.value)} placeholder="-100..." />
            </div>
            <div className="space-y-1.5">
              <Label>Message ID</Label>
              <Input value={messageId} onChange={(e) => setMessageId(e.target.value)} placeholder="消息 ID" />
            </div>
            <div className="space-y-1.5">
              <Label>用户 ID</Label>
              <Input value={senderUserId} onChange={(e) => setSenderUserId(e.target.value)} placeholder="sender user id" />
            </div>
            <div className="space-y-1.5">
              <Label>开始时间</Label>
              <Input type="datetime-local" value={since} onChange={(e) => setSince(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>结束时间</Label>
              <Input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>自动刷新</Label>
              <div className="flex h-10 items-center gap-2">
                <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} />
                <span className="text-sm text-muted-foreground">
                  {autoRefresh ? "5 秒" : "已暂停"}
                </span>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      <TraceQuickCheck
        traces={eventsQ.data ?? []}
        loading={eventsQ.isLoading}
        error={eventsQ.error}
        detail={traceDetailQ.data}
        detailLoading={traceDetailQ.isLoading}
        detailError={traceDetailQ.error}
        selectedTraceId={selectedTraceId}
        onSelectTrace={(nextTraceId) => {
          setSelectedTraceId(nextTraceId);
          setTraceId(nextTraceId);
          setMainTab("events");
        }}
        timezone={timezone}
      />

      <Tabs value={mainTab} onValueChange={(v) => setMainTab(v as MainTab)}>
        <TabsList className="flex h-auto flex-wrap justify-start gap-1">
          <TabsTrigger value="raw" className="gap-1.5">
            <ServerCog className="h-4 w-4" /> 原始日志
          </TabsTrigger>
          <TabsTrigger value="overview" className="gap-1.5">
            <Activity className="h-4 w-4" /> 总览
          </TabsTrigger>
          <TabsTrigger value="events" className="gap-1.5">
            <Workflow className="h-4 w-4" /> 消息链路
          </TabsTrigger>
          <TabsTrigger value="plugins" className="gap-1.5">
            <Puzzle className="h-4 w-4" /> 插件诊断
          </TabsTrigger>
          <TabsTrigger value="commands" className="gap-1.5">
            <TerminalSquare className="h-4 w-4" /> 命令链路
          </TabsTrigger>
          <TabsTrigger value="actions" className="gap-1.5">
            <MousePointerClick className="h-4 w-4" /> 动作发送
          </TabsTrigger>
        </TabsList>

        <LogToolGuide activeTab={mainTab} />

        <TabsContent value="overview">
          <OverviewPanel
            loading={overviewQ.isLoading}
            error={overviewQ.error}
            health={healthQ.data}
            healthLoading={healthQ.isLoading}
            healthError={healthQ.error}
            data={overviewQ.data}
            timezone={timezone}
            onTraceSelect={(traceId) => {
              setSelectedTraceId(traceId);
              setMainTab("events");
            }}
          />
        </TabsContent>

        <TabsContent value="events">
          <TraceExplorer
            traces={eventsQ.data ?? []}
            loading={eventsQ.isLoading}
            error={eventsQ.error}
            selectedTraceId={selectedTraceId}
            onSelectTrace={(nextTraceId) => {
              setSelectedTraceId(nextTraceId);
            }}
            detail={traceDetailQ.data}
            detailLoading={traceDetailQ.isLoading}
            detailError={traceDetailQ.error}
            timezone={timezone}
          />
        </TabsContent>

        <TabsContent value="plugins">
          <PluginDiagnostics
            plugins={pluginsQ.data ?? []}
            loading={pluginsQ.isLoading}
            error={pluginsQ.error}
            selectedPluginKey={selectedPluginKey}
            onSelectPlugin={setSelectedPluginKey}
            detail={pluginDetailQ.data}
            detailLoading={pluginDetailQ.isLoading}
            detailError={pluginDetailQ.error}
            timezone={timezone}
            onTraceSelect={(traceId) => {
              setSelectedTraceId(traceId);
              setMainTab("events");
            }}
          />
        </TabsContent>

        <TabsContent value="commands">
          <TraceListCard
            title="命令链路"
            description="管理员命令、sudo 命令和插件命令的调用链。"
            traces={commandsQ.data ?? []}
            loading={commandsQ.isLoading}
            error={commandsQ.error}
            selectedTraceId={selectedTraceId}
            onSelectTrace={(traceId) => {
              setSelectedTraceId(traceId);
              setMainTab("events");
            }}
            timezone={timezone}
          />
        </TabsContent>

        <TabsContent value="actions">
          <ActionsPanel
            actions={actionsQ.data ?? []}
            loading={actionsQ.isLoading}
            error={actionsQ.error}
            timezone={timezone}
            onTraceSelect={(traceId) => {
              setSelectedTraceId(traceId);
              setMainTab("events");
            }}
          />
        </TabsContent>

        <TabsContent value="raw">
          <RawLogsPanel
            accountId={accountId}
            pluginKey={pluginKey}
            keyword={keyword}
            runtimeMinLevel={settingsQ.data?.log_retention?.runtime_log_min_level}
            timezone={timezone}
          />
        </TabsContent>
      </Tabs>
    </PageShell>
  );
}

function TraceQuickCheck({
  traces,
  loading,
  error,
  detail,
  detailLoading,
  detailError,
  selectedTraceId,
  onSelectTrace,
  timezone,
}: {
  traces: EventTraceSummary[];
  loading: boolean;
  error?: unknown;
  detail?: EventTraceDetail;
  detailLoading: boolean;
  detailError?: unknown;
  selectedTraceId: string;
  onSelectTrace: (traceId: string) => void;
  timezone?: string;
}) {
  const stages = buildQuickCheckStages(detail, {
    loading: detailLoading,
    hasCandidates: traces.length > 0,
    hasSelectedTrace: Boolean(selectedTraceId),
  });
  const candidates = traces.slice(0, 4);
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Search}
          title="消息四段检查"
          description={selectedTraceId || "按当前过滤条件匹配最近消息"}
          meta={(
            <SignalPill
              tone={detail ? stageOverallTone(stages) : traces.length ? "warn" : "neutral"}
              label="候选"
              value={loading ? "加载中" : `${traces.length} 条`}
            />
          )}
        />
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? <ErrorHint text="消息链路加载失败" error={error} /> : null}
        {detailError ? <ErrorHint text="链路详情加载失败" error={detailError} /> : null}
        <div className="grid gap-2 md:grid-cols-4">
          {stages.map((stage) => (
            <div key={stage.key} className={`rounded-md border p-3 ${diagnosisToneClass(stage.tone)}`}>
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <QuickStageIcon stage={stage.key} />
                  <span className="truncate text-sm font-medium">{stage.title}</span>
                </div>
                <StatusBadge status={stage.status} />
              </div>
              <p className="mt-2 line-clamp-3 text-xs leading-5">{stage.detail}</p>
            </div>
          ))}
        </div>
        {detail ? (
          <div className="grid gap-2 text-xs text-muted-foreground md:grid-cols-5">
            <InfoCell label="Trace" value={detail.trace_id} />
            <InfoCell label="来源" value={`${detail.source_channel || "-"} / ${detail.event_type}`} />
            <InfoCell label="会话" value={detail.chat_id ?? "-"} />
            <InfoCell label="消息" value={detail.message_id ?? "-"} />
            <InfoCell label="时间" value={formatDateTime(detail.started_at, timezone)} />
          </div>
        ) : loading || detailLoading ? (
          <InlineLoading />
        ) : candidates.length ? (
          <div className="flex flex-wrap gap-2">
            {candidates.map((trace) => (
              <Button
                key={trace.trace_id}
                type="button"
                variant={trace.trace_id === selectedTraceId ? "default" : "outline"}
                size="sm"
                className="max-w-full justify-start"
                onClick={() => onSelectTrace(trace.trace_id)}
              >
                <span className="truncate font-mono text-xs">{trace.trace_id}</span>
              </Button>
            ))}
          </div>
        ) : (
          <EmptyHint text="当前过滤条件下没有消息链路" />
        )}
      </CardContent>
    </Card>
  );
}

function QuickStageIcon({ stage }: { stage: QuickCheckStage["key"] }) {
  if (stage === "receive") return <MessageSquareText className="h-4 w-4 shrink-0" />;
  if (stage === "route") return <Workflow className="h-4 w-4 shrink-0" />;
  if (stage === "plugin") return <Puzzle className="h-4 w-4 shrink-0" />;
  return <MousePointerClick className="h-4 w-4 shrink-0" />;
}

function OverviewPanel({
  loading,
  error,
  health,
  healthLoading,
  healthError,
  data,
  timezone,
  onTraceSelect,
}: {
  loading: boolean;
  error?: unknown;
  health?: HealthOverview;
  healthLoading: boolean;
  healthError?: unknown;
  data?: TraceOverview;
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  if (loading) return <LoadingCard />;
  if (error) return <ErrorCard title="Trace 总览加载失败" error={error} />;
  const overview = data ?? {
    last_5m_total: 0,
    last_5m_failed: 0,
    last_5m_warning: 0,
    source_channel_counts: {},
    recent_errors: [],
    recent_failed_actions: [],
    recent_plugin_errors: [],
  };
  return (
    <div className="space-y-4">
      <HealthOverviewStrip data={health} traceOverview={overview} loading={healthLoading} error={healthError} />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <MetricCard title="最近 5 分钟事件" value={overview.last_5m_total} icon={Activity} tone="primary" />
        <MetricCard title="失败事件" value={overview.last_5m_failed} icon={AlertTriangle} tone="danger" />
        <MetricCard title="警告事件" value={overview.last_5m_warning} icon={ShieldCheck} tone="warn" />
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <RecentTraceCard title="最近错误链路" traces={overview.recent_errors} timezone={timezone} onTraceSelect={onTraceSelect} />
        <RecentActionCard title="最近失败动作" actions={overview.recent_failed_actions} timezone={timezone} onTraceSelect={onTraceSelect} />
        <RecentPluginCard title="最近插件异常" plugins={overview.recent_plugin_errors} timezone={timezone} />
      </div>
    </div>
  );
}

function LogToolGuide({ activeTab }: { activeTab: MainTab }) {
  const guides: Record<MainTab, { title: string; text: string; tone: DiagnosisTone }> = {
    raw: {
      title: "先看连续日志",
      text: "查关键词、trace、Chat ID、Message ID；看到 open_trace 后进入消息链路看插件阶段和发送动作。",
      tone: "neutral",
    },
    events: {
      title: "查单条消息",
      text: "用于回答：这条消息有没有进 TelePilot、命中了哪个插件、卡在哪个阶段、为什么没响应。",
      tone: "warn",
    },
    plugins: {
      title: "查插件状态",
      text: "用于回答：插件有没有加载成功、最近一次调用是否失败、失败 trace 是哪一条。",
      tone: "warn",
    },
    commands: {
      title: "查命令触发",
      text: "用于回答：管理员命令是否被识别、权限是否通过、后续有没有进入插件或动作发送。",
      tone: "neutral",
    },
    actions: {
      title: "查发送动作",
      text: "用于回答：插件请求发消息、编辑、按钮或结算后，平台最终用哪个通道执行以及是否失败。",
      tone: "neutral",
    },
    overview: {
      title: "看整体健康",
      text: "用于快速确认 Worker、DB、Redis、交互 Bot 和最近异常数量，定位范围后再回到原始日志或消息链路。",
      tone: "neutral",
    },
  };
  const guide = guides[activeTab];
  return (
    <div className={`mt-3 rounded-md border px-3 py-2 text-sm ${diagnosisToneClass(guide.tone)}`}>
      <span className="font-medium">{guide.title}</span>
      <span className="ml-2 text-muted-foreground">{guide.text}</span>
    </div>
  );
}

function MetricCard({
  title,
  value,
  icon: Icon,
  tone,
}: {
  title: string;
  value: number;
  icon: typeof Activity;
  tone: "primary" | "warn" | "danger";
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        <Icon className={tone === "danger" ? "h-4 w-4 text-destructive" : tone === "warn" ? "h-4 w-4 text-amber-600" : "h-4 w-4 text-primary"} />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
      </CardContent>
    </Card>
  );
}

function HealthOverviewStrip({
  data,
  traceOverview,
  loading,
  error,
}: {
  data?: HealthOverview;
  traceOverview?: Pick<TraceOverview, "source_channel_counts">;
  loading: boolean;
  error?: unknown;
}) {
  if (loading) return <InlineLoading />;
  if (error) return <ErrorHint text="系统健康状态加载失败" error={error} />;
  if (!data) return null;
  const workerValue = `${data.workers.runtime_desired_running_alive}/${data.workers.runtime_desired_running}`;
  const workerOk = data.workers.runtime_desired_running === data.workers.runtime_desired_running_alive &&
    data.workers.runtime_failing === 0;
  const sourceCounts = traceOverview?.source_channel_counts ?? {};
  const userbotCount = sourceCounts.userbot ?? 0;
  const interactionBotCount = sourceCounts.interaction_bot ?? 0;
  const externalNoticeCount = sourceCounts.external_payment_notice ?? 0;
  return (
    <div className="grid grid-cols-1 gap-2 md:grid-cols-3 xl:grid-cols-6">
      <HealthTile
        label="DB"
        tone={data.db.ok && data.alembic.ok ? "success" : "danger"}
        value={data.db.ok ? "可用" : "异常"}
        detail={data.alembic.ok ? "迁移一致" : data.alembic.error || "迁移待同步"}
      />
      <HealthTile
        label="Redis"
        tone={data.redis.ok ? "success" : "danger"}
        value={data.redis.ok ? "可用" : "异常"}
        detail={data.redis.error || "队列连接正常"}
      />
      <HealthTile
        label="UserBot"
        tone={workerOk ? "success" : "danger"}
        value={workerValue}
        detail={data.workers.runtime_failing > 0 ? `${data.workers.runtime_failing} 个 worker 失败` : "期望运行 / 已在线"}
      />
      <HealthTile
        label="交互 Bot"
        tone={interactionBotCount > 0 ? "success" : "neutral"}
        value={`${interactionBotCount}`}
        detail={interactionBotCount > 0 ? "近 5 分钟链路活跃" : "近 5 分钟未见交互 Bot trace"}
      />
      <HealthTile
        label="转账通知来源"
        tone={externalNoticeCount > 0 ? "success" : "neutral"}
        value={`${externalNoticeCount}`}
        detail={externalNoticeCount > 0 ? "近 5 分钟有外部通知 trace" : "无近期外部转账通知 trace"}
      />
      <HealthTile
        label="UserBot 事件"
        tone={userbotCount > 0 ? "success" : data.workers.runtime_desired_running_alive > 0 ? "neutral" : "danger"}
        value={`${userbotCount}`}
        detail={userbotCount > 0 ? "近 5 分钟有 userbot trace" : "无近期 trace，按 worker 在线判断"}
      />
    </div>
  );
}

function HealthTile({
  label,
  tone,
  value,
  detail,
}: {
  label: string;
  tone: "success" | "danger" | "neutral";
  value: string;
  detail: string;
}) {
  const className = tone === "success"
    ? "border-emerald-200 bg-emerald-50/60"
    : tone === "danger"
      ? "border-destructive/30 bg-destructive/5"
      : "border-border bg-muted/30";
  return (
    <div className={`rounded-md border p-3 ${className}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        {tone === "success" ? (
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
        ) : tone === "danger" ? (
          <AlertTriangle className="h-4 w-4 text-destructive" />
        ) : (
          <Clock className="h-4 w-4 text-muted-foreground" />
        )}
      </div>
      <div className="mt-1 text-lg font-semibold">{value}</div>
      <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">{detail}</div>
    </div>
  );
}

function TraceExplorer({
  traces,
  loading,
  error,
  selectedTraceId,
  onSelectTrace,
  detail,
  detailLoading,
  detailError,
  timezone,
}: {
  traces: EventTraceSummary[];
  loading: boolean;
  error?: unknown;
  selectedTraceId: string;
  onSelectTrace: (traceId: string) => void;
  detail?: EventTraceDetail;
  detailLoading: boolean;
  detailError?: unknown;
  timezone?: string;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
      <TraceListCard
        title="消息链路"
        description="每条 Telegram 事件进入系统后的完整生命周期。"
        traces={traces}
        loading={loading}
        error={error}
        selectedTraceId={selectedTraceId}
        onSelectTrace={onSelectTrace}
        timezone={timezone}
      />
      <TraceDetailCard detail={detail} loading={detailLoading} error={detailError} timezone={timezone} />
    </div>
  );
}

function TraceListCard({
  title,
  description,
  traces,
  loading,
  error,
  selectedTraceId,
  onSelectTrace,
  timezone,
}: {
  title: string;
  description: string;
  traces: EventTraceSummary[];
  loading: boolean;
  error?: unknown;
  selectedTraceId: string;
  onSelectTrace: (traceId: string) => void;
  timezone?: string;
}) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title={title}
          description={description}
          meta={<SignalPill tone="neutral" label="窗口" value={`${traces.length} 条`} />}
        />
      </CardHeader>
      <CardContent className="space-y-2">
        {loading ? (
          <InlineLoading />
        ) : error ? (
          <ErrorHint text="链路列表加载失败" error={error} />
        ) : traces.length ? (
          traces.map((trace) => (
            <button
              key={trace.trace_id}
              type="button"
              onClick={() => onSelectTrace(trace.trace_id)}
              className={`w-full rounded-md border p-3 text-left transition hover:border-primary/50 ${
                selectedTraceId === trace.trace_id ? "border-primary bg-primary/5" : "border-border bg-background"
              }`}
            >
              <div className="flex min-w-0 items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={trace.status} />
                    <Badge variant="secondary">{trace.event_type}</Badge>
                    {trace.source_channel ? <Badge variant="secondary">{trace.source_channel}</Badge> : null}
                  </div>
                  <div className="mt-2 break-all font-mono text-xs text-muted-foreground">{trace.trace_id}</div>
                </div>
                <div className="shrink-0 text-right text-xs text-muted-foreground">
                  {formatDateTime(trace.started_at, timezone)}
                </div>
              </div>
              <div className="mt-2 line-clamp-2 text-sm">
                {trace.text_preview || trace.sender_name || "无文本摘要"}
              </div>
              <InlineTraceSummary trace={trace} compact />
              <div className="mt-2 flex flex-wrap gap-1.5 text-xs text-muted-foreground">
                <span>插件 {trace.plugin_count}</span>
                <span>动作 {trace.action_count}</span>
                <span>错误 {trace.error_count}</span>
                {trace.chat_id ? <span>chat {trace.chat_id}</span> : null}
                <NativeRawMetaPill meta={trace.native_raw_meta} />
              </div>
            </button>
          ))
        ) : (
          <EmptyHint text="当前过滤条件下没有链路记录" />
        )}
      </CardContent>
    </Card>
  );
}

function TraceDetailCard({
  detail,
  loading,
  error,
  timezone,
}: {
  detail?: EventTraceDetail;
  loading: boolean;
  error?: unknown;
  timezone?: string;
}) {
  if (!detail && loading) return <LoadingCard />;
  if (error) return <ErrorCard title="链路详情加载失败" error={error} />;
  if (!detail) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>链路详情</CardTitle>
          <CardDescription>选择左侧一条 trace 查看时间线。</CardDescription>
        </CardHeader>
        <CardContent>
          <EmptyHint text="尚未选择 trace" />
        </CardContent>
      </Card>
    );
  }
  const diagnosis = buildTraceDiagnosis(detail);
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title="链路详情"
          description={detail.trace_id}
          meta={(
            <div className="flex flex-wrap justify-end gap-1.5">
              <StatusBadge status={detail.status} />
              <SignalPill tone="neutral" label="耗时" value={detail.duration_ms == null ? "-" : `${detail.duration_ms}ms`} />
            </div>
          )}
        />
      </CardHeader>
      <CardContent className="space-y-4">
        <TraceDiagnosisPanel detail={detail} diagnosis={diagnosis} timezone={timezone} />
        <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground md:grid-cols-4">
          <InfoCell label="事件" value={detail.event_type} />
          <InfoCell label="来源" value={detail.source_channel || "-"} />
          <InfoCell label="会话" value={detail.chat_id ?? "-"} />
          <InfoCell label="消息" value={detail.message_id ?? "-"} />
        </div>
        {detail.text_preview ? <p className="rounded-md bg-muted p-3 text-sm whitespace-pre-wrap">{detail.text_preview}</p> : null}
        <InlineTraceSummary trace={detail} actions={detail.actions} />
        <NativeRawSummary meta={detail.native_raw_meta} />
        <ProbeReportPanel report={detail.probe_report} />
        <Timeline spans={detail.spans} actions={detail.actions} timezone={timezone} />
        <details className="rounded-md border p-3">
          <summary className="cursor-pointer text-sm font-medium">高级数据</summary>
          <div className="mt-3 space-y-3">
            <JsonBlock title="native_raw_meta" value={detail.native_raw_meta} />
            <JsonBlock title="raw_summary" value={detail.raw_summary} />
            <JsonBlock title="payload_snapshot" value={detail.payload_snapshot} />
            {detail.related_runtime_logs.length ? (
              <JsonBlock title="related_runtime_logs" value={detail.related_runtime_logs} />
            ) : null}
          </div>
        </details>
      </CardContent>
    </Card>
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
    <section className="rounded-md border bg-muted/20 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Workflow className="h-4 w-4 text-primary" />
            <span className="font-medium">开发探针</span>
            <Badge variant="secondary">{report.headline}</Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            把当前 trace 翻译成标准字段路径、订阅建议、动作建议和能力边界。
          </p>
        </div>
        {warnings.length ? <SignalPill tone="warn" label="提示" value={`${warnings.length} 条`} /> : null}
      </div>

      {fieldPaths.length ? (
        <div className="mt-3">
          <div className="mb-2 text-xs font-medium text-muted-foreground">标准信封路径</div>
          <ProbeItemGrid items={fieldPaths} />
        </div>
      ) : null}

      {messageFacts.length ? (
        <div className="mt-3">
          <div className="mb-2 text-xs font-medium text-muted-foreground">消息补充摘要</div>
          <ProbeItemGrid items={messageFacts} />
        </div>
      ) : null}

      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <ProbeSuggestionList title="订阅建议" items={subscriptions} jsonKey="manifest" />
        <ProbeSuggestionList title="动作建议" items={actions} jsonKey="action" />
      </div>

      {capabilities.length ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {capabilities.map((item, index) => (
            <div key={`${item.title}-${index}`} className="rounded-md border bg-background/70 p-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <StatusBadge status={item.level || "info"} />
                <span className="text-sm font-medium">{item.title}</span>
                {item.capability ? <Badge variant="secondary">{item.capability}</Badge> : null}
              </div>
              {item.reason ? <p className="mt-1 text-xs text-muted-foreground">{item.reason}</p> : null}
              {item.reason_code ? <p className="mt-1 text-xs text-muted-foreground">{reasonDisplay(item.reason_code)}</p> : null}
            </div>
          ))}
        </div>
      ) : null}

      {routing.length ? (
        <div className="mt-3">
          <div className="mb-2 text-xs font-medium text-muted-foreground">路由解释</div>
          <div className="space-y-2">
            {routing.slice(0, 8).map((item, index) => (
              <ProbeRoutingRow key={`${item.phase}-${item.plugin_key}-${index}`} item={item} />
            ))}
          </div>
        </div>
      ) : null}

      {warnings.length ? (
        <div className="mt-3 rounded-md border border-amber-300/70 bg-amber-50/70 p-2 text-sm text-amber-950">
          {warnings.map((item, index) => (
            <div key={`${item}-${index}`}>{item}</div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function ProbeItemGrid({ items }: { items: EventProbeReport["field_paths"] }) {
  return (
    <div className="grid gap-2 md:grid-cols-2">
      {items.slice(0, 12).map((item, index) => (
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
  );
}

function ProbeSuggestionList({
  title,
  items,
  jsonKey,
}: {
  title: string;
  items: EventProbeSuggestion[];
  jsonKey: "manifest" | "action";
}) {
  return (
    <div className="rounded-md border bg-background/70 p-2">
      <div className="mb-2 text-xs font-medium text-muted-foreground">{title}</div>
      {items.length ? items.map((item, index) => {
        const jsonValue = item[jsonKey];
        return (
          <div key={`${item.title}-${index}`} className={index ? "mt-3 border-t pt-3" : ""}>
            <div className="text-sm font-medium">{item.title}</div>
            {item.reason ? <p className="mt-1 text-xs text-muted-foreground">{item.reason}</p> : null}
            {jsonValue ? (
              <pre className="mt-2 max-h-52 overflow-auto rounded-md bg-muted p-2 text-xs leading-relaxed whitespace-pre-wrap break-all">
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
    <div className={`rounded-md border p-2 ${item.matched ? "border-emerald-200 bg-emerald-50/50" : "bg-background/70"}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge status={item.matched ? "ok" : item.status || "skipped"} />
        {item.phase ? <Badge variant="secondary">{item.phase}</Badge> : null}
        {item.plugin_key ? <Badge variant="secondary">{item.plugin_key}</Badge> : null}
        {item.entry_key ? <Badge variant="secondary">{item.entry_key}</Badge> : null}
      </div>
      <div className="mt-1 text-xs text-muted-foreground">
        {item.reason_code ? reasonDisplay(item.reason_code) : item.message || "路由阶段已记录"}
      </div>
      {item.filters ? <div className="mt-1 break-all font-mono text-xs text-muted-foreground">filters={stringifyShort(item.filters)}</div> : null}
    </div>
  );
}

function TraceDiagnosisPanel({
  detail,
  diagnosis,
  timezone,
}: {
  detail: EventTraceDetail;
  diagnosis: DiagnosisResult;
  timezone?: string;
}) {
  const pluginKeys = Array.from(new Set(detail.spans.map((span) => span.plugin_key).filter(Boolean))).join(", ");
  const entryKeys = Array.from(new Set(detail.spans.map((span) => span.entry_key).filter(Boolean))).join(", ");
  return (
    <section className={`rounded-md border p-3 ${diagnosisToneClass(diagnosis.tone)}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={detail.status} />
            <span className="font-medium">排查结论：{diagnosis.title}</span>
          </div>
          <p className="mt-2 text-sm">{diagnosis.message}</p>
        </div>
        <span className="shrink-0 text-xs text-muted-foreground">{formatDateTime(detail.started_at, timezone)}</span>
      </div>
      <div className="mt-3 grid gap-2 text-xs md:grid-cols-4">
        <InfoCell label="触发来源" value={`${detail.source_channel || "-"} / ${detail.event_type}`} />
        <InfoCell label="发起人" value={detail.sender_name || detail.sender_user_id || "-"} />
        <InfoCell label="命中插件" value={pluginKeys || (detail.plugin_count ? `${detail.plugin_count} 个` : "未命中")} />
        <InfoCell label="入口" value={entryKeys || diagnosis.entryKey || "-"} />
      </div>
      {diagnosis.reasonCode || diagnosis.pluginKey ? (
        <div className="mt-2 flex flex-wrap gap-1.5 text-xs text-muted-foreground">
          {diagnosis.pluginKey ? <span>插件 {diagnosis.pluginKey}</span> : null}
          {diagnosis.entryKey ? <span>入口 {diagnosis.entryKey}</span> : null}
          {diagnosis.reasonCode ? <span>{reasonDisplay(diagnosis.reasonCode)}</span> : null}
        </div>
      ) : null}
      <div className="mt-3 rounded-md border border-current/10 bg-background/60 px-3 py-2 text-sm">
        下一步：{diagnosis.nextStep}
      </div>
    </section>
  );
}

function Timeline({
  spans,
  actions,
  timezone,
}: {
  spans: EventSpanItem[];
  actions: EventActionItem[];
  timezone?: string;
}) {
  const [showAll, setShowAll] = useState(false);
  const items = useMemo<TimelineItem[]>(() => [
    ...spans.map((span) => ({ kind: "span" as const, ts: span.started_at, span })),
    ...actions.map((action) => ({ kind: "action" as const, ts: action.created_at, action })),
  ].sort((a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime()), [actions, spans]);
  const visibleItems = useMemo(() => showAll ? items : pickDiagnosticTimelineItems(items), [items, showAll]);
  const hiddenCount = Math.max(0, items.length - visibleItems.length);
  const problemCount = items.filter(isProblemTimelineItem).length;

  if (!items.length) return <EmptyHint text="该 trace 暂无 span/action 明细" />;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium">
          关键时间线
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {problemCount ? `${problemCount} 个异常/告警` : "默认隐藏低价值阶段"}
          </span>
        </div>
        {hiddenCount || showAll ? (
          <Button type="button" variant="ghost" size="sm" onClick={() => setShowAll((v) => !v)}>
            {showAll ? "只看关键" : `显示全部 ${items.length} 项`}
          </Button>
        ) : null}
      </div>
      {visibleItems.map((item, index) => (
        <div key={`${item.kind}-${index}-${item.ts}`} className={`rounded-md border p-3 ${timelineItemClass(item)}`}>
          {item.kind === "span" ? (
            <div className="space-y-1">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-1.5">
                  <StatusBadge status={item.span.status} />
                  <Badge variant="secondary">{item.span.phase}</Badge>
                  {item.span.component ? <Badge variant="secondary">{item.span.component}</Badge> : null}
                </div>
                <span className="text-xs text-muted-foreground">{formatDateTime(item.span.started_at, timezone)}</span>
              </div>
              <p className="text-sm">{item.span.message || reasonDisplay(item.span.reason_code) || "阶段完成"}</p>
              <TraceMeta pluginKey={item.span.plugin_key} entryKey={item.span.entry_key} reasonCode={item.span.reason_code} />
            </div>
          ) : (
            <div className="space-y-1">
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
  );
}

function PluginDiagnostics({
  plugins,
  loading,
  error,
  selectedPluginKey,
  onSelectPlugin,
  detail,
  detailLoading,
  detailError,
  timezone,
  onTraceSelect,
}: {
  plugins: PluginRuntimeStatusItem[];
  loading: boolean;
  error?: unknown;
  selectedPluginKey: string;
  onSelectPlugin: (key: string) => void;
  detail?: PluginRuntimeDetail;
  detailLoading: boolean;
  detailError?: unknown;
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
      <Card>
        <CardHeader>
          <SectionHeader
            title="插件诊断"
            description="加载状态、最近调用和最近错误。"
            meta={<SignalPill tone="neutral" label="插件" value={plugins.length} />}
          />
        </CardHeader>
        <CardContent className="space-y-2">
          {loading ? <InlineLoading /> : error ? <ErrorHint text="插件诊断加载失败" error={error} /> : plugins.length ? plugins.map((plugin) => (
            <button
              key={`${plugin.account_id ?? "global"}-${plugin.plugin_key}`}
              type="button"
              onClick={() => onSelectPlugin(plugin.plugin_key)}
              className={`w-full rounded-md border p-3 text-left transition hover:border-primary/50 ${
                selectedPluginKey === plugin.plugin_key ? "border-primary bg-primary/5" : "border-border"
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="break-all font-medium">{plugin.plugin_key}</span>
                <StatusBadge status={plugin.last_invocation_status || plugin.load_status} />
              </div>
              <PluginStatusSnippet plugin={plugin} />
            </button>
          )) : <EmptyHint text="暂无插件运行状态" />}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>插件详情</CardTitle>
          <CardDescription>{selectedPluginKey || "选择插件查看最近调用"}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {detailLoading ? <InlineLoading /> : detailError ? <ErrorHint text="插件详情加载失败" error={detailError} /> : detail ? (
            <>
              <PluginDiagnosisPanel detail={detail} selectedPluginKey={selectedPluginKey} timezone={timezone} onTraceSelect={onTraceSelect} />
              <PluginStatusList statuses={detail.statuses} timezone={timezone} onTraceSelect={onTraceSelect} />
              <PluginSpanIssues spans={detail.recent_spans} timezone={timezone} onTraceSelect={onTraceSelect} />
              <TraceMiniList traces={detail.recent_traces} timezone={timezone} onTraceSelect={onTraceSelect} />
              <details className="rounded-md border p-3">
                <summary className="cursor-pointer text-sm font-medium">展开最近 span 原始数据</summary>
                <div className="mt-3">
                  <JsonBlock title="recent_spans" value={detail.recent_spans} />
                </div>
              </details>
            </>
          ) : <EmptyHint text="尚未选择插件" />}
        </CardContent>
      </Card>
    </div>
  );
}

function PluginStatusSnippet({ plugin }: { plugin: PluginRuntimeStatusItem }) {
  const bad = plugin.last_load_error || isFailedStatus(plugin.last_invocation_status) || isFailedStatus(plugin.load_status);
  if (plugin.last_load_error) {
    return (
      <div className="mt-2 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
        加载失败：{plugin.last_load_error}
      </div>
    );
  }
  if (bad) {
    return (
      <div className="mt-2 text-xs text-destructive">
        最近调用失败{plugin.last_trace_id ? `，trace=${plugin.last_trace_id}` : ""}
      </div>
    );
  }
  return (
    <div className="mt-2 text-xs text-muted-foreground">
      {plugin.last_trace_id ? `最近 trace：${plugin.last_trace_id}` : "暂无异常"}
    </div>
  );
}

function PluginDiagnosisPanel({
  detail,
  selectedPluginKey,
  timezone,
  onTraceSelect,
}: {
  detail: PluginRuntimeDetail;
  selectedPluginKey: string;
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  const diagnosis = buildPluginDiagnosis(detail, selectedPluginKey);
  return (
    <section className={`rounded-md border p-3 ${diagnosisToneClass(diagnosis.tone)}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium">排查结论：{diagnosis.title}</div>
          <p className="mt-2 text-sm">{diagnosis.message}</p>
        </div>
        {diagnosis.traceId ? (
          <Button type="button" variant="outline" size="sm" onClick={() => onTraceSelect(diagnosis.traceId as string)}>
            查看 trace
          </Button>
        ) : null}
      </div>
      <div className="mt-3 grid gap-2 text-xs md:grid-cols-3">
        <InfoCell label="插件" value={selectedPluginKey || diagnosis.pluginKey || "-"} />
        <InfoCell label="最近调用" value={latestPluginInvokedAt(detail, timezone)} />
        <InfoCell label="最近异常原因" value={diagnosis.reasonCode ? reasonDisplay(diagnosis.reasonCode) : "-"} />
      </div>
      <div className="mt-3 rounded-md border border-current/10 bg-background/60 px-3 py-2 text-sm">
        下一步：{diagnosis.nextStep}
      </div>
    </section>
  );
}

function PluginSpanIssues({
  spans,
  timezone,
  onTraceSelect,
}: {
  spans: EventSpanItem[];
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  const issues = spans.filter((span) => isFailedStatus(span.status) || isWarnStatus(span.status) || isProblemReasonCode(span.reason_code)).slice(0, 8);
  if (!issues.length) return null;
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">最近异常阶段</div>
      {issues.map((span) => (
        <div key={span.span_id} className={`rounded-md border p-3 ${isFailedStatus(span.status) ? "border-destructive/30 bg-destructive/5" : "border-amber-300/60 bg-amber-50/50"}`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <StatusBadge status={span.status} />
              <Badge variant="secondary">{span.phase}</Badge>
              {span.entry_key ? <Badge variant="outline">{span.entry_key}</Badge> : null}
            </div>
            <span className="text-xs text-muted-foreground">{formatDateTime(span.started_at, timezone)}</span>
          </div>
          <p className="mt-2 text-sm">{spanIssueText(span)}</p>
          <Button type="button" variant="ghost" size="sm" className="mt-2 px-0" onClick={() => onTraceSelect(span.trace_id)}>
            查看这次消息链路
          </Button>
        </div>
      ))}
    </div>
  );
}

function PluginStatusList({
  statuses,
  timezone,
  onTraceSelect,
}: {
  statuses: PluginRuntimeStatusItem[];
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  if (!statuses.length) return <EmptyHint text="暂无运行状态记录" />;
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">运行状态</div>
      {statuses.map((status) => (
        <div key={`${status.account_id ?? "global"}-${status.plugin_key}`} className="rounded-md border p-3 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <StatusBadge status={status.last_invocation_status || status.load_status} />
              <Badge variant={status.enabled ? "secondary" : "outline"}>{status.enabled ? "已启用" : "未启用"}</Badge>
              {status.account_id ? <Badge variant="outline">账号 {status.account_id}</Badge> : null}
            </div>
            <span className="text-xs text-muted-foreground">{formatDateTime(status.updated_at, timezone)}</span>
          </div>
          <div className="mt-2 grid gap-2 text-xs text-muted-foreground md:grid-cols-3">
            <InfoCell label="加载状态" value={status.load_status || "-"} />
            <InfoCell label="版本" value={status.installed_version || "-"} />
            <InfoCell label="最近调用" value={status.last_invoked_at ? formatDateTime(status.last_invoked_at, timezone) : "-"} />
          </div>
          {status.last_load_error ? (
            <p className="mt-2 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
              {status.last_load_error}
            </p>
          ) : null}
          {status.last_trace_id ? (
            <Button variant="ghost" size="sm" className="mt-2 px-0" onClick={() => onTraceSelect(status.last_trace_id as string)}>
              查看最近 trace
            </Button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function ActionsPanel({
  actions,
  loading,
  error,
  timezone,
  onTraceSelect,
}: {
  actions: EventActionItem[];
  loading: boolean;
  error?: unknown;
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title="动作发送"
          description="插件请求动作、平台实际通道、Telegram API 或 userbot 执行结果。"
          meta={<SignalPill tone="neutral" label="窗口" value={`${actions.length} 条`} />}
        />
      </CardHeader>
      <CardContent>
        {loading ? <InlineLoading /> : error ? <ErrorHint text="动作记录加载失败" error={error} /> : actions.length ? (
          <div className="space-y-2">
            {actions.map((action) => (
              <div key={action.action_id} className="rounded-md border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={action.status} />
                    <Badge variant="secondary">{action.action_type}</Badge>
                    {action.plugin_key ? <Badge variant="secondary">{action.plugin_key}</Badge> : null}
                  </div>
                  <span className="text-xs text-muted-foreground">{formatDateTime(action.created_at, timezone)}</span>
                </div>
                <div className="mt-2 grid gap-2 text-xs text-muted-foreground md:grid-cols-5">
                  <InfoCell label="请求通道" value={action.requested_send_via || "-"} />
                  <InfoCell label="实际通道" value={action.actual_send_via || "-"} />
                  <InfoCell label="目标会话" value={action.target_chat_id ?? "-"} />
                  <InfoCell label="消息 ID" value={action.telegram_message_id ?? action.target_message_id ?? "-"} />
                  <InfoCell label="Inline 结果" value={inlineResultCountLabel(action)} />
                </div>
                {action.error_message || action.error_code ? (
                  <p className="mt-2 text-sm text-destructive">{actionErrorLabel(action)}</p>
                ) : null}
                {action.detail ? (
                  <div className="mt-2">
                    <JsonBlock title="动作详情" value={action.detail} />
                  </div>
                ) : null}
                <Button variant="ghost" size="sm" className="mt-2 px-0" onClick={() => onTraceSelect(action.trace_id)}>
                  查看 trace
                </Button>
              </div>
            ))}
          </div>
        ) : <EmptyHint text="当前过滤条件下没有动作记录" />}
      </CardContent>
    </Card>
  );
}

function RawLogsPanel({
  accountId,
  pluginKey,
  keyword,
  runtimeMinLevel,
  timezone,
}: {
  accountId: string;
  pluginKey: string;
  keyword: string;
  runtimeMinLevel?: "debug" | "info" | "warn" | "error";
  timezone?: string;
}) {
  const [rawTab, setRawTab] = useState<RawTab>("runtime");
  const [runtimeSource, setRuntimeSource] = useState<RuntimeSourceFilter>("");
  const [runtimeLevel, setRuntimeLevel] = useState<RuntimeLevelFilter>("");
  const [runtimeAutoRefresh, setRuntimeAutoRefresh] = useState(true);
  const [showRuntimeDetail, setShowRuntimeDetail] = useState(false);
  const [hideRuntimeNoise, setHideRuntimeNoise] = useState(true);
  const [wrapRuntimeLines, setWrapRuntimeLines] = useState(true);
  const [auditAction, setAuditAction] = useState("");
  const [showAuditDetail, setShowAuditDetail] = useState(false);
  const [hideAuditNoise, setHideAuditNoise] = useState(true);
  const [wrapAuditLines, setWrapAuditLines] = useState(true);
  const inheritedFilters = [
    accountId ? `account=#${accountId}` : "全部账号",
    pluginKey ? `plugin=${pluginKey}` : null,
    keyword.trim() ? `keyword=${keyword.trim()}` : null,
  ].filter(Boolean);

  return (
    <Tabs value={rawTab} onValueChange={(v) => setRawTab(v as RawTab)} className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <TabsList className="flex h-auto flex-wrap justify-start gap-1">
          <TabsTrigger value="runtime" className="gap-1.5"><Activity className="h-4 w-4" /> 运行日志</TabsTrigger>
          <TabsTrigger value="audit" className="gap-1.5"><ShieldCheck className="h-4 w-4" /> 审计日志</TabsTrigger>
        </TabsList>
        <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
          {inheritedFilters.map((item) => (
            <span key={item} className="rounded-md border border-border/70 bg-muted/40 px-2 py-1">
              {item}
            </span>
          ))}
        </div>
      </div>
      <TabsContent value="runtime" className="space-y-4">
        <section className="rounded-lg border border-border/70 bg-card/80 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="text-base font-semibold">控制台运行日志</h3>
              <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
                查系统有没有收到消息、插件有没有启动、调用卡在哪一步、发送动作有没有失败。热更新和普通配置刷新默认隐藏。
              </p>
            </div>
            <div className="space-y-1 text-xs text-muted-foreground sm:text-right">
              <span className="inline-flex rounded-md border border-border/70 bg-muted/40 px-2 py-1">
                当前记录阈值：{runtimeMinLevelLabel(runtimeMinLevel)}
              </span>
              {runtimeMinLevel === "debug" ? (
                <p className="max-w-lg sm:max-w-sm">
                  debug 表示允许保留 debug 行；如果当前链路没有写入 debug，列表仍会以 info、warn 和 error 为主。
                </p>
              ) : null}
            </div>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-6 xl:items-end">
            <div className="space-y-1.5">
              <Label>来源</Label>
              <Select value={runtimeSource} onChange={(e) => setRuntimeSource(e.target.value as RuntimeSourceFilter)}>
                <option value="">全部来源</option>
                <option value="event">消息事件</option>
                <option value="plugin">插件日志</option>
                <option value="system">系统运行</option>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>最低级别</Label>
              <Select value={runtimeLevel} onChange={(e) => setRuntimeLevel(e.target.value as RuntimeLevelFilter)}>
                <option value="">全部已记录</option>
                <option value="debug">debug 及以上</option>
                <option value="info">info 及以上</option>
                <option value="warn">warn 及 error</option>
                <option value="error">仅 error</option>
              </Select>
            </div>
            <SwitchField label="隐藏噪声" checked={hideRuntimeNoise} onCheckedChange={setHideRuntimeNoise} />
            <SwitchField label="显示 detail" checked={showRuntimeDetail} onCheckedChange={setShowRuntimeDetail} />
            <SwitchField label="自动刷新" checked={runtimeAutoRefresh} onCheckedChange={setRuntimeAutoRefresh} />
            <SwitchField label="自动换行" checked={wrapRuntimeLines} onCheckedChange={setWrapRuntimeLines} />
          </div>
        </section>
        <RuntimeLogConsole
          accountId={accountId}
          pluginKey={pluginKey}
          keyword={keyword}
          source={runtimeSource}
          level={runtimeLevel}
          autoRefresh={runtimeAutoRefresh}
          hideNoise={hideRuntimeNoise}
          showDetail={showRuntimeDetail}
          wrapLines={wrapRuntimeLines}
          timezone={timezone}
        />
      </TabsContent>
      <TabsContent value="audit" className="space-y-4">
        <section className="rounded-lg border border-border/70 bg-card/80 p-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold">控制台审计日志</h3>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              查 Web 面板里是谁在什么时候改了配置、启停插件、安装更新插件。它不负责解释消息为什么没响应。
            </p>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-4 md:items-end">
            <div className="space-y-1.5 md:col-span-1">
              <Label>操作类型</Label>
              <Input value={auditAction} onChange={(e) => setAuditAction(e.target.value)} placeholder="account_bot.test" />
            </div>
            <SwitchField label="显示 detail" checked={showAuditDetail} onCheckedChange={setShowAuditDetail} />
            <SwitchField label="隐藏登录噪声" checked={hideAuditNoise} onCheckedChange={setHideAuditNoise} />
            <SwitchField label="自动换行" checked={wrapAuditLines} onCheckedChange={setWrapAuditLines} />
          </div>
        </section>
        <AuditLogConsole
          action={auditAction}
          keyword={keyword}
          hideNoise={hideAuditNoise}
          showDetail={showAuditDetail}
          wrapLines={wrapAuditLines}
          timezone={timezone}
        />
      </TabsContent>
    </Tabs>
  );
}

function RuntimeLogConsole({
  accountId,
  pluginKey,
  keyword,
  source,
  level,
  autoRefresh,
  hideNoise,
  showDetail,
  wrapLines,
  timezone,
}: {
  accountId: string;
  pluginKey: string;
  keyword: string;
  source: RuntimeSourceFilter;
  level: RuntimeLevelFilter;
  autoRefresh: boolean;
  hideNoise: boolean;
  showDetail: boolean;
  wrapLines: boolean;
  timezone?: string;
}) {
  const serverLevel = level === "warn" ? "warning" : level === "error" ? "error" : undefined;
  const filters = {
    account_id: accountId || undefined,
    plugin_key: pluginKey || undefined,
    source: source || undefined,
    level: serverLevel,
    limit: 300,
  };
  const logsQ = useQuery({
    queryKey: ["logs", "runtime", filters],
    queryFn: () => listRuntimeLogs(filters),
    refetchInterval: autoRefresh ? 5_000 : false,
    refetchIntervalInBackground: false,
  });
  const filtered = useMemo(() => {
    return (logsQ.data ?? []).filter((row) => {
      if (level && !passesRuntimeLevel(row.level, level)) return false;
      if (hideNoise && isLowValueRuntimeLog(row)) return false;
      return matchesRuntimeKeyword(row, keyword);
    });
  }, [hideNoise, keyword, level, logsQ.data]);
  const visibleText = useMemo(
    () => filtered.flatMap((row) => runtimeConsoleText(row, timezone, showDetail)).join("\n"),
    [filtered, showDetail, timezone],
  );
  const [copied, setCopied] = useState(false);
  return (
    <ConsoleFrame
      title="runtime.log"
      description="最近 300 条，最新在上。顶部账号、插件、关键词过滤会同步作用于这里。"
      count={filtered.length}
      copied={copied}
      onCopy={() => copyConsoleText(visibleText, setCopied)}
    >
      {logsQ.isLoading ? <InlineLoading /> : logsQ.isError ? (
        <ConsoleError text="运行日志加载失败" error={logsQ.error} />
      ) : filtered.length ? (
        <div className={wrapLines ? "min-w-0" : "min-w-max"}>
          {filtered.map((row) => (
            <RuntimeConsoleRow
              key={row.id}
              row={row}
              keyword={keyword}
              showDetail={showDetail}
              wrapLines={wrapLines}
              timezone={timezone}
            />
          ))}
        </div>
      ) : <ConsoleEmpty text="没有符合筛选条件的运行日志行" />}
    </ConsoleFrame>
  );
}

function AuditLogConsole({
  action,
  keyword,
  hideNoise,
  showDetail,
  wrapLines,
  timezone,
}: {
  action: string;
  keyword: string;
  hideNoise: boolean;
  showDetail: boolean;
  wrapLines: boolean;
  timezone?: string;
}) {
  const logsQ = useQuery({
    queryKey: ["logs", "audit", { action: action || undefined, keyword: keyword || undefined, limit: 300 }],
    queryFn: () => listAuditLogs({ action: action || undefined, keyword: keyword || undefined, limit: 300 }),
  });
  const endpointMissing = isAxiosError(logsQ.error) &&
    (logsQ.error.response?.status === 404 || logsQ.error.response?.status === 405);
  const visibleRows = useMemo(() => {
    return (logsQ.data ?? []).filter((row) => {
      if (hideNoise && !action.trim() && isLowValueAuditLog(row)) return false;
      return true;
    });
  }, [action, hideNoise, logsQ.data]);
  const visibleText = useMemo(
    () => visibleRows.flatMap((row) => auditConsoleText(row, timezone, showDetail)).join("\n"),
    [showDetail, timezone, visibleRows],
  );
  const [copied, setCopied] = useState(false);
  return (
    <ConsoleFrame
      title="audit.log"
      description="最近 300 条，最新在上。关键词沿用顶部排查过滤。"
      count={visibleRows.length}
      copied={copied}
      onCopy={() => copyConsoleText(visibleText, setCopied)}
    >
      {logsQ.isLoading ? <InlineLoading /> : endpointMissing ? (
        <ConsoleEmpty text="当前环境未提供审计日志接口" />
      ) : logsQ.isError ? (
        <ConsoleError text="审计日志加载失败" error={logsQ.error} />
      ) : visibleRows.length ? (
        <div className={wrapLines ? "min-w-0" : "min-w-max"}>
          {visibleRows.map((row) => (
            <AuditConsoleRow
              key={row.id}
              row={row}
              keyword={keyword}
              showDetail={showDetail}
              wrapLines={wrapLines}
              timezone={timezone}
            />
          ))}
        </div>
      ) : <ConsoleEmpty text="没有符合筛选条件的审计日志行" />}
    </ConsoleFrame>
  );
}

function RuntimeConsoleRow({
  row,
  keyword,
  showDetail,
  wrapLines,
  timezone,
}: {
  row: RuntimeLogItem;
  keyword: string;
  showDetail: boolean;
  wrapLines: boolean;
  timezone?: string;
}) {
  const level = normalizeRuntimeLevel(row.level);
  const parts = runtimeConsoleParts(row, timezone);
  return (
    <div className={`border-b border-white/5 text-zinc-100 last:border-b-0 hover:bg-white/[0.035] ${runtimeConsoleRowClass(level)}`}>
      <div className={`${consoleLineClass(wrapLines)} ${isLowValueRuntimeLog(row) ? "opacity-55" : ""}`}>
        <span className="text-zinc-500">[{parts.timestamp}] </span>
        <span className={`font-semibold ${runtimeConsoleLevelBadgeClass(level)}`}>[{parts.levelLabel}]</span>
        <span className="text-zinc-500"> [{parts.meta.join(" ")}] </span>
        <span className="text-zinc-100">
          <HighlightedMessage text={parts.message} keyword={keyword} />
        </span>
        {parts.traceId ? (
          <>
            {" "}
            <Link className="text-sky-300 underline-offset-2 hover:underline" to={`/logs?tab=events&trace_id=${encodeURIComponent(parts.traceId)}`}>
              open_trace
            </Link>
          </>
        ) : null}
      </div>
      {showDetail ? <ConsoleJson value={row.detail} wrapLines={wrapLines} /> : null}
    </div>
  );
}

function AuditConsoleRow({
  row,
  keyword,
  showDetail,
  wrapLines,
  timezone,
}: {
  row: AuditLogItem;
  keyword: string;
  showDetail: boolean;
  wrapLines: boolean;
  timezone?: string;
}) {
  const line = auditConsoleLine(row, timezone);
  return (
    <div className="border-b border-white/5 text-zinc-100 last:border-b-0 hover:bg-white/[0.035]">
      <div className={consoleLineClass(wrapLines)}>
        <HighlightedMessage text={line} keyword={keyword} />
      </div>
      {showDetail ? <ConsoleJson value={row.detail} wrapLines={wrapLines} /> : null}
    </div>
  );
}

function SwitchField({
  label,
  checked,
  onCheckedChange,
}: {
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <div className="flex h-10 items-center gap-2">
        <Switch checked={checked} onCheckedChange={onCheckedChange} />
        <span className="text-sm text-muted-foreground">{checked ? "开启" : "关闭"}</span>
      </div>
    </div>
  );
}

function ConsoleFrame({
  title,
  description,
  count,
  copied,
  onCopy,
  children,
}: {
  title: string;
  description: string;
  count: number;
  copied: boolean;
  onCopy: () => void;
  children: ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-zinc-800 bg-[#08090d] shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 bg-zinc-950 px-3 py-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs font-semibold text-zinc-100">{title}</span>
            <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-300">{count} lines</span>
          </div>
          <p className="mt-1 text-xs text-zinc-400">{description}</p>
        </div>
        <Button type="button" variant="outline" size="sm" className="border-zinc-700 bg-zinc-900 text-zinc-100 hover:bg-zinc-800" onClick={onCopy}>
          <Copy className="mr-1.5 h-3.5 w-3.5" />
          {copied ? "已复制" : "复制当前视图"}
        </Button>
      </div>
      <div className="max-h-[68vh] overflow-auto overscroll-contain bg-[#08090d] text-xs">
        {children}
      </div>
    </section>
  );
}

function ConsoleError({ text, error }: { text: string; error: unknown }) {
  return (
    <div className="px-3 py-8 font-mono text-xs text-red-300">
      {text}: {errorMessage(error)}
    </div>
  );
}

function ConsoleEmpty({ text }: { text: string }) {
  return (
    <div className="px-3 py-8 font-mono text-xs text-zinc-400">
      {text}
    </div>
  );
}

function buildQuickCheckStages(
  detail: EventTraceDetail | undefined,
  state: { loading: boolean; hasCandidates: boolean; hasSelectedTrace: boolean },
): QuickCheckStage[] {
  if (!detail) {
    const receiveTone: DiagnosisTone = state.loading ? "neutral" : state.hasCandidates ? "warn" : "danger";
    const receiveStatus = state.loading ? "running" : state.hasCandidates ? "warning" : "failed";
    return [
      {
        key: "receive",
        title: "收到",
        tone: receiveTone,
        status: receiveStatus,
        detail: state.loading
          ? "正在读取链路"
          : state.hasCandidates
            ? "已找到候选消息，选择一条 trace"
            : "当前条件下没有 trace",
      },
      {
        key: "route",
        title: "路由",
        tone: "neutral",
        status: state.hasSelectedTrace ? "running" : "skipped",
        detail: state.hasSelectedTrace ? "正在读取详情" : "等待 trace",
      },
      {
        key: "plugin",
        title: "插件",
        tone: "neutral",
        status: "skipped",
        detail: "等待 trace",
      },
      {
        key: "send",
        title: "发送",
        tone: "neutral",
        status: "skipped",
        detail: "等待 trace",
      },
    ];
  }

  const routeSpans = detail.spans.filter((span) => isRouteSpan(span));
  const pluginSpans = detail.spans.filter((span) => isPluginSpan(span));
  const failedRoute = routeSpans.find((span) => isFailedStatus(span.status));
  const warningRoute = routeSpans.find((span) => isWarnStatus(span.status) || isProblemReasonCode(span.reason_code));
  const failedPlugin = pluginSpans.find((span) => isFailedStatus(span.status));
  const warningPlugin = pluginSpans.find((span) => isWarnStatus(span.status) || isProblemReasonCode(span.reason_code));
  const failedAction = detail.actions.find((action) => isFailedStatus(action.status) || action.error_code || action.error_message);
  const plugins = Array.from(new Set(pluginSpans.map((span) => span.plugin_key).filter(Boolean)));
  return [
    {
      key: "receive",
      title: "收到",
      tone: isFailedStatus(detail.status) ? "warn" : "success",
      status: detail.status || "ok",
      detail: detail.text_preview || detail.sender_name || `${detail.source_channel || "-"} / ${detail.event_type}`,
    },
    {
      key: "route",
      title: "路由",
      tone: failedRoute ? "danger" : warningRoute ? "warn" : routeSpans.length ? "success" : "warn",
      status: failedRoute?.status || warningRoute?.status || (routeSpans.length ? "ok" : "skipped"),
      detail: failedRoute
        ? spanIssueText(failedRoute)
        : warningRoute
          ? spanIssueText(warningRoute)
          : routeSpans.length
            ? `${routeSpans.length} 个路由阶段`
            : "没有路由阶段记录",
    },
    {
      key: "plugin",
      title: "插件",
      tone: failedPlugin ? "danger" : warningPlugin ? "warn" : pluginSpans.length ? "success" : "warn",
      status: failedPlugin?.status || warningPlugin?.status || (pluginSpans.length ? "ok" : "skipped"),
      detail: failedPlugin
        ? spanIssueText(failedPlugin)
        : warningPlugin
          ? spanIssueText(warningPlugin)
          : plugins.length
            ? `命中 ${plugins.slice(0, 3).join(", ")}${plugins.length > 3 ? ` 等 ${plugins.length} 个` : ""}`
            : "没有插件执行阶段",
    },
    {
      key: "send",
      title: "发送",
      tone: failedAction ? "danger" : detail.actions.length ? "success" : "neutral",
      status: failedAction?.status || (detail.actions.length ? "ok" : "skipped"),
      detail: failedAction
        ? actionErrorLabel(failedAction)
        : detail.actions.length
          ? `${detail.actions.length} 个动作，${detail.actions.filter((action) => action.actual_send_via).length} 个已落地通道`
          : "没有发送动作",
    },
  ];
}

function isRouteSpan(span: EventSpanItem): boolean {
  const phase = span.phase.toLowerCase();
  const component = (span.component || "").toLowerCase();
  return phase.includes("route") ||
    phase.includes("subscription") ||
    component.includes("guard") ||
    component.includes("dispatcher") ||
    component.includes("event_bus");
}

function isPluginSpan(span: EventSpanItem): boolean {
  const phase = span.phase.toLowerCase();
  return Boolean(span.plugin_key) || phase.includes("plugin");
}

function stageOverallTone(stages: QuickCheckStage[]): DiagnosisTone {
  if (stages.some((stage) => stage.tone === "danger")) return "danger";
  if (stages.some((stage) => stage.tone === "warn")) return "warn";
  if (stages.some((stage) => stage.tone === "success")) return "success";
  return "neutral";
}

function buildTraceDiagnosis(detail: EventTraceDetail): DiagnosisResult {
  const failedAction = detail.actions.find((action) => isFailedStatus(action.status) || action.error_code || action.error_message);
  if (failedAction) {
    return {
      tone: "danger",
      title: "发送动作失败",
      message: actionErrorLabel(failedAction),
      nextStep: "打开“动作发送”或查看下方关键时间线，确认请求通道、实际通道、目标会话和 Telegram API 错误。",
      reasonCode: failedAction.error_code,
      pluginKey: failedAction.plugin_key,
      traceId: failedAction.trace_id,
    };
  }

  const failedSpan = detail.spans.find((span) => isFailedStatus(span.status));
  if (failedSpan) {
    return {
      tone: "danger",
      title: failedSpan.plugin_key ? "插件执行失败" : "链路阶段失败",
      message: spanIssueText(failedSpan),
      nextStep: failedSpan.plugin_key
        ? "打开“插件诊断”查看该插件最近错误；若这里是 handler_error，优先看插件抛出的原始异常。"
        : "查看该阶段的 reason 和 detail，确认是触发入口、权限、会话、Contract Guard 还是运行时链路问题。",
      reasonCode: failedSpan.reason_code,
      pluginKey: failedSpan.plugin_key,
      entryKey: failedSpan.entry_key,
      traceId: failedSpan.trace_id,
    };
  }

  const runtimeError = detail.related_runtime_logs.find((row) => {
    const level = normalizeRuntimeLevel(row.level);
    return level === "error" || level === "warn";
  });
  if (runtimeError) {
    return {
      tone: normalizeRuntimeLevel(runtimeError.level) === "error" ? "danger" : "warn",
      title: "运行日志有异常",
      message: runtimeError.message,
      nextStep: "展开高级数据里的 related_runtime_logs，或回到原始日志用 trace_id 搜索同一段上下文。",
      reasonCode: pickString(runtimeError.detail, ["reason_code", "error_code"]),
      pluginKey: pickString(runtimeError.detail, ["plugin_key", "feature_key"]),
      traceId: pickString(runtimeError.detail, ["trace_id", "context.trace_id"]),
    };
  }

  const warningSpan = detail.spans.find((span) => isWarnStatus(span.status) || isProblemReasonCode(span.reason_code));
  if (warningSpan) {
    return {
      tone: "warn",
      title: reasonDisplay(warningSpan.reason_code) || "链路有告警",
      message: spanIssueText(warningSpan),
      nextStep: "如果消息没有响应，先看是否是触发入口未命中、过滤条件未命中、插件未启用或发送通道不满足。",
      reasonCode: warningSpan.reason_code,
      pluginKey: warningSpan.plugin_key,
      entryKey: warningSpan.entry_key,
      traceId: warningSpan.trace_id,
    };
  }

  if (!detail.plugin_count && detail.event_type !== "session_close") {
    return {
      tone: "warn",
      title: "未命中插件",
      message: "消息进入了 TelePilot，但没有进入任何插件处理阶段。",
      nextStep: "检查触发关键词、触发入口、允许会话、插件启用状态和规则作用账号。",
    };
  }

  if (isFailedStatus(detail.status)) {
    return {
      tone: "danger",
      title: "链路失败",
      message: "该 trace 标记为失败，但当前明细没有返回更具体的失败阶段。",
      nextStep: "用 trace_id 回到原始运行日志搜索同一时间段，确认是否有插件异常或 Trace 写入降级。",
    };
  }

  if (isWarnStatus(detail.status)) {
    return {
      tone: "warn",
      title: "链路有告警",
      message: "该 trace 不是完全失败，但存在跳过或告警状态。",
      nextStep: "看关键时间线里的 warn/skipped 阶段，确认是否是预期过滤，还是规则配置不匹配。",
    };
  }

  return {
    tone: "success",
    title: "链路已完成",
    message: detail.action_count ? "消息已进入插件或动作发送链路，并完成记录。" : "消息已处理完成，但没有产生发送动作。",
    nextStep: detail.action_count ? "如仍觉得群里没有响应，打开“动作发送”确认实际发送通道和目标会话。" : "如果本来应该回复，检查插件是否返回了发送动作或是否被配置为只记录不发送。",
  };
}

function buildPluginDiagnosis(detail: PluginRuntimeDetail, selectedPluginKey: string): DiagnosisResult {
  const loadError = detail.statuses.find((status) => status.last_load_error);
  if (loadError) {
    return {
      tone: "danger",
      title: "插件加载失败",
      message: loadError.last_load_error || "插件加载失败",
      nextStep: "先修 manifest、依赖或入口模块；加载失败时不会进入正常消息触发流程。",
      pluginKey: loadError.plugin_key,
      traceId: loadError.last_trace_id,
    };
  }

  const failedSpan = detail.recent_spans.find((span) => isFailedStatus(span.status));
  if (failedSpan) {
    return {
      tone: "danger",
      title: "最近调用失败",
      message: spanIssueText(failedSpan),
      nextStep: "点击 trace 查看这一次消息的完整链路，再根据 phase 判断是触发入口、执行、契约还是发送动作失败。",
      reasonCode: failedSpan.reason_code,
      pluginKey: failedSpan.plugin_key || selectedPluginKey,
      entryKey: failedSpan.entry_key,
      traceId: failedSpan.trace_id,
    };
  }

  const warnSpan = detail.recent_spans.find((span) => isWarnStatus(span.status) || isProblemReasonCode(span.reason_code));
  if (warnSpan) {
    return {
      tone: "warn",
      title: "最近调用有告警",
      message: spanIssueText(warnSpan),
      nextStep: "如果插件没有响应，优先确认触发入口条件、触发词、会话策略和 Contract Guard 提示。",
      reasonCode: warnSpan.reason_code,
      pluginKey: warnSpan.plugin_key || selectedPluginKey,
      entryKey: warnSpan.entry_key,
      traceId: warnSpan.trace_id,
    };
  }

  const badStatus = detail.statuses.find((status) => isFailedStatus(status.last_invocation_status) || isFailedStatus(status.load_status));
  if (badStatus) {
    return {
      tone: "danger",
      title: "状态记录异常",
      message: `${badStatus.plugin_key} 当前状态为 ${badStatus.last_invocation_status || badStatus.load_status}`,
      nextStep: "查看运行状态和最近 trace，确认异常是否仍在发生。",
      pluginKey: badStatus.plugin_key,
      traceId: badStatus.last_trace_id,
    };
  }

  return {
    tone: "success",
    title: "插件最近状态正常",
    message: detail.recent_traces.length ? "最近调用没有发现失败阶段。" : "暂无最近调用记录。",
    nextStep: detail.recent_traces.length ? "若群内无响应，请到消息链路按关键词或 Chat ID 查对应消息。" : "先触发一次插件，再回到这里看是否产生 trace。",
    pluginKey: selectedPluginKey,
  };
}

function spanIssueText(span: EventSpanItem): string {
  const reason = reasonDisplay(span.reason_code);
  if (span.message && reason && span.message !== reasonLabel(span.reason_code)) return `${reason}：${span.message}`;
  return span.message || reason || "阶段没有返回具体错误消息";
}

function latestPluginInvokedAt(detail: PluginRuntimeDetail, timezone?: string): string {
  const latest = detail.statuses
    .map((status) => status.last_invoked_at)
    .filter((value): value is string => Boolean(value))
    .sort((a, b) => new Date(b).getTime() - new Date(a).getTime())[0];
  return latest ? formatDateTime(latest, timezone) : "-";
}

function pickDiagnosticTimelineItems(items: TimelineItem[]): TimelineItem[] {
  const selected = new Set<number>();
  items.forEach((item, index) => {
    if (index === 0 || index === items.length - 1 || isProblemTimelineItem(item) || isImportantTimelineItem(item)) {
      selected.add(index);
    }
  });
  if (selected.size > 10) {
    const problemIndexes = items
      .map((item, index) => (isProblemTimelineItem(item) ? index : -1))
      .filter((index) => index >= 0);
    return items.filter((item, index) => index === 0 || index === items.length - 1 || problemIndexes.includes(index)).slice(0, 10);
  }
  return items.filter((_, index) => selected.has(index)).slice(0, 10);
}

function isImportantTimelineItem(item: TimelineItem): boolean {
  if (item.kind === "action") return true;
  const phase = item.span.phase.toLowerCase();
  return [
    "subscription",
    "subscription_match",
    "command",
    "command_match",
    "plugin_invoke",
    "plugin_return",
    "contract_guard",
    "delivery",
    "settlement",
  ].some((key) => phase.includes(key));
}

function isProblemTimelineItem(item: TimelineItem): boolean {
  if (item.kind === "action") return isFailedStatus(item.action.status) || Boolean(item.action.error_code || item.action.error_message);
  return isFailedStatus(item.span.status) || isWarnStatus(item.span.status) || isProblemReasonCode(item.span.reason_code);
}

function timelineItemClass(item: TimelineItem): string {
  if (item.kind === "action") {
    if (isFailedStatus(item.action.status) || item.action.error_code || item.action.error_message) {
      return "border-destructive/30 bg-destructive/5";
    }
    return "";
  }
  if (isFailedStatus(item.span.status)) return "border-destructive/30 bg-destructive/5";
  if (isWarnStatus(item.span.status) || isProblemReasonCode(item.span.reason_code)) return "border-amber-300/60 bg-amber-50/50";
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

function isProblemReasonCode(code?: string | null): boolean {
  if (!code) return false;
  return !NORMAL_REASON_CODES.has(code);
}

function diagnosisToneClass(tone: DiagnosisTone): string {
  if (tone === "success") return "border-emerald-200 bg-emerald-50/70 text-emerald-950";
  if (tone === "danger") return "border-destructive/30 bg-destructive/5 text-foreground";
  if (tone === "warn") return "border-amber-300 bg-amber-50/70 text-amber-950";
  return "border-border bg-muted/30 text-foreground";
}

function runtimeSourceLabel(source?: string | null): string {
  const value = (source || "").toLowerCase();
  if (value === "event" || value === "interaction" || value === "account_bot") return "消息事件";
  if (value === "plugin" || value === "builtin") return "插件日志";
  if (value === "system" || value === "worker") return "系统运行";
  return source || "运行日志";
}

function runtimeSourceShort(source?: string | null): string {
  const value = (source || "").toLowerCase();
  if (value === "event" || value === "interaction" || value === "account_bot") return "event";
  if (value === "plugin" || value === "builtin") return "plugin";
  if (value === "system" || value === "worker") return "system";
  return value || "runtime";
}

function runtimeMinLevelLabel(level?: "debug" | "info" | "warn" | "error"): string {
  if (level === "debug") return "debug（排障最详细）";
  if (level === "warn") return "warn（告警和错误）";
  if (level === "error") return "error（仅错误）";
  return "info（日常）";
}

function normalizeRuntimeLevel(level?: string | null): NormalizedRuntimeLevel {
  const value = (level || "").toLowerCase();
  if (value === "debug" || value === "info" || value === "error") return value;
  if (value === "warn" || value === "warning") return "warn";
  return "unknown";
}

function passesRuntimeLevel(level: string, minLevel: RuntimeLevelFilter): boolean {
  if (!minLevel) return true;
  const current = RUNTIME_LEVEL_RANK[level.toLowerCase()] ?? 1;
  const threshold = RUNTIME_LEVEL_RANK[minLevel] ?? 0;
  return current >= threshold;
}

function matchesRuntimeKeyword(row: RuntimeLogItem, keyword: string): boolean {
  const q = keyword.trim().toLowerCase();
  if (!q) return true;
  const detail = row.detail ? safeJsonStringify(row.detail).toLowerCase() : "";
  return [
    row.message,
    row.level,
    row.source || "",
    runtimeSourceLabel(row.source),
    String(row.account_id ?? ""),
    detail,
  ].some((value) => value.toLowerCase().includes(q));
}

function isLowValueRuntimeLog(row: RuntimeLogItem): boolean {
  const level = normalizeRuntimeLevel(row.level);
  if (level === "warn" || level === "error") return false;
  return LOW_VALUE_RUNTIME_PATTERNS.some((pattern) => row.message.includes(pattern));
}

function isLowValueAuditLog(row: AuditLogItem): boolean {
  return LOW_VALUE_AUDIT_ACTIONS.has(row.action);
}

function runtimeConsoleParts(row: RuntimeLogItem, timezone?: string) {
  const detail = row.detail ?? {};
  const traceId = pickString(detail, ["trace_id", "context.trace_id", "event.trace_id"]);
  const pluginKey = pickString(detail, ["plugin_key", "feature_key", "module_key"]);
  const entryKey = pickString(detail, ["entry_key", "entry", "handler"]);
  const reasonCode = pickString(detail, ["reason_code", "error_code", "guard_level"]);
  const chatId = pickString(detail, ["chat_id", "target_chat_id", "message.chat_id", "event.chat_id"]);
  const messageId = pickString(detail, ["message_id", "target_message_id", "event.message_id"]);
  const actor = pickString(detail, ["display_name", "username", "user_id", "sender_user_id"]);
  const meta = [
    `id=#${row.id}`,
    `source=${runtimeSourceShort(row.source)}`,
    row.account_id ? `account=#${row.account_id}` : null,
    pluginKey ? `plugin=${pluginKey}` : null,
    entryKey ? `entry=${entryKey}` : null,
    traceId ? `trace=${traceId}` : null,
    chatId ? `chat=${chatId}` : null,
    messageId ? `message=${messageId}` : null,
    actor ? `actor=${actor}` : null,
    reasonCode ? `reason=${reasonCode}` : null,
  ].filter(Boolean);
  const level = normalizeRuntimeLevel(row.level).toUpperCase();
  return {
    timestamp: formatConsoleTimestamp(row.created_at, timezone),
    levelLabel: level,
    meta,
    message: compactConsoleText(row.message),
    traceId,
  };
}

function runtimeConsoleLine(row: RuntimeLogItem, timezone?: string): string {
  const parts = runtimeConsoleParts(row, timezone);
  return `[${parts.timestamp}] [${parts.levelLabel}] [${parts.meta.join(" ")}] ${parts.message}`;
}

function runtimeConsoleText(row: RuntimeLogItem, timezone: string | undefined, showDetail: boolean): string[] {
  const lines = [runtimeConsoleLine(row, timezone)];
  if (showDetail && row.detail && Object.keys(row.detail).length) {
    lines.push(indentConsoleBlock(safeJsonStringify(row.detail, 2)));
  }
  return lines;
}

function auditConsoleLine(row: AuditLogItem, timezone?: string): string {
  const detail = row.detail ?? {};
  const pluginKey = pickString(detail, ["plugin_key", "feature_key", "key"]);
  const accountId = pickString(detail, ["account_id", "aid"]);
  const status = pickString(detail, ["status", "state", "result"]);
  const version = pickString(detail, ["version", "new_version", "target_version"]);
  const meta = [
    `id=#${row.id}`,
    row.user_id ? `user=#${row.user_id}` : null,
    `action=${row.action}`,
    row.target ? `target=${row.target}` : null,
    accountId ? `account=#${accountId}` : null,
    pluginKey ? `plugin=${pluginKey}` : null,
    status ? `status=${status}` : null,
    version ? `version=${version}` : null,
  ].filter(Boolean);
  const summary = auditDetailSummaryText(detail);
  const message = summary ? `${auditActionTitle(row.action)} | ${summary}` : auditActionTitle(row.action);
  return `[${formatConsoleTimestamp(row.ts, timezone)}] [AUDIT] [${meta.join(" ")}] ${compactConsoleText(message)}`;
}

function auditConsoleText(row: AuditLogItem, timezone: string | undefined, showDetail: boolean): string[] {
  const lines = [auditConsoleLine(row, timezone)];
  if (showDetail && row.detail && Object.keys(row.detail).length) {
    lines.push(indentConsoleBlock(safeJsonStringify(row.detail, 2)));
  }
  return lines;
}

function auditDetailSummaryText(detail: Record<string, unknown>): string {
  const preferred = [
    "message",
    "error",
    "reason",
    "plugin_key",
    "feature_key",
    "repo_url",
    "branch",
    "account_id",
    "enabled",
    "status",
  ];
  const picked = preferred
    .filter((key) => key in detail)
    .map((key) => `${key}=${stringifyShort(detail[key])}`);
  const fallback = Object.entries(detail)
    .filter(([key]) => !preferred.includes(key))
    .slice(0, Math.max(0, 4 - picked.length))
    .map(([key, value]) => `${key}=${stringifyShort(value)}`);
  return [...picked, ...fallback].slice(0, 4).join(" ");
}

function consoleLineClass(wrapLines: boolean): string {
  const wrapping = wrapLines ? "whitespace-pre-wrap break-words" : "min-w-max whitespace-pre";
  return `${wrapping} px-3 py-1.5 font-mono text-[11px] leading-5`;
}

function runtimeConsoleRowClass(level: NormalizedRuntimeLevel): string {
  if (level === "error") return "bg-red-950/10";
  if (level === "warn") return "bg-amber-950/10";
  return "";
}

function runtimeConsoleLevelBadgeClass(level: NormalizedRuntimeLevel): string {
  if (level === "error") return "text-red-300";
  if (level === "warn") return "text-amber-300";
  if (level === "info") return "text-sky-300";
  if (level === "debug") return "text-zinc-500";
  return "text-zinc-300";
}

function ConsoleJson({ value, wrapLines }: { value?: Record<string, unknown> | null; wrapLines: boolean }) {
  if (!value || Object.keys(value).length === 0) return null;
  return (
    <pre className={`${wrapLines ? "whitespace-pre-wrap break-words" : "min-w-max whitespace-pre"} border-l border-white/10 bg-black/20 px-6 py-2 font-mono text-[11px] leading-5 text-zinc-400`}>
      {safeJsonStringify(value, 2)}
    </pre>
  );
}

async function copyConsoleText(text: string, setCopied: (value: boolean) => void): Promise<void> {
  if (!text.trim() || !navigator.clipboard) return;
  await navigator.clipboard.writeText(text);
  setCopied(true);
  window.setTimeout(() => setCopied(false), 1200);
}

function safeJsonStringify(value: unknown, space?: number): string {
  try {
    return JSON.stringify(value, null, space);
  } catch {
    return String(value);
  }
}

function compactConsoleText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function indentConsoleBlock(value: string): string {
  return value.split("\n").map((line) => `  ${line}`).join("\n");
}

function formatConsoleTimestamp(iso?: string | null, tz?: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: tz || "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(d).replace(/\//g, "-");
  } catch {
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
}

function auditActionTitle(action: string): string {
  const labels: Record<string, string> = {
    "account_bot.test": "测试账号 Bot",
    "account_bot.save": "保存账号 Bot 配置",
    "interaction.rule.save": "保存交互规则",
    "plugin.install": "安装插件",
    "plugin.update": "更新插件",
    "plugin.remove": "移除插件",
    "plugin.enable": "启用插件",
    "plugin.disable": "停用插件",
    "settings.update": "更新系统设置",
  };
  if (labels[action]) return labels[action];
  return action.split(".").filter(Boolean).join(" / ") || action;
}

function InlineTraceSummary({
  trace,
  actions,
  compact = false,
}: {
  trace: EventTraceSummary | EventTraceDetail;
  actions?: EventActionItem[];
  compact?: boolean;
}) {
  const query = trace.inline_query || pickString(trace, ["payload_snapshot.inline_query.query", "raw_summary.inline_query.query", "payload_snapshot.query"]);
  const chosen = trace.chosen_inline_result_id || pickString(trace, ["payload_snapshot.chosen_inline_result.result_id", "raw_summary.chosen_inline_result.result_id"]);
  const choiceQuery = trace.chosen_inline_query || pickString(trace, ["payload_snapshot.chosen_inline_result.query", "raw_summary.chosen_inline_result.query"]);
  const inlineActions = (actions ?? []).filter((action) => action.action_type === "answer_inline_query");
  const resultCount = inlineActions.find((action) => action.inline_result_count != null)?.inline_result_count;
  const failedAction = inlineActions.find((action) => action.status === "failed" || action.error_code || action.error_message);

  if (!query && !chosen && resultCount == null && !failedAction && trace.event_type !== "inline_query" && trace.event_type !== "chosen_inline_result") {
    return null;
  }

  if (compact) {
    return (
      <div className="mt-2 flex flex-wrap gap-1.5 text-xs text-muted-foreground">
        {query ? <span className="max-w-full break-all">Inline query: {query}</span> : null}
        {chosen ? <span className="max-w-full break-all">Chosen: {chosen}</span> : null}
      </div>
    );
  }

  return (
    <div className="rounded-md border border-primary/20 bg-primary/5 p-3 text-xs">
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
    </div>
  );
}

function NativeRawSummary({ meta }: { meta?: EventTraceSummary["native_raw_meta"] }) {
  if (!meta) return null;
  return (
    <div className="rounded-md border bg-muted/30 p-3 text-xs">
      <div className="mb-2 font-medium text-foreground">native_raw_meta 摘要</div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <InfoCell label="声明状态" value={meta.enabled ? "已允许" : "未允许"} />
        <InfoCell label="驱动" value={meta.driver || meta.source || "-"} />
        <InfoCell label="对象" value={meta.object || "-"} />
        <InfoCell label="持久化" value={meta.stored_in_trace ? `已保存 ${meta.size_bytes ?? "-"} bytes` : "未保存"} />
      </div>
      {meta.reason_code ? (
        <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-2 py-1.5 text-amber-900">
          {reasonDisplay(meta.reason_code)}
        </div>
      ) : null}
    </div>
  );
}

function NativeRawMetaPill({ meta }: { meta?: EventTraceSummary["native_raw_meta"] }) {
  if (!meta) return null;
  const label = meta.enabled ? "native_raw 已声明" : "native_raw 未授权";
  return (
    <span className={meta.enabled ? "text-amber-700" : "text-muted-foreground"}>
      {label}{meta.stored_in_trace ? " · 已保存" : ""}
    </span>
  );
}

function RecentTraceCard({
  title,
  traces,
  timezone,
  onTraceSelect,
}: {
  title: string;
  traces: EventTraceSummary[];
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {traces.length ? traces.map((trace) => (
          <button key={trace.trace_id} type="button" className="w-full rounded-md border p-3 text-left hover:border-primary/50" onClick={() => onTraceSelect(trace.trace_id)}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <StatusBadge status={trace.status} />
              <span className="text-xs text-muted-foreground">{formatDateTime(trace.started_at, timezone)}</span>
            </div>
            <div className="mt-2 break-all font-mono text-xs text-muted-foreground">{trace.trace_id}</div>
            <p className="mt-1 line-clamp-2 text-sm">{trace.text_preview || trace.event_type}</p>
          </button>
        )) : <EmptyHint text="暂无记录" />}
      </CardContent>
    </Card>
  );
}

function TraceMiniList({
  traces,
  timezone,
  onTraceSelect,
}: {
  traces: EventTraceSummary[];
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  if (!traces.length) return <EmptyHint text="暂无最近调用" />;
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">最近调用</div>
      {traces.map((trace) => (
        <button
          key={trace.trace_id}
          type="button"
          className="w-full rounded-md border p-3 text-left hover:border-primary/50"
          onClick={() => onTraceSelect(trace.trace_id)}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <StatusBadge status={trace.status} />
            <span className="text-xs text-muted-foreground">{formatDateTime(trace.started_at, timezone)}</span>
          </div>
          <div className="mt-2 break-all font-mono text-xs text-muted-foreground">{trace.trace_id}</div>
          <p className="mt-1 line-clamp-2 text-sm">{trace.text_preview || trace.event_type}</p>
        </button>
      ))}
    </div>
  );
}

function RecentActionCard({
  title,
  actions,
  timezone,
  onTraceSelect,
}: {
  title: string;
  actions: EventActionItem[];
  timezone?: string;
  onTraceSelect: (traceId: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {actions.length ? actions.map((action) => (
          <button key={action.action_id} type="button" className="w-full rounded-md border p-3 text-left hover:border-primary/50" onClick={() => onTraceSelect(action.trace_id)}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <StatusBadge status={action.status} />
              <span className="text-xs text-muted-foreground">{formatDateTime(action.created_at, timezone)}</span>
            </div>
            <p className="mt-2 text-sm">{actionErrorLabel(action)}</p>
            <div className="mt-1 break-all font-mono text-xs text-muted-foreground">{action.plugin_key || action.trace_id}</div>
          </button>
        )) : <EmptyHint text="暂无记录" />}
      </CardContent>
    </Card>
  );
}

function RecentPluginCard({ title, plugins, timezone }: { title: string; plugins: PluginRuntimeStatusItem[]; timezone?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {plugins.length ? plugins.map((plugin) => (
          <div key={`${plugin.account_id ?? "global"}-${plugin.plugin_key}`} className="rounded-md border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="break-all font-medium">{plugin.plugin_key}</span>
              <StatusBadge status={plugin.load_status} />
            </div>
            <p className="mt-2 text-sm text-destructive">{plugin.last_load_error || "加载异常"}</p>
            <p className="mt-1 text-xs text-muted-foreground">{formatDateTime(plugin.updated_at, timezone)}</p>
          </div>
        )) : <EmptyHint text="暂无记录" />}
      </CardContent>
    </Card>
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

function TraceMeta({ pluginKey, entryKey, reasonCode }: { pluginKey?: string | null; entryKey?: string | null; reasonCode?: string | null }) {
  if (!pluginKey && !entryKey && !reasonCode) return null;
  return (
    <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
      {pluginKey ? <span>插件 {pluginKey}</span> : null}
      {entryKey ? <span>入口 {entryKey}</span> : null}
      {reasonCode ? <span>{reasonDisplay(reasonCode)}</span> : null}
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

function PluginSelect({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  const keys = Array.from(new Set([...options, ...(value ? [value] : [])])).sort();
  return (
    <div className="space-y-1.5">
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">全部插件</option>
        {keys.map((key) => (
          <option key={key} value={key}>{key}</option>
        ))}
      </Select>
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value.trim())}
        placeholder="或输入远程插件 key"
      />
    </div>
  );
}

function SearchInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="relative">
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <Input className="pl-9" value={value} onChange={(e) => onChange(e.target.value)} placeholder="搜索关键词" />
    </div>
  );
}

function HighlightedMessage({ text, keyword }: { text: string; keyword: string }) {
  const q = keyword.trim();
  if (!q) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="rounded bg-yellow-200 px-0.5 text-yellow-950">{text.slice(idx, idx + q.length)}</mark>
      {text.slice(idx + q.length)}
    </>
  );
}

function LoadingCard() {
  return (
    <Card>
      <CardContent className="flex h-32 items-center justify-center">
        <Spinner className="text-primary" />
      </CardContent>
    </Card>
  );
}

function ErrorCard({ title, error }: { title: string; error: unknown }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base text-destructive">{title}</CardTitle>
        <CardDescription>{errorMessage(error)}</CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Trace API 没有返回有效数据，请先检查后端日志、鉴权状态和服务健康。
        </p>
      </CardContent>
    </Card>
  );
}

function InlineLoading() {
  return (
    <div className="flex h-20 items-center justify-center">
      <Spinner className="text-primary" />
    </div>
  );
}

function ErrorHint({ text, error }: { text: string; error: unknown }) {
  return (
    <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm">
      <div className="font-medium text-destructive">{text}</div>
      <div className="mt-1 break-words text-muted-foreground">{errorMessage(error)}</div>
    </div>
  );
}

function EmptyHint({ text }: { text: string }) {
  return <p className="py-8 text-center text-sm text-muted-foreground">{text}</p>;
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

function stringifyShort(value: unknown): string {
  if (value == null) return "-";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function actionDisplayText(action: EventActionItem): string {
  if (action.error_message || action.error_code) {
    return actionErrorLabel(action);
  }
  if (action.action_type === "answer_inline_query") {
    return `Inline 回答已记录，结果 ${inlineResultCountLabel(action)}`;
  }
  return "动作已记录";
}

function actionErrorLabel(action: EventActionItem): string {
  const label = reasonDisplay(action.error_code);
  if (label && action.error_message && action.error_message !== reasonLabel(action.error_code)) {
    return `${label}：${action.error_message}`;
  }
  return label || action.error_message || "动作失败";
}

function inlineResultCountLabel(action: EventActionItem): string {
  if (action.action_type !== "answer_inline_query" && action.inline_result_count == null) return "-";
  return action.inline_result_count == null ? "未知" : `${action.inline_result_count}`;
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

function reasonLabel(code?: string | null): string {
  const labels: Record<string, string> = {
    action_failed: "动作执行失败",
    account_not_matched: "账号不匹配",
    account_bot_user_unauthorized: "账号 Bot 用户未授权",
    bot_token_missing: "Bot token 缺失",
    bot_not_configured: "交互 Bot 未配置",
    bot_self_message: "忽略交互 Bot 自身消息",
    callback_query: "按钮回调",
    command_matched: "命令已命中",
    command_not_matched: "未命中命令",
    command_unauthorized: "命令权限不足",
    contract_failed: "契约失败",
    contract_warning: "契约告警",
    callback_query_id_missing: "按钮回调 ID 缺失",
    empty_message_text: "消息文本为空",
    entry_key_missing: "入口缺失",
    event_bus_delivery_disabled: "Event Bus 投递已关闭",
    event_type_not_subscribed: "事件类型不在触发入口内",
    filter_not_matched: "过滤条件未命中",
    handler_error: "处理器异常",
    inline_disabled: "Inline 已关闭",
    inline_query_answer_failed: "Inline 回答失败",
    inline_query_id_missing: "Inline Query ID 缺失",
    manifest_invalid: "Manifest 不合法",
    matched: "已命中",
    media_payload_empty: "媒体内容为空",
    media_payload_invalid: "媒体内容格式无效",
    media_payload_missing: "媒体内容缺失",
    native_raw_not_allowed: "未声明原生数据能力",
    native_raw_skipped: "原生数据未下发",
    permission_denied: "权限不足",
    plugin_disabled: "插件未启用",
    plugin_load_failed: "插件加载失败",
    plugin_not_installed: "插件未安装",
    plugin_runtime_error: "插件运行异常",
    rate_limited: "触发频控",
    scope_not_matched: "范围不匹配",
    send_channel_deprecated: "发送通道已废弃",
    session_control_action: "会话控制动作",
    settlement_requires_userbot: "结算需要 UserBot",
    session_expired: "会话已过期",
    session_not_found: "会话不存在",
    source_not_subscribed: "来源不在触发入口内",
    subscription_load_failed: "触发入口加载失败",
    subscription_not_matched: "触发入口未命中",
    target_message_id_missing: "目标消息 ID 缺失",
    telegram_api_error: "Telegram API 错误",
    trace_write_failed: "Trace 写入降级",
    unsupported_send_via: "发送通道不支持",
    userbot_offline: "UserBot 离线",
  };
  return code ? labels[code] || code : "";
}

function reasonDisplay(code?: string | null): string {
  if (!code) return "";
  const label = reasonLabel(code);
  return label && label !== code ? `${label} (${code})` : code;
}

function localDateTimeToIso(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

import { useEffect, useId, useMemo, useState, type ComponentType } from "react";
import {
  Activity,
  AlertTriangle,
  Compass,
  Cpu,
  MessageSquareText,
  Play,
  Route,
  ShieldCheck,
  Sparkles,
  Timer,
  Wrench,
} from "lucide-react";

import {
  buildAgentTraceOverview,
  defaultPerspectiveEvent,
  filterPerspectiveEvents,
  perspectiveTraceEvents,
  traceEventHint,
  traceEventCategory,
  type TraceEvent,
  type TraceEventCategory,
  type TraceEventFilter,
} from "@/components/assistant/runTraceState";
import { Badge } from "@/components/ui/badge";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";
import { cn, formatDateTime } from "@/lib/utils";

type DetailTab = "semantic" | "raw";

const FILTERS: Array<{ value: TraceEventFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "model", label: "路由与模型" },
  { value: "tool", label: "工具" },
  { value: "issue", label: "异常" },
];

export function AgentRunPerspective({
  events,
  running,
  failed,
  runStatus,
  traceNotice,
  timezone,
}: {
  events: TraceEvent[];
  running?: boolean;
  failed?: boolean;
  runStatus?: string;
  traceNotice?: string;
  timezone?: string;
}) {
  const [filter, setFilter] = useState<TraceEventFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<DetailTab>("semantic");
  const detailTabsId = useId();
  const overview = useMemo(() => buildAgentTraceOverview(events), [events]);
  const allEvents = useMemo(() => perspectiveTraceEvents(events), [events]);
  const visibleEvents = useMemo(() => filterPerspectiveEvents(events, filter), [events, filter]);
  const defaultEvent = useMemo(() => defaultPerspectiveEvent(visibleEvents), [visibleEvents]);
  const selected = visibleEvents.find((event) => eventId(event, allEvents) === selectedId) ?? defaultEvent;

  useEffect(() => {
    if (!defaultEvent) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !visibleEvents.some((event) => eventId(event, allEvents) === selectedId)) {
      setSelectedId(eventId(defaultEvent, allEvents));
      setDetailTab("semantic");
    }
  }, [allEvents, defaultEvent, selectedId, visibleEvents]);

  const status = normalizeRunStatus(runStatus, { running, failed, inferred: overview.status });

  return (
    <div className="space-y-3" data-testid="agent-run-perspective">
      <section className="overflow-hidden rounded-md border bg-background" aria-label="Agent 运行概览">
        <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-[minmax(0,0.8fr)_minmax(12rem,1.65fr)_repeat(4,minmax(0,1fr))]">
          <OverviewCell label="状态" value={statusLabel(status)} tone={statusTone(status)} />
          <OverviewCell
            label="Provider / 模型"
            value={overview.providerName || overview.model ? `${overview.providerName || "未知"} / ${overview.model || "未知"}` : "等待选择"}
          />
          <OverviewCell label="总耗时" value={formatDuration(overview.totalMs)} />
          <OverviewCell label="Token" value={formatCount(overview.totalTokens)} />
          <OverviewCell
            label="工具"
            value={overview.availableTools == null ? String(overview.toolCount) : `${overview.toolCount} / ${overview.availableTools}`}
            hint={overview.availableTools == null ? "调用次数" : "调用 / 可用"}
          />
          <OverviewCell
            label="恢复动作"
            value={`${overview.retryCount} 重试 · ${overview.fallbackCount} 切换`}
            tone={overview.retryCount || overview.fallbackCount ? "warn" : "neutral"}
          />
        </div>
        {overview.domains.length || overview.skills.length ? (
          <div className="flex flex-wrap items-center gap-1.5 border-t bg-muted/20 px-3 py-2">
            {overview.domains.map((domain) => (
              <Badge key={`domain-${domain}`} variant="outline" className="font-normal">
                <Route className="mr-1 h-3 w-3" />
                {domain}
              </Badge>
            ))}
            {overview.skills.map((skill) => (
              <Badge
                key={`skill-${skill}`}
                variant="outline"
                className="border-info/30 bg-info/10 font-normal text-foreground"
              >
                <Sparkles className="mr-1 h-3 w-3 text-info" />
                {skill}
              </Badge>
            ))}
          </div>
        ) : null}
      </section>

      {traceNotice ? (
        <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] text-foreground" role="status">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
          <span>{traceNotice}</span>
        </div>
      ) : null}

      <div className="grid min-h-[22rem] overflow-hidden rounded-md border bg-background lg:grid-cols-[minmax(13rem,0.78fr)_minmax(0,1.5fr)]">
        <section className="min-w-0 border-b bg-muted/15 lg:border-b-0 lg:border-r" aria-label="Agent 事件时间线">
          <div className="border-b px-2.5 py-2">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h4 className="text-xs font-semibold">事件时间线</h4>
                <p className="mt-0.5 text-[10px] text-muted-foreground">{allEvents.length} 个关键事件</p>
              </div>
              <Activity className="h-4 w-4 text-muted-foreground" aria-hidden />
            </div>
            <div className="mt-2 flex max-w-full gap-1 overflow-x-auto" role="group" aria-label="筛选 Agent 事件">
              {FILTERS.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  aria-pressed={filter === item.value}
                  onClick={() => setFilter(item.value)}
                  className={cn(
                    "min-h-10 shrink-0 rounded-md px-2 text-[10px] font-medium transition-colors active:scale-95 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/25 motion-reduce:transform-none",
                    filter === item.value
                      ? "bg-primary/15 text-foreground ring-1 ring-inset ring-primary/30"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>

          <div className="max-h-72 overflow-y-auto p-1.5 lg:max-h-[32rem]">
            {visibleEvents.length ? (
              <ol className="space-y-0.5">
                {visibleEvents.map((event, index) => {
                  const id = eventId(event, allEvents);
                  const selectedEvent = id === eventId(selected, allEvents);
                  const tone = eventTone(event);
                  const Icon = eventIcon(traceEventCategory(event));
                  return (
                    <li key={id} className="relative">
                      {index < visibleEvents.length - 1 ? (
                        <span className="pointer-events-none absolute bottom-[-6px] left-[18px] top-9 w-px bg-border" aria-hidden />
                      ) : null}
                      <button
                        type="button"
                        onClick={() => {
                          setSelectedId(id);
                          setDetailTab("semantic");
                        }}
                        aria-pressed={selectedEvent}
                        className={cn(
                          "relative flex min-h-14 w-full items-start gap-2 rounded-md px-2 py-2 text-left transition-colors active:scale-[0.99] focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/25 motion-reduce:transform-none",
                          selectedEvent ? "bg-background shadow-sm ring-1 ring-border" : "hover:bg-muted/70",
                        )}
                      >
                        <span className={cn("relative z-10 grid h-7 w-7 shrink-0 place-items-center rounded-md", tone.iconWrap)}>
                          <Icon className={cn("h-3.5 w-3.5", tone.icon)} aria-hidden />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-1.5">
                            <span className="min-w-0 flex-1 truncate text-[11px] font-semibold">{eventTitle(event)}</span>
                            <span className="shrink-0 font-mono text-[9px] tabular-nums text-muted-foreground">
                              {event.seq == null ? "" : `#${event.seq}`}
                            </span>
                          </span>
                          <span className="mt-0.5 block truncate text-[10px] text-muted-foreground" title={eventSubtitle(event)}>
                            {eventSubtitle(event)}
                          </span>
                          <span className="mt-1 block text-[9px] tabular-nums text-muted-foreground">
                            {eventTime(event, timezone)}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <div className="grid min-h-32 place-items-center px-4 text-center text-xs text-muted-foreground">
                当前筛选下没有事件
              </div>
            )}
          </div>
        </section>

        <section className="flex min-w-0 flex-col" aria-label="Agent 事件详情">
          {selected ? (
            <>
              <EventHeader event={selected} timezone={timezone} />
              <div className="flex min-h-10 items-end gap-1 border-b px-3" role="tablist" aria-label="Agent 事件详情模式">
                {(["semantic", "raw"] as const).map((tab) => (
                  <button
                    key={tab}
                    id={`${detailTabsId}-${tab}`}
                    type="button"
                    role="tab"
                    aria-selected={detailTab === tab}
                    aria-controls={`${detailTabsId}-panel`}
                    onClick={() => setDetailTab(tab)}
                    className={cn(
                      "min-h-10 border-b-2 px-2.5 text-[11px] font-semibold transition-colors active:scale-95 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/25 motion-reduce:transform-none",
                      detailTab === tab
                        ? "border-primary text-foreground"
                        : "border-transparent text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {tab === "semantic" ? "语义详情" : "原始事件"}
                  </button>
                ))}
              </div>
              <div
                id={`${detailTabsId}-panel`}
                className="min-h-0 flex-1 overflow-auto p-3"
                role="tabpanel"
                aria-labelledby={`${detailTabsId}-${detailTab}`}
              >
                {detailTab === "semantic" ? (
                  <SemanticEventDetail event={selected} />
                ) : (
                  <pre className="max-h-[30rem] overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/50 p-3 font-mono text-[10px] leading-4 text-foreground">
                    {JSON.stringify(selected, null, 2)}
                  </pre>
                )}
              </div>
              <p className="border-t px-3 py-2 text-[9px] text-muted-foreground">
                事件入库前会统一脱敏，语义详情不会展开回答正文或推理内容。
              </p>
            </>
          ) : (
            <div className="grid min-h-64 place-items-center px-6 text-center text-xs text-muted-foreground">
              选择左侧事件查看运行细节
            </div>
          )}
        </section>
      </div>

      <TimingStrip overview={overview} />
    </div>
  );
}

function OverviewCell({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "neutral" | "success" | "warn" | "danger" | "info";
}) {
  const colors = {
    neutral: "bg-muted-foreground",
    success: "bg-success",
    warn: "bg-warning",
    danger: "bg-destructive",
    info: "bg-info",
  } as const;
  return (
    <div className="min-w-0 border-b border-r px-3 py-2.5 last:border-r-0 xl:border-b-0">
      <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", colors[tone])} />
        {label}
      </div>
      <div className="mt-1 truncate text-xs font-semibold tabular-nums" title={value}>{value}</div>
      {hint ? <div className="mt-0.5 text-[9px] text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

function EventHeader({ event, timezone }: { event: TraceEvent; timezone?: string }) {
  const category = traceEventCategory(event);
  const Icon = eventIcon(category);
  const tone = eventTone(event);
  return (
    <header className="flex min-w-0 items-start gap-2.5 border-b px-3 py-3">
      <span className={cn("grid h-8 w-8 shrink-0 place-items-center rounded-md", tone.iconWrap)}>
        <Icon className={cn("h-4 w-4", tone.icon)} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <h4 className="text-sm font-semibold">{eventTitle(event)}</h4>
          <Badge variant="outline" className={tone.badgeClass}>{categoryLabel(category)}</Badge>
        </div>
        <p className="mt-0.5 break-words text-[11px] text-muted-foreground">{eventSubtitle(event)}</p>
      </div>
      <div className="shrink-0 text-right font-mono text-[9px] tabular-nums text-muted-foreground">
        <div>{event.seq == null ? "" : `#${event.seq}`}</div>
        <div className="mt-1">{eventTime(event, timezone)}</div>
      </div>
    </header>
  );
}

function SemanticEventDetail({ event }: { event: TraceEvent }) {
  const facts = eventFacts(event);
  const payloads = eventPayloads(event);
  const hint = traceEventHint(event.hint);
  return (
    <div className="space-y-3">
      {event.type === "error" ? (
        <div className="rounded-md border border-destructive/25 bg-destructive/5 px-3 py-2.5 text-xs text-destructive">
          <div className="font-mono text-[10px] font-semibold">{String(event.code || "AGENT_ERROR")}</div>
          <div className="mt-1 break-words text-foreground">{String(event.message || "Agent 运行失败")}</div>
          {hint ? <div className="mt-1.5 break-words text-muted-foreground">下一步：{hint}</div> : null}
        </div>
      ) : null}

      {event.understanding_summary ? (
        <div className="rounded-md bg-info/5 px-3 py-2.5 text-xs text-foreground ring-1 ring-inset ring-info/20">
          <div className="mb-1 text-[10px] font-semibold text-info">Agent 理解</div>
          <p className="text-pretty leading-5">{event.understanding_summary}</p>
        </div>
      ) : null}

      {facts.length ? (
        <dl className="overflow-hidden rounded-md border">
          {facts.map(([label, value]) => (
            <div key={label} className="grid grid-cols-[7.5rem_minmax(0,1fr)] border-b px-3 py-2 text-[11px] last:border-b-0">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="min-w-0 break-words font-medium tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {payloads.map(({ title, value }) => (
        <section key={title}>
          <h5 className="mb-1.5 text-[10px] font-semibold text-muted-foreground">{title}</h5>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/50 p-3 font-mono text-[10px] leading-4 text-foreground">
            {formatStructured(value)}
          </pre>
        </section>
      ))}

      {!facts.length && !payloads.length && event.type !== "error" ? (
        <div className="grid min-h-32 place-items-center rounded-md border border-dashed px-4 text-center text-xs text-muted-foreground">
          该事件没有更多结构化详情，可切换到原始事件查看。
        </div>
      ) : null}
    </div>
  );
}

function TimingStrip({ overview }: { overview: ReturnType<typeof buildAgentTraceOverview> }) {
  const items = [
    { label: "能力检查", value: overview.verifyMs },
    { label: "路由", value: overview.routeMs },
    { label: "首 Token", value: overview.firstTokenMs },
    { label: "总耗时", value: overview.totalMs },
  ];
  if (items.every((item) => item.value == null)) return null;
  return (
    <section className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border bg-muted/20 px-3 py-2" aria-label="Agent 阶段耗时">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
        <Timer className="h-3.5 w-3.5" aria-hidden />
        阶段耗时
      </div>
      {items.map((item) => (
        <div key={item.label} className="flex items-baseline gap-1.5 text-[10px]">
          <span className="text-muted-foreground">{item.label}</span>
          <span className="font-semibold tabular-nums">{formatDuration(item.value)}</span>
        </div>
      ))}
    </section>
  );
}

function eventFacts(event: TraceEvent): Array<[string, string]> {
  const facts: Array<[string, string]> = [];
  const add = (label: string, value: unknown) => {
    const text = inlineValue(value);
    if (text) facts.push([label, text]);
  };
  add("事件类型", event.type);
  add("Provider", event.provider_name);
  add("模型", event.model);
  add("选择原因", event.reason);
  add("选择模式", event.selection_mode);
  add("模型尝试", event.attempt);
  add("重试进度", event.retry_number && event.max_retries ? `${event.retry_number} / ${event.max_retries}` : null);
  add("路由领域", event.domains);
  add("路由来源", event.route_source);
  add("路由原因", event.route_reason);
  add("Skill", event.skill_names ?? event.skills ?? event.skill);
  add("工具", event.tool_description || event.tool_name);
  add("调用 ID", event.call_id);
  add("结果", event.is_error == null ? null : event.is_error ? "失败" : "完成");
  add("错误代码", event.code);
  add("完成状态", event.type === "done" ? (event.ok ? "成功" : "未成功") : null);
  return facts;
}

function eventPayloads(event: TraceEvent): Array<{ title: string; value: unknown }> {
  const payloads: Array<{ title: string; value: unknown }> = [];
  if (event.arguments_summary != null) payloads.push({ title: "工具参数摘要", value: event.arguments_summary });
  if (event.result_summary != null) payloads.push({ title: event.is_error ? "失败结果摘要" : "工具结果摘要", value: event.result_summary });
  if (event.usage != null) payloads.push({ title: "Token 与模型用量", value: event.usage });
  if (event.stage_timings != null) payloads.push({ title: "阶段耗时", value: event.stage_timings });
  if (event.action != null) payloads.push({ title: "待确认操作", value: event.action });
  return payloads;
}

function eventTitle(event: TraceEvent): string {
  switch (String(event.type || "")) {
    case "run_started": return "开始运行";
    case "model_capability_check": return "检查模型能力";
    case "provider_selected": return "选择 Provider";
    case "model_attempt": return "请求模型";
    case "retry_scheduled": return "安排重试";
    case "model_exhausted": return "模型尝试耗尽";
    case "route_selected": return "确定任务路由";
    case "skill_selected": return "加载 Skill";
    case "tool_started": return "调用工具";
    case "tool_finished": return event.is_error ? "工具调用失败" : "工具调用完成";
    case "action_proposed": return "等待操作确认";
    case "assistant_delta_reset": return "切换到工具阶段";
    case "assistant_message": return "生成最终回答";
    case "error": return "运行异常";
    case "done": return event.ok ? "运行完成" : "运行结束";
    default: return String(event.type || "运行事件");
  }
}

function eventSubtitle(event: TraceEvent): string {
  const type = String(event.type || "");
  if (["provider_selected", "model_capability_check", "model_attempt", "model_exhausted"].includes(type)) {
    return [event.provider_name, event.model].filter(Boolean).join(" · ") || "等待模型信息";
  }
  if (type === "retry_scheduled") {
    return `第 ${event.retry_number || "?"}/${event.max_retries || "?"} 次 · ${event.provider_name || event.model || "当前模型"}`;
  }
  if (type === "route_selected") return inlineValue(event.domains) || "未启用工具路由";
  if (type === "skill_selected") return inlineValue(event.skill_names ?? event.skills ?? event.skill) || "已加载运行能力";
  if (type === "tool_started" || type === "tool_finished") {
    return systemAgentToolLabel(String(event.tool_description || ""), String(event.tool_name || ""));
  }
  if (type === "action_proposed") return "写操作尚未执行，需要人工确认";
  if (type === "assistant_message") {
    const usage = asRecord(event.usage);
    const tokens = numberValue(usage?.total_tokens);
    return tokens == null ? "回答与用量信息已落库" : `回答已落库 · ${formatCount(tokens)} Token`;
  }
  if (type === "error") return String(event.message || event.code || "请查看错误详情");
  if (type === "done") return event.ok ? "本轮 Agent 已成功收敛" : "本轮未成功，可查看异常节点";
  if (type === "run_started") return event.channel ? `${String(event.channel)} 入口` : "开始处理本轮请求";
  return String(event.message || event.reason || "运行状态已更新");
}

function eventIcon(category: TraceEventCategory): ComponentType<{ className?: string }> {
  if (category === "model") return Cpu;
  if (category === "routing") return Compass;
  if (category === "tool") return Wrench;
  if (category === "response") return MessageSquareText;
  if (category === "issue") return AlertTriangle;
  if (category === "action") return ShieldCheck;
  return Play;
}

function eventTone(event: TraceEvent): {
  iconWrap: string;
  icon: string;
  badgeClass: string;
} {
  const category = traceEventCategory(event);
  if (category === "issue") return { iconWrap: "bg-destructive/10", icon: "text-destructive", badgeClass: "border-destructive/30 bg-destructive/10 text-foreground" };
  if (event.type === "done" || (event.type === "tool_finished" && !event.is_error)) {
    return { iconWrap: "bg-success/10", icon: "text-success", badgeClass: "border-success/30 bg-success/10 text-foreground" };
  }
  if (category === "tool" || category === "action") return { iconWrap: "bg-warning/10", icon: "text-warning", badgeClass: "border-warning/30 bg-warning/10 text-foreground" };
  if (category === "model" || category === "routing") return { iconWrap: "bg-info/10", icon: "text-info", badgeClass: "border-info/30 bg-info/10 text-foreground" };
  return { iconWrap: "bg-muted", icon: "text-muted-foreground", badgeClass: "border-border bg-muted text-foreground" };
}

function categoryLabel(category: TraceEventCategory): string {
  if (category === "model") return "模型";
  if (category === "routing") return "路由";
  if (category === "tool") return "工具";
  if (category === "response") return "回答";
  if (category === "issue") return "异常";
  if (category === "action") return "确认";
  return "运行";
}

type PerspectiveRunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

function normalizeRunStatus(
  runStatus: string | undefined,
  fallback: { running?: boolean; failed?: boolean; inferred: "running" | "succeeded" | "failed" },
): PerspectiveRunStatus {
  if (["queued", "running", "succeeded", "failed", "cancelled"].includes(String(runStatus || ""))) {
    return runStatus as PerspectiveRunStatus;
  }
  if (fallback.failed) return "failed";
  if (fallback.running) return "running";
  return fallback.inferred;
}

function statusLabel(status: PerspectiveRunStatus): string {
  if (status === "queued") return "排队中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  return "运行中";
}

function statusTone(status: PerspectiveRunStatus): "neutral" | "info" | "success" | "warn" | "danger" {
  if (status === "queued") return "neutral";
  if (status === "succeeded") return "success";
  if (status === "failed") return "danger";
  if (status === "cancelled") return "warn";
  return "info";
}

function eventTime(event: TraceEvent, timezone?: string): string {
  const value = event.created_at || event.ts;
  return value ? formatDateTime(String(value), timezone) : "时间未记录";
}

function eventId(event: TraceEvent | null, allEvents: TraceEvent[]): string {
  if (!event) return "";
  const index = allEvents.indexOf(event);
  return `${event.seq ?? index}:${String(event.type || "event")}`;
}

function formatDuration(value: number | null): string {
  if (value == null) return "-";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} 秒`;
}

function formatCount(value: number | null): string {
  return value == null ? "-" : new Intl.NumberFormat("zh-CN").format(value);
}

function formatStructured(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function inlineValue(value: unknown): string {
  if (value == null || value === "") return "";
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean).join("、");
  if (typeof value === "object") return formatStructured(value);
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

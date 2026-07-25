import { Fragment, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowRight, ChevronDown, Copy, History, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { listRecentLLMUsage, resetRecentLLMUsage } from "@/api/llmUsage";
import type { LLMUsageRecord } from "@/api/llmUsage";
import { listLLMProviders } from "@/api/commands";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/ui/misc";
import { Button } from "@/components/ui/button";
import { MetaBadge } from "@/components/ui/meta-badge";
import { MeterBar, SectionHeader, SignalPill, ToneRailCard } from "@/components/ui/status";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export function RecentUsageContent() {
  const queryClient = useQueryClient();
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | "success" | "failed">("all");
  const providersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
  });

  const providerCount = providersQ.data?.length ?? 0;
  const hasProviders = providerCount > 0;
  const usageQ = useQuery({
    queryKey: ["llm-usage", "recent", 100],
    queryFn: () => listRecentLLMUsage(100),
    retry: false,
    enabled: hasProviders,
  });
  const resetUsageMut = useMutation({
    mutationFn: resetRecentLLMUsage,
    onSuccess: (res) => {
      setExpandedId(null);
      toast.success(res.deleted > 0 ? `已清空 ${res.deleted} 条 AI 调用记录` : "AI 调用记录已是空的");
      void queryClient.invalidateQueries({ queryKey: ["llm-usage"] });
      void queryClient.invalidateQueries({ queryKey: ["llm", "plugin-usage-summary"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const handleResetUsage = () => {
    if (!window.confirm("确认清空 AI 调用记录？近期调用列表、成功率和插件 AI 用量统计都会从零开始。")) return;
    resetUsageMut.mutate();
  };

  if (providersQ.isLoading || (hasProviders && usageQ.isLoading)) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  if (providersQ.isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="inline-flex items-center gap-2">
            <History className="h-4 w-4" /> 近期调用
          </CardTitle>
          <CardDescription>暂时无法读取模型提供商：{getErrMsg(providersQ.error)}</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (providerCount === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="inline-flex items-center gap-2">
            <History className="h-4 w-4" /> 近期调用
          </CardTitle>
          <CardDescription>先配置至少一个模型提供商，才会产生可查看的调用记录。</CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild>
            <Link to="/ai?tab=providers">
              前往配置模型提供商
              <ArrowRight className="ml-1 h-4 w-4" />
            </Link>
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (usageQ.isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="inline-flex items-center gap-2">
            <History className="h-4 w-4" /> 近期调用
          </CardTitle>
          <CardDescription>暂时无法读取调用记录：{getErrMsg(usageQ.error)}</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const usage = usageQ.data;
  const rows = usage?.items || [];
  const summary = usage?.summary;
  const filteredRows = rows.filter((record) => {
    if (statusFilter === "success") return record.success;
    if (statusFilter === "failed") return !record.success;
    return true;
  });
  const toggleStatusFilter = (next: "success" | "failed") => {
    setExpandedId(null);
    setStatusFilter((current) => current === next ? "all" : next);
  };

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={History}
          title="近期调用"
          description="展示最近 100 条 LLM 调用记录与核心摘要。"
          actions={(
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive"
              disabled={resetUsageMut.isPending || rows.length === 0}
              onClick={handleResetUsage}
            >
              <Trash2 className="mr-1 h-4 w-4" />
              清空记录
            </Button>
          )}
        />
      </CardHeader>
      <CardContent className="space-y-4">
        {summary && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
              <SignalPill tone="neutral" label="总 Token" value={summary.total_tokens} />
              <SignalPill tone="primary" label="请求数" value={summary.request_count} />
              <button
                type="button"
                aria-pressed={statusFilter === "success"}
                onClick={() => toggleStatusFilter("success")}
                className={cn("rounded-full text-left active:scale-95 motion-reduce:transform-none", statusFilter === "success" && "ring-2 ring-success ring-offset-2 ring-offset-background")}
              >
                <SignalPill tone="success" label="成功" value={summary.success_count} />
              </button>
              <button
                type="button"
                aria-pressed={statusFilter === "failed"}
                onClick={() => toggleStatusFilter("failed")}
                className={cn("rounded-full text-left active:scale-95 motion-reduce:transform-none", statusFilter === "failed" && "ring-2 ring-warning ring-offset-2 ring-offset-background")}
              >
                <SignalPill tone={summary.failed_count > 0 ? "warn" : "neutral"} label="失败" value={summary.failed_count} />
              </button>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <ToneRailCard
                icon={History}
                title="成功率"
                value={`${summary.request_count > 0 ? Math.round((summary.success_count / summary.request_count) * 100) : 0}%`}
                description={<MeterBar value={summary.request_count > 0 ? (summary.success_count / summary.request_count) * 100 : 0} tone={summary.failed_count > 0 ? "warn" : "success"} className="mt-2" />}
                tone={summary.failed_count > 0 ? "warn" : "success"}
              />
              <ToneRailCard
                icon={History}
                title="失败占比"
                value={`${summary.request_count > 0 ? Math.round((summary.failed_count / summary.request_count) * 100) : 0}%`}
                description={<MeterBar value={summary.request_count > 0 ? (summary.failed_count / summary.request_count) * 100 : 0} tone={summary.failed_count > 0 ? "warn" : "neutral"} className="mt-2" />}
                tone={summary.failed_count > 0 ? "warn" : "neutral"}
              />
            </div>
            {statusFilter !== "all" ? (
              <div className="text-xs text-muted-foreground">
                当前仅显示{statusFilter === "success" ? "成功" : "失败"}记录，再点一次指标可取消筛选。
              </div>
            ) : null}
          </div>
        )}

        {filteredRows.length === 0 ? (
          <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
            {rows.length === 0 ? "暂无调用记录。触发一次 AI 指令后再回来查看。" : "当前筛选下没有调用记录。"}
          </p>
        ) : (
          <>
          <div className="hidden overflow-x-auto md:block">
            <Table className="min-w-[1040px]">
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>来源</TableHead>
                  <TableHead>模型提供商</TableHead>
                  <TableHead>模型</TableHead>
                  <TableHead>客户端</TableHead>
                  <TableHead>Token</TableHead>
                  <TableHead>耗时</TableHead>
                  <TableHead>结果</TableHead>
                  <TableHead>Fallback</TableHead>
                  <TableHead>错误</TableHead>
                  <TableHead className="text-right">详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredRows.map((r) => {
                  const tokens = (r.input_tokens || 0) + (r.output_tokens || 0);
                  const expanded = expandedId === r.id;
                  return (
                    <Fragment key={r.id}>
                      <TableRow
                        className="cursor-pointer"
                        onClick={() => setExpandedId((current) => (current === r.id ? null : r.id))}
                      >
                        <TableCell className="text-xs text-muted-foreground">{new Date(r.created_at).toLocaleString()}</TableCell>
                        <TableCell title={r.source || undefined}>
                          <div className="font-medium">{usageSourceLabel(r.source)}</div>
                        </TableCell>
                        <TableCell>{r.provider_name || (r.provider_id ? `#${r.provider_id}` : "-")}</TableCell>
                        <TableCell className="font-mono text-xs">{r.model || "-"}</TableCell>
                        <TableCell>{clientIdentityLabel(r.client_identity_profile)}</TableCell>
                        <TableCell>{tokens}</TableCell>
                        <TableCell>{r.latency_ms != null ? `${r.latency_ms}ms` : "-"}</TableCell>
                        <TableCell>
                          <MetaBadge tone={r.success ? "success" : "warn"}>{r.success ? "成功" : "失败"}</MetaBadge>
                        </TableCell>
                        <TableCell>{r.used_fallback ? "已使用" : "-"}</TableCell>
                        <TableCell className="text-xs" title={r.error_type || undefined}>
                          {usageErrorLabel(r.error_type)}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            type="button"
                            size="sm"
                            variant={expanded ? "secondary" : "default"}
                            className={expanded ? "" : "shadow-sm"}
                            onClick={(event) => {
                            event.stopPropagation();
                            setExpandedId(expanded ? null : r.id);
                          }}
                          >
                            {expanded ? "收起" : "查看详情"}
                            <ChevronDown className={cn("ml-1 h-4 w-4 transition-transform", expanded ? "rotate-180" : "")} />
                          </Button>
                        </TableCell>
                      </TableRow>
                      {expanded ? (
                        <TableRow>
                          <TableCell colSpan={11} className="bg-muted/25 p-0">
                            <UsageDetailPanel record={r} />
                          </TableCell>
                        </TableRow>
                      ) : null}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <div className="space-y-3 md:hidden">
            {filteredRows.map((r) => (
              <UsageRecordCard
                key={r.id}
                record={r}
                expanded={expandedId === r.id}
                onToggle={() => setExpandedId((current) => (current === r.id ? null : r.id))}
              />
            ))}
          </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function UsageRecordCard({
  record,
  expanded,
  onToggle,
}: {
  record: LLMUsageRecord;
  expanded: boolean;
  onToggle: () => void;
}) {
  const tokens = (record.input_tokens || 0) + (record.output_tokens || 0);
  return (
    <div data-usage-record className="rounded-xl border border-border/70 bg-background/70 p-3">
      <button type="button" className="block w-full text-left" onClick={onToggle}>
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">
              {record.provider_name || (record.provider_id ? `Provider #${record.provider_id}` : "未知 Provider")}
            </div>
            <div className="mt-1 truncate font-mono text-xs text-muted-foreground">
              {record.model || "-"}
            </div>
          </div>
          <MetaBadge tone={record.success ? "success" : "warn"}>
            {record.success ? "成功" : "失败"}
          </MetaBadge>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
          <InfoCell label="时间" value={new Date(record.created_at).toLocaleString()} />
          <InfoCell label="来源" value={usageSourceLabel(record.source)} />
          <InfoCell label="Token" value={tokens} />
          <InfoCell label="耗时" value={record.latency_ms != null ? `${record.latency_ms}ms` : "-"} />
        </div>
        <div className="mt-3 flex items-center justify-between gap-2">
          <div className="flex flex-wrap gap-1.5">
            <MetaBadge tone="info">客户端 {clientIdentityLabel(record.client_identity_profile)}</MetaBadge>
            {record.used_fallback ? <MetaBadge tone="outline">已 Fallback</MetaBadge> : null}
            {record.error_type ? <MetaBadge tone="warn">{usageErrorLabel(record.error_type)}</MetaBadge> : null}
          </div>
          <span className="inline-flex shrink-0 items-center rounded-full border border-primary/30 bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary">
            {expanded ? "收起详情" : "查看详情"}
            <ChevronDown className={cn("ml-1 h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} />
          </span>
        </div>
      </button>
      {expanded ? (
        <div className="mt-3 border-t border-border/70 pt-3">
          <UsageDetailPanel record={record} />
        </div>
      ) : null}
    </div>
  );
}

function UsageDetailPanel({ record }: { record: LLMUsageRecord }) {
  return (
    <div className="space-y-3 md:p-4">
      <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-5">
        <InfoCell label="调用来源" value={usageSourceLabel(record.source)} />
        <InfoCell label="账号" value={record.account_id == null ? "-" : `#${record.account_id}`} />
        <InfoCell label="模型提供商" value={record.provider_name || (record.provider_id ? `#${record.provider_id}` : "-")} />
        <InfoCell label="客户端" value={clientIdentityLabel(record.client_identity_profile)} />
        <InfoCell label="Token" value={`${record.input_tokens || 0} 输入 / ${record.output_tokens || 0} 输出`} />
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <PreviewBlock title="请求预览" text={record.request_preview} empty="这条历史调用没有保存请求预览；更新后产生的新调用会显示截断脱敏内容。" />
        <PreviewBlock
          title={record.success ? "返回预览" : "错误预览"}
          text={record.response_preview || (!record.success ? llmErrorPreview(record) : null)}
          empty={record.success ? "这条历史调用没有保存返回预览；更新后产生的新调用会显示截断脱敏内容。" : "这次失败没有保存响应正文；可继续看错误类型和系统控制台日志。"}
          tone={record.success ? "normal" : "danger"}
        />
      </div>
    </div>
  );
}

function InfoCell({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="rounded-md border bg-background px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 break-all text-xs font-medium">{String(value ?? "-")}</div>
    </div>
  );
}

function PreviewBlock({
  title,
  text,
  empty,
  tone = "normal",
}: {
  title: string;
  text?: string | null;
  empty: string;
  tone?: "normal" | "danger";
}) {
  const value = text?.trim();
  return (
    <div className={cn("rounded-lg border bg-background", tone === "danger" && "border-destructive/35 bg-destructive/5")}>
      <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
        <div className={cn("text-sm font-medium", tone === "danger" && "text-destructive")}>{title}</div>
        {value ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="border-primary/30 bg-primary/5 text-primary hover:bg-primary/10"
            onClick={() => copyPreview(value, title)}
          >
            <Copy className="mr-1 h-4 w-4" />
            复制
          </Button>
        ) : null}
      </div>
      {value ? (
        <pre className="max-h-72 overflow-auto p-3 text-xs leading-5 whitespace-pre-wrap break-words">{value}</pre>
      ) : (
        <p className="p-3 text-xs leading-5 text-muted-foreground">{empty}</p>
      )}
    </div>
  );
}

function llmErrorPreview(record: LLMUsageRecord): string | null {
  const errorType = record.error_type?.trim();
  if (!errorType) return null;
  return `错误类型：${usageErrorLabel(errorType)}`;
}

function usageSourceLabel(source?: string | null): string {
  const value = source || "";
  const labels: Record<string, string> = {
    system_agent: "系统助手",
    system_agent_router: "系统助手意图路由",
    "diagnostic:protocol_detection": "协议检测",
    "diagnostic:chat-test": "模型对话测试",
    "diagnostic:test-model": "模型快速测试",
    "diagnostic:full-liveness": "模型完整检测",
    scheduler: "定时任务",
    system: "系统",
  };
  if (labels[value]) return labels[value];
  if (value.startsWith("plugin:")) return `插件 ${value.slice("plugin:".length)}`;
  if (value.startsWith("command:")) return `AI 指令 ${value.slice("command:".length)}`;
  return value || "未知来源";
}

function clientIdentityLabel(profile?: string | null): string {
  const value = profile?.trim() || "";
  const labels: Record<string, string> = {
    auto: "自动选择",
    minimal: "最小身份",
    openai_sdk: "OpenAI SDK（标准 API）",
    codex_cli: "Codex CLI",
    codex_desktop: "Codex CLI（旧 Desktop 配置）",
    claude_code: "Claude Code CLI",
    claude_desktop: "Claude Code CLI（旧 Desktop 配置）",
    grok_cli: "Grok CLI",
  };
  return labels[value] || value || "未记录";
}

function usageErrorLabel(errorType?: string | null): string {
  const value = errorType?.trim() || "";
  const labels: Record<string, string> = {
    auth: "鉴权失败",
    budget_exceeded: "额度不足",
    cancelled: "调用已取消",
    consumer_closed: "连接已关闭",
    llmerror: "模型调用错误",
    network: "网络错误",
    rate_limit: "请求过多",
    server_error: "上游服务错误",
    timeout: "请求超时",
    unknown: "未知错误",
    valueerror: "响应格式错误",
  };
  return labels[value] || value || "-";
}

async function copyPreview(text: string, title: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`已复制${title}`);
  } catch {
    toast.error("复制失败，请检查浏览器剪贴板权限");
  }
}

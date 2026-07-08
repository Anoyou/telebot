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
            <div className="flex flex-wrap gap-2">
              <SignalPill tone="primary" label="请求数" value={summary.request_count} />
              <SignalPill tone="success" label="成功" value={summary.success_count} />
              <SignalPill tone={summary.failed_count > 0 ? "warn" : "neutral"} label="失败" value={summary.failed_count} />
              <SignalPill tone="neutral" label="Fallback" value={summary.fallback_count} />
              <SignalPill tone="neutral" label="总 Token" value={summary.total_tokens} />
              <SignalPill tone="primary" label="平均耗时" value={`${summary.avg_latency_ms}ms`} />
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
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
              <ToneRailCard
                icon={History}
                title="Fallback 占比"
                value={`${summary.request_count > 0 ? Math.round((summary.fallback_count / summary.request_count) * 100) : 0}%`}
                description={<MeterBar value={summary.request_count > 0 ? (summary.fallback_count / summary.request_count) * 100 : 0} tone="primary" className="mt-2" />}
                tone="primary"
              />
            </div>
          </div>
        )}

        {rows.length === 0 ? (
          <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
            暂无调用记录。触发一次 AI 指令后再回来查看。
          </p>
        ) : (
          <div className="overflow-x-auto">
            <Table className="min-w-[900px]">
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>来源</TableHead>
                  <TableHead>模型提供商</TableHead>
                  <TableHead>模型</TableHead>
                  <TableHead>Token</TableHead>
                  <TableHead>耗时</TableHead>
                  <TableHead>结果</TableHead>
                  <TableHead>Fallback</TableHead>
                  <TableHead>错误</TableHead>
                  <TableHead className="text-right">详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => {
                  const tokens = (r.input_tokens || 0) + (r.output_tokens || 0);
                  const expanded = expandedId === r.id;
                  return (
                    <Fragment key={r.id}>
                      <TableRow
                        className="cursor-pointer"
                        onClick={() => setExpandedId((current) => (current === r.id ? null : r.id))}
                      >
                        <TableCell className="text-xs text-muted-foreground">{new Date(r.created_at).toLocaleString()}</TableCell>
                        <TableCell>
                          <div className="font-medium">{usageSourceLabel(r.source)}</div>
                          <div className="font-mono text-[11px] text-muted-foreground">{r.source || "-"}</div>
                        </TableCell>
                        <TableCell>{r.provider_name || (r.provider_id ? `#${r.provider_id}` : "-")}</TableCell>
                        <TableCell className="font-mono text-xs">{r.model || "-"}</TableCell>
                        <TableCell>{tokens}</TableCell>
                        <TableCell>{r.latency_ms != null ? `${r.latency_ms}ms` : "-"}</TableCell>
                        <TableCell>
                          <MetaBadge tone={r.success ? "success" : "warn"}>{r.success ? "成功" : "失败"}</MetaBadge>
                        </TableCell>
                        <TableCell>{r.used_fallback ? "已使用" : "-"}</TableCell>
                        <TableCell className="font-mono text-xs">{r.error_type || "-"}</TableCell>
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
                          <TableCell colSpan={10} className="bg-muted/25 p-0">
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
        )}
      </CardContent>
    </Card>
  );
}

function UsageDetailPanel({ record }: { record: LLMUsageRecord }) {
  return (
    <div className="space-y-3 p-4">
      <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <InfoCell label="调用来源" value={usageSourceLabel(record.source)} />
        <InfoCell label="账号" value={record.account_id == null ? "-" : `#${record.account_id}`} />
        <InfoCell label="模型提供商" value={record.provider_name || (record.provider_id ? `#${record.provider_id}` : "-")} />
        <InfoCell label="Token" value={`${record.input_tokens || 0} 输入 / ${record.output_tokens || 0} 输出`} />
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <PreviewBlock title="请求预览" text={record.request_preview} empty="这条历史调用没有保存请求预览；更新后产生的新调用会显示截断脱敏内容。" />
        <PreviewBlock title="返回预览" text={record.response_preview} empty={record.success ? "这条历史调用没有保存返回预览；更新后产生的新调用会显示截断脱敏内容。" : "失败调用通常没有返回正文，先看错误类型和系统控制台日志。"} />
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

function PreviewBlock({ title, text, empty }: { title: string; text?: string | null; empty: string }) {
  const value = text?.trim();
  return (
    <div className="rounded-lg border bg-background">
      <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
        <div className="text-sm font-medium">{title}</div>
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

function usageSourceLabel(source?: string | null): string {
  const value = source || "";
  if (value.startsWith("plugin:")) return `插件 ${value.slice("plugin:".length)}`;
  if (value.startsWith("command:")) return `AI 指令 ${value.slice("command:".length)}`;
  if (value === "scheduler") return "定时任务";
  if (value === "system") return "系统";
  return value || "未知来源";
}

async function copyPreview(text: string, title: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`已复制${title}`);
  } catch {
    toast.error("复制失败，请检查浏览器剪贴板权限");
  }
}

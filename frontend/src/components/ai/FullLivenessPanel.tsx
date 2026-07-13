import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Filter, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { cancelFullLiveness, fullLivenessPreview, fullLivenessRun, fullLivenessStatus } from "@/api/commands";
import type { FullLivenessPreviewResponse, FullLivenessRunResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";

const DEFAULT_CHAT_TEST_SYSTEM_PROMPT =
  "你是一个自然、简洁的中文聊天助手。请像真实聊天一样直接回复用户，不要只返回 ping/pong。";

const CLIENT_IDENTITY_LABELS: Record<string, string> = {
  auto: "自动选择",
  minimal: "最小身份",
  openai_sdk: "OpenAI SDK",
  codex_cli: "Codex CLI",
  codex_desktop: "Codex Desktop",
  claude_code: "Claude Code",
  claude_desktop: "Claude Desktop",
  grok_cli: "Grok CLI",
};

const FULL_LIVENESS_STORAGE_KEY = "telepilot:llm-full-liveness-result";

type FullLivenessPersistedState = {
  preview: FullLivenessPreviewResponse | null;
  result: FullLivenessRunResponse | null;
};

function readFullLivenessState(): FullLivenessPersistedState {
  try {
    const raw = window.sessionStorage.getItem(FULL_LIVENESS_STORAGE_KEY);
    if (!raw) return { preview: null, result: null };
    const parsed = JSON.parse(raw) as Partial<FullLivenessPersistedState>;
    const result = parsed.result ?? null;
    // 页面刷新会丢失轮询句柄，不能把孤立的后台任务继续显示成“运行中”。
    const normalizedResult = result && (result.status === "queued" || result.status === "running")
      ? { ...result, status: "cancelled" as const }
      : result;
    return {
      preview: parsed.preview ?? null,
      result: normalizedResult,
    };
  } catch {
    return { preview: null, result: null };
  }
}

function writeFullLivenessState(state: FullLivenessPersistedState): void {
  try {
    window.sessionStorage.setItem(FULL_LIVENESS_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // sessionStorage 不可用时仍允许测活，只是不在当前标签页保留结果。
  }
}

function markFullLivenessRunCancelled(runId: string): FullLivenessPersistedState {
  const retained = readFullLivenessState();
  if (
    retained.result?.run_id === runId
    && (retained.result.status === "queued" || retained.result.status === "running")
  ) {
    retained.result = { ...retained.result, status: "cancelled" };
    writeFullLivenessState(retained);
  }
  return retained;
}
function livenessStatusTone(status: string): "success" | "warn" | "danger" | "neutral" {
  if (status === "healthy") return "success";
  if (status === "cancelled" || status === "skipped_disabled") return "neutral";
  if (["auth_failed", "client_rejected", "protocol_rejected", "model_missing", "config_error"].includes(status)) return "danger";
  return "warn";
}

const LIVENESS_STATUS_LABEL: Record<string, string> = {
  healthy: "正常",
  empty_response: "空响应",
  rate_limited: "限流(429)",
  auth_failed: "鉴权失败(401)",
  client_rejected: "身份被拒",
  protocol_rejected: "协议不符",
  model_missing: "模型缺失",
  timeout: "超时",
  upstream_error: "上游错误",
  config_error: "配置缺失",
  cancelled: "已取消",
  network_error: "网络异常",
  skipped_provider_missing: "Provider 缺失",
  no_enabled_models: "无启用模型",
};

function livenessStatusLabel(status: string): string {
  return LIVENESS_STATUS_LABEL[status] ?? status;
}

type LivenessResultFilter = "all" | "healthy" | "failed" | "skipped" | "cancelled";

function livenessResultCategory(result: FullLivenessRunResponse["results"][number]): LivenessResultFilter {
  if (result.status === "healthy") return "healthy";
  if (result.status === "cancelled") return "cancelled";
  if (result.skipped) return "skipped";
  return "failed";
}

function livenessProtocolLabel(value?: string | null): string {
  if (value === "chat_completions") return "Chat Completions";
  if (value === "responses") return "Responses";
  if (value === "anthropic_messages") return "Anthropic Messages";
  return value || "未知协议";
}

function livenessIdentityLabel(value?: string | null): string {
  return CLIENT_IDENTITY_LABELS[value || ""] || value || "未知客户端";
}

export function FullLivenessPanel() {
  const retainedState = useRef(readFullLivenessState()).current;
  const [preview, setPreview] = useState<FullLivenessPreviewResponse | null>(retainedState.preview);
  const [result, setResult] = useState<FullLivenessRunResponse | null>(retainedState.result);
  const [maxTokens, setMaxTokens] = useState(256);
  const [timeoutSeconds, setTimeoutSeconds] = useState(90);
  const [globalConcurrency, setGlobalConcurrency] = useState(8);
  const [systemPrompt, setSystemPrompt] = useState(DEFAULT_CHAT_TEST_SYSTEM_PROMPT);
  const [message, setMessage] = useState("用一句话自我介绍，并说明你现在能做什么。");
  const [previewExpanded, setPreviewExpanded] = useState(true);
  const [resultExpanded, setResultExpanded] = useState(true);
  const [resultFilter, setResultFilter] = useState<LivenessResultFilter>("all");
  const [collapsedPreviewProviders, setCollapsedPreviewProviders] = useState<Record<number, boolean>>({});
  const [collapsedResultProviders, setCollapsedResultProviders] = useState<Record<number, boolean>>({});
  const runIdRef = useRef<string | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    writeFullLivenessState({ preview, result });
  }, [preview, result]);

  useEffect(() => () => {
    const activeRunId = runIdRef.current;
    if (pollRef.current != null) window.clearTimeout(pollRef.current);
    pollRef.current = null;
    runIdRef.current = null;
    if (activeRunId) {
      markFullLivenessRunCancelled(activeRunId);
      void cancelFullLiveness(activeRunId)
        .then((next) => {
          const retained = readFullLivenessState();
          writeFullLivenessState({ ...retained, result: next });
        })
        .catch(() => undefined);
    }
  }, []);

  const previewMut = useMutation({
    mutationFn: () => fullLivenessPreview({ max_tokens: maxTokens, global_concurrency: globalConcurrency }),
    onSuccess: (resp) => {
      setPreview(resp);
      setPreviewExpanded(true);
      setCollapsedPreviewProviders({});
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const runMut = useMutation({
    mutationFn: () => fullLivenessRun({
      system_prompt: systemPrompt.trim() || DEFAULT_CHAT_TEST_SYSTEM_PROMPT,
      message: message.trim(),
      max_tokens: maxTokens,
      timeout_seconds: timeoutSeconds,
      global_concurrency: globalConcurrency,
    }),
    onSuccess: (resp) => {
      runIdRef.current = resp.run_id;
      setResult({ run_id: resp.run_id, status: resp.status, task_total: resp.task_total, completed: 0, healthy: 0, failed: 0, skipped: 0, cancelled: 0, results: [] });
      setResultExpanded(true);
      setResultFilter("all");
      setCollapsedResultProviders({});
      const poll = async () => {
        if (!runIdRef.current) return;
        try {
          const next = await fullLivenessStatus(runIdRef.current);
          setResult(next);
          if (next.status === "queued" || next.status === "running") {
            pollRef.current = window.setTimeout(poll, 500);
          } else {
            runIdRef.current = null;
          }
        } catch (err) {
          toast.error(getErrMsg(err));
          runIdRef.current = null;
        }
      };
      void poll();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const running = runMut.isPending || result?.status === "queued" || result?.status === "running";
  const filteredResults = result?.results.filter((item) => (
    resultFilter === "all" || livenessResultCategory(item) === resultFilter
  )) ?? [];
  const groupedResults = filteredResults.reduce<Array<{
    providerId: number;
    providerName: string;
    items: FullLivenessRunResponse["results"];
  }>>((groups, item) => {
    let group = groups.find((candidate) => candidate.providerId === item.provider_id);
    if (!group) {
      group = { providerId: item.provider_id, providerName: item.provider_name, items: [] };
      groups.push(group);
    }
    group.items.push(item);
    return groups;
  }, []);
  const resultCounts: Record<LivenessResultFilter, number> = {
    all: result?.results.length ?? 0,
    healthy: result?.results.filter((item) => livenessResultCategory(item) === "healthy").length ?? 0,
    failed: result?.results.filter((item) => livenessResultCategory(item) === "failed").length ?? 0,
    skipped: result?.results.filter((item) => livenessResultCategory(item) === "skipped").length ?? 0,
    cancelled: result?.results.filter((item) => livenessResultCategory(item) === "cancelled").length ?? 0,
  };
  const filterOptions: Array<{ value: LivenessResultFilter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "healthy", label: "正常" },
    { value: "failed", label: "异常" },
    { value: "skipped", label: "跳过" },
    { value: "cancelled", label: "取消" },
  ];

  return (
    <div className="grid min-h-0 flex-1 gap-4 overflow-hidden lg:grid-cols-[320px_minmax(0,1fr)]">
      <div className="min-h-0 space-y-4 overflow-y-auto rounded-md border bg-muted/20 p-3">
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1.5">
            <Label className="text-xs">最大输出 Token</Label>
            <Input
              type="number"
              min={64}
              max={8000}
              value={maxTokens}
              onChange={(e) => setMaxTokens(Math.max(64, Math.min(8000, Number(e.target.value) || 256)))}
              disabled={running}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">超时秒数</Label>
            <Input
              type="number"
              min={10}
              max={600}
              value={timeoutSeconds}
              onChange={(e) => setTimeoutSeconds(Math.max(10, Math.min(600, Number(e.target.value) || 90)))}
              disabled={running}
            />
          </div>
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs">全局并发</Label>
          <Select
            value={String(globalConcurrency)}
            onChange={(e) => setGlobalConcurrency(Number(e.target.value))}
            disabled={running}
          >
            {[2, 4, 8, 12].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs">系统提示词</Label>
          <Textarea
            value={systemPrompt}
            rows={4}
            maxLength={2000}
            onChange={(e) => setSystemPrompt(e.target.value)}
            disabled={running}
          />
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs">测活词</Label>
          <Textarea
            value={message}
            rows={3}
            maxLength={2000}
            onChange={(e) => setMessage(e.target.value)}
            disabled={running}
          />
        </div>
        <Button
          type="button"
          variant="outline"
          className="w-full"
          onClick={() => previewMut.mutate()}
          disabled={previewMut.isPending || running || !message.trim() || !systemPrompt.trim()}
        >
          {previewMut.isPending ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
          刷新模型范围
        </Button>
      </div>

      <div className="flex min-h-0 flex-col overflow-hidden rounded-md border bg-background">
        <div className="border-b px-3 py-2">
          <div className="text-sm font-medium">全量已启用模型</div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            每个模型收到相同提示词，仅记录脱敏诊断，不修改生产健康状态。
          </div>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-muted/20 p-3 text-xs">
          {preview ? (
            <section className="overflow-hidden rounded-md border bg-background">
              <button
                type="button"
                className="flex min-h-10 w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/35"
                onClick={() => setPreviewExpanded((value) => !value)}
                aria-expanded={previewExpanded}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {previewExpanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
                  <span className="font-medium">模型范围</span>
                </span>
                <span className="flex flex-wrap justify-end gap-1.5">
                  <MetaBadge>Provider {preview.executable_provider_total}/{preview.provider_total}</MetaBadge>
                  <MetaBadge>任务 {preview.task_total}</MetaBadge>
                  <MetaBadge mono>~{preview.max_output_tokens} tok</MetaBadge>
                  {preview.needs_confirmation ? <MetaBadge tone="warn">任务较多</MetaBadge> : null}
                </span>
              </button>
              {previewExpanded ? (
                <div className="divide-y border-t">
                  {preview.providers.map((provider) => {
                    const collapsed = collapsedPreviewProviders[provider.provider_id] === true;
                    return (
                      <div key={provider.provider_id}>
                        <button
                          type="button"
                          className="flex min-h-10 w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/30"
                          onClick={() => setCollapsedPreviewProviders((current) => ({
                            ...current,
                            [provider.provider_id]: !collapsed,
                          }))}
                          aria-expanded={!collapsed}
                        >
                          <span className="flex min-w-0 items-center gap-2">
                            {collapsed ? <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
                            <span className="truncate font-medium">{provider.provider_name}</span>
                          </span>
                          {provider.executable ? (
                            <MetaBadge tone="success">{provider.enabled_models.length} 个模型</MetaBadge>
                          ) : (
                            <MetaBadge tone="warn">{livenessStatusLabel(provider.skipped_reason || "no_enabled_models")}</MetaBadge>
                          )}
                        </button>
                        {!collapsed && provider.enabled_models.length > 0 ? (
                          <div className="flex flex-wrap gap-1.5 bg-muted/20 px-3 pb-3 pt-1">
                            {provider.enabled_models.map((model) => <MetaBadge key={model} mono>{model}</MetaBadge>)}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              ) : null}
            </section>
          ) : (
            <div className="flex min-h-48 items-center justify-center rounded-md border border-dashed bg-background px-4 text-center text-sm text-muted-foreground">
              点击“刷新模型范围”查看每个 Provider 的已启用模型。
            </div>
          )}

          {result ? (
            <section className="overflow-hidden rounded-md border bg-background">
              <button
                type="button"
                className="flex min-h-10 w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/35"
                onClick={() => setResultExpanded((value) => !value)}
                aria-expanded={resultExpanded}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {resultExpanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
                  <span className="font-medium">测活结果</span>
                </span>
                <span className="flex flex-wrap justify-end gap-1.5">
                  <MetaBadge>{result.completed}/{result.task_total}</MetaBadge>
                  <MetaBadge tone="success">正常 {result.healthy}</MetaBadge>
                  <MetaBadge tone="warn">异常 {result.failed}</MetaBadge>
                  {result.skipped ? <MetaBadge>跳过 {result.skipped}</MetaBadge> : null}
                  {result.cancelled ? <MetaBadge>取消 {result.cancelled}</MetaBadge> : null}
                </span>
              </button>
              {resultExpanded ? (
                <div className="border-t">
                  <div className="flex flex-col gap-2 border-b bg-muted/15 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex items-center gap-1.5 text-muted-foreground">
                      <Filter className="h-3.5 w-3.5" />
                      <span>按结果筛选</span>
                    </div>
                    <div className="flex max-w-full gap-1 overflow-x-auto rounded-md bg-muted/60 p-1">
                      {filterOptions.map((option) => (
                        <Button
                          key={option.value}
                          type="button"
                          size="sm"
                          variant={resultFilter === option.value ? "secondary" : "ghost"}
                          className="h-7 shrink-0 px-2.5"
                          onClick={() => setResultFilter(option.value)}
                        >
                          {option.label} {resultCounts[option.value]}
                        </Button>
                      ))}
                    </div>
                  </div>
                  {result.error ? <div className="border-b px-3 py-2 break-words text-amber-600 dark:text-amber-400">{result.error}</div> : null}
                  {groupedResults.length > 0 ? (
                    <div className="divide-y">
                      {groupedResults.map((group) => {
                        const collapsed = collapsedResultProviders[group.providerId] === true;
                        const allProviderResults = result.results.filter((item) => item.provider_id === group.providerId);
                        const healthyCount = allProviderResults.filter((item) => item.status === "healthy").length;
                        return (
                          <div key={group.providerId}>
                            <button
                              type="button"
                              className="flex min-h-11 w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/30"
                              onClick={() => setCollapsedResultProviders((current) => ({
                                ...current,
                                [group.providerId]: !collapsed,
                              }))}
                              aria-expanded={!collapsed}
                            >
                              <span className="flex min-w-0 items-center gap-2">
                                {collapsed ? <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
                                <span className="truncate text-sm font-semibold">{group.providerName}</span>
                              </span>
                              <span className="flex items-center gap-1.5">
                                {resultFilter !== "all" ? <MetaBadge>{group.items.length} 条匹配</MetaBadge> : null}
                                <MetaBadge tone={healthyCount === allProviderResults.length ? "success" : "warn"}>
                                  正常 {healthyCount}/{allProviderResults.length}
                                </MetaBadge>
                              </span>
                            </button>
                            {!collapsed ? (
                              <div className="divide-y border-t bg-muted/10">
                                {group.items.map((item, index) => (
                                  <div key={`${item.provider_id}-${item.model_id}-${index}`} className="px-3 py-2.5">
                                    <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
                                      <div className="min-w-0">
                                        <div className="break-all font-mono text-xs font-semibold">{item.model_id}</div>
                                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                                          {item.effective_api_format ? (
                                            <MetaBadge tone="outline">协议 {livenessProtocolLabel(item.effective_api_format)}</MetaBadge>
                                          ) : null}
                                          {item.client_identity_profile ? (
                                            <MetaBadge tone="info">客户端 {livenessIdentityLabel(item.client_identity_profile)}</MetaBadge>
                                          ) : null}
                                          {item.skipped && !item.effective_api_format && !item.client_identity_profile ? (
                                            <MetaBadge>未发起请求</MetaBadge>
                                          ) : null}
                                          {item.input_tokens || item.output_tokens ? (
                                            <MetaBadge mono>{item.input_tokens}/{item.output_tokens} tok</MetaBadge>
                                          ) : null}
                                        </div>
                                      </div>
                                      <MetaBadge mono tone={livenessStatusTone(item.status)}>
                                        {livenessStatusLabel(item.status)}{item.latency_ms ? ` · ${item.latency_ms}ms` : ""}
                                      </MetaBadge>
                                    </div>
                                    {item.preview ? <div className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">{item.preview}</div> : null}
                                    {item.error ? <div className="mt-2 break-words text-amber-600 dark:text-amber-400">{item.error}</div> : null}
                                    {item.suggestion && item.suggestion !== item.error ? (
                                      <div className="mt-1 break-words text-muted-foreground">建议：{item.suggestion}</div>
                                    ) : null}
                                  </div>
                                ))}
                              </div>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="px-4 py-8 text-center text-sm text-muted-foreground">
                      当前筛选条件下没有结果。
                    </div>
                  )}
                </div>
              ) : null}
            </section>
          ) : null}
        </div>
        <div className="flex justify-end gap-2 border-t p-3">
          {running ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                if (runIdRef.current) {
                  cancelFullLiveness(runIdRef.current)
                    .then(setResult)
                    .catch((err) => toast.error(getErrMsg(err)));
                }
              }}
            >
              停止
            </Button>
          ) : null}
          <Button
            type="button"
            onClick={() => runMut.mutate()}
            disabled={running || !preview || preview.task_total === 0 || !message.trim() || !systemPrompt.trim()}
          >
            {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
            {running ? "测活中…" : "开始全量测活"}
          </Button>
        </div>
      </div>
    </div>
  );
}

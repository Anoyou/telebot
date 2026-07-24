import { cn } from "@/lib/utils";

export type ModelRunMetaUsage = Record<string, unknown> | null | undefined;

function num(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

function str(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  return null;
}

/** 工具调用数：优先实际调用 tool_calls，兼容旧 tool_count（历史消息）。 */
export function resolveToolCallCount(usage: ModelRunMetaUsage): number | null {
  if (!usage || typeof usage !== "object") return null;
  const actual = num(usage.tool_calls);
  if (actual != null) return actual;
  // schema_version>=2 前 tool_count 是暴露数，无法区分；仅当无 tool_calls 时降级
  if (num(usage.schema_version) === 2) return actual;
  return num(usage.tool_count);
}

function formatElapsed(ms: number | null): string | null {
  if (ms == null) return null;
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function formatTokens(value: number | null): string {
  return value == null ? "–" : value.toLocaleString("zh-CN");
}

/** 从 usage.stage_timings 抽出探测/路由/首 token 耗时标签。 */
export function formatStageTimings(usage: ModelRunMetaUsage): string[] {
  if (!usage || typeof usage !== "object") return [];
  const raw = usage.stage_timings;
  if (!raw || typeof raw !== "object") return [];
  const stages = raw as Record<string, unknown>;
  const bits: string[] = [];
  const verify = num(stages.verify_ms);
  const route = num(stages.route_ms);
  const first = num(stages.first_token_ms);
  if (verify != null) bits.push(`探测 ${formatElapsed(verify)}`);
  if (route != null) bits.push(`路由 ${formatElapsed(route)}`);
  if (first != null) bits.push(`首 token ${formatElapsed(first)}`);
  return bits.filter((bit): bit is string => Boolean(bit));
}

/**
 * 模型执行信息行（Agent 回答下方 / 测活结果复用）。
 * 不显示费用。
 */
export function ModelRunMeta({
  usage,
  expected,
  className,
  compact,
}: {
  usage?: ModelRunMetaUsage;
  expected?: { providerName?: string; model?: string } | null;
  className?: string;
  /** 单行紧凑模式（测活列表） */
  compact?: boolean;
}) {
  if (!usage || typeof usage !== "object") return null;

  const provider = str(usage.provider_name);
  const model = str(usage.model);
  const input = num(usage.input_tokens);
  const output = num(usage.output_tokens);
  const tools = resolveToolCallCount(usage);
  const retries = num(usage.retry_count);
  const elapsed = num(usage.elapsed_ms);
  const apiFormat = str(usage.api_format);
  const usedFallback = Boolean(usage.used_fallback);
  const streamFallback = Boolean(usage.stream_fallback);
  const selectionMode = str(usage.selection_mode);
  const reqProvider = str(usage.requested_provider_name);
  const reqModel = str(usage.requested_model);
  const reported = [provider, model].filter(Boolean).join(" · ");
  const requested = [reqProvider, reqModel].filter(Boolean).join(" · ");
  const summaryModel =
    !usedFallback && reqModel && (!reqProvider || reqProvider === provider) ? reqModel : model;
  const summary = [provider || reqProvider, summaryModel].filter(Boolean).join(" · ");
  const expectedLabel = [expected?.providerName, expected?.model].filter(Boolean).join(" · ");
  const mismatch =
    Boolean(expectedLabel) && Boolean(reported) && expectedLabel !== reported;

  const stageBits = formatStageTimings(usage);
  const bits: string[] = [];
  if (summary) bits.push(summary);
  if (input != null || output != null) {
    bits.push(`in ${input ?? "–"} / out ${output ?? "–"}`);
  }
  if (tools != null) bits.push(`工具 ${tools}`);
  if (retries != null && retries > 0) bits.push(`重试 ${retries}`);
  const elapsedLabel = formatElapsed(elapsed);
  if (elapsedLabel) bits.push(elapsedLabel);
  if (apiFormat && !compact) bits.push(apiFormat);

  if (!bits.length && !stageBits.length) return null;

  if (compact) {
    return (
      <div className={cn("text-[11px] leading-4 text-muted-foreground tabular-nums", className)}>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {bits.map((bit) => <span key={bit}>{bit}</span>)}
          {usedFallback ? <span className="rounded border border-warning/40 px-1 text-warning">fallback</span> : null}
          {streamFallback ? <span className="rounded border px-1">完整响应</span> : null}
          {selectionMode === "pinned" ? <span className="rounded border px-1">本轮固定</span> : null}
        </div>
        {stageBits.length ? (
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 opacity-90">
            {stageBits.map((bit) => <span key={bit}>{bit}</span>)}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div
      className={cn(
        "w-full max-w-full space-y-1.5 rounded-lg border border-border/60 bg-muted/20 px-2.5 py-2 text-[11px] leading-4 text-muted-foreground tabular-nums sm:w-auto sm:rounded-none sm:border-0 sm:bg-transparent sm:px-0 sm:py-0",
        className,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        {summary ? (
          <span className={cn("min-w-0 break-all font-medium text-foreground/80", usedFallback && mismatch && "text-warning")}>
            {summary}
          </span>
        ) : null}
        <span className="flex-1" />
        {usedFallback ? <span className="rounded border border-warning/40 px-1 text-warning">fallback</span> : null}
        {streamFallback ? <span className="rounded border px-1">完整响应</span> : null}
        {selectionMode === "pinned" ? <span className="rounded border px-1">本轮固定</span> : null}
      </div>
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        {input != null || output != null ? (
          <span className="inline-flex flex-wrap items-center gap-x-2">
            <span>输入 {formatTokens(input)}</span>
            <span>输出 {formatTokens(output)}</span>
          </span>
        ) : null}
        {tools != null ? <span>工具 {tools}</span> : null}
        {retries != null && retries > 0 ? <span>重试 {retries}</span> : null}
        {elapsedLabel ? <span>{elapsedLabel}</span> : null}
        {apiFormat ? <span className="rounded bg-muted px-1.5 py-0.5">{apiFormat}</span> : null}
      </div>
      {stageBits.length ? (
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-t border-border/50 pt-1.5 text-[10px] sm:border-0 sm:pt-0">
          {stageBits.map((bit) => (
            <span key={bit}>{bit}</span>
          ))}
        </div>
      ) : null}
      {usedFallback && requested && reported && requested !== reported ? (
        <div className="space-y-0.5 border-t border-border/50 pt-1.5 text-[10px] text-muted-foreground sm:border-0 sm:pt-0">
          <div>原模型：{requested}</div>
          <div>实际使用：{reported}</div>
          <div>原因：主模型不可用或已切换</div>
        </div>
      ) : null}
      {mismatch && !usedFallback ? (
        <div className="break-words border-t border-border/50 pt-1.5 text-[10px] text-muted-foreground sm:border-0 sm:pt-0">
          本轮请求 {expectedLabel}；上游返回模型标识 {reported}
        </div>
      ) : null}
    </div>
  );
}

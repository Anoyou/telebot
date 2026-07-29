import { ModelRunMeta, type ModelRunMetaUsage } from "@/components/ai/ModelRunMeta";
import { RunTrace } from "@/components/assistant/RunTrace";
import { cn } from "@/lib/utils";

export type ResponseMetaUsage = ModelRunMetaUsage;

function str(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  return null;
}

/**
 * 回答下方：ModelRunMeta + 可选历史轨迹入口（run_id）。
 * 轨迹展开面板在正文上方由 Conversation 布局控制；此处保留懒加载入口兼容。
 */
export function ResponseMeta({
  usage,
  expected,
  className,
  showTrace = true,
}: {
  usage?: ResponseMetaUsage;
  expected?: { providerName?: string; model?: string } | null;
  className?: string;
  showTrace?: boolean;
}) {
  if (!usage || typeof usage !== "object") return null;
  const runId = str(usage.run_id);

  return (
    <div className={cn("mt-1 space-y-1", className)}>
      <ModelRunMeta usage={usage} expected={expected} />
      {showTrace && runId ? <RunTrace runId={runId} defaultOpen={false} /> : null}
    </div>
  );
}

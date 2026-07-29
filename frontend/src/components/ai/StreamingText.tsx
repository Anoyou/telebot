import { Spinner } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

export function StreamingText({
  text,
  active = false,
  fallback = false,
  waitingLabel = "正在等待上游返回首段内容",
  className,
}: {
  text: string;
  active?: boolean;
  fallback?: boolean;
  waitingLabel?: string;
  className?: string;
}) {
  if (!text && active) {
    return (
      <span className={cn("inline-flex items-center gap-2 text-muted-foreground", className)}>
        <Spinner className="h-4 w-4 text-primary" />
        {waitingLabel}
      </span>
    );
  }

  if (!text) return null;

  return (
    <div className={cn("whitespace-pre-wrap break-words", className)}>
      {fallback ? (
        <span className="mb-2 mr-2 inline-flex rounded-full border px-2 py-0.5 align-middle text-[10px] font-normal text-foreground">
          完整响应
        </span>
      ) : null}
      {active ? <span className="sr-only" role="status" aria-live="polite">正在接收回复</span> : null}
      {text}
      {active && !fallback ? (
        <span
          className="streaming-caret ml-0.5 inline-block h-[1.05em] w-0.5 translate-y-[0.14em] bg-primary"
          aria-hidden="true"
        />
      ) : null}
    </div>
  );
}

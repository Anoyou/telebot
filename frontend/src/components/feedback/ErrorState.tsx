import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function ErrorState({
  title = "读取失败",
  error,
  onRetry,
  className,
}: {
  title?: string;
  error?: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const message = typeof error === "string" ? error : error instanceof Error ? error.message : "暂时无法读取数据，请稍后重试。";
  return (
    <div role="alert" className={cn("flex min-h-36 flex-col items-center justify-center rounded-lg border border-destructive/25 bg-destructive/5 px-4 py-8 text-center", className)}>
      <AlertTriangle className="mb-2 h-5 w-5 text-destructive" />
      <div className="text-sm font-semibold">{title}</div>
      <p className="mt-1 max-w-md text-xs leading-5 text-muted-foreground">{message}</p>
      {onRetry ? <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onRetry}>重试</Button> : null}
    </div>
  );
}

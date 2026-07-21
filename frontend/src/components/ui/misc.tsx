// 占位/分割线小组件
import { cn } from "@/lib/utils";

export function Separator({ className }: { className?: string }) {
  return <div className={cn("h-px w-full bg-border", className)} />;
}

/**
 * 形状由 className 决定：默认文本行，可传 rounded-full 构成头像或 rounded-md 构成内容块。
 * 骨架只负责视觉占位，外层加载状态仍应保留实际的可访问性说明。
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn("skeleton-shimmer h-4 w-full rounded-md", className)}
    />
  );
}

const spinnerSizes = {
  sm: "h-3.5 w-3.5",
  md: "h-4 w-4",
  lg: "h-6 w-6",
} as const;

export function Spinner({ className, size = "md" }: { className?: string; size?: keyof typeof spinnerSizes }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-block shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent motion-reduce:animate-none",
        spinnerSizes[size],
        className,
      )}
    />
  );
}

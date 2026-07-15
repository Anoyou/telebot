import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

type MetaBadgeTone = "neutral" | "success" | "warn" | "danger" | "info" | "outline";

const toneClass: Record<MetaBadgeTone, string> = {
  neutral: "border-transparent bg-muted text-foreground",
  success: "border-transparent bg-success/15 text-success",
  warn: "border-transparent bg-warning/15 text-warning",
  danger: "border-transparent bg-destructive/15 text-destructive",
  info: "border-transparent bg-info/15 text-info",
  outline: "border-border/80 bg-background text-foreground",
};

export function MetaBadge({
  tone = "neutral",
  mono = false,
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & {
  tone?: MetaBadgeTone;
  mono?: boolean;
}) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full shrink-0 items-center gap-1 overflow-hidden text-ellipsis whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-semibold leading-5",
        mono && "font-mono",
        toneClass[tone],
        className,
      )}
      {...props}
    />
  );
}

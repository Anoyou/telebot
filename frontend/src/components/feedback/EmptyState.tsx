import type { ComponentType, ReactNode } from "react";
import { Inbox } from "lucide-react";

import { cn } from "@/lib/utils";

const sizeClasses = {
  sm: "min-h-24 py-5",
  md: "min-h-36 py-8",
  lg: "min-h-48 py-10",
} as const;

export function EmptyState({
  icon: Icon = Inbox,
  title,
  description,
  action,
  size = "md",
  className,
}: {
  icon?: ComponentType<{ className?: string }>;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  size?: keyof typeof sizeClasses;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center rounded-lg border border-dashed px-4 text-center", sizeClasses[size], className)}>
      <Icon className="mb-2 h-5 w-5 text-muted-foreground/70" />
      <div className="text-sm font-medium text-foreground">{title}</div>
      {description ? <p className="mt-1 max-w-md text-xs leading-5 text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}

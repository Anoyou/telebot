import type { ComponentType, ReactNode } from "react";

import { cn } from "@/lib/utils";

type PageShellProps = {
  children: ReactNode;
  className?: string;
};

export function PageShell({ children, className }: PageShellProps) {
  return (
    <div className={cn("w-full min-w-0 space-y-6", className)}>
      {children}
    </div>
  );
}

type PageHeaderProps = {
  title: string;
  description: ReactNode;
  icon: ComponentType<{ className?: string }>;
  actions?: ReactNode;
  signals?: ReactNode;
  aside?: ReactNode;
  size?: "default" | "hero";
};

export function PageHeader({
  title,
  description,
  icon: Icon,
  actions,
  signals,
  aside,
  size = "default",
}: PageHeaderProps) {
  const hero = size === "hero";
  return (
    <section className="rounded-lg border border-border/80 bg-card px-4 py-4 shadow-sm md:px-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-primary/20 bg-primary/10 text-primary">
              <Icon className={cn(hero ? "h-5 w-5" : "h-4 w-4")} />
            </div>
            <div className="min-w-0">
              <h1
                className={cn(
                  "break-words tracking-tight text-foreground",
                  hero ? "text-3xl font-bold" : "text-2xl font-semibold",
                )}
              >
                {title}
              </h1>
              <p className={cn("mt-1 max-w-3xl text-muted-foreground", hero ? "text-base leading-7" : "text-sm leading-6")}>
                {description}
              </p>
            </div>
          </div>
          {signals ? <div className="mt-4 flex flex-wrap gap-2">{signals}</div> : null}
        </div>
        {(aside || actions) ? (
          <div className="flex w-full min-w-0 flex-col gap-3 lg:w-auto lg:items-end">
            {aside}
            {actions ? <div className="flex w-full min-w-0 flex-wrap gap-2 lg:w-auto lg:justify-end">{actions}</div> : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

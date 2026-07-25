import type { ComponentType, ReactNode } from "react";

import {
  Card,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";

export type VisualTone = "primary" | "success" | "warn" | "danger" | "info" | "neutral";
export type StatusToneDomain = "account" | "health" | "network";

export function statusTone(domain: StatusToneDomain, status: string): VisualTone {
  const maps: Record<StatusToneDomain, Record<string, VisualTone>> = {
    account: { active: "success", paused: "neutral", floodwait: "warn", dead: "danger", login_required: "warn" },
    health: { ok: "success", warn: "warn", err: "danger", loading: "neutral" },
    network: { online: "success", error: "warn", loading: "neutral" },
  };
  return maps[domain][status] ?? "neutral";
}

type ToneClasses = {
  rail: string;
  iconWrap: string;
  icon: string;
  pill: string;
  dot: string;
  bar: string;
};

export function toneClasses(tone: VisualTone): ToneClasses {
  const map: Record<VisualTone, ToneClasses> = {
    primary: {
      rail: "bg-primary",
      iconWrap: "bg-primary/10",
      icon: "text-primary",
      pill: "border-primary/20 bg-primary/10",
      dot: "bg-primary",
      bar: "bg-primary",
    },
    success: {
      rail: "bg-success",
      iconWrap: "bg-success/10",
      icon: "text-success",
      pill: "border-success/20 bg-success/10",
      dot: "bg-success",
      bar: "bg-success",
    },
    warn: {
      rail: "bg-warning",
      iconWrap: "bg-warning/10",
      icon: "text-warning",
      pill: "border-warning/25 bg-warning/10",
      dot: "bg-warning",
      bar: "bg-warning",
    },
    danger: {
      rail: "bg-destructive",
      iconWrap: "bg-destructive/10",
      icon: "text-destructive",
      pill: "border-destructive/25 bg-destructive/10",
      dot: "bg-destructive",
      bar: "bg-destructive",
    },
    info: {
      rail: "bg-info",
      iconWrap: "bg-info/10",
      icon: "text-info",
      pill: "border-info/20 bg-info/10",
      dot: "bg-info",
      bar: "bg-info",
    },
    neutral: {
      rail: "bg-border",
      iconWrap: "bg-muted",
      icon: "text-muted-foreground",
      pill: "border-border/70 bg-background/80",
      dot: "bg-muted-foreground",
      bar: "bg-muted-foreground",
    },
  };
  return map[tone];
}

export function SignalPill({
  tone,
  label,
  value,
  className,
}: {
  tone: VisualTone;
  label: string;
  value: ReactNode;
  className?: string;
}) {
  const toneClass = toneClasses(tone);
  return (
    <div
      className={cn(
        "inline-flex min-h-9 max-w-full items-center gap-2 whitespace-nowrap rounded-full border px-3 text-xs shadow-sm",
        toneClass.pill,
        className,
      )}
    >
      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", toneClass.dot)} />
      <span className={cn("shrink-0", tone === "warn" ? "text-foreground/80" : "text-muted-foreground")}>
        {label}
      </span>
      <span className="min-w-0 truncate font-semibold text-foreground">{value}</span>
    </div>
  );
}

export function MeterBar({
  value,
  tone = "neutral",
  className,
}: {
  value?: number | null;
  tone?: VisualTone;
  className?: string;
}) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  const toneClass = toneClasses(tone);
  return (
    <div className={cn("h-1.5 overflow-hidden rounded-full bg-background", className)}>
      <div
        className={cn("h-full rounded-full transition-[width] duration-300", toneClass.bar)}
        style={{ width: `${clamp(value, 2, 100)}%` }}
      />
    </div>
  );
}

export function ToneRailCard({
  icon: Icon,
  title,
  value,
  description,
  tone = "neutral",
  railTone,
  className,
  titleClassName,
  valueClassName,
  actions,
  actionsPlacement = "header",
}: {
  icon: ComponentType<{ className?: string }>;
  title: ReactNode;
  value: ReactNode;
  description?: ReactNode;
  tone?: VisualTone;
  railTone?: VisualTone;
  className?: string;
  titleClassName?: string;
  valueClassName?: string;
  actions?: ReactNode;
  actionsPlacement?: "header" | "footer";
}) {
  const toneClass = toneClasses(tone);
  const railClass = toneClasses(railTone ?? tone).rail;
  return (
    <Card
      className={cn(
        "group relative h-full overflow-hidden transition duration-200 hover:-translate-y-0.5 hover:shadow-md",
        className,
      )}
    >
      <div className={cn("absolute inset-x-0 top-0 h-1", railClass)} />
      <CardHeader className="flex-row items-start justify-between gap-2 space-y-0">
        <div className="min-w-0">
          <CardTitle className={cn("inline-flex max-w-full items-center gap-2 truncate", titleClassName)}>
            <span className={cn("grid h-7 w-7 shrink-0 place-items-center rounded-lg", toneClass.iconWrap)}>
              <Icon className={cn("h-4 w-4", toneClass.icon)} />
            </span>
            <span className="truncate">{title}</span>
          </CardTitle>
          {description ? (
            <CardDescription className="mt-3 text-sm leading-5">
              {description}
            </CardDescription>
          ) : null}
        </div>
        {actions && actionsPlacement === "header" ? (
          <div className="flex shrink-0 items-center gap-1">{actions}</div>
        ) : null}
      </CardHeader>
      <CardFooter className={cn("pt-0", actionsPlacement === "footer" && "justify-between gap-2")}>
        <div
          className={cn(
            "min-w-0",
            valueClassName ?? "truncate text-2xl font-bold tracking-tight",
          )}
        >
          {value}
        </div>
        {actions && actionsPlacement === "footer" ? (
          <div className="flex shrink-0 items-center gap-1">{actions}</div>
        ) : null}
      </CardFooter>
    </Card>
  );
}

export function StatusSummaryPanel({
  icon: Icon,
  title,
  description,
  signals,
  aside,
  actions,
  className,
  titleLevel = "h1",
}: {
  icon: ComponentType<{ className?: string }>;
  title: ReactNode;
  description: ReactNode;
  signals?: ReactNode;
  aside?: ReactNode;
  actions?: ReactNode;
  className?: string;
  titleLevel?: "h1" | "h2";
}) {
  const Heading = titleLevel;
  return (
    <section
      className={cn(
        "relative overflow-hidden rounded-lg border border-border/80 bg-card shadow-md",
        className,
      )}
    >
      <div className="absolute inset-0 pointer-events-none bg-[radial-gradient(circle_at_8%_0%,hsl(var(--primary)/0.10),transparent_28rem),linear-gradient(115deg,hsl(var(--card)),hsl(var(--muted)/0.45))]" />
      <div className="relative grid gap-6 p-5 lg:grid-cols-[minmax(0,1fr)_auto] md:p-6 lg:p-7">
        <div className="min-w-0">
          <div className="inline-flex h-10 w-10 items-center justify-center rounded-xl border border-border/70 bg-background/80 text-primary shadow-sm">
            <Icon className="h-5 w-5" />
          </div>
          <Heading className="mt-4 text-3xl font-bold tracking-tight text-foreground">
            {title}
          </Heading>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground md:text-base">
            {description}
          </p>
          {signals ? <div className="mt-5 flex flex-wrap gap-2">{signals}</div> : null}
        </div>
        {(aside || actions) ? (
          <div className="flex flex-col justify-between gap-4 lg:min-w-64 lg:items-end">
            {aside}
            {actions ? <div className="flex flex-wrap gap-2 lg:justify-end">{actions}</div> : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

export function SectionHeader({
  icon: Icon,
  title,
  description,
  actions,
  meta,
  className,
}: {
  icon?: ComponentType<{ className?: string }>;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col gap-3 md:flex-row md:items-start md:justify-between", className)}>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          {Icon ? <Icon className="h-4 w-4 shrink-0 text-primary" /> : null}
          <div className="min-w-0 truncate text-base font-semibold tracking-tight">
            {title}
          </div>
        </div>
        {description ? (
          <div className="mt-1 text-sm leading-5 text-muted-foreground">
            {description}
          </div>
        ) : null}
      </div>
      {(meta || actions) ? (
        <div className="flex w-full min-w-0 flex-wrap items-center gap-2 md:w-auto md:shrink-0 md:justify-end">
          {meta}
          {actions}
        </div>
      ) : null}
    </div>
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

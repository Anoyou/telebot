import { MetaBadge } from "@/components/ui/meta-badge";
import {
  hasUpstreamErrorFacts,
  type UpstreamErrorFactsValue,
} from "@/lib/upstreamErrorFacts";
import { cn } from "@/lib/utils";

export function UpstreamErrorFacts({
  value,
  className,
}: {
  value: UpstreamErrorFactsValue;
  className?: string;
}) {
  if (!hasUpstreamErrorFacts(value)) return null;

  return (
    <div className={cn("space-y-2 rounded-md border border-border/70 bg-muted/25 px-3 py-2.5", className)}>
      <div className="flex flex-wrap items-center gap-1.5">
        {value.upstream_status_code ? (
          <MetaBadge mono tone={value.upstream_status_code >= 500 ? "warn" : "outline"}>
            上游 HTTP {value.upstream_status_code}
          </MetaBadge>
        ) : null}
        {value.upstream_error_code ? (
          <MetaBadge mono tone="outline">错误码 {value.upstream_error_code}</MetaBadge>
        ) : null}
      </div>
      {value.upstream_error_message ? (
        <div className="whitespace-pre-wrap break-words text-sm text-foreground">
          {value.upstream_error_message}
        </div>
      ) : null}
      {value.upstream_error_detail ? (
        <div className="whitespace-pre-wrap break-all font-mono text-xs leading-5 text-muted-foreground">
          详细信息：{value.upstream_error_detail}
        </div>
      ) : null}
      <dl className="grid gap-1 text-xs text-muted-foreground">
        {value.gateway_request_id ? (
          <div className="grid min-w-0 gap-0.5 sm:grid-cols-[8.5rem_minmax(0,1fr)]">
            <dt>Gateway Request ID</dt>
            <dd className="break-all font-mono text-foreground/80">{value.gateway_request_id}</dd>
          </div>
        ) : null}
        {value.upstream_request_id ? (
          <div className="grid min-w-0 gap-0.5 sm:grid-cols-[8.5rem_minmax(0,1fr)]">
            <dt>上游 Request ID</dt>
            <dd className="break-all font-mono text-foreground/80">{value.upstream_request_id}</dd>
          </div>
        ) : null}
        {value.client_request_id ? (
          <div className="grid min-w-0 gap-0.5 sm:grid-cols-[8.5rem_minmax(0,1fr)]">
            <dt>Client Request ID</dt>
            <dd className="break-all font-mono text-foreground/80">{value.client_request_id}</dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}

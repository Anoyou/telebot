import { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";

/** 顶部"返回 + 标题"行，4 个 feature page 共用。 */
export function RulePageHeader({
  title,
  backLabel = "返回账号",
  backHref,
}: {
  title: string;
  backLabel?: string;
  backHref?: string;
}) {
  const nav = useNavigate();
  return (
    <div className="flex flex-wrap items-center gap-3">
      {backHref ? (
        <Button variant="default" size="sm" className="gap-1.5 shadow-sm" onClick={() => nav(backHref)}>
          <ArrowLeft className="h-4 w-4" /> {backLabel}
        </Button>
      ) : null}
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
    </div>
  );
}

/** "功能总开关" Card，AutoReply/Autorepeat/Forward 共用。 */
export function RuleFeatureToggleCard({
  enabled,
  onToggle,
  description = "关闭后所有规则都不会触发；启用即生效",
  state,
  lastError,
}: {
  enabled: boolean;
  onToggle: (next: boolean) => void;
  description?: string;
  state?: string | null;
  lastError?: string | null;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">功能总开关</CardTitle>
            <CardDescription>{description}</CardDescription>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge variant={enabled ? "default" : "outline"}>
                {enabled ? "已启用" : "未启用"}
              </Badge>
              {state ? <span>状态：{state}</span> : null}
              {lastError ? <span className="text-destructive">最近错误：{lastError}</span> : null}
            </div>
          </div>
          <Switch checked={enabled} onCheckedChange={onToggle} />
        </div>
      </CardHeader>
    </Card>
  );
}

/** 使用说明容器，和单配置插件页保持一致。 */
export function RuleInfoBox({ children }: { children: ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">使用说明</CardTitle>
        <CardDescription>规则保存后立即生效，无需重启 worker。</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
          <ul className="list-inside list-disc space-y-1">{children}</ul>
        </div>
      </CardContent>
    </Card>
  );
}

/** Label + 子内容；4 个文件原本各自定义一份同样的 Field。 */
export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1">
      <Label>{label}</Label>
      {children}
    </div>
  );
}

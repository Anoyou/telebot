import { Bug } from "lucide-react";

import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function DispatchDebugPage() {
  return (
    <PageShell>
      <PageHeader
        icon={Bug}
        title="命中调试"
        description="该页面为波次 6 预埋入口，当前不包含业务功能。"
      />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">功能建设中</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          当前仅用于路由占位。
        </CardContent>
      </Card>
    </PageShell>
  );
}

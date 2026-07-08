import { useNavigate } from "react-router-dom";
import { ArrowLeft, CalendarClock } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { SectionHeader } from "@/components/ui/status";
import { goBackOr } from "@/lib/navigation";
import { SchedulerConfig } from "@/pages/Plugins/configs/Scheduler";
import { PluginWorkspaceNav } from "./WorkspaceNav";

export function PluginsSchedulerPage() {
  const nav = useNavigate();

  return (
    <div className="space-y-4">
      <Button variant="default" size="sm" className="gap-1.5 shadow-sm" onClick={() => goBackOr(nav, "/plugins")}>
        <ArrowLeft className="h-4 w-4" /> 返回上一页
      </Button>
      <Card>
        <CardHeader>
          <SectionHeader
            icon={CalendarClock}
            title="定时任务"
            description="按账号编排定时动作与调度策略，和插件中心启用状态保持一致。"
          />
        </CardHeader>
      </Card>
      <PluginWorkspaceNav activeTab="scheduler" />
      <SchedulerConfig />
    </div>
  );
}

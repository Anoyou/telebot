import { CalendarClock, FileText, ListTodo, ShieldCheck } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { PageHeader } from "@/components/layout/PageScaffold";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

export type OperationsWorkspaceTab = "templates" | "scheduler" | "whitelist";

export function OperationsWorkspaceHeader({ activeTab }: { activeTab: OperationsWorkspaceTab }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const aid = searchParams.get("aid");
  const withAccount = (path: string) => aid ? `${path}?aid=${encodeURIComponent(aid)}` : path;
  const targets: Record<OperationsWorkspaceTab, string> = {
    templates: "/operations/templates",
    scheduler: withAccount("/operations/scheduler"),
    whitelist: withAccount("/operations/auto-command-whitelist"),
  };

  return (
    <>
      <PageHeader
        icon={ListTodo}
        title="指令与任务"
        description="集中管理自定义指令、定时执行与自动触发安全范围。"
      />
      <Tabs value={activeTab} onValueChange={(value) => navigate(targets[value as OperationsWorkspaceTab])}>
        <TabsList className="w-full justify-start overflow-x-auto sm:w-auto">
          <TabsTrigger value="templates" className="gap-1.5">
            <FileText className="h-4 w-4" />自定义指令
          </TabsTrigger>
          <TabsTrigger value="scheduler" className="gap-1.5">
            <CalendarClock className="h-4 w-4" />定时任务
          </TabsTrigger>
          <TabsTrigger value="whitelist" className="gap-1.5">
            <ShieldCheck className="h-4 w-4" />自动指令白名单
          </TabsTrigger>
        </TabsList>
      </Tabs>
    </>
  );
}

import { useNavigate } from "react-router-dom";
import { ArrowLeft, FileText } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { SectionHeader } from "@/components/ui/status";
import { goBackOr } from "@/lib/navigation";
import { CommandTemplates } from "@/pages/Plugins/TemplatesEditor";
import { PluginWorkspaceNav } from "./WorkspaceNav";

export function PluginsTemplatesPage() {
  const nav = useNavigate();

  return (
    <div className="space-y-4">
      <Button variant="default" size="sm" className="gap-1.5 shadow-sm" onClick={() => goBackOr(nav, "/plugins")}>
        <ArrowLeft className="h-4 w-4" /> 返回上一页
      </Button>
      <Card>
        <CardHeader>
          <SectionHeader
            icon={FileText}
            title="指令模板"
            description="统一维护常用回复、转发和 AI 指令模板，供插件中心按账号复用。"
          />
        </CardHeader>
      </Card>
      <PluginWorkspaceNav activeTab="templates" />
      <CommandTemplates />
    </div>
  );
}

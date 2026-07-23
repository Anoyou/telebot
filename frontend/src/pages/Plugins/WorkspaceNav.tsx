import { useNavigate } from "react-router-dom";
import { Boxes, CalendarClock, FileText, PackagePlus, ShieldCheck } from "lucide-react";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

type PluginWorkspaceTab = "home" | "templates" | "scheduler" | "whitelist" | "manage";

interface PluginWorkspaceNavProps {
  activeTab: PluginWorkspaceTab;
  selectedAid?: number | null;
  guideActive?: boolean;
}

export function PluginWorkspaceNav({
  activeTab,
  selectedAid = null,
  guideActive = false,
}: PluginWorkspaceNavProps) {
  const nav = useNavigate();
  const targets: Record<PluginWorkspaceTab, string> = {
    home: "/plugins",
    templates: "/plugins/templates",
    scheduler: "/plugins/scheduler",
    whitelist: selectedAid
      ? `/plugins/auto-command-whitelist?aid=${selectedAid}`
      : "/plugins/auto-command-whitelist",
    manage: "/plugins/manage?tab=plugins",
  };
  const contextualTabs = new Set<PluginWorkspaceTab>(["templates", "scheduler", "whitelist"]);

  return (
    <div className="flex flex-wrap items-center justify-center gap-2 sm:justify-start">
      <Tabs
        className="w-full sm:w-auto"
        value={activeTab}
        onValueChange={(value) => {
          const target = targets[value as PluginWorkspaceTab];
          if (target) nav(target);
        }}
      >
        <TabsList>
          <TabsTrigger value="home" className="gap-1.5">
            <Boxes className="h-4 w-4" />
            插件中心
          </TabsTrigger>
          {contextualTabs.has(activeTab) ? (
            <TabsTrigger value={activeTab} className={`gap-1.5 ${guideActive ? "siri-glow" : ""}`}>
              {activeTab === "templates" ? <FileText className="h-4 w-4" /> : null}
              {activeTab === "scheduler" ? <CalendarClock className="h-4 w-4" /> : null}
              {activeTab === "whitelist" ? <ShieldCheck className="h-4 w-4" /> : null}
              {activeTab === "templates" ? "指令模板" : activeTab === "scheduler" ? "定时任务" : "自动指令白名单"}
            </TabsTrigger>
          ) : null}
          <TabsTrigger value="manage" className={`gap-1.5 ${guideActive ? "siri-glow" : ""}`}>
            <PackagePlus className="h-4 w-4" />
            插件管理
          </TabsTrigger>
        </TabsList>
      </Tabs>
    </div>
  );
}

import { useNavigate } from "react-router-dom";
import { Boxes, PackagePlus } from "lucide-react";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

type PluginWorkspaceTab = "home" | "manage";

interface PluginWorkspaceNavProps {
  activeTab: PluginWorkspaceTab;
  guideActive?: boolean;
}

export function PluginWorkspaceNav({
  activeTab,
  guideActive = false,
}: PluginWorkspaceNavProps) {
  const nav = useNavigate();
  const targets: Record<PluginWorkspaceTab, string> = {
    home: "/plugins",
    manage: "/plugins/manage?tab=plugins",
  };

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
          <TabsTrigger value="manage" className={`gap-1.5 ${guideActive ? "siri-glow" : ""}`}>
            <PackagePlus className="h-4 w-4" />
            插件管理
          </TabsTrigger>
        </TabsList>
      </Tabs>
    </div>
  );
}

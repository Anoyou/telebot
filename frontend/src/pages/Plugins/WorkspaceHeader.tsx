import { Boxes } from "lucide-react";

import { PageHeader } from "@/components/layout/PageScaffold";

import { PluginWorkspaceNav } from "./WorkspaceNav";

type PluginWorkspaceTab = "home" | "manage";

type PluginWorkspaceHeaderProps = {
  activeTab: PluginWorkspaceTab;
  guideActive?: boolean;
};

export function PluginWorkspaceHeader({
  activeTab,
  guideActive = false,
}: PluginWorkspaceHeaderProps) {
  return (
    <>
      <PageHeader
        icon={Boxes}
        title="插件中心"
        description="先在这里沉淀一套好用的指令、消息和 AI 模板，再按账号启用复用；新账号不用从零重配。"
      />
      <PluginWorkspaceNav activeTab={activeTab} guideActive={guideActive} />
    </>
  );
}

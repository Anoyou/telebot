import { Boxes } from "lucide-react";

import { PageHeader } from "@/components/layout/PageScaffold";

import { PluginWorkspaceNav } from "./WorkspaceNav";

type PluginWorkspaceTab = "home" | "templates" | "scheduler" | "whitelist" | "manage";

type PluginWorkspaceHeaderProps = {
  activeTab: PluginWorkspaceTab;
  selectedAid?: number | null;
};

export function PluginWorkspaceHeader({
  activeTab,
  selectedAid = null,
}: PluginWorkspaceHeaderProps) {
  return (
    <>
      <PageHeader
        icon={Boxes}
        title="插件中心"
        description="管理插件安装、指令模板和自动化能力。切换子页时保持同一工作台抬头与操作位置。"
      />
      <PluginWorkspaceNav activeTab={activeTab} selectedAid={selectedAid} />
    </>
  );
}

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Boxes } from "lucide-react";

import { getFeatureMatrix } from "@/api/features";
import { PageHeader } from "@/components/layout/PageScaffold";
import { SignalPill } from "@/components/ui/status";
import { isPlatformFeature } from "@/lib/plugin-modes";

import { PluginWorkspaceNav } from "./WorkspaceNav";

type PluginWorkspaceTab = "home" | "manage";

type PluginWorkspaceHeaderProps = {
  activeTab: PluginWorkspaceTab;
  selectedAid?: number | null;
  guideActive?: boolean;
};

export function PluginWorkspaceHeader({
  activeTab,
  selectedAid = null,
  guideActive = false,
}: PluginWorkspaceHeaderProps) {
  const matrixQ = useQuery({
    queryKey: ["matrix"],
    queryFn: getFeatureMatrix,
  });
  const accounts = matrixQ.data?.accounts ?? [];
  const pluginCount = useMemo(
    () => (matrixQ.data?.features ?? []).filter(
      (feature) => !isPlatformFeature(feature) && feature.key !== "forward",
    ).length,
    [matrixQ.data?.features],
  );
  const selectedAccount = accounts.find((account) => account.id === selectedAid) ?? accounts[0];

  return (
    <>
      <PageHeader
        icon={Boxes}
        title="插件中心"
        description="先在这里沉淀一套好用的指令、消息和 AI 模板，再按账号启用复用；新账号不用从零重配。"
        signals={(
          <>
            <SignalPill tone="primary" label="插件总数" value={matrixQ.isLoading ? "…" : pluginCount} />
            <SignalPill tone="success" label="账号数量" value={matrixQ.isLoading ? "…" : accounts.length} />
            <SignalPill tone="neutral" label="当前账号" value={selectedAccount?.name ?? "未选择"} />
          </>
        )}
      />
      <PluginWorkspaceNav activeTab={activeTab} guideActive={guideActive} />
    </>
  );
}

import { SchedulerConfig } from "@/pages/Plugins/configs/Scheduler";
import { PageShell } from "@/components/layout/PageScaffold";
import { PluginWorkspaceHeader } from "./WorkspaceHeader";

export function PluginsSchedulerPage() {
  return (
    <PageShell>
      <PluginWorkspaceHeader activeTab="scheduler" />
      <SchedulerConfig />
    </PageShell>
  );
}

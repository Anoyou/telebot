import { PageShell } from "@/components/layout/PageScaffold";
import { SchedulerConfig } from "@/pages/Plugins/configs/Scheduler";
import { OperationsWorkspaceHeader } from "./WorkspaceHeader";

export function OperationsSchedulerPage() {
  return (
    <PageShell>
      <OperationsWorkspaceHeader activeTab="scheduler" />
      <SchedulerConfig />
    </PageShell>
  );
}

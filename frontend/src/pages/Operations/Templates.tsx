import { PageShell } from "@/components/layout/PageScaffold";
import { CommandTemplates } from "@/pages/Plugins/TemplatesEditor";
import { OperationsWorkspaceHeader } from "./WorkspaceHeader";

export function OperationsTemplatesPage() {
  return (
    <PageShell>
      <OperationsWorkspaceHeader activeTab="templates" />
      <CommandTemplates />
    </PageShell>
  );
}

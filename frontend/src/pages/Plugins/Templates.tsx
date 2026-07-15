import { CommandTemplates } from "@/pages/Plugins/TemplatesEditor";
import { PageShell } from "@/components/layout/PageScaffold";
import { PluginWorkspaceHeader } from "./WorkspaceHeader";

export function PluginsTemplatesPage() {
  return (
    <PageShell>
      <PluginWorkspaceHeader activeTab="templates" />
      <CommandTemplates />
    </PageShell>
  );
}

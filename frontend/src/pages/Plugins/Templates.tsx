import { CommandTemplates } from "@/pages/Plugins/TemplatesEditor";
import { PluginWorkspaceNav } from "./WorkspaceNav";

export function PluginsTemplatesPage() {
  return (
    <div className="space-y-4">
      <PluginWorkspaceNav activeTab="templates" />
      <CommandTemplates />
    </div>
  );
}

import { SchedulerConfig } from "@/pages/Plugins/configs/Scheduler";
import { PluginWorkspaceNav } from "./WorkspaceNav";

export function PluginsSchedulerPage() {
  return (
    <div className="space-y-4">
      <PluginWorkspaceNav activeTab="scheduler" />
      <SchedulerConfig />
    </div>
  );
}

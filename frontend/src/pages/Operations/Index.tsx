import { Navigate, Route, Routes } from "react-router-dom";

import { OperationsAutoCommandWhitelistPage } from "./AutoCommandWhitelist";
import { OperationsSchedulerPage } from "./Scheduler";
import { OperationsTemplatesPage } from "./Templates";

/**
 * 指令与任务共用一个懒加载边界。首次进入时一次加载整个工作区，后续切换
 * 只替换子页内容，不再卸载工作区壳或闪回全页加载骨架。
 */
export function OperationsWorkspaceRoutes() {
  return (
    <Routes>
      <Route index element={<Navigate to="/operations/templates" replace />} />
      <Route path="templates" element={<OperationsTemplatesPage />} />
      <Route path="scheduler" element={<OperationsSchedulerPage />} />
      <Route path="auto-command-whitelist" element={<OperationsAutoCommandWhitelistPage />} />
      <Route path="*" element={<Navigate to="/operations/templates" replace />} />
    </Routes>
  );
}

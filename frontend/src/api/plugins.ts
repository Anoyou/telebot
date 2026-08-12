import { api } from "@/lib/api";

export interface PluginInstallOut {
  key: string;
  source: "builtin" | "zip" | "repo" | "local" | "git" | string;
  source_url?: string | null;
  source_label?: string | null;
  version: string;
  enabled: boolean;
  signature_ok: boolean | null;
  installed_path: string;
  manifest?: Record<string, unknown> | null;
  installed_at: string;
  updated_at: string;
}

export interface InstalledPluginOverviewTraceRef {
  trace_id: string;
  account_id: number | null;
  status: string | null;
  event_type: string | null;
  source_channel: string | null;
  started_at: string | null;
}

export interface InstalledPluginOverviewLoadError {
  source: string;
  account_id: number | null;
  load_status: string | null;
  message: string;
  updated_at: string | null;
}

export interface InstalledPluginOverviewAccountItem {
  account_id: number;
  account_name: string;
  enabled: boolean;
  state: string;
  last_error?: string | null;
  load_status?: string | null;
  last_load_error?: string | null;
  last_trace?: InstalledPluginOverviewTraceRef | null;
}

export interface InstalledPluginOverviewUpdateStatus {
  update_available: boolean;
  latest_version?: string | null;
  last_update_check_at?: string | null;
  last_update_check_error?: string | null;
}

export interface InstalledPluginOverviewItem {
  key: string;
  display_name: string;
  source: "builtin" | "zip" | "repo" | "local" | "git" | string;
  source_url?: string | null;
  source_label?: string | null;
  version?: string | null;
  global_enabled: boolean;
  signature_ok?: boolean | null;
  trust_tier?: string | null;
  lint_warnings: string[];
  update: InstalledPluginOverviewUpdateStatus;
  accounts: InstalledPluginOverviewAccountItem[];
  recent_load_error?: InstalledPluginOverviewLoadError | null;
  recent_trace?: InstalledPluginOverviewTraceRef | null;
}

export interface PluginChangelogResponse {
  key: string;
  available: boolean;
  content: string;
  truncated: boolean;
  message?: string | null;
}

export interface PluginBatchStateResult {
  enabled: boolean;
  succeeded: number;
  failed: number;
  reloaded_accounts: number;
  items: Array<{
    key: string;
    ok: boolean;
    code?: string | null;
    message?: string | null;
    plugin?: PluginInstallOut | null;
  }>;
}

export interface PluginInstallHistoryItem {
  id: number;
  plugin_key: string;
  event_type: "installed" | "updated" | "enabled" | "disabled" | "uninstalled" | string;
  version?: string | null;
  previous_version?: string | null;
  source?: string | null;
  source_label?: string | null;
  enabled?: boolean | null;
  signature_ok?: boolean | null;
  detail?: string | null;
  created_at: string;
}

export async function listInstalledPackages(): Promise<PluginInstallOut[]> {
  const { data } = await api.get<PluginInstallOut[]>(
    "/api/plugins/installed-packages",
  );
  return data;
}

export async function listInstalledOverview(): Promise<InstalledPluginOverviewItem[]> {
  const { data } = await api.get<InstalledPluginOverviewItem[]>(
    "/api/plugins/installed-overview",
  );
  return data;
}

export async function getInstalledPluginChangelog(key: string): Promise<PluginChangelogResponse> {
  const { data } = await api.get<PluginChangelogResponse>(
    `/api/plugins/install/${encodeURIComponent(key)}/changelog`,
  );
  return data;
}

export async function uploadPluginZip(
  file: File,
  signature?: File | null,
): Promise<PluginInstallOut> {
  const form = new FormData();
  form.append("file", file);
  if (signature) form.append("signature_file", signature);
  const { data } = await api.post<PluginInstallOut>(
    "/api/plugins/install/upload",
    form,
    {
      headers: { "Content-Type": "multipart/form-data" },
    },
  );
  return data;
}

export async function enableInstall(key: string): Promise<PluginInstallOut> {
  const { data } = await api.post<PluginInstallOut>(
    `/api/plugins/install/${encodeURIComponent(key)}/enable`,
  );
  return data;
}

export async function disableInstall(key: string): Promise<PluginInstallOut> {
  const { data } = await api.post<PluginInstallOut>(
    `/api/plugins/install/${encodeURIComponent(key)}/disable`,
  );
  return data;
}

export async function batchSetInstallState(
  keys: string[],
  enabled: boolean,
): Promise<PluginBatchStateResult> {
  const { data } = await api.post<PluginBatchStateResult>(
    "/api/plugins/install/batch-state",
    { keys, enabled },
  );
  return data;
}

export async function listPluginInstallHistory(
  key: string,
): Promise<PluginInstallHistoryItem[]> {
  const { data } = await api.get<PluginInstallHistoryItem[]>(
    `/api/plugins/install/${encodeURIComponent(key)}/history`,
  );
  return data;
}

export async function uninstallPlugin(key: string): Promise<void> {
  await api.delete(`/api/plugins/install/${encodeURIComponent(key)}`);
}

import { api } from "@/lib/api";
import type { RemotePlugin } from "@/types/remotePlugin";
import type {
  InstallFromRepoBody,
  PluginRepo,
  PluginRepoBulkUpdateResult,
  PluginRepoCreate,
  PluginRepoCredentialUpdate,
  PluginRepoPlugin,
} from "@/types/pluginRepo";

const BASE = "/api/plugin-repos";
const PLUGIN_REPO_GIT_TIMEOUT_MS = 120000;

export async function fetchPluginRepos(): Promise<PluginRepo[]> {
  const { data } = await api.get<PluginRepo[]>(BASE);
  return data;
}

export async function addPluginRepo(body: PluginRepoCreate): Promise<PluginRepo> {
  const { data } = await api.post<PluginRepo>(BASE, body);
  return data;
}

export async function updatePluginRepoCredential(
  id: number,
  body: PluginRepoCredentialUpdate,
): Promise<PluginRepo> {
  const { data } = await api.put<PluginRepo>(`${BASE}/${id}/credential`, body);
  return data;
}

export async function deletePluginRepo(
  id: number,
): Promise<{ ok: boolean; id: number }> {
  const { data } = await api.delete(`${BASE}/${id}`);
  return data;
}

export async function fetchRepoPlugins(
  repoId: number,
): Promise<PluginRepoPlugin[]> {
  const { data } = await api.get<PluginRepoPlugin[]>(
    `${BASE}/${repoId}/plugins`,
    { timeout: PLUGIN_REPO_GIT_TIMEOUT_MS },
  );
  return data;
}

export async function refreshRepoPlugins(
  repoId: number,
): Promise<PluginRepoPlugin[]> {
  const { data } = await api.post<PluginRepoPlugin[]>(
    `${BASE}/${repoId}/refresh`,
    undefined,
    { timeout: PLUGIN_REPO_GIT_TIMEOUT_MS },
  );
  return data;
}

export async function updateInstalledPluginsFromRepo(
  repoId: number,
): Promise<PluginRepoBulkUpdateResult> {
  const { data } = await api.post<PluginRepoBulkUpdateResult>(
    `${BASE}/${repoId}/update-installed`,
    undefined,
    { timeout: PLUGIN_REPO_GIT_TIMEOUT_MS },
  );
  return data;
}

export async function installFromRepo(
  repoId: number,
  pluginName: string,
  body?: InstallFromRepoBody,
): Promise<RemotePlugin> {
  const { data } = await api.post<RemotePlugin>(
    `${BASE}/${repoId}/plugins/${encodeURIComponent(pluginName)}/install`,
    body ?? {},
    { timeout: PLUGIN_REPO_GIT_TIMEOUT_MS },
  );
  return data;
}

export async function fetchLocalPlugins(): Promise<PluginRepoPlugin[]> {
  const { data } = await api.get<PluginRepoPlugin[]>(
    `${BASE}/local/plugins`,
  );
  return data;
}

export async function fetchOfficialPlugins(): Promise<PluginRepoPlugin[]> {
  const { data } = await api.get<PluginRepoPlugin[]>(
    `${BASE}/official/plugins`,
  );
  return data;
}

export async function installLocalPlugin(
  pluginName: string,
  body?: InstallFromRepoBody,
): Promise<RemotePlugin> {
  const { data } = await api.post<RemotePlugin>(
    `${BASE}/local/plugins/${encodeURIComponent(pluginName)}/install`,
    body ?? {},
  );
  return data;
}

export async function installOfficialPlugin(
  pluginName: string,
  body?: InstallFromRepoBody,
): Promise<RemotePlugin> {
  const { data } = await api.post<RemotePlugin>(
    `${BASE}/official/plugins/${encodeURIComponent(pluginName)}/install`,
    body ?? {},
  );
  return data;
}

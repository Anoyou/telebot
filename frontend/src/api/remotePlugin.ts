import { api } from "@/lib/api";
import type { RemotePlugin, InstallRequest, AccountPluginAction, RemotePluginUpdateCheckResponse } from "@/types/remotePlugin";

const BASE = "/api/remote-plugins";
// git clone/pull 可能较慢；与 pluginRepo 对齐放宽到 120s。
const REMOTE_PLUGIN_GIT_TIMEOUT_MS = 120_000;

export async function fetchRemotePlugins(): Promise<RemotePlugin[]> {
  const { data } = await api.get<RemotePlugin[]>(BASE);
  return data;
}

export async function installRemotePlugin(
  body: InstallRequest
): Promise<RemotePlugin> {
  const { data } = await api.post<RemotePlugin>(`${BASE}/install`, body, {
    timeout: REMOTE_PLUGIN_GIT_TIMEOUT_MS,
  });
  return data;
}

export async function enableRemotePlugin(
  name: string
): Promise<{ ok: boolean; name: string; enabled: boolean; applied?: number }> {
  const { data } = await api.post(`${BASE}/${encodeURIComponent(name)}/enable`);
  return data;
}

export async function disableRemotePlugin(
  name: string
): Promise<{ ok: boolean; name: string; enabled: boolean }> {
  const { data } = await api.post(
    `${BASE}/${encodeURIComponent(name)}/disable`
  );
  return data;
}

export async function enableRemotePluginForAccounts(
  name: string,
  body: AccountPluginAction
): Promise<{ ok: boolean; name: string; applied: number }> {
  const { data } = await api.post(
    `${BASE}/${encodeURIComponent(name)}/enable-accounts`,
    body
  );
  return data;
}

export async function disableRemotePluginForAccounts(
  name: string,
  body: AccountPluginAction
): Promise<{ ok: boolean; name: string; applied: number }> {
  const { data } = await api.post(
    `${BASE}/${encodeURIComponent(name)}/disable-accounts`,
    body
  );
  return data;
}

export async function updateRemotePlugin(name: string): Promise<RemotePlugin> {
  const { data } = await api.post<RemotePlugin>(
    `${BASE}/${encodeURIComponent(name)}/update`,
    undefined,
    { timeout: REMOTE_PLUGIN_GIT_TIMEOUT_MS },
  );
  return data;
}

export async function checkRemotePluginUpdates(): Promise<RemotePluginUpdateCheckResponse> {
  const { data } = await api.post<RemotePluginUpdateCheckResponse>(
    `${BASE}/check-updates`,
    undefined,
    { timeout: REMOTE_PLUGIN_GIT_TIMEOUT_MS },
  );
  return data;
}

export async function uninstallRemotePlugin(
  name: string
): Promise<{ ok: boolean; name: string }> {
  const { data } = await api.delete(
    `${BASE}/${encodeURIComponent(name)}`
  );
  return data;
}

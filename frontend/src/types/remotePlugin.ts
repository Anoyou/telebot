import type { PluginCapabilities, PluginEventSubscription } from "@/types/pluginContract";

export interface RemotePlugin {
  id: number;
  name: string;
  display_name: string;
  description: string;
  usage?: string | null;
  author: string;
  source_url: string;
  version: string;
  latest_version?: string | null;
  update_available?: boolean;
  last_update_check_at?: string | null;
  last_update_check_error?: string | null;
  lint_warnings?: string[];
  event_subscriptions?: PluginEventSubscription[];
  capabilities?: PluginCapabilities;
  permissions?: string[];
  enabled: boolean;
  default_enabled: boolean;
  installed_at: string | null;
}

export interface InstallRequest {
  source_url: string;
  default_enabled?: boolean;
}

export interface AccountPluginAction {
  account_ids: number[];
}

export interface RemotePluginUpdateCheckResponse {
  total: number;
  checked: number;
  update_available: number;
  failed: number;
}

import type { PluginCapabilities, PluginEventSubscription } from "@/types/pluginContract";

export interface PluginRepo {
  id: number;
  name: string;
  url: string;
  description: string;
  auth_type: "none" | "github_token" | string;
  has_credentials: boolean;
  added_at: string | null;
  updated_at: string | null;
}

export interface PluginRepoCredentialUpdate {
  auth_type?: "none" | "github_token" | string | null;
  token?: string | null;
}

export interface PluginRepoCreate {
  url: string;
  name?: string | null;
  description?: string | null;
  credential?: PluginRepoCredentialUpdate | null;
}

export interface PluginRepoPlugin {
  name: string;
  display_name: string;
  description: string;
  usage?: string | null;
  author: string;
  version: string;
  installed: boolean;
  installed_version?: string | null;
  update_available?: boolean;
  event_subscriptions?: PluginEventSubscription[];
  capabilities?: PluginCapabilities;
  permissions?: string[];
  tags?: string[];
  subdir: string;
}

export interface InstallFromRepoBody {
  default_enabled?: boolean;
}

export interface PluginRepoBulkUpdateItem {
  name: string;
  display_name: string;
  from_version?: string | null;
  to_version?: string | null;
  status: "updated" | "skipped" | "failed" | string;
  message: string;
}

export interface PluginRepoBulkUpdateResult {
  repo_id: number;
  repo_name: string;
  checked: number;
  update_available: number;
  updated: number;
  skipped: number;
  failed: number;
  items: PluginRepoBulkUpdateItem[];
}

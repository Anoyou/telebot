import { api } from "@/lib/api";

export interface WebhookHook {
  key: string;
  label: string;
  enabled: boolean;
}

export interface WebhookRateLimit {
  action: string;
  per_second?: number | null;
  per_minute?: number | null;
  per_hour?: number | null;
  per_day?: number | null;
}

export interface AccountWebhookConfig {
  account_id: number;
  token: string;
  token_header: string;
  token_storage: string;
  hooks: WebhookHook[];
  max_body_bytes: number;
  rate_limit: WebhookRateLimit;
}

export async function getAccountWebhookConfig(aid: number): Promise<AccountWebhookConfig> {
  const { data } = await api.get<AccountWebhookConfig>(`/api/webhooks/${aid}`);
  return data;
}

export async function resetAccountWebhookToken(aid: number): Promise<AccountWebhookConfig> {
  const { data } = await api.post<AccountWebhookConfig>(`/api/webhooks/${aid}/token/reset`);
  return data;
}

import { api } from "@/lib/api";

export type LedgerDirection = "in" | "out";

export interface LedgerQueryParams {
  since?: string;
  until?: string;
  account_id?: string;
  chat_id?: string;
  plugin_key?: string;
  direction?: LedgerDirection;
  amount?: string;
  amount_min?: string;
  amount_max?: string;
  status?: string;
  limit?: number;
}

export interface LedgerEntry {
  id: number;
  source: string;
  source_id: number;
  direction: LedgerDirection;
  amount: string;
  signed_amount: string;
  status: string;
  account_id: number;
  chat_id: number | null;
  plugin_key: string | null;
  entry_key: string | null;
  channel: string | null;
  session_key: string | null;
  action_type: string;
  payout_key: string | null;
  error_code: string | null;
  created_at: string;
  params_summary: Record<string, unknown>;
}

export interface LedgerEntriesResponse {
  items: LedgerEntry[];
}

export interface LedgerSummaryBucket {
  key: string;
  label: string;
  income: string;
  payout: string;
  net: string;
  count: number;
}

export interface LedgerSummary {
  income: string;
  payout: string;
  net: string;
  count: number;
  by_day: LedgerSummaryBucket[];
  by_chat: LedgerSummaryBucket[];
}

export interface LedgerCompensation {
  id: number;
  payout_key: string;
  account_id: number;
  trace_id: string | null;
  plugin_key: string | null;
  entry_key: string | null;
  origin: string;
  chat_id: number;
  amount: string;
  status: string;
  error_code_first: string | null;
  error_code_last: string | null;
  error_last: string | null;
  ambiguous: boolean;
  retry_count: number;
  next_attempt_at: string;
  sent_message_id: number | null;
  sent_at: string | null;
  notified_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface LedgerCompensationsResponse {
  items: LedgerCompensation[];
}

function cleanParams<T extends object>(params?: T): Partial<T> {
  if (!params) return {};
  return Object.fromEntries(
    Object.entries(params as Record<string, unknown>).filter(
      ([, value]) => value !== undefined && value !== null && value !== "",
    ),
  ) as Partial<T>;
}

export async function listLedgerEntries(params?: LedgerQueryParams): Promise<LedgerEntriesResponse> {
  const { data } = await api.get<LedgerEntriesResponse>("/api/ledger", {
    params: cleanParams(params),
  });
  return data;
}

export async function getLedgerSummary(params?: Omit<LedgerQueryParams, "limit">): Promise<LedgerSummary> {
  const { data } = await api.get<LedgerSummary>("/api/ledger/summary", {
    params: cleanParams(params),
  });
  return data;
}

export async function listLedgerCompensations(params?: {
  account_id?: string;
  chat_id?: string;
  plugin_key?: string;
  limit?: number;
}): Promise<LedgerCompensationsResponse> {
  const { data } = await api.get<LedgerCompensationsResponse>("/api/ledger/compensations", {
    params: cleanParams(params),
  });
  return data;
}

export async function markLedgerCompensationManualPaid(
  compensationId: number,
  note?: string,
): Promise<LedgerCompensation> {
  const { data } = await api.post<LedgerCompensation>(
    `/api/ledger/compensations/${compensationId}/manual-paid`,
    { note: note || null },
  );
  return data;
}

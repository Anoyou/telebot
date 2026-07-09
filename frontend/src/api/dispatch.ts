import { api } from "@/lib/api";

export interface DispatchSimulateRequest {
  account_id: number;
  chat_type: string;
  chat_id?: number | null;
  sender_id?: number | null;
  text: string;
  via: string;
}

export interface DispatchTraceChat {
  chat_id?: number | null;
  sender_id?: number | null;
  direction?: string | null;
  edited?: boolean | null;
}

export interface DispatchTraceStage {
  stage: string;
  matched: boolean;
  reason_code: string;
  message: string;
  matches?: Array<Record<string, unknown>>;
  candidates?: Array<Record<string, unknown>>;
  decisions?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface DispatchTrace {
  account_id: number | null;
  via: string;
  chat: DispatchTraceChat;
  text: string;
  stages: DispatchTraceStage[];
}

export async function simulateDispatch(
  payload: DispatchSimulateRequest,
): Promise<DispatchTrace> {
  const { data } = await api.post<DispatchTrace>("/api/dispatch/simulate", payload);
  return data;
}

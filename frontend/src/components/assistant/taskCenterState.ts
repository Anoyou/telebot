import type { SystemAgentQueueItem, SystemAgentRun } from "@/api/systemAgent";

const RUN_STATUS_PRIORITY: Record<string, number> = {
  waiting_approval: 0,
  waiting_input: 1,
  running: 2,
  queued: 3,
  failed: 4,
  succeeded: 5,
  cancelled: 6,
};

export type SystemAgentRunSettlement =
  | "waiting"
  | "complete"
  | "failed"
  | "cancelled";

export function classifySystemAgentRunSettlement(
  status: string,
): SystemAgentRunSettlement {
  if (status === "waiting_input" || status === "waiting_approval") return "waiting";
  if (status === "succeeded") return "complete";
  if (status === "cancelled") return "cancelled";
  return "failed";
}

function timestamp(value?: string | null): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function sortSystemAgentRuns(runs: SystemAgentRun[]): SystemAgentRun[] {
  return [...runs].sort((left, right) => {
    const statusDelta =
      (RUN_STATUS_PRIORITY[left.status] ?? 99) -
      (RUN_STATUS_PRIORITY[right.status] ?? 99);
    if (statusDelta !== 0) return statusDelta;
    const timeDelta =
      timestamp(right.updated_at || right.created_at) -
      timestamp(left.updated_at || left.created_at);
    return timeDelta || left.id.localeCompare(right.id);
  });
}

export function sortSystemAgentQueue(
  queue: SystemAgentQueueItem[],
): SystemAgentQueueItem[] {
  return [...queue].sort((left, right) => {
    const positionDelta = left.position - right.position;
    if (positionDelta !== 0) return positionDelta;
    const timeDelta = timestamp(left.created_at) - timestamp(right.created_at);
    return timeDelta || left.id.localeCompare(right.id);
  });
}

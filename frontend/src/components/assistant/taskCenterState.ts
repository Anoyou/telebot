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

const OPEN_RUN_STATUSES = new Set([
  "queued",
  "running",
  "waiting_input",
  "waiting_approval",
]);

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

export function sessionRunStatusById(
  runs: SystemAgentRun[],
): Record<string, string> {
  const statuses: Record<string, string> = {};

  // 未结束的运行需要持续提示；同一会话出现多个未结束记录时沿用任务中心优先级。
  for (const run of sortSystemAgentRuns(runs)) {
    if (
      statuses[run.session_id] === undefined
      && OPEN_RUN_STATUSES.has(run.status)
    ) {
      statuses[run.session_id] = run.status;
    }
  }

  // 已结束的运行只看该会话最新一次结果，避免历史失败覆盖后续成功。
  const latestBySession = new Map<string, SystemAgentRun>();
  for (const run of runs) {
    const current = latestBySession.get(run.session_id);
    if (
      current === undefined
      || timestamp(run.updated_at || run.created_at)
        > timestamp(current.updated_at || current.created_at)
    ) {
      latestBySession.set(run.session_id, run);
    }
  }
  for (const [sessionId, run] of latestBySession) {
    if (statuses[sessionId] === undefined && run.status === "failed") {
      statuses[sessionId] = run.status;
    }
  }

  return statuses;
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

export const ACTIVE_UPDATE_JOB_STORAGE_KEY = "telepilot.active-update-job.v1";

const DEFAULT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const MAX_RETRY_DELAY_MS = 15_000;

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface PersistedUpdateJob<TPlan = unknown> {
  jobId: string;
  plan: TPlan;
  savedAt: number;
}

export function saveActiveUpdateJob<TPlan>(
  storage: StorageLike | null | undefined,
  job: PersistedUpdateJob<TPlan>,
): void {
  if (!storage) return;
  try {
    storage.setItem(ACTIVE_UPDATE_JOB_STORAGE_KEY, JSON.stringify(job));
  } catch {
    // 浏览器隐私模式或存储空间不足时，仍允许本次页面继续轮询。
  }
}

export function loadActiveUpdateJob<TPlan>(
  storage: StorageLike | null | undefined,
  now = Date.now(),
  maxAgeMs = DEFAULT_MAX_AGE_MS,
): PersistedUpdateJob<TPlan> | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(ACTIVE_UPDATE_JOB_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PersistedUpdateJob<TPlan>>;
    const valid =
      typeof parsed.jobId === "string" &&
      parsed.jobId.trim().length > 0 &&
      typeof parsed.savedAt === "number" &&
      Number.isFinite(parsed.savedAt) &&
      parsed.savedAt > 0 &&
      parsed.savedAt <= now &&
      now - parsed.savedAt <= maxAgeMs &&
      parsed.plan != null &&
      typeof parsed.plan === "object";
    if (!valid) {
      storage.removeItem(ACTIVE_UPDATE_JOB_STORAGE_KEY);
      return null;
    }
    return parsed as PersistedUpdateJob<TPlan>;
  } catch {
    try {
      storage.removeItem(ACTIVE_UPDATE_JOB_STORAGE_KEY);
    } catch {
      // 存储不可写时忽略清理失败。
    }
    return null;
  }
}

export function clearActiveUpdateJob(storage: StorageLike | null | undefined): void {
  if (!storage) return;
  try {
    storage.removeItem(ACTIVE_UPDATE_JOB_STORAGE_KEY);
  } catch {
    // 存储不可写时无需阻断更新结果展示。
  }
}

export function getUpdateJobRetryDelay(failures: number): number {
  const normalizedFailures = Math.max(1, Math.floor(failures));
  return Math.min(2_000 * (2 ** (normalizedFailures - 1)), MAX_RETRY_DELAY_MS);
}

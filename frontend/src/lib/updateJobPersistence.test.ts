import assert from "node:assert/strict";
import test from "node:test";

import {
  ACTIVE_UPDATE_JOB_STORAGE_KEY,
  clearActiveUpdateJob,
  getUpdateJobRetryDelay,
  loadActiveUpdateJob,
  saveActiveUpdateJob,
  type StorageLike,
} from "./updateJobPersistence.ts";

class MemoryStorage implements StorageLike {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

test("更新任务可在页面重载后恢复", () => {
  const storage = new MemoryStorage();
  const savedAt = 1_000_000;
  const job = {
    jobId: "job-large-update",
    plan: { remote: "origin", branch: "Agent-beta", requiresMigration: true },
    savedAt,
  };

  saveActiveUpdateJob(storage, job);

  assert.deepEqual(loadActiveUpdateJob(storage, savedAt + 60_000), job);
});

test("损坏或过期的更新任务不会阻塞后续检查", () => {
  const storage = new MemoryStorage();
  storage.setItem(ACTIVE_UPDATE_JOB_STORAGE_KEY, "not-json");
  assert.equal(loadActiveUpdateJob(storage), null);
  assert.equal(storage.getItem(ACTIVE_UPDATE_JOB_STORAGE_KEY), null);

  saveActiveUpdateJob(storage, {
    jobId: "job-stale",
    plan: { remote: "origin", branch: "main" },
    savedAt: 1_000,
  });
  assert.equal(loadActiveUpdateJob(storage, 10_000, 1_000), null);
  assert.equal(storage.getItem(ACTIVE_UPDATE_JOB_STORAGE_KEY), null);
});

test("任务进入终态后可清除恢复记录", () => {
  const storage = new MemoryStorage();
  saveActiveUpdateJob(storage, {
    jobId: "job-complete",
    plan: { remote: "origin", branch: "Agent-beta" },
    savedAt: Date.now(),
  });

  clearActiveUpdateJob(storage);

  assert.equal(loadActiveUpdateJob(storage), null);
});

test("连续断线只增加重试间隔，不会在第五次后停止恢复", () => {
  assert.equal(getUpdateJobRetryDelay(1), 2_000);
  assert.equal(getUpdateJobRetryDelay(2), 4_000);
  assert.equal(getUpdateJobRetryDelay(5), 15_000);
  assert.equal(getUpdateJobRetryDelay(100), 15_000);
});

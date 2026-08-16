import assert from "node:assert/strict";
import test from "node:test";

import type { SystemAgentQueueItem, SystemAgentRun } from "../../api/systemAgent.ts";
import {
  classifySystemAgentRunSettlement,
  sessionRunStatusById,
  sortSystemAgentQueue,
  sortSystemAgentRuns,
  taskCenterVisibleRuns,
} from "./taskCenterState.ts";

function run(
  id: string,
  status: SystemAgentRun["status"],
  updatedAt: string,
): SystemAgentRun {
  return {
    id,
    run_id: id,
    session_id: "session",
    web_user_id: 1,
    user_message_id: null,
    client_request_id: id,
    kind: "message",
    status,
    last_seq: 0,
    cancel_requested: false,
    updated_at: updatedAt,
  };
}

function queueItem(
  id: string,
  position: number,
  status: SystemAgentQueueItem["status"] = "pending",
): SystemAgentQueueItem {
  return {
    id,
    session_id: "session",
    channel: "web",
    kind: "message",
    position,
    status,
    content: id,
    created_at: `2026-07-30T00:00:0${position}Z`,
  };
}

test("任务中心优先显示等待状态，同状态按最新活动排序", () => {
  const rows = sortSystemAgentRuns([
    run("failed", "failed", "2026-07-30T04:00:00Z"),
    run("running-old", "running", "2026-07-30T01:00:00Z"),
    run("approval", "waiting_approval", "2026-07-30T00:00:00Z"),
    run("running-new", "running", "2026-07-30T03:00:00Z"),
    run("input", "waiting_input", "2026-07-30T02:00:00Z"),
  ]);

  assert.deepEqual(rows.map((row) => row.id), [
    "approval",
    "input",
    "running-new",
    "running-old",
    "failed",
  ]);
});

test("任务中心按持久化位置排序混合队列且不修改输入", () => {
  const source = [
    queueItem("paused", 20, "paused"),
    queueItem("pending", 10),
    queueItem("dispatching", 5, "dispatching"),
  ];

  assert.deepEqual(sortSystemAgentQueue(source).map((row) => row.id), [
    "dispatching",
    "pending",
    "paused",
  ]);
  assert.deepEqual(source.map((row) => row.id), [
    "paused",
    "pending",
    "dispatching",
  ]);
});

test("运行流结束后按持久状态区分等待、完成、失败和取消", () => {
  assert.equal(classifySystemAgentRunSettlement("waiting_input"), "waiting");
  assert.equal(classifySystemAgentRunSettlement("waiting_approval"), "waiting");
  assert.equal(classifySystemAgentRunSettlement("succeeded"), "complete");
  assert.equal(classifySystemAgentRunSettlement("failed"), "failed");
  assert.equal(classifySystemAgentRunSettlement("cancelled"), "cancelled");
});

test("会话徽标不会让历史失败覆盖后续成功", () => {
  const statuses = sessionRunStatusById([
    run("failed-old", "failed", "2026-07-30T01:00:00Z"),
    run("succeeded-new", "succeeded", "2026-07-30T02:00:00Z"),
  ]);

  assert.deepEqual(statuses, {});
});

test("会话徽标显示最新失败并优先提示未结束运行", () => {
  const latestFailed = run("failed-new", "failed", "2026-07-30T03:00:00Z");
  latestFailed.session_id = "failed-session";
  const olderSuccess = run("succeeded-old", "succeeded", "2026-07-30T02:00:00Z");
  olderSuccess.session_id = "failed-session";
  const waiting = run("waiting", "waiting_approval", "2026-07-30T01:00:00Z");
  waiting.session_id = "active-session";
  const newerFailure = run("failed", "failed", "2026-07-30T04:00:00Z");
  newerFailure.session_id = "active-session";

  assert.deepEqual(
    sessionRunStatusById([olderSuccess, latestFailed, waiting, newerFailure]),
    {
      "active-session": "waiting_approval",
      "failed-session": "failed",
    },
  );
});

test("任务中心只保留同一消息最新且未解决的失败", () => {
  const failedOld = run("failed-old", "failed", "2026-07-30T01:00:00Z");
  failedOld.user_message_id = 10;
  const failedNew = run("failed-new", "failed", "2026-07-30T02:00:00Z");
  failedNew.user_message_id = 10;
  const succeeded = run("succeeded", "succeeded", "2026-07-30T03:00:00Z");
  succeeded.user_message_id = 10;
  const unresolved = run("unresolved", "failed", "2026-07-30T04:00:00Z");
  unresolved.user_message_id = 11;

  assert.deepEqual(
    taskCenterVisibleRuns([failedOld, failedNew, succeeded, unresolved]).map((row) => row.id),
    ["unresolved"],
  );
  assert.deepEqual(
    taskCenterVisibleRuns(
      [failedOld, failedNew, succeeded, unresolved],
      new Set(["unresolved"]),
    ),
    [],
  );
});

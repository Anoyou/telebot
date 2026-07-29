import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

import { installApiFixture } from "./fixtures";

const runFixture = {
  id: "run-telepilot-agent-001",
  run_id: "run-telepilot-agent-001",
  session_id: "session-ops-diagnosis",
  web_user_id: 1,
  user_message_id: 318,
  client_request_id: "visual-agent-request-001",
  kind: "message",
  status: "succeeded",
  last_seq: 10,
  cancel_requested: false,
  error_code: null,
  error_message: null,
  started_at: "2026-07-27T02:30:00Z",
  finished_at: "2026-07-27T02:30:02Z",
  created_at: "2026-07-27T02:30:00Z",
  updated_at: "2026-07-27T02:30:02Z",
};

const eventFixture = [
  { type: "run_started", channel: "web", account_id: 1 },
  { type: "model_capability_check", provider_name: "DeepSeek", model: "deepseek-chat" },
  { type: "provider_selected", provider_name: "DeepSeek", model: "deepseek-chat", reason: "configured", selection_mode: "auto" },
  { type: "route_selected", domains: ["logs", "system"], route_source: "classifier", route_reason: "诊断最近一次 Agent 失败", tool_count: 3, available_tool_count: 24 },
  { type: "skill_selected", skill_names: ["日志诊断"], understanding_summary: "检查最近的 Agent 运行，定位失败节点并给出下一步。", tool_count: 3 },
  { type: "tool_started", tool_name: "logs.recent", tool_description: "读取最近运行日志", call_id: "call-logs-001", arguments_summary: { level: "error", limit: 20 } },
  { type: "tool_finished", tool_name: "logs.recent", tool_description: "读取最近运行日志", call_id: "call-logs-001", is_error: false, result_summary: { matched: 4, latest_code: "MODEL_TIMEOUT" } },
  { type: "retry_scheduled", provider_name: "DeepSeek", model: "deepseek-chat", retry_number: 1, max_retries: 5, delay_seconds: 3 },
  { type: "assistant_message", usage: {
    schema_version: 2,
    provider_name: "DeepSeek",
    model: "deepseek-chat",
    input_tokens: 1320,
    output_tokens: 284,
    total_tokens: 1604,
    tool_calls: 1,
    available_tools: 3,
    used_fallback: false,
    route_domains: ["logs", "system"],
    stage_timings: { verify_ms: 18, route_ms: 31, first_token_ms: 420, total_ms: 1870 },
  } },
  { type: "done", ok: true, steps: 2, tool_calls: 1, available_tools: 3, stage_timings: { verify_ms: 18, route_ms: 31, first_token_ms: 420, total_ms: 1870 } },
].map((event, index) => ({
  run_id: runFixture.id,
  seq: index + 1,
  event: {
    ...event,
    run_id: runFixture.id,
    session_id: runFixture.session_id,
    seq: index + 1,
    ts: `2026-07-27T02:30:0${Math.min(index, 9)}Z`,
  },
  created_at: `2026-07-27T02:30:0${Math.min(index, 9)}Z`,
}));

const longRunFixture = {
  ...runFixture,
  id: "run-telepilot-agent-long-001",
  run_id: "run-telepilot-agent-long-001",
  client_request_id: "visual-agent-request-long-001",
  last_seq: 5_010,
};

const longEventTemplates = new Map<number, Record<string, unknown>>([
  [1, eventFixture[0]!.event],
  [2, eventFixture[1]!.event],
  [3, eventFixture[2]!.event],
  [4, eventFixture[3]!.event],
  [5, eventFixture[4]!.event],
  [4_005, eventFixture[7]!.event],
  [5_001, eventFixture[5]!.event],
  [5_002, eventFixture[6]!.event],
  [5_003, eventFixture[7]!.event],
  [5_004, eventFixture[8]!.event],
  [5_010, eventFixture[9]!.event],
]);

function longEventPage(afterSeq: number, limit: number) {
  const end = Math.min(longRunFixture.last_seq, afterSeq + limit);
  return Array.from({ length: Math.max(0, end - afterSeq) }, (_, index) => {
    const seq = afterSeq + index + 1;
    const template = longEventTemplates.get(seq) ?? { type: "assistant_delta", delta: "." };
    return {
      run_id: longRunFixture.id,
      seq,
      event: {
        ...template,
        run_id: longRunFixture.id,
        session_id: longRunFixture.session_id,
        seq,
        ts: "2026-07-27T02:30:05Z",
      },
      created_at: "2026-07-27T02:30:05Z",
    };
  });
}

function incrementalEventPage(afterSeq: number, limit: number, lastSeq: number) {
  const templates = new Map<number, Record<string, unknown>>([
    [1, eventFixture[0]!.event],
    [2, eventFixture[2]!.event],
    [3, eventFixture[3]!.event],
    [1_001, eventFixture[5]!.event],
    [1_002, eventFixture[6]!.event],
  ]);
  const end = Math.min(lastSeq, afterSeq + limit);
  return Array.from({ length: Math.max(0, end - afterSeq) }, (_, index) => {
    const seq = afterSeq + index + 1;
    const template = templates.get(seq) ?? { type: "assistant_delta", delta: "." };
    return {
      run_id: "run-telepilot-agent-incremental-001",
      seq,
      event: {
        ...template,
        run_id: "run-telepilot-agent-incremental-001",
        session_id: runFixture.session_id,
        seq,
        ts: "2026-07-27T02:30:05Z",
      },
      created_at: "2026-07-27T02:30:05Z",
    };
  });
}

test("Agent 运行视角可筛选、下钻且不产生横向溢出", async ({ page }, testInfo) => {
  await page.emulateMedia({
    colorScheme: testInfo.project.name === "desktop" ? "dark" : "light",
    reducedMotion: "reduce",
  });
  const fixture = await installApiFixture(page);
  await page.route("**/api/system-agent/runs**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(pathname.endsWith("/events") ? eventFixture : [runFixture]),
    });
  });

  await page.goto("/logs?view=agent", { waitUntil: "networkidle" });
  await expect(page.getByRole("heading", { name: "Agent 运行" })).toBeVisible();
  await page.getByRole("button", { name: /已完成/ }).click();

  const perspective = page.getByTestId("agent-run-perspective");
  await expect(perspective).toBeVisible();
  const overview = perspective.getByRole("region", { name: "Agent 运行概览" });
  await expect(overview).toContainText("DeepSeek / deepseek-chat");
  await expect(overview).toContainText("1,604");
  await expect(overview).toContainText("1.87 秒");

  await perspective.getByRole("button", { name: "工具", exact: true }).click();
  await expect(perspective.getByRole("button", { name: /调用工具/ })).toBeVisible();
  await expect(perspective.getByRole("button", { name: /工具调用完成/ })).toBeVisible();
  await perspective.getByRole("button", { name: /工具调用完成/ }).click();
  await expect(perspective.getByText("工具结果摘要")).toBeVisible();

  await perspective.getByRole("tab", { name: "原始事件" }).click();
  await expect(perspective.getByRole("tabpanel")).toContainText('"type": "tool_finished"');
  await perspective.getByRole("tab", { name: "语义详情" }).click();
  await expect(perspective.getByText("工具结果摘要")).toBeVisible();

  const axe = await new AxeBuilder({ page })
    .include('[data-testid="agent-run-perspective"]')
    .analyze();
  const blockers = axe.violations.filter((item) => item.impact === "critical" || item.impact === "serious");
  expect(blockers, JSON.stringify(blockers, null, 2)).toEqual([]);

  const overflow = await page.evaluate(() => ({
    viewportWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.viewportWidth + 1);

  await page.screenshot({
    path: testInfo.outputPath(`agent-perspective-${testInfo.project.name}.png`),
    fullPage: true,
  });
  fixture.assertClean();
});

test("长运行扫描全部关键事件并折叠流式噪声", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "分页终态回归只需桌面项目执行一次");
  const fixture = await installApiFixture(page);
  await page.route("**/api/system-agent/runs**", async (route) => {
    const url = new URL(route.request().url());
    const isEvents = url.pathname.endsWith("/events");
    const afterSeq = Number(url.searchParams.get("after_seq") || 0);
    const limit = Number(url.searchParams.get("limit") || 500);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(isEvents ? longEventPage(afterSeq, limit) : [longRunFixture]),
    });
  });

  await page.goto("/logs?view=agent", { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /已完成/ }).click();
  const perspective = page.getByTestId("agent-run-perspective");
  await expect(perspective).toContainText("已折叠 4,999 条心跳与流式事件");
  await expect(perspective.getByRole("region", { name: "Agent 运行概览" })).toContainText("1,604");
  await expect(perspective.getByRole("region", { name: "Agent 运行概览" })).toContainText("2 重试");
  await expect(perspective.getByText("#4005", { exact: true })).toBeVisible();
  await expect(perspective.getByRole("button", { name: /运行完成/ })).toBeVisible();
  fixture.assertClean();
});

test("运行序号增长时从缓存末尾增量加载", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "增量刷新回归只需桌面项目执行一次");
  const fixture = await installApiFixture(page);
  const eventCursors: number[] = [];
  let runListRequests = 0;
  let currentLastSeq = 1_000;

  await page.route("**/api/system-agent/runs**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/events")) {
      const afterSeq = Number(url.searchParams.get("after_seq") || 0);
      const limit = Number(url.searchParams.get("limit") || 500);
      eventCursors.push(afterSeq);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(incrementalEventPage(afterSeq, limit, currentLastSeq)),
      });
      return;
    }

    runListRequests += 1;
    if (runListRequests > 1) currentLastSeq = 1_010;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{
        ...runFixture,
        id: "run-telepilot-agent-incremental-001",
        run_id: "run-telepilot-agent-incremental-001",
        status: "running",
        last_seq: currentLastSeq,
        finished_at: null,
      }]),
    });
  });

  await page.goto("/logs?view=agent", { waitUntil: "networkidle" });
  await expect(page.getByTestId("agent-run-perspective")).toBeVisible();
  await page.getByRole("switch").click();

  await expect.poll(() => eventCursors.join(","), { timeout: 8_000 }).toContain("1000");
  expect(eventCursors).toEqual([0, 1_000]);
  await expect(page.getByText("#1001", { exact: true })).toBeVisible();
  fixture.assertClean();
});

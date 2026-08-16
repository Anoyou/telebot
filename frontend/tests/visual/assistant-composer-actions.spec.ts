import { expect, test } from "@playwright/test";

import { installApiFixture } from "./fixtures";

const session = {
  id: "composer-actions-session",
  web_user_id: 1,
  bot_tg_user_id: null,
  account_id: null,
  channel: "web",
  title: "输入区动作验证",
  origin: "interactive",
  status: "active",
  created_at: "2026-08-17T01:00:00Z",
  updated_at: "2026-08-17T01:02:00Z",
};

const waitingRun = {
  id: "waiting-run",
  run_id: "waiting-run",
  session_id: session.id,
  web_user_id: 1,
  user_message_id: 11,
  client_request_id: "waiting-request",
  kind: "message",
  status: "waiting_input",
  phase: "等待补充",
  last_seq: 4,
  cancel_requested: false,
  created_at: "2026-08-17T01:01:00Z",
  updated_at: "2026-08-17T01:02:00Z",
};

const failedRun = {
  id: "failed-run",
  run_id: "failed-run",
  session_id: session.id,
  web_user_id: 1,
  user_message_id: 10,
  client_request_id: "failed-request",
  kind: "message",
  status: "failed",
  phase: "调用模型",
  last_seq: 8,
  cancel_requested: false,
  error_message: "上游网络请求失败",
  created_at: "2026-08-17T00:58:00Z",
  updated_at: "2026-08-17T00:59:00Z",
};

const queuedItem = {
  id: "queued-item",
  session_id: session.id,
  run_id: "queued-run",
  web_user_id: 1,
  channel: "web",
  kind: "message",
  position: 1,
  status: "pending",
  content: "这是一条贴在输入框上方的待处理消息",
};

test("待处理消息贴在输入框上方并可选择用途，失败任务可移除", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "tablet", "桌面与 375px 移动视口各验证一次");
  const fixture = await installApiFixture(page);
  if (testInfo.project.name === "mobile") {
    await page.setViewportSize({ width: 375, height: 812 });
  }

  await page.route("**/api/system-agent/sessions?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([session]),
    });
  });
  await page.route(
    "**/api/system-agent/sessions/composer-actions-session/messages?**",
    async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    },
  );
  await page.route("**/api/system-agent/runs?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([waitingRun, failedRun]),
    });
  });
  await page.route("**/api/system-agent/queue?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([queuedItem]),
    });
  });

  await page.goto("/assistant", { waitUntil: "networkidle" });

  const composer = page.locator("[data-assistant-composer]");
  await expect(composer).toBeVisible();
  await expect(composer.getByText("这条消息怎么处理？")).toHaveCount(0);
  await expect(composer.locator("[data-assistant-composer-queue]")).toBeVisible();
  await expect(composer.getByText(queuedItem.content)).toBeVisible();
  const actionButton = composer.getByRole("button", { name: /选择.*用途/ });
  await expect(actionButton).toBeVisible();
  // 移动端底部导航可能覆盖触发器的可视区域，使用键盘语义触发避免依赖坐标命中。
  await actionButton.focus();
  await page.keyboard.press("Enter");

  const actionMenu = page.getByRole("menu");
  await expect(actionMenu.getByText("稍后执行", { exact: true })).toBeVisible();
  await expect(actionMenu.getByText("等当前任务完成后，再处理这条消息。")).toBeVisible();
  await expect(actionMenu.getByText("补充说明/调整方向", { exact: true })).toBeVisible();
  await expect(actionMenu.getByText("当前任务恢复运行后才可调整方向。")).toBeVisible();
  await expect(actionMenu.getByText("改做这条")).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: /任务中心/ }).click();
  const dismiss = page.getByRole("button", { name: "从任务中心移除失败任务" });
  await expect(dismiss).toBeVisible();
  await dismiss.click();
  await expect(dismiss).toHaveCount(0);

  const overflow = await page.evaluate(() => ({
    viewportWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.viewportWidth + 1);

  await page.screenshot({
    path: testInfo.outputPath(`assistant-composer-actions-${testInfo.project.name}.png`),
    fullPage: true,
  });
  fixture.assertClean();
});

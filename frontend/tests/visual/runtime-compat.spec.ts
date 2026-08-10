import { expect, test } from "@playwright/test";

import { installApiFixture } from "./fixtures";

test.describe("前端运行时兼容", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "只在 Chromium 项目运行");

  test("React Router 保持指令工作区默认与兜底跳转", async ({ page }) => {
    const fixture = await installApiFixture(page);

    await page.goto("/operations", { waitUntil: "networkidle" });
    await expect(page).toHaveURL(/\/operations\/templates$/);
    await expect(page.getByRole("heading", { name: "指令与任务" })).toBeVisible();

    await page.goto("/operations/unknown", { waitUntil: "networkidle" });
    await expect(page).toHaveURL(/\/operations\/templates$/);
    await expect(page.getByRole("tab", { name: "自定义指令" })).toBeVisible();
    fixture.assertClean();
  });

  test("ECharts 可渲染、切换主题并响应视口变化", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "仅桌面视口验证图表运行态");
    const fixture = await installApiFixture(page);
    const browserErrors: string[] = [];
    page.on("pageerror", (error) => browserErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text());
    });
    await page.route("**/api/ledger/summary**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          income: "180",
          payout: "60",
          net: "120",
          count: 2,
          by_day: [
            { key: "2026-08-09", label: "08-09", income: "80", payout: "20", net: "60", count: 1 },
            { key: "2026-08-10", label: "08-10", income: "100", payout: "40", net: "60", count: 1 },
          ],
          by_chat: [],
          by_recipient: [],
        }),
      });
    });

    await page.goto("/ledger", { waitUntil: "networkidle" });
    const chart = page.locator("canvas").last();
    await expect(chart).toBeVisible();
    const initial = await chart.evaluate((canvas: HTMLCanvasElement) => ({
      width: canvas.width,
      height: canvas.height,
      image: canvas.toDataURL(),
    }));
    expect(initial.width).toBeGreaterThan(300);
    expect(initial.height).toBeGreaterThan(200);

    await page.getByRole("button", { name: "切换主题" }).click();
    await page.getByRole("menuitem", { name: "深色" }).click();
    await expect(page.locator("html")).toHaveClass(/dark/);
    await expect.poll(
      () => chart.evaluate((canvas: HTMLCanvasElement) => canvas.toDataURL()),
    ).not.toBe(initial.image);

    await page.setViewportSize({ width: 1100, height: 900 });
    await expect.poll(
      () => chart.evaluate((canvas: HTMLCanvasElement) => canvas.width),
    ).not.toBe(initial.width);
    expect(browserErrors).toEqual([]);
    fixture.assertClean();
  });
});

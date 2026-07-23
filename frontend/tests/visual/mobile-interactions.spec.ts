import { expect, test } from "@playwright/test";

import { APP_VERSION } from "../../src/lib/version";
import { installApiFixture, installProviderFixture } from "./fixtures";

test.describe("移动端交互细节", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "只在 Chromium 项目运行");

  test("滚动边界版本提示不会占用页面高度并会自动回收", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/ledger", { waitUntil: "networkidle" });
    const main = page.locator("[data-app-main]");
    const topEdge = page.locator('[data-edge="top"]');
    const topLabel = topEdge.locator(".mobile-scroll-edge-label");
    const initialHeight = await topEdge.evaluate((element) => element.getBoundingClientRect().height);
    expect(initialHeight).toBe(0);
    await expect(topLabel).toHaveCSS("border-top-width", "0px");

    await main.evaluate((element) => { element.scrollTop = 180; });
    await main.evaluate((element) => { element.scrollTop = 0; });
    await expect(topEdge).toHaveAttribute("data-visible", "true");
    await expect(topEdge).toHaveAttribute("data-visible", "false", { timeout: 2_000 });
    fixture.assertClean();
  });

  test("侧栏明确展示更新日志入口并可打开内容", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/ledger", { waitUntil: "networkidle" });

    await page.getByRole("button", { name: "打开导航菜单" }).click();
    const changelogButton = page.getByRole("button", { name: "更新日志" });
    await expect(changelogButton).toBeVisible();
    await expect(changelogButton).toContainText("更新日志");
    await expect(changelogButton).toContainText(`v${APP_VERSION}`);
    const bounds = await changelogButton.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, viewport: document.documentElement.clientWidth };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(bounds.viewport);

    await changelogButton.click();
    await expect(page.getByText("最近版本的主要变化，完整记录见仓库 CHANGELOG.md。"))
      .toBeVisible();
    const menuBounds = await page.getByRole("menu").evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return {
        top: rect.top,
        left: rect.left,
        right: rect.right,
        bottom: rect.bottom,
        viewportWidth: document.documentElement.clientWidth,
        viewportHeight: document.documentElement.clientHeight,
      };
    });
    expect(menuBounds.top).toBeGreaterThanOrEqual(0);
    expect(menuBounds.left).toBeGreaterThanOrEqual(0);
    expect(menuBounds.right).toBeLessThanOrEqual(menuBounds.viewportWidth);
    expect(menuBounds.bottom).toBeLessThanOrEqual(menuBounds.viewportHeight);
    fixture.assertClean();
  });

  test("Provider 创建步骤保持单行置顶并随滚动更新", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/ai?tab=providers&newProvider=1", { waitUntil: "networkidle" });
    const steps = page.locator('ol[aria-label="创建步骤"]');
    await expect(steps).toBeVisible();
    const columns = await steps.evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(" ").length);
    expect(columns).toBe(3);

    const saveTitle = page.getByRole("heading", { name: "保存信息" });
    await saveTitle.evaluate((element) => element.scrollIntoView({ block: "start" }));
    const activeSaveStep = steps.locator("li.bg-primary");
    await expect(activeSaveStep).toContainText("保存");
    const stepsTop = await steps.evaluate((element) => Math.round(element.getBoundingClientRect().top));
    expect(stepsTop).toBeGreaterThanOrEqual(0);
    fixture.assertClean();
  });

  test("Provider 列表排序和测活控件使用紧凑全局样式", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await installProviderFixture(page);
    await page.goto("/ai?tab=providers", { waitUntil: "networkidle" });
    const sort = page.getByLabel("排序");
    await expect(sort).toBeVisible();
    await sort.selectOption("models");
    const providerCards = page.locator("[data-provider-card]");
    await expect(providerCards).toHaveCount(2);
    const cardText = await providerCards.allTextContents();
    expect(cardText[0]).toContain("Grok");

    await page.goto("/ai/liveness?provider=2", { waitUntil: "networkidle" });
    await expect(page.getByRole("tab", { name: "Provider 多模型对话" })).toBeVisible();
    await expect(page.getByRole("button", { name: "打开测试范围" })).toContainText("范围");
    await expect(page.getByRole("button", { name: "打开请求设置" })).toContainText("设置");
    fixture.assertClean();
  });

  test("更新详情默认折叠并展示当前与目标版本", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);

    await page.goto("/plugins", { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "检查更新" }).click();
    const dialog = page.getByRole("dialog", { name: "检查更新" });
    await expect(dialog).toBeVisible();
    const checkingHeight = await dialog.evaluate((element) => Math.round(element.getBoundingClientRect().height));
    const details = page.locator("details").filter({ hasText: "更新详情" });
    await expect(details).toBeVisible();
    const resolvedHeight = await dialog.evaluate((element) => Math.round(element.getBoundingClientRect().height));
    expect(Math.abs(resolvedHeight - checkingHeight)).toBeLessThanOrEqual(1);
    await expect(details).not.toHaveAttribute("open", "");
    await expect(details.locator("summary")).toContainText(`v${APP_VERSION} → v0.72.0-beta.2`);
    await expect(details.getByText("当前提交: aaaa1111aaaa")).toBeHidden();
    await details.locator("summary").click();
    await expect(details).toHaveAttribute("open", "");
    await expect(details.getByText("当前提交: aaaa1111aaaa")).toBeVisible();
    fixture.assertClean();
  });
});

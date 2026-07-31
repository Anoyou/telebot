import { expect, test } from "@playwright/test";

import { installApiFixture } from "./fixtures";

test.describe("系统助手原位编辑与重新生成", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "只在 Chromium 项目运行");

  test("桌面配置并入页头且窄屏不再显示配置行", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "tablet", "桌面与 375px 移动视口各验证一次");
    const fixture = await installApiFixture(page);
    if (testInfo.project.name === "mobile") {
      await page.setViewportSize({ width: 375, height: 812 });
    }
    const session = {
      id: "header-controls-session",
      web_user_id: 1,
      bot_tg_user_id: null,
      account_id: null,
      channel: "web",
      title: "页头配置验证",
      origin: "interactive",
      status: "active",
      created_at: "2026-07-31T01:00:00Z",
      updated_at: "2026-07-31T01:00:00Z",
    };
    await page.route("**/api/system-agent/sessions?**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([session]),
      });
    });
    await page.route(
      "**/api/system-agent/sessions/header-controls-session/messages?**",
      async (route) => {
        await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      },
    );

    await page.goto("/assistant", { waitUntil: "networkidle" });

    const composer = page.locator("[data-assistant-composer]");
    await expect(composer.getByPlaceholder("想让 Agent 怎么帮你？直接用自然语言问她吧！")).toBeVisible();
    const headerControls = page.locator('[data-assistant-context-controls="header"]');
    const settingsControls = page.locator('[data-assistant-context-controls="settings"]');
    await expect(settingsControls).toHaveCount(0);
    if (testInfo.project.name === "desktop") {
      const header = page.locator("[data-assistant-surface] [data-page-header]");
      await expect(headerControls).toBeVisible();
      const positions = await header.evaluate((element) => {
        const headerRect = element.getBoundingClientRect();
        const controls = element.querySelector<HTMLElement>(
          '[data-assistant-context-controls="header"]',
        );
        const controlsRect = controls?.getBoundingClientRect();
        return {
          headerCenter: headerRect.left + headerRect.width / 2,
          controlsLeft: controlsRect?.left ?? 0,
        };
      });
      expect(positions.controlsLeft).toBeGreaterThan(positions.headerCenter);
    } else {
      await expect(headerControls).toBeHidden();
      await page.locator("[data-assistant-mobile-summary]").click();
      const bounds = await page.evaluate(() => ({
        viewportWidth: document.documentElement.clientWidth,
        documentWidth: document.documentElement.scrollWidth,
      }));
      expect(bounds.viewportWidth).toBe(375);
      expect(bounds.documentWidth).toBeLessThanOrEqual(bounds.viewportWidth);
    }

    await page.screenshot({
      path: testInfo.outputPath(`assistant-header-controls-${testInfo.project.name}.png`),
      fullPage: true,
    });
    fixture.assertClean();
  });

  test("只有最新完成轮次可编辑和重新生成", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "tablet", "桌面与移动视口各验证一次");
    const fixture = await installApiFixture(page);
    const session = {
      id: "regenerate-session",
      web_user_id: 1,
      bot_tg_user_id: null,
      account_id: null,
      channel: "web",
      title: "原位编辑验证",
      origin: "interactive",
      status: "active",
      created_at: "2026-07-28T01:00:00Z",
      updated_at: "2026-07-28T01:02:00Z",
    };
    const messages = [
      {
        id: 11,
        session_id: session.id,
        role: "user",
        content: { text: "旧问题不应再提供编辑入口" },
        run_status: "succeeded",
        created_at: "2026-07-28T01:00:00Z",
      },
      {
        id: 12,
        session_id: session.id,
        role: "assistant",
        content: { text: "旧回答仍然可以复制，但不能重新生成。" },
        run_status: "completed",
        created_at: "2026-07-28T01:00:10Z",
      },
      {
        id: 13,
        session_id: session.id,
        role: "user",
        content: { text: "请检查最新一轮的运行日志与 Provider 状态" },
        run_status: "succeeded",
        created_at: "2026-07-28T01:01:00Z",
      },
      {
        id: 14,
        session_id: session.id,
        role: "assistant",
        content: { text: "最新一轮已经完成，可以原位重新生成。" },
        run_status: "completed",
        created_at: "2026-07-28T01:01:10Z",
      },
    ];

    await page.route("**/api/system-agent/sessions?**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([session]),
      });
    });
    await page.route(
      "**/api/system-agent/sessions/regenerate-session/messages?**",
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(messages),
        });
      },
    );
    await page.route("**/api/system-agent/actions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });

    await page.goto("/assistant", { waitUntil: "networkidle" });

    const editButton = page.getByRole("button", { name: "编辑并重新生成" });
    const regenerateButton = page.getByRole("button", {
      name: "使用当前选择的模型重新生成",
    });
    await expect(editButton).toHaveCount(1);
    await expect(regenerateButton).toHaveCount(1);
    await expect(page.getByRole("button", { name: "复制回答" })).toHaveCount(2);

    await editButton.click();
    const editor = page.getByRole("textbox", { name: "编辑消息" });
    await expect(editor).toBeVisible();
    await expect(editor).toHaveValue("请检查最新一轮的运行日志与 Provider 状态");
    await expect(page.getByRole("button", { name: "取消编辑" })).toBeVisible();
    await expect(page.getByRole("button", { name: "保存并重新生成" })).toBeVisible();

    const bounds = await editor.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        width: rect.width,
        viewportWidth: document.documentElement.clientWidth,
        documentWidth: document.documentElement.scrollWidth,
      };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(bounds.viewportWidth);
    expect(bounds.width).toBeGreaterThan(testInfo.project.name === "mobile" ? 180 : 320);
    expect(bounds.documentWidth).toBeLessThanOrEqual(bounds.viewportWidth);

    await page.screenshot({
      path: testInfo.outputPath(`assistant-inline-edit-${testInfo.project.name}.png`),
      fullPage: true,
    });
    await page.getByRole("button", { name: "取消编辑" }).click();
    await expect(editor).toHaveCount(0);
    await page.screenshot({
      path: testInfo.outputPath(`assistant-actions-${testInfo.project.name}.png`),
      fullPage: true,
    });
    if (testInfo.project.name === "mobile") {
      const assistantEntry = page.locator("[data-assistant-mobile-button]");
      const regenerateBounds = await regenerateButton.boundingBox();
      const assistantBounds = await assistantEntry.boundingBox();
      const overlaps = !(
        (regenerateBounds?.x || 0) + (regenerateBounds?.width || 0) <=
          (assistantBounds?.x || 0) ||
        (assistantBounds?.x || 0) + (assistantBounds?.width || 0) <=
          (regenerateBounds?.x || 0) ||
        (regenerateBounds?.y || 0) + (regenerateBounds?.height || 0) <=
          (assistantBounds?.y || 0) ||
        (assistantBounds?.y || 0) + (assistantBounds?.height || 0) <=
          (regenerateBounds?.y || 0)
      );
      expect(overlaps).toBe(false);
    }
    fixture.assertClean();
  });
});

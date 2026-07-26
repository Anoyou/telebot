import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

test("iOS 主屏幕图标进入 Vite 内容哈希管线", () => {
  const html = readFileSync(new URL("../../index.html", import.meta.url), "utf8");
  const icon = new URL("../assets/apple-touch-icon.png", import.meta.url);

  assert.match(
    html,
    /<link rel="apple-touch-icon" sizes="180x180" href="\/src\/assets\/apple-touch-icon\.png" \/>/,
  );
  assert.equal(existsSync(icon), true);
  assert.doesNotMatch(html, /rel="apple-touch-icon"[^>]+href="\/apple-touch-icon\.png"/);
});

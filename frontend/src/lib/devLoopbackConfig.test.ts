import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const viteConfig = readFileSync(new URL("../../vite.config.ts", import.meta.url), "utf8");
const packageJson = JSON.parse(
  readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
) as { scripts?: Record<string, string> };

test("开发代理固定使用 IPv4 回环地址", () => {
  assert.match(viteConfig, /"\/api":\s*"http:\/\/127\.0\.0\.1:8000"/);
  assert.doesNotMatch(viteConfig, /"\/api":\s*"http:\/\/localhost:8000"/);
});

test("OpenAPI codegen 使用仓库内固定契约快照", () => {
  assert.match(
    packageJson.scripts?.codegen ?? "",
    /openapi-typescript \.\.\/openapi\/telepilot\.openapi\.json/,
  );
  assert.doesNotMatch(packageJson.scripts?.codegen ?? "", /https?:\/\//);
});

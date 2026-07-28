import assert from "node:assert/strict";
import test from "node:test";

import { resolveModelBrand } from "./modelBrand.ts";

test("按模型 ID 识别主流公司", () => {
  assert.equal(resolveModelBrand("deepseek-v4-flash").id, "deepseek");
  assert.equal(resolveModelBrand("deepseek-ai/deepseek-chat").id, "deepseek");
  assert.equal(resolveModelBrand("claude-sonnet-4-5").id, "anthropic");
  assert.equal(resolveModelBrand("gpt-5.4").id, "openai");
  assert.equal(resolveModelBrand("o3-mini").id, "openai");
  assert.equal(resolveModelBrand("gemini-2.5-pro").id, "google");
  assert.equal(resolveModelBrand("grok-3").id, "xai");
  assert.equal(resolveModelBrand("meta-llama/llama-3.3-70b").id, "meta");
  assert.equal(resolveModelBrand("mistral-large-latest").id, "mistral");
  assert.equal(resolveModelBrand("qwen2.5-72b").id, "qwen");
  assert.equal(resolveModelBrand("kimi-k2").id, "moonshot");
  assert.equal(resolveModelBrand("glm-4.5").id, "zhipu");
  assert.equal(resolveModelBrand("doubao-pro-32k").id, "bytedance");
});

test("模型无法识别时回退 Provider 名", () => {
  assert.equal(resolveModelBrand("custom-router-1", "DeepSeek Official").id, "deepseek");
  assert.equal(resolveModelBrand("proxy-model", "OpenRouter").id, "openrouter");
  assert.equal(resolveModelBrand("local-model", "Ollama Home").id, "ollama");
});

test("自动路由与未知品牌", () => {
  assert.equal(resolveModelBrand(undefined, undefined, { auto: true }).id, "auto");
  assert.equal(resolveModelBrand("totally-custom-xyz").id, "unknown");
});

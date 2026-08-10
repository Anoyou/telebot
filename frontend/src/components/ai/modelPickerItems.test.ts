import assert from "node:assert/strict";
import test from "node:test";

import { matrixToPickerItems } from "./modelPickerItems.ts";

test("matrixToPickerItems 默认只保留已启用模型", () => {
  const items = matrixToPickerItems(
    [
      {
        provider_id: 1,
        provider_name: "猫羽",
        model: "deepseek-chat",
        enabled: true,
        declared_supports_tools: true,
      },
      {
        provider_id: 1,
        provider_name: "猫羽",
        model: "deepseek-reasoner",
        enabled: false,
        declared_supports_tools: true,
      },
      {
        provider_id: 2,
        provider_name: "OpenAI",
        model: "gpt-5.6-terra-max",
        execution_backend: "codex_gateway",
        // 缺省 enabled 视为可用，兼容旧缓存
        declared_supports_tools: true,
      },
    ],
    { requireTools: true },
  );

  assert.deepEqual(
    items.map((item) => item.model),
    ["deepseek-chat", "gpt-5.6-terra-max"],
  );
  assert.deepEqual(
    items.map((item) => item.executionBackend),
    ["direct", "codex_gateway"],
  );
});

test("matrixToPickerItems 可显式包含未启用模型", () => {
  const items = matrixToPickerItems(
    [
      {
        provider_id: 1,
        provider_name: "猫羽",
        model: "deepseek-chat",
        enabled: false,
        declared_supports_tools: true,
      },
    ],
    { requireTools: true, includeDisabled: true },
  );
  assert.equal(items.length, 1);
  assert.equal(items[0]?.model, "deepseek-chat");
});

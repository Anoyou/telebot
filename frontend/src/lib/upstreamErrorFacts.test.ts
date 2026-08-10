import assert from "node:assert/strict";
import test from "node:test";

import {
  hasUpstreamErrorFacts,
  upstreamErrorRequestIds,
} from "./upstreamErrorFacts.ts";

test("没有结构化上游事实时不展示错误事实卡片", () => {
  assert.equal(hasUpstreamErrorFacts({}), false);
  assert.equal(hasUpstreamErrorFacts({ upstream_error_message: "" }), false);
});

test("外层状态与真实上游状态分开保留", () => {
  assert.equal(
    hasUpstreamErrorFacts({
      upstream_status_code: 400,
      upstream_error_detail: "Unsupported parameter: max_output_tokens",
    }),
    true,
  );
});

test("三类 Request ID 使用不同标签展示", () => {
  assert.equal(
    upstreamErrorRequestIds({
      gateway_request_id: "gateway-1",
      upstream_request_id: "upstream-2",
      client_request_id: "client-3",
    }),
    [
      "Gateway Request ID：gateway-1",
      "上游 Request ID：upstream-2",
      "Client Request ID：client-3",
    ].join("\n"),
  );
});

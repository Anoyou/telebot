import assert from "node:assert/strict";
import test from "node:test";

import { NdjsonDecoder } from "./ndjsonStream.ts";

test("NDJSON 在任意字节和 UTF-8 边界分块时不丢事件", () => {
  const raw = new TextEncoder().encode(
    '{"type":"delta","delta":"你"}\n{"type":"delta","delta":"好"}\n{"type":"done"}',
  );
  const decoder = new NdjsonDecoder<Record<string, string>>();
  const events = [
    ...decoder.push(raw.slice(0, 9)),
    ...decoder.push(raw.slice(9, 31)),
    ...decoder.push(raw.slice(31, 47)),
    ...decoder.push(raw.slice(47)),
    ...decoder.finish(),
  ];

  assert.deepEqual(events, [
    { type: "delta", delta: "你" },
    { type: "delta", delta: "好" },
    { type: "done" },
  ]);
});

test("NDJSON 忽略空行并保留没有末尾换行的终态", () => {
  const decoder = new NdjsonDecoder<{ type: string }>();
  const events = [
    ...decoder.push(new TextEncoder().encode('\n{"type":"start"}\n\n{"type":')),
    ...decoder.push(new TextEncoder().encode('"done"}')),
    ...decoder.finish(),
  ];

  assert.deepEqual(events, [{ type: "start" }, { type: "done" }]);
});

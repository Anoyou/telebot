import assert from "node:assert/strict";
import test from "node:test";

import { serializeCsv } from "./csv.ts";

test("serializeCsv 转义逗号、双引号和换行", () => {
  const csv = serializeCsv(
    [{ name: '甲,乙"丙\n丁', detail: { ok: true } }],
    [
      { header: "名称", value: (row) => row.name },
      { header: "详情", value: (row) => row.detail },
    ],
  );

  assert.equal(
    csv,
    '\uFEFF"名称","详情"\r\n"甲,乙""丙\n丁","{""ok"":true}"\r\n',
  );
});

test("serializeCsv 防止表格软件执行公式", () => {
  const csv = serializeCsv(
    [{ value: "=1+1" }, { value: "@SUM(A1)" }, { value: "-2" }],
    [{ header: "值", value: (row) => row.value }],
  );

  assert.match(csv, /"'=1\+1"/);
  assert.match(csv, /"'@SUM\(A1\)"/);
  assert.match(csv, /"'-2"/);
});

test("serializeCsv 防止前导空白绕过公式防护", () => {
  const csv = serializeCsv(
    [{ value: " =1+1" }, { value: "\t=cmd()" }, { value: "\u00a0=SUM(1,2)" }],
    [{ header: "值", value: (row) => row.value }],
  );

  assert.match(csv, /"' =1\+1"/);
  assert.match(csv, /"'\t=cmd\(\)"/);
  assert.match(csv, /"'\u00a0=SUM\(1,2\)"/);
});

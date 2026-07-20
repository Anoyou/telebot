const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");

function loadSanitizer() {
  const sourcePath = path.join(__dirname, "TelegramHtmlPreview.tsx");
  const source = fs.readFileSync(sourcePath, "utf8");
  const componentStart = source.indexOf("export function TelegramHtmlPreview");
  assert.notEqual(componentStart, -1, "TelegramHtmlPreview export marker must exist");

  const parserSource = source
    .slice(0, componentStart)
    .replace("export function sanitizeTelegramHtml", "function sanitizeTelegramHtml");
  const output = ts.transpileModule(
    `${parserSource}\nmodule.exports = { sanitizeTelegramHtml };`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRef = { exports: {} };
  new Function("module", "exports", output)(moduleRef, moduleRef.exports);
  return moduleRef.exports.sanitizeTelegramHtml;
}

const sanitizeTelegramHtml = loadSanitizer();

test("renders Telegram Rich HTML blocks and inline formatting", () => {
  const rendered = sanitizeTelegramHtml(`
<h1>结算报告</h1>
<p>Tom &amp; Jerry <mark>重点</mark> H<sub>2</sub>O</p>
<ul><li><input type="checkbox" checked>已完成</li></ul>
<table bordered striped><tr><th>项目</th><td align="right">1</td></tr></table>
<details open><summary>详情</summary>正文</details>
<tg-time unix="1647531900" format="r">明天</tg-time>
<tg-math>x^2</tg-math>
  `);

  assert.match(rendered, /<h1>结算报告<\/h1>/);
  assert.match(rendered, /Tom &amp; Jerry/);
  assert.match(rendered, /telegram-checkbox is-checked/);
  assert.match(rendered, /telegram-table--bordered telegram-table--striped/);
  assert.match(rendered, /<details class="telegram-details" open>/);
  assert.match(rendered, /Telegram 时间 1647531900/);
  assert.match(rendered, /class="telegram-math"/);
  assert.doesNotMatch(rendered, /<\/p>\s+<ul>/);
  assert.doesNotMatch(rendered, /<\/ul>\s+<div class="telegram-table-scroll">/);
});

test("keeps expandable quotes and both Telegram spoiler syntaxes interactive", () => {
  const rendered = sanitizeTelegramHtml(`
<blockquote expandable><b>完整名单</b></blockquote>
<tg-spoiler>新语法</tg-spoiler>
<span class="tg-spoiler">兼容语法</span>
  `);

  assert.match(rendered, /<details class="telegram-expandable-quote">/);
  assert.match(rendered, /<\/blockquote><\/details>/);
  assert.equal((rendered.match(/class="telegram-spoiler"/g) || []).length, 2);
});

test("drops executable tags, handlers, and unsafe link protocols", () => {
  const rendered = sanitizeTelegramHtml(`
<script>alert(1)</script>
<img src="x" onerror="alert(2)">
<a href="javascript:alert(3)" onclick="alert(4)">危险链接</a>
  `);

  assert.doesNotMatch(rendered, /(?:<script|onerror|onclick|javascript:)/i);
  assert.match(rendered, /telegram-media-placeholder/);
  assert.match(rendered, /<span class="telegram-link">危险链接<\/span>/);
});

test("ignores invalid numeric entities in allowed attributes", () => {
  assert.doesNotThrow(() =>
    sanitizeTelegramHtml('<a href="https://example.com/?x=&#9999999999;">链接</a>'),
  );
});

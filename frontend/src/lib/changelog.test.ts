import assert from "node:assert/strict";
import test from "node:test";

import { extractRecentChangelogSections } from "./changelog.ts";

test("展示非空 Unreleased 并使用开发分支标题", () => {
  const sections = extractRecentChangelogSections(
    "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- 新能力\n\n## [1.0.0]\n\n- 已发布\n",
    4,
  );
  assert.equal(sections[0].title, "当前开发分支 · 尚未发布");
  assert.equal(sections[0].unreleased, true);
  assert.match(sections[0].body, /新能力/);
  assert.equal(sections[1].title, "[1.0.0]");
});

test("空 Unreleased 不占用最近版本数量", () => {
  const sections = extractRecentChangelogSections(
    "## [Unreleased]\n\n## [1.0.0]\n\n- 已发布\n",
    1,
  );
  assert.deepEqual(sections.map((section) => section.title), ["[1.0.0]"]);
});

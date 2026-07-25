// 更新日志面板：仅在用户打开版本号菜单时读取独立静态资源，避免把完整
// CHANGELOG 作为 JavaScript 字符串参与 Rollup 解析和压缩。
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import changelogUrl from "../../../../CHANGELOG.md?url";

function extractRecentChangelogSections(
  md: string,
  limit: number,
): Array<{ title: string; body: string }> {
  const lines = md.split(/\r?\n/);
  const starts: Array<{ idx: number; title: string }> = [];
  for (let i = 0; i < lines.length; i += 1) {
    const m = lines[i].match(/^##\s+\[(.+?)\].*$/);
    if (!m) continue;
    const title = lines[i].replace(/^##\s+/, "").trim();
    if (m[1].toLowerCase() === "unreleased") continue;
    starts.push({ idx: i, title });
  }
  const out: Array<{ title: string; body: string }> = [];
  for (let i = 0; i < starts.length && out.length < limit; i += 1) {
    const begin = starts[i].idx + 1;
    const end = i + 1 < starts.length ? starts[i + 1].idx : lines.length;
    const body = lines.slice(begin, end).join("\n").trim();
    if (!body) continue;
    out.push({ title: starts[i].title, body });
  }
  return out;
}

export default function ChangelogMenu() {
  const [changelogRaw, setChangelogRaw] = useState("");
  const [loadFailed, setLoadFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch(changelogUrl, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then(setChangelogRaw)
      .catch((error: unknown) => {
        if ((error as { name?: string })?.name !== "AbortError") setLoadFailed(true);
      });
    return () => controller.abort();
  }, []);
  const sections = useMemo(
    () => extractRecentChangelogSections(changelogRaw, 4),
    [changelogRaw],
  );
  return (
    <>
      <div className="border-b px-4 py-3">
        <div className="text-base font-semibold">更新日志</div>
        <div className="mt-1 text-sm text-muted-foreground">
          最近版本的主要变化，完整记录见仓库 CHANGELOG.md。
        </div>
      </div>
      <div className="space-y-5 p-4">
        {sections.length > 0 ? (
          sections.map((sec) => (
            <div key={sec.title}>
              <div className="text-sm font-semibold">{sec.title}</div>
              <article className="prose prose-sm mt-2 max-w-none text-sm text-muted-foreground dark:prose-invert">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{sec.body}</ReactMarkdown>
              </article>
            </div>
          ))
        ) : loadFailed ? (
          <p className="text-sm text-muted-foreground">未解析到更新日志内容，请检查 CHANGELOG.md。</p>
        ) : (
          <p className="text-sm text-muted-foreground">正在读取更新日志…</p>
        )}
      </div>
    </>
  );
}

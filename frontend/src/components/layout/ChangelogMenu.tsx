// 更新日志面板：仅在用户打开版本号菜单时读取独立静态资源，避免把完整
// CHANGELOG 作为 JavaScript 字符串参与 Rollup 解析和压缩。
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import changelogUrl from "../../../../CHANGELOG.md?url";
import { extractRecentChangelogSections } from "@/lib/changelog";

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
              <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
                {sec.title}
                {sec.unreleased ? (
                  <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                    开发中
                  </span>
                ) : null}
              </div>
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

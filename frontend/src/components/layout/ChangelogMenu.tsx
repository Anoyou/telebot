// 更新日志面板：仅在用户打开版本号菜单时读取独立静态资源，避免把完整
// CHANGELOG 作为 JavaScript 字符串参与 Rollup 解析和压缩。
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Button } from "@/components/ui/button";
import { extractRecentChangelogSections } from "@/lib/changelog";

const CHANGELOG_URL = "/runtime-content/CHANGELOG.md";

export default function ChangelogMenu() {
  const changelogQ = useQuery({
    queryKey: ["runtime-content", "changelog"],
    queryFn: async ({ signal }) => {
      const response = await fetch(CHANGELOG_URL, { cache: "no-cache", signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.text();
    },
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
  const sections = useMemo(
    () => extractRecentChangelogSections(changelogQ.data ?? "", 4),
    [changelogQ.data],
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
        ) : changelogQ.isError ? (
          <div className="space-y-3 text-sm text-muted-foreground">
            <p>更新日志读取失败：{(changelogQ.error as Error)?.message || "网络错误"}</p>
            <Button size="sm" variant="outline" onClick={() => void changelogQ.refetch()}>
              <RefreshCw className="mr-1.5 h-4 w-4" />
              重新读取
            </Button>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">正在读取更新日志…</p>
        )}
      </div>
    </>
  );
}

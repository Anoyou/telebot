export interface ChangelogSection {
  title: string;
  body: string;
  unreleased: boolean;
}

export function extractRecentChangelogSections(
  markdown: string,
  limit: number,
): ChangelogSection[] {
  const lines = markdown.split(/\r?\n/);
  const starts: Array<{ idx: number; rawTitle: string; unreleased: boolean }> = [];
  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^##\s+\[(.+?)\].*$/);
    if (!match) continue;
    starts.push({
      idx: index,
      rawTitle: lines[index].replace(/^##\s+/, "").trim(),
      unreleased: match[1].toLowerCase() === "unreleased",
    });
  }

  const sections: ChangelogSection[] = [];
  for (let index = 0; index < starts.length && sections.length < limit; index += 1) {
    const start = starts[index];
    const end = index + 1 < starts.length ? starts[index + 1].idx : lines.length;
    const body = lines.slice(start.idx + 1, end).join("\n").trim();
    if (!body) continue;
    sections.push({
      title: start.unreleased ? "当前开发分支 · 尚未发布" : start.rawTitle,
      body,
      unreleased: start.unreleased,
    });
  }
  return sections;
}

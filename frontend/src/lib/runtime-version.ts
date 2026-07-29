import type { BackendVersionInfo } from "@/api/types";

export function formatRuntimeVersionLabel(
  info?: BackendVersionInfo | null,
  fallback = "正在读取…",
): string {
  if (!info) return fallback;
  const parts = [`v${info.version}`];
  if (info.stage) parts.push(info.stage);
  if (info.revision) parts.push(info.revision.slice(0, 8));
  return parts.join(" · ");
}

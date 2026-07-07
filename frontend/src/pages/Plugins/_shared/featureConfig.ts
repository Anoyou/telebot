import type { FeatureInfo } from "@/api/types";

export const FEATURE_CONFIG_PAGE_KEYS = new Set([
  "auto_reply",
  "autorepeat",
  "chatgpt_image",
  "codex_image",
  "scheduler",
  "game24",
]);

export type FeatureConfigSource = "account" | "plugins";

export function featureConfigPath(
  aid: number | null | undefined,
  key: string,
  feature?: Pick<FeatureInfo, "config_schema"> | null,
  options?: { source?: FeatureConfigSource },
): string | null {
  if (!aid || !key) return null;
  if (!FEATURE_CONFIG_PAGE_KEYS.has(key) && !feature?.config_schema) return null;
  const path = `/accounts/${aid}/features/${key}`;
  if (!options?.source || options.source === "account") return path;
  const params = new URLSearchParams({ from: options.source });
  return `${path}?${params.toString()}`;
}

export function featureConfigBackTarget(
  aid: number,
  search = "",
): { backLabel: string; backHref: string } {
  const source = new URLSearchParams(search).get("from");
  if (source === "plugins") {
    const params = new URLSearchParams({ account: String(aid) });
    return {
      backLabel: "返回插件中心",
      backHref: `/plugins?${params.toString()}`,
    };
  }
  return {
    backLabel: "返回账号",
    backHref: `/accounts/${aid}?tab=features`,
  };
}

export function formatFeatureVersion(version?: string | null): string {
  const value = (version || "").trim();
  if (!value) return "版本未知";
  return value.startsWith("v") ? value : `v${value}`;
}

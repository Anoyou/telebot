type SchemaLike = object | null | undefined;

const USAGE_FIELD_KEYS = [
  "usage_preview",
  "usage_guide",
  "usage_instructions",
  "ai_usage_guide",
];

const USAGE_SCHEMA_KEYS = [
  "x-usage-guide",
  "x-usage-instructions",
  "x-usage-steps",
  "x-help",
];

const USAGE_WARNING_RE = /使用说明|usage(_|-| )?(preview|guide|instructions)|x-usage/i;

export function hasDeclaredUsageGuide(schema: SchemaLike, usage?: unknown): boolean {
  if (schemaHasContent(usage)) return true;
  if (!schema || typeof schema !== "object") return false;
  const schemaRecord = schema as Record<string, unknown>;
  for (const key of USAGE_SCHEMA_KEYS) {
    if (schemaHasContent(schemaRecord[key])) return true;
  }

  const properties = schemaRecord.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return false;
  const entries = Object.entries(properties as Record<string, unknown>);
  return entries.some(([key, rawField]) => {
    const field = rawField && typeof rawField === "object" ? rawField as Record<string, unknown> : {};
    return (
      USAGE_FIELD_KEYS.includes(key) &&
      (schemaHasContent(field.default) || schemaHasContent(field.description))
    );
  });
}

export function pluginUsageGuideWarning(feature: { config_schema?: SchemaLike; usage?: unknown }): string | null {
  if (hasDeclaredUsageGuide(feature.config_schema, feature.usage)) return null;
  return "高级规范警告：插件未声明详细使用说明。请在 plugin.json 中提供 usage，或在 config_schema 中提供 usage_preview、x-usage-guide 或 x-usage-steps。";
}

export function isHighSeverityPluginWarning(warning: string): boolean {
  return USAGE_WARNING_RE.test(warning) || warning.includes("高级规范警告");
}

export function splitPluginWarnings(warnings?: string[]): {
  high: string[];
  normal: string[];
  all: string[];
} {
  const all = Array.from(
    new Set((warnings ?? []).map((item) => item.trim()).filter(Boolean)),
  );
  const high = all.filter(isHighSeverityPluginWarning);
  const normal = all.filter((item) => !isHighSeverityPluginWarning(item));
  return { high, normal, all };
}

function schemaHasContent(value: unknown): boolean {
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.some(schemaHasContent);
  if (value && typeof value === "object") return Object.keys(value).length > 0;
  return false;
}

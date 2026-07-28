import type { CSSProperties } from "react";

import { cn } from "@/lib/utils";

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
import { resolveModelBrand, type ModelBrandId } from "@/lib/modelBrand";
import { MODEL_BRAND_ICONS } from "@/lib/modelBrandIcons";

const BRAND_META_FALLBACK: Record<ModelBrandId, { label: string; accent: string }> = {
  auto: { label: "自动路由", accent: "#8b9cff" },
  openai: { label: "OpenAI", accent: "#10a37f" },
  anthropic: { label: "Anthropic", accent: "#d4a27f" },
  deepseek: { label: "DeepSeek", accent: "#4D6BFE" },
  google: { label: "Google", accent: "#4285f4" },
  xai: { label: "xAI", accent: "#e8e8e8" },
  meta: { label: "Meta", accent: "#0668e1" },
  mistral: { label: "Mistral", accent: "#ff7000" },
  qwen: { label: "通义千问", accent: "#615ced" },
  moonshot: { label: "Moonshot", accent: "#94a3b8" },
  zhipu: { label: "智谱", accent: "#e8e8e8" },
  bytedance: { label: "字节豆包", accent: "#3b82f6" },
  microsoft: { label: "Microsoft", accent: "#00a4ef" },
  amazon: { label: "Amazon", accent: "#ff9900" },
  cohere: { label: "Cohere", accent: "#39594d" },
  perplexity: { label: "Perplexity", accent: "#22b8cd" },
  nvidia: { label: "NVIDIA", accent: "#76b900" },
  minimax: { label: "MiniMax", accent: "#e11d48" },
  baichuan: { label: "百川", accent: "#ff6a00" },
  yi: { label: "零一万物", accent: "#a3a3a3" },
  ollama: { label: "Ollama", accent: "#c4c4c4" },
  openrouter: { label: "OpenRouter", accent: "#a78bfa" },
  unknown: { label: "未知", accent: "#94a3b8" },
};

function AutoMark({ className, title, color }: { className?: string; title?: string; color?: string }) {
  const style: CSSProperties | undefined = color ? { color } : undefined;
  return (
    <svg viewBox="0 0 24 24" aria-hidden={title ? undefined : true} role={title ? "img" : undefined} className={cn("shrink-0", className)} style={style}>
      {title ? <title>{title}</title> : null}
      <path
        fill="currentColor"
        d="M12 3.2 13.4 8.4 18.8 9.8 13.4 11.2 12 16.4 10.6 11.2 5.2 9.8 10.6 8.4 12 3.2Zm5.8 9.6.8 2.6 2.6.8-2.6.8-.8 2.6-.8-2.6-2.6-.8 2.6-.8.8-2.6Z"
      />
    </svg>
  );
}

function UnknownMark({ className, title, color }: { className?: string; title?: string; color?: string }) {
  const style: CSSProperties | undefined = color ? { color } : undefined;
  return (
    <svg viewBox="0 0 24 24" aria-hidden={title ? undefined : true} role={title ? "img" : undefined} className={cn("shrink-0", className)} style={style}>
      {title ? <title>{title}</title> : null}
      <path
        fill="currentColor"
        d="M12 3.4a8.6 8.6 0 1 1 0 17.2 8.6 8.6 0 0 1 0-17.2Zm0 2.2a6.4 6.4 0 1 0 0 12.8 6.4 6.4 0 0 0 0-12.8Zm0 2.3c.8 0 1.4.6 1.4 1.4v2.3h2.3a1.4 1.4 0 0 1 0 2.8h-2.3v2.3a1.4 1.4 0 0 1-2.8 0v-2.3H8.3a1.4 1.4 0 0 1 0-2.8h2.3V9.3c0-.8.6-1.4 1.4-1.4Z"
      />
    </svg>
  );
}

function SourcedBrandMark({
  brandId,
  className,
  title,
  color,
}: {
  brandId: ModelBrandId;
  className?: string;
  title?: string;
  color?: string;
}) {
  if (brandId === "auto") return <AutoMark className={className} title={title} color={color} />;
  const icon = MODEL_BRAND_ICONS[brandId];
  if (!icon) return <UnknownMark className={className} title={title} color={color} />;

  const style: CSSProperties | undefined = color ? { color } : undefined;
  return (
    <svg
      viewBox={icon.viewBox}
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      className={cn("shrink-0", className)}
      style={style}
      data-icon-source={icon.source}
      // Real brand paths from lobe-icons / simple-icons, monochrome currentColor.
      dangerouslySetInnerHTML={{ __html: (title ? `<title>${escapeHtml(title)}</title>` : "") + icon.paths }}
    />
  );
}

export function ModelBrandLogo({
  model,
  providerName,
  auto = false,
  brandId,
  className,
  size = 14,
  tinted = true,
  title,
}: {
  model?: string | null;
  providerName?: string | null;
  auto?: boolean;
  brandId?: ModelBrandId;
  className?: string;
  size?: number;
  /** true：品牌强调色；false：跟随当前文字色，适配主题 */
  tinted?: boolean;
  title?: string;
}) {
  const resolved = resolveModelBrand(model, providerName, { auto: auto || brandId === "auto" });
  const id = brandId || resolved.id;
  const meta = BRAND_META_FALLBACK[id] || BRAND_META_FALLBACK.unknown;
  const label = id === resolved.id ? resolved.label : meta.label;
  const accent = BRAND_META_FALLBACK[id]?.accent || resolved.accent || meta.accent;

  return (
    <span
      className={cn("inline-flex items-center justify-center leading-none", className)}
      style={{ width: size, height: size }}
      data-model-brand={id}
    >
      <SourcedBrandMark
        brandId={id}
        className="h-full w-full"
        title={title || label}
        color={tinted ? accent : undefined}
      />
    </span>
  );
}

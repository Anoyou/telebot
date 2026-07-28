import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";
import { resolveModelBrand, type ModelBrandId } from "@/lib/modelBrand";

type MarkProps = {
  brandId: ModelBrandId;
  className?: string;
  title?: string;
  color?: string;
};

function SvgShell({
  className,
  title,
  children,
  color,
}: {
  className?: string;
  title?: string;
  children: ReactNode;
  color?: string;
}) {
  const style: CSSProperties | undefined = color ? { color } : undefined;
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      className={cn("shrink-0", className)}
      style={style}
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  );
}

function BrandMark({ brandId, className, title, color }: MarkProps) {
  switch (brandId) {
    case "auto":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M12 2.5 13.7 8.3 19.5 10 13.7 11.7 12 17.5 10.3 11.7 4.5 10 10.3 8.3 12 2.5Zm6.5 11.2 1 3.2 3.2 1-3.2 1-1 3.2-1-3.2-3.2-1 3.2-1 1-3.2Z"
          />
        </SvgShell>
      );
    case "openai":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M16.4 9.1c.3-1-.1-2.1-1-2.7-.9-.6-2.1-.6-3 0l-1.1.6-1.1-.6c-.9-.6-2.1-.6-3 0s-1.3 1.7-1 2.7l.3 1.2-1 .7c-.9.6-1.3 1.7-1 2.7.3 1 1.2 1.6 2.2 1.6h.3l1.1-.1.3 1.1c.3 1 1.2 1.7 2.2 1.7s1.9-.7 2.2-1.7l.3-1.1 1.1.1h.3c1 0 1.9-.6 2.2-1.6.3-1-.1-2.1-1-2.7l-1-.7.3-1.2Zm-4.4 6.2c-.4 0-.8-.2-1-.5l-.4-1.3h2.8l-.4 1.3c-.2.3-.6.5-1 .5Zm3.4-2.5-.9-.1-.5-1.5.8-.6c.3-.2.4-.5.3-.8l-.2-.9c-.1-.3.1-.5.3-.6l.9-.5c.2-.1.5 0 .6.2l.5 1.2c.1.3 0 .6-.2.8l-.8.6.2 1.6c0 .3-.2.6-.5.6h-1.1Zm-6.8 0c-.3 0-.5-.3-.5-.6l.2-1.6-.8-.6c-.2-.2-.3-.5-.2-.8l.5-1.2c.1-.2.4-.3.6-.2l.9.5c.2.1.4.3.3.6l-.2.9c-.1.3 0 .6.3.8l.8.6-.5 1.5-.9.1H8.6Zm1.4-3.1.9-1.5c.2-.3.6-.3.8 0l.9 1.5.5 1.6h-3.6l.5-1.6Z"
          />
        </SvgShell>
      );
    case "anthropic":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M12.8 3.2h2.6L21 20.8h-2.8l-1.2-3.6H9.7l-1.2 3.6H5.8L12.8 3.2Zm.1 5-2.4 7h4.9l-2.5-7Z"
          />
        </SvgShell>
      );
    case "deepseek":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M5 7.5c0-1.4 1.1-2.5 2.5-2.5H13c3.6 0 6.5 2.9 6.5 6.5S16.6 18 13 18H9.8l-2.6 2.2c-.5.4-1.2 0-1.2-.6V18H7.5C6.1 18 5 16.9 5 15.5v-8Zm3.2 1.3v6.4h4.8c2.1 0 3.8-1.7 3.8-3.8S15.1 7.6 13 7.6H8.2v1.2Z"
          />
        </SvgShell>
      );
    case "google":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M21.2 12.2c0-.6-.1-1.2-.2-1.7H12v3.3h5.2c-.2 1.2-.9 2.2-1.9 2.9v2.4h3.1c1.8-1.7 2.8-4.1 2.8-6.9ZM12 21.5c2.6 0 4.8-.9 6.4-2.4l-3.1-2.4c-.9.6-2 1-3.3 1-2.5 0-4.6-1.7-5.4-4H3.4v2.5C5 19.7 8.2 21.5 12 21.5Zm-5.4-7.1c-.2-.6-.3-1.2-.3-1.9s.1-1.3.3-1.9V8.1H3.4C2.8 9.3 2.5 10.6 2.5 12s.3 2.7.9 3.9l3.2-1.5Zm5.4-8.4c1.4 0 2.7.5 3.7 1.4l2.8-2.8C16.8 3.1 14.6 2.2 12 2.2 8.2 2.2 5 4 3.4 7.1l3.2 2.5c.8-2.3 2.9-4 5.4-4Z"
          />
        </SvgShell>
      );
    case "xai":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M5.2 4.5h3.3l3.4 4.7 3.5-4.7h3.4l-5.2 7 5.6 7.5h-3.4l-3.8-5.1-3.8 5.1H5.8l5.6-7.5-5.2-7Z"
          />
        </SvgShell>
      );
    case "meta":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M8.4 7.2c1.1 0 2 .7 2.9 2.3.5.9 1 2 1.5 3.2.5-1.2 1-2.3 1.5-3.2.9-1.6 1.8-2.3 2.9-2.3 1.6 0 2.8 1.3 2.8 3.4 0 2.7-1.7 5.9-3.7 8.1-1.1 1.2-2.2 1.8-3.2 1.8s-2.1-.6-3.2-1.8C7.9 16.5 6.2 13.3 6.2 10.6c0-2.1 1.2-3.4 2.2-3.4Zm0 1.6c-.4 0-.9.5-.9 1.8 0 2 1.4 4.8 3 6.6.8.9 1.5 1.3 2.1 1.3s1.3-.4 2.1-1.3c1.6-1.8 3-4.6 3-6.6 0-1.3-.5-1.8-.9-1.8-.5 0-1 .5-1.7 1.8-.7 1.2-1.4 2.9-2 4.5l-.5 1.3-.5-1.3c-.6-1.6-1.3-3.3-2-4.5-.7-1.3-1.2-1.8-1.7-1.8Z"
          />
        </SvgShell>
      );
    case "mistral":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M3.5 6.5h3.2v3.2H3.5V6.5Zm4.4 0h3.2v11H7.9v-11Zm4.4 0h3.2v3.2h-3.2V6.5Zm0 7.8h3.2v3.2h-3.2v-3.2Zm4.4-7.8h3.2v11h-3.2v-11Z"
          />
        </SvgShell>
      );
    case "qwen":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M12 3.5A8.5 8.5 0 1 1 3.5 12 8.5 8.5 0 0 1 12 3.5Zm0 2.2A6.3 6.3 0 1 0 18.3 12 6.3 6.3 0 0 0 12 5.7Zm-2.2 3.1h4.4l1.3 2.2-1.3 2.2H9.8L8.5 11l1.3-2.2Zm7.4 8.4 1.6 1.6-1.5 1.5-1.6-1.6 1.5-1.5Z"
          />
        </SvgShell>
      );
    case "moonshot":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M14.2 4.2a8.2 8.2 0 1 0 5.4 14.4 8.3 8.3 0 0 1-11-11.4 8.1 8.1 0 0 0 5.6-3Z"
          />
        </SvgShell>
      );
    case "zhipu":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path fill="currentColor" d="M5.5 5.5h13v2.4l-7.2 7.2H18.5v3.4h-13v-2.4l7.2-7.2H5.5V5.5Z" />
        </SvgShell>
      );
    case "bytedance":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path fill="currentColor" d="M6 4.8h3.2v14.4H6V4.8Zm8.8 0H18v14.4h-3.2V4.8ZM10.6 9h2.8v6h-2.8V9Z" />
        </SvgShell>
      );
    case "microsoft":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path fill="currentColor" d="M4.5 4.5h7v7h-7v-7Zm8 0h7v7h-7v-7Zm-8 8h7v7h-7v-7Zm8 0h7v7h-7v-7Z" />
        </SvgShell>
      );
    case "amazon":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M5.2 14.8c2.8 1.4 6.5 2.1 9.7 1.3 1.4-.3 2.8-1 3.9-1.8.3-.2 0-.6-.3-.4-1.9 1.1-4.1 1.8-6.5 1.8-2.7 0-5.3-.8-7.5-2.1-.3-.2-.6.1-.3.6l1 .6Zm11.4-3.4c-.2-.3-1.4-.1-2-.1-.2 0-.2-.2 0-.3 1-.7 2.6-.5 2.8-.2s-.2 2-.1 2.3c0 .2.2.2.3 0 .7-.8 1.1-2.1-1-1.7Z"
          />
        </SvgShell>
      );
    case "cohere":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M12 3.8a8.2 8.2 0 1 1-5.9 14l1.6-1.7A5.9 5.9 0 1 0 12 6.1c1.5 0 2.9.6 3.9 1.5l1.6-1.7A8.1 8.1 0 0 0 12 3.8Z"
          />
        </SvgShell>
      );
    case "perplexity":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path fill="currentColor" d="M7 3.8h2.4v6.2L16.6 3.8H20L12.2 11l8 9.2h-3.5l-6.3-7.2v7.2H7V3.8Z" />
        </SvgShell>
      );
    case "nvidia":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M4 14.2c3.8 2.1 8.4 2.7 12.2 1.5 1.4-.4 2.8-1.1 3.8-2 .2-.2 0-.4-.2-.3-2.5 1.1-5.4 1.8-8.4 1.5-2.4-.2-4.8-1-6.9-2.3-.3-.2-.6.1-.5.6Zm9.4-7.5c.8 0 1.5.2 2 .7.3.3.3.7 0 1l-2.6 2.5c-.3.3-.7.3-1 0L9.3 8.4c-.3-.3-.3-.7 0-1 .5-.5 1.2-.7 2-.7h2.1Z"
          />
        </SvgShell>
      );
    case "minimax":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M4.5 17.5 8.8 6.5h2.8l2.6 7.2 2.6-7.2h2.8l4.3 11h-2.9l-2.7-7.2-2.7 7.2h-2.8l-2.7-7.2-2.7 7.2H4.5Z"
          />
        </SvgShell>
      );
    case "baichuan":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path fill="currentColor" d="M6 5.5h12v2.2H6V5.5Zm0 5.4h12v2.2H6v-2.2Zm0 5.4h12V18.5H6v-2.2Z" />
        </SvgShell>
      );
    case "yi":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M10.2 4.5h3.6v9.2c0 2.4-1.4 4.3-4.4 4.3H7.8v-2.6h1.4c1.3 0 1.9-.7 1.9-1.9V4.5Z"
          />
        </SvgShell>
      );
    case "ollama":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M12 3.5c2.4 0 4.4 1.5 5.2 3.6.9-.3 1.9-.2 2.7.4 1.2.9 1.5 2.5.8 3.8.8.7 1.3 1.8 1.3 3 0 2.2-1.8 4-4 4H14l-1.5 2.6c-.2.4-.8.4-1 0L10 18.3H7c-2.2 0-4-1.8-4-4 0-1.2.5-2.3 1.3-3-.7-1.3-.4-2.9.8-3.8.8-.6 1.8-.7 2.7-.4C7.6 5 9.6 3.5 12 3.5Zm-2.6 7.2a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Zm5.2 0a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Z"
          />
        </SvgShell>
      );
    case "openrouter":
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M7.2 6.2h3.2v4.4l4.2-4.4h3.6l-5.5 5.6 5.8 6.4h-3.7l-4.4-4.9v4.9H7.2V6.2Z"
          />
        </SvgShell>
      );
    default:
      return (
        <SvgShell className={className} title={title} color={color}>
          <path
            fill="currentColor"
            d="M12 3.8a8.2 8.2 0 1 1 0 16.4 8.2 8.2 0 0 1 0-16.4Zm0 2.1a6.1 6.1 0 1 0 0 12.2 6.1 6.1 0 0 0 0-12.2Zm0 2.2c.7 0 1.3.6 1.3 1.3v2.2h2.2a1.3 1.3 0 0 1 0 2.6h-2.2v2.2a1.3 1.3 0 0 1-2.6 0v-2.2H8.5a1.3 1.3 0 0 1 0-2.6h2.2V9.4c0-.7.6-1.3 1.3-1.3Z"
          />
        </SvgShell>
      );
  }
}

const BRAND_META_FALLBACK: Record<ModelBrandId, { label: string; accent: string }> = {
  auto: { label: "自动路由", accent: "#8b9cff" },
  openai: { label: "OpenAI", accent: "#10a37f" },
  anthropic: { label: "Anthropic", accent: "#d4a27f" },
  deepseek: { label: "DeepSeek", accent: "#4d6bfe" },
  google: { label: "Google", accent: "#4285f4" },
  xai: { label: "xAI", accent: "#e8e8e8" },
  meta: { label: "Meta", accent: "#0668e1" },
  mistral: { label: "Mistral", accent: "#ff7000" },
  qwen: { label: "通义千问", accent: "#615ced" },
  moonshot: { label: "Moonshot", accent: "#94a3b8" },
  zhipu: { label: "智谱", accent: "#3859ff" },
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
  const accent = id === resolved.id ? resolved.accent : meta.accent;

  return (
    <span
      className={cn("inline-flex items-center justify-center leading-none", className)}
      style={{ width: size, height: size }}
      data-model-brand={id}
    >
      <BrandMark
        brandId={id}
        className="h-full w-full"
        title={title || label}
        color={tinted ? accent : undefined}
      />
    </span>
  );
}

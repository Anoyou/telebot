/**
 * 从模型 ID / Provider 名称推断所属公司品牌，用于 UI logo。
 * 优先看 model 本体（含 OpenRouter 风格 org/model），再回退 providerName。
 */

export type ModelBrandId =
  | "auto"
  | "openai"
  | "anthropic"
  | "deepseek"
  | "google"
  | "xai"
  | "meta"
  | "mistral"
  | "qwen"
  | "moonshot"
  | "zhipu"
  | "bytedance"
  | "microsoft"
  | "amazon"
  | "cohere"
  | "perplexity"
  | "nvidia"
  | "minimax"
  | "baichuan"
  | "yi"
  | "ollama"
  | "openrouter"
  | "unknown";

export type ModelBrand = {
  id: ModelBrandId;
  label: string;
  /** 深色背景下可读的品牌色 */
  accent: string;
};

const BRANDS: Record<ModelBrandId, ModelBrand> = {
  auto: { id: "auto", label: "自动路由", accent: "#8b9cff" },
  openai: { id: "openai", label: "OpenAI", accent: "#10a37f" },
  anthropic: { id: "anthropic", label: "Anthropic", accent: "#d4a27f" },
  deepseek: { id: "deepseek", label: "DeepSeek", accent: "#4D6BFE" },
  google: { id: "google", label: "Google", accent: "#4285f4" },
  xai: { id: "xai", label: "xAI", accent: "#e8e8e8" },
  meta: { id: "meta", label: "Meta", accent: "#0668e1" },
  mistral: { id: "mistral", label: "Mistral", accent: "#ff7000" },
  qwen: { id: "qwen", label: "通义千问", accent: "#615ced" },
  moonshot: { id: "moonshot", label: "Moonshot", accent: "#94a3b8" },
  zhipu: { id: "zhipu", label: "智谱", accent: "#e8e8e8" },
  bytedance: { id: "bytedance", label: "字节豆包", accent: "#3b82f6" },
  microsoft: { id: "microsoft", label: "Microsoft", accent: "#00a4ef" },
  amazon: { id: "amazon", label: "Amazon", accent: "#ff9900" },
  cohere: { id: "cohere", label: "Cohere", accent: "#39594d" },
  perplexity: { id: "perplexity", label: "Perplexity", accent: "#22b8cd" },
  nvidia: { id: "nvidia", label: "NVIDIA", accent: "#76b900" },
  minimax: { id: "minimax", label: "MiniMax", accent: "#e11d48" },
  baichuan: { id: "baichuan", label: "百川", accent: "#ff6a00" },
  yi: { id: "yi", label: "零一万物", accent: "#a3a3a3" },
  ollama: { id: "ollama", label: "Ollama", accent: "#c4c4c4" },
  openrouter: { id: "openrouter", label: "OpenRouter", accent: "#a78bfa" },
  unknown: { id: "unknown", label: "未知", accent: "#94a3b8" },
};

/** 模型 ID 片段匹配（按优先级从前到后） */
const MODEL_RULES: Array<{ brand: ModelBrandId; pattern: RegExp }> = [
  { brand: "deepseek", pattern: /\bdeepseek\b|deepseek-ai/i },
  { brand: "anthropic", pattern: /\bclaude\b|\banthropic\b/i },
  { brand: "openai", pattern: /\bgpt[-_.]?\d|\bo[1-9](-|\b)|\bchatgpt\b|\bopenai\b|\bo4-mini\b|\bo3\b|\bo1\b/i },
  { brand: "google", pattern: /\bgemini\b|\bgemma\b|\bgoogle\b|\bpalm\b/i },
  { brand: "xai", pattern: /\bgrok\b|\bxai\b/i },
  { brand: "meta", pattern: /\bllama\b|\bmeta-llama\b|\bllava\b/i },
  { brand: "mistral", pattern: /\bmistral\b|\bmixtral\b|\bcodestral\b|\bpixtral\b/i },
  { brand: "qwen", pattern: /\bqwen\d|\bqwen[-_.]|\bqwen\b|\bqwq\b|\btongyi\b|\bdashscope\b/i },
  { brand: "moonshot", pattern: /\bmoonshot\b|\bkimi\b/i },
  { brand: "zhipu", pattern: /\bglm[-_.]?\d|\bchatglm\b|\bzhipu\b|\bbigmodel\b/i },
  { brand: "bytedance", pattern: /\bdoubao\b|\bseed[-_.]?\d|\bbytedance\b|\bvolcengine\b/i },
  { brand: "microsoft", pattern: /\bphi[-_.]?\d|\bwizardlm\b|\bmicrosoft\b/i },
  { brand: "amazon", pattern: /\bnova[-_.]?\w|\btitan\b|\bamazon\b|\bbedrock\b/i },
  { brand: "cohere", pattern: /\bcommand-r\b|\bcommand\b|\bcohere\b|\baya\b/i },
  { brand: "perplexity", pattern: /\bsonar\b|\bperplexity\b|\bpplx\b/i },
  { brand: "nvidia", pattern: /\bnemtron\b|\bnvidia\b/i },
  { brand: "minimax", pattern: /\bminimax\b|\babab\b/i },
  { brand: "baichuan", pattern: /\bbaichuan\b/i },
  { brand: "yi", pattern: /\byi[-_.]?\d|\b01-ai\b/i },
  { brand: "ollama", pattern: /\bollama\b/i },
  { brand: "openrouter", pattern: /\bopenrouter\b/i },
];

/** Provider 名称匹配（ollama 需在 meta/llama 前，避免 Ollama 被 llama 子串误伤） */
const PROVIDER_RULES: Array<{ brand: ModelBrandId; pattern: RegExp }> = [
  { brand: "deepseek", pattern: /deepseek/i },
  { brand: "anthropic", pattern: /anthropic|claude/i },
  { brand: "openai", pattern: /openai|chatgpt/i },
  { brand: "google", pattern: /google|gemini|vertex/i },
  { brand: "xai", pattern: /\bxai\b|grok/i },
  { brand: "ollama", pattern: /ollama/i },
  { brand: "meta", pattern: /\bmeta\b|\bllama\b|meta-llama/i },
  { brand: "mistral", pattern: /mistral/i },
  { brand: "qwen", pattern: /qwen|通义|dashscope|阿里/i },
  { brand: "moonshot", pattern: /moonshot|kimi|月之暗面/i },
  { brand: "zhipu", pattern: /zhipu|智谱|bigmodel|glm/i },
  { brand: "bytedance", pattern: /doubao|豆包|bytedance|字节|volc/i },
  { brand: "microsoft", pattern: /microsoft|azure|phi/i },
  { brand: "amazon", pattern: /amazon|bedrock|aws/i },
  { brand: "cohere", pattern: /cohere/i },
  { brand: "perplexity", pattern: /perplexity/i },
  { brand: "nvidia", pattern: /nvidia/i },
  { brand: "minimax", pattern: /minimax/i },
  { brand: "baichuan", pattern: /baichuan|百川/i },
  { brand: "yi", pattern: /零一|01\.ai|yi-/i },
  { brand: "openrouter", pattern: /openrouter/i },
];

function matchRules(
  text: string,
  rules: Array<{ brand: ModelBrandId; pattern: RegExp }>,
): ModelBrandId | null {
  for (const rule of rules) {
    if (rule.pattern.test(text)) return rule.brand;
  }
  return null;
}

/** OpenRouter / 网关风格 `org/model` 取 org 段 */
function orgSegment(model: string): string | null {
  const slash = model.indexOf("/");
  if (slash <= 0) return null;
  return model.slice(0, slash).trim() || null;
}

export function resolveModelBrand(
  model?: string | null,
  providerName?: string | null,
  opts?: { auto?: boolean },
): ModelBrand {
  if (opts?.auto) return BRANDS.auto;

  const rawModel = (model || "").trim();
  const rawProvider = (providerName || "").trim();

  if (rawModel) {
    const fromModel = matchRules(rawModel, MODEL_RULES);
    if (fromModel) return BRANDS[fromModel];

    const org = orgSegment(rawModel);
    if (org) {
      const fromOrg = matchRules(org, MODEL_RULES) || matchRules(org, PROVIDER_RULES);
      if (fromOrg) return BRANDS[fromOrg];
    }
  }

  if (rawProvider) {
    const fromProvider = matchRules(rawProvider, PROVIDER_RULES);
    if (fromProvider) return BRANDS[fromProvider];
  }

  return BRANDS.unknown;
}

export function modelBrandLabel(brand: ModelBrandId): string {
  return BRANDS[brand]?.label || BRANDS.unknown.label;
}

export function listModelBrands(): ModelBrand[] {
  return Object.values(BRANDS);
}

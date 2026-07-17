import { Select } from "@/components/ui/select";
import type {
  LLMApiFormat,
  LLMReasoningEffort,
} from "@/api/types";

export type ReasoningEffortValue = "" | LLMReasoningEffort;

const EFFORT_ORDER: LLMReasoningEffort[] = [
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];

const EFFORT_LABELS: Record<LLMReasoningEffort, string> = {
  minimal: "Minimal（最少推理）",
  low: "Low（轻度，快速响应）",
  medium: "Medium（平衡速度与深度）",
  high: "High（深度推理）",
  xhigh: "xHigh / Extra High（OpenAI 极高档）",
  max: "Max（Claude Opus 最高档）",
};

export function reasoningEffortsForModel({
  declared,
  apiFormat,
  modelId,
}: {
  declared?: LLMReasoningEffort[] | null;
  apiFormat: LLMApiFormat;
  modelId?: string | null;
}): { efforts: LLMReasoningEffort[]; declared: boolean } {
  if (declared) {
    const known = new Set(declared);
    return {
      efforts: EFFORT_ORDER.filter((effort) => known.has(effort)),
      declared: true,
    };
  }
  if (apiFormat === "anthropic_messages") {
    const efforts: LLMReasoningEffort[] = ["low", "medium", "high"];
    if ((modelId || "").toLowerCase().includes("opus")) efforts.push("max");
    return { efforts, declared: false };
  }
  return {
    efforts: ["minimal", "low", "medium", "high", "xhigh"],
    declared: false,
  };
}

export function ReasoningEffortSelect({
  id,
  value,
  onChange,
  declaredEfforts,
  apiFormat,
  modelId,
  disabled,
}: {
  id?: string;
  value: ReasoningEffortValue;
  onChange: (value: ReasoningEffortValue) => void;
  declaredEfforts?: LLMReasoningEffort[] | null;
  apiFormat: LLMApiFormat;
  modelId?: string | null;
  disabled?: boolean;
}) {
  const capability = reasoningEffortsForModel({
    declared: declaredEfforts,
    apiFormat,
    modelId,
  });
  const options = capability.efforts.includes(value as LLMReasoningEffort)
    ? capability.efforts
    : value
      ? [value as LLMReasoningEffort, ...capability.efforts]
      : capability.efforts;

  return (
    <div className="space-y-1.5">
      <Select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as ReasoningEffortValue)}
      >
        <option value="">Off（不指定强度，跟随模型）</option>
        {options.map((effort) => (
          <option key={effort} value={effort}>
            {EFFORT_LABELS[effort]}
          </option>
        ))}
      </Select>
      <p className="text-xs leading-5 text-muted-foreground">
        {capability.efforts.length === 0
          ? "当前协议未声明可调推理强度，将使用上游默认行为。"
          : capability.declared
            ? `该模型已声明支持：${capability.efforts.join(" / ")}。`
            : "上游模型列表未声明档位；这里按协议提供候选值，需通过真实验证确认。"}
      </p>
    </div>
  );
}

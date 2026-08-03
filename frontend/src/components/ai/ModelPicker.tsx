import { Check, ChevronDown } from "lucide-react";

import { ModelBrandLogo } from "@/components/ai/ModelBrandLogo";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export type ModelPickerItem = {
  providerId: number;
  providerName: string;
  model: string;
  executionBackend?: "direct" | "codex_gateway" | null;
  /** 声明支持 tools（metadata） */
  declaredTools?: boolean | null;
  declaredVision?: boolean | null;
  declaredReasoning?: boolean | null;
  /** 探测缓存实测 */
  probedTools?: boolean | null;
  probedStatus?: string | null;
  /** 健康：healthy | cooling | uncertain */
  healthState?: string | null;
  cooldownSeconds?: number | null;
  lastError?: string | null;
  /** Agent 场景是否可选（无 tools 则灰显） */
  agentEligible?: boolean;
  disabledReason?: string | null;
};

export type ModelPickerValue =
  | { mode: "auto" }
  | { mode: "pinned"; providerId: number; model: string };

function badge(
  label: string,
  tone: "default" | "ok" | "warn" | "muted" = "default",
): { label: string; className: string } {
  const tones = {
    default: "border-border/60 text-muted-foreground",
    ok: "border-emerald-500/40 text-emerald-600 dark:text-emerald-400",
    warn: "border-warning/40 text-warning",
    muted: "border-border/40 text-muted-foreground/70",
  };
  return { label, className: tones[tone] };
}

function itemBadges(item: ModelPickerItem): { label: string; className: string }[] {
  const out: { label: string; className: string }[] = [];
  if (item.declaredTools === true) out.push(badge("声明 Tools", "ok"));
  if (item.declaredTools === false) out.push(badge("无 Tools", "muted"));
  if (item.declaredVision === true) out.push(badge("Vision"));
  if (item.declaredReasoning === true) out.push(badge("Reasoning"));
  if (item.probedTools === true) out.push(badge("实测✓", "ok"));
  if (item.probedStatus === "unsupported" || item.probedTools === false) {
    out.push(badge("实测×", "warn"));
  }
  if (item.healthState === "cooling") {
    const sec = item.cooldownSeconds ?? "?";
    out.push(badge(`冷却 ${sec}s`, "warn"));
  } else if (item.healthState === "uncertain") {
    out.push(badge("健康未知", "muted"));
  }
  if (item.agentEligible === false) {
    out.push(badge(item.disabledReason || "不可用", "muted"));
  }
  return out;
}

function currentLabel(
  value: ModelPickerValue,
  items: ModelPickerItem[],
): { text: string; model?: string; providerName?: string; auto: boolean } {
  if (value.mode === "auto") {
    return { text: "自动路由", auto: true };
  }
  const hit = items.find(
    (item) => item.providerId === value.providerId && item.model === value.model,
  );
  return {
    text: value.model,
    model: value.model,
    providerName: hit?.providerName,
    auto: false,
  };
}

/**
 * 按 Provider 分组的模型选择器。
 * 默认「自动路由」；选具体模型仅影响本轮（由父组件写会话本地）。
 * 模型名前展示所属公司 logo（按模型 ID / Provider 名自适应识别）。
 */
export function ModelPicker({
  items,
  value,
  onChange,
  onSetDefault,
  disabled,
  className,
  showSetDefault,
  compact = false,
}: {
  items: ModelPickerItem[];
  value: ModelPickerValue;
  onChange: (next: ModelPickerValue) => void;
  /** 将当前 pinned 写入全局默认配置 */
  onSetDefault?: (providerId: number, model: string) => void;
  disabled?: boolean;
  className?: string;
  showSetDefault?: boolean;
  /** 紧凑模式用于输入框工具栏，避免长模型名挤占发送区。 */
  compact?: boolean;
}) {
  const groups = new Map<string, ModelPickerItem[]>();
  for (const item of items) {
    const key = `${item.providerId}::${item.providerName}`;
    const list = groups.get(key) || [];
    list.push(item);
    groups.set(key, list);
  }

  const current = currentLabel(value, items);
  const logoSize = compact ? 13 : 14;

  return (
    <div
      className={cn(
        "flex min-w-0 items-center gap-1.5",
        compact ? "flex-nowrap" : "flex-wrap",
        className,
      )}
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild disabled={disabled}>
          <button
            type="button"
            aria-label="本轮模型"
            disabled={disabled}
            className={cn(
              "inline-flex h-8 min-w-0 max-w-full items-center gap-1.5 rounded-md border border-border/60 bg-background/80 text-left text-xs outline-none transition-colors",
              "hover:bg-muted/30 focus-visible:ring-[3px] focus-visible:ring-ring/40",
              "disabled:cursor-not-allowed disabled:opacity-50",
              compact
                ? "w-[6.5rem] flex-none px-1.5 sm:w-[11rem] sm:px-2"
                : "w-full flex-1 px-2 sm:w-[min(18rem,72vw)] sm:flex-none",
            )}
          >
            <ModelBrandLogo
              auto={current.auto}
              model={current.model}
              providerName={current.providerName}
              size={logoSize}
              className="shrink-0 opacity-95"
            />
            <span className="min-w-0 flex-1 truncate">{current.text}</span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className={cn(
            "max-h-[min(24rem,70vh)] w-[min(22rem,calc(100vw-1.5rem))] overflow-y-auto p-1",
            compact && "w-[min(20rem,calc(100vw-1.5rem))]",
          )}
        >
          <DropdownMenuItem
            className="gap-2 py-2"
            onSelect={() => onChange({ mode: "auto" })}
          >
            <ModelBrandLogo auto size={15} className="shrink-0" />
            <span className="min-w-0 flex-1 truncate font-medium">自动路由</span>
            {value.mode === "auto" ? <Check className="h-3.5 w-3.5 shrink-0 text-primary" /> : null}
          </DropdownMenuItem>

          {[...groups.entries()].map(([key, list], groupIndex) => {
            const name = list[0]?.providerName || key;
            return (
              <div key={key}>
                <DropdownMenuSeparator className={groupIndex === 0 ? "my-1" : "my-1.5"} />
                <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/80">
                  {name}
                </div>
                {list.map((item) => {
                  const badges = itemBadges(item);
                  const selected =
                    value.mode === "pinned" &&
                    value.providerId === item.providerId &&
                    value.model === item.model;
                  const disabledOpt = item.agentEligible === false;
                  return (
                    <DropdownMenuItem
                      key={`${item.providerId}-${item.model}`}
                      disabled={disabledOpt}
                      className="items-start gap-2 py-2"
                      onSelect={() =>
                        onChange({
                          mode: "pinned",
                          providerId: item.providerId,
                          model: item.model,
                        })
                      }
                    >
                      <ModelBrandLogo
                        model={item.model}
                        providerName={item.providerName}
                        size={15}
                        className="mt-0.5 shrink-0"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm leading-5">{item.model}</div>
                        {badges.length > 0 ? (
                          <div className="mt-0.5 flex flex-wrap gap-1">
                            {badges.map((b) => (
                              <span
                                key={b.label}
                                className={cn(
                                  "rounded border px-1 py-px text-[10px] leading-3",
                                  b.className,
                                )}
                              >
                                {b.label}
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                      {selected ? <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" /> : null}
                    </DropdownMenuItem>
                  );
                })}
              </div>
            );
          })}
          {showSetDefault && value.mode === "pinned" && onSetDefault ? (
            <>
              <DropdownMenuSeparator className="my-1.5" />
              <DropdownMenuItem
                className="gap-2 py-2"
                onSelect={() => onSetDefault(value.providerId, value.model)}
              >
                <Check className="h-3.5 w-3.5 text-primary" />
                将当前模型设为全局默认
              </DropdownMenuItem>
            </>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

export { matrixToPickerItems } from "./modelPickerItems";
export type { ModelMatrixRow } from "./modelPickerItems";

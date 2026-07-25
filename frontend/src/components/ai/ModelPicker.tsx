import { cn } from "@/lib/utils";

export type ModelPickerItem = {
  providerId: number;
  providerName: string;
  model: string;
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

/**
 * 按 Provider 分组的模型选择器。
 * 默认「自动路由」；选具体模型仅影响本轮（由父组件写会话本地）。
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

  const selectValue =
    value.mode === "auto"
      ? "auto"
      : `${value.providerId}::${value.model}`;

  return (
    <div
      className={cn(
        "flex min-w-0 items-center gap-1.5",
        // 紧凑工具栏禁止换行，避免「设为默认」把选择框挤到上一行
        compact ? "flex-nowrap" : "flex-wrap",
        className,
      )}
    >
      <select
        aria-label="本轮模型"
        disabled={disabled}
        value={selectValue}
        onChange={(event) => {
          const raw = event.target.value;
          if (raw === "auto") {
            onChange({ mode: "auto" });
            return;
          }
          const [pid, ...rest] = raw.split("::");
          const model = rest.join("::");
          onChange({ mode: "pinned", providerId: Number(pid), model });
        }}
        className={cn(
          "h-8 min-w-0 max-w-full rounded-md border border-border/60 bg-background/80 text-xs",
          compact
            ? // 再收一档：给「设为默认」和发送按钮留位，避免窄屏换行上顶
              "w-[min(8.75rem,34vw)] flex-none px-1.5 sm:w-[10rem] sm:px-2"
            : "w-full flex-1 px-2 sm:w-[min(18rem,72vw)] sm:flex-none",
          "disabled:opacity-50",
        )}
      >
        <option value="auto">自动路由</option>
        {[...groups.entries()].map(([key, list]) => {
          const name = list[0]?.providerName || key;
          return (
            <optgroup key={key} label={name}>
              {list.map((item) => {
                const badges = itemBadges(item)
                  .map((b) => b.label)
                  .join(" · ");
                const disabledOpt = item.agentEligible === false;
                return (
                  <option
                    key={`${item.providerId}-${item.model}`}
                    value={`${item.providerId}::${item.model}`}
                    disabled={disabledOpt}
                  >
                    {badges ? `${item.model} · ${badges}` : item.model}
                  </option>
                );
              })}
            </optgroup>
          );
        })}
      </select>
      {showSetDefault && value.mode === "pinned" && onSetDefault ? (
        <button
          type="button"
          disabled={disabled}
          className={cn(
            "h-8 shrink-0 rounded-md border border-border/60 text-muted-foreground hover:bg-muted/40 disabled:opacity-50",
            compact ? "px-1.5 text-[10px] leading-none" : "px-2 text-[11px]",
          )}
          onClick={() => onSetDefault(value.providerId, value.model)}
          title="将当前选择写入全局默认配置"
        >
          设为默认
        </button>
      ) : null}
    </div>
  );
}

/** 从 capabilities.model_matrix 构造选项 */
export function matrixToPickerItems(
  matrix: Array<{
    provider_id: number;
    provider_name: string;
    model: string;
    declared_supports_tools?: boolean | null;
    declared_supports_images?: boolean | null;
    declared_reasoning_efforts?: unknown;
    probed_supports_tools?: boolean | null;
    probed_status?: string | null;
    health?: {
      state?: string;
      cooldown_remaining_seconds?: number;
      last_error_message?: string | null;
    };
  }>,
  opts?: { requireTools?: boolean },
): ModelPickerItem[] {
  const requireTools = opts?.requireTools !== false;
  return matrix.map((row) => {
    const noTools = row.declared_supports_tools === false;
    const probedBad = row.probed_status === "unsupported" || row.probed_supports_tools === false;
    const agentEligible = requireTools ? !noTools && !probedBad : true;
    return {
      providerId: row.provider_id,
      providerName: row.provider_name,
      model: row.model,
      declaredTools: row.declared_supports_tools,
      declaredVision: row.declared_supports_images,
      declaredReasoning: Array.isArray(row.declared_reasoning_efforts)
        ? row.declared_reasoning_efforts.length > 0
        : Boolean(row.declared_reasoning_efforts),
      probedTools: row.probed_supports_tools,
      probedStatus: row.probed_status,
      healthState: row.health?.state,
      cooldownSeconds: row.health?.cooldown_remaining_seconds,
      lastError: row.health?.last_error_message,
      agentEligible,
      disabledReason: noTools
        ? "不支持 Tools"
        : probedBad
          ? "实测不支持 Tools"
          : null,
    };
  });
}

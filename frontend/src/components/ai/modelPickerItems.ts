/** 本轮模型选择器的纯函数构造逻辑（可被 node test 直接导入）。 */

export type ModelMatrixRow = {
  provider_id: number;
  provider_name: string;
  model: string;
  enabled?: boolean | null;
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
};

export type ModelPickerItemData = {
  providerId: number;
  providerName: string;
  model: string;
  declaredTools?: boolean | null;
  declaredVision?: boolean | null;
  declaredReasoning?: boolean | null;
  probedTools?: boolean | null;
  probedStatus?: string | null;
  healthState?: string | null;
  cooldownSeconds?: number | null;
  lastError?: string | null;
  agentEligible?: boolean;
  disabledReason?: string | null;
};

/** 从 capabilities.model_matrix 构造选项。
 * 默认只展示 enabled 模型；未启用模型不进入本轮选择器。
 */
export function matrixToPickerItems(
  matrix: ModelMatrixRow[],
  opts?: { requireTools?: boolean; includeDisabled?: boolean },
): ModelPickerItemData[] {
  const requireTools = opts?.requireTools !== false;
  const includeDisabled = opts?.includeDisabled === true;
  return matrix
    .filter((row) => includeDisabled || row.enabled !== false)
    .map((row) => {
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

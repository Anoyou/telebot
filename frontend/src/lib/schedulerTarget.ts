const INTEGER_RE = /^[+-]?\d+$/;
const USERNAME_RE = /^@[A-Za-z][A-Za-z0-9_]{4,31}$/;

export type SchedulerTarget = number | string;

export function normalizeSchedulerTarget(
  value: unknown,
  required = true,
): SchedulerTarget | undefined {
  if (typeof value === "boolean") {
    throw new Error("目标聊天必须是非零数字 ID 或 @username");
  }

  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value === 0) {
      if (!required && value === 0) return undefined;
      throw new Error("目标聊天必须是非零安全整数 ID 或 @username");
    }
    return value;
  }

  if (value == null) {
    if (!required) return undefined;
    throw new Error("目标聊天必填，请填写非零数字 ID 或 @username");
  }

  const raw = String(value).trim();
  if (!raw || raw === "0") {
    if (!required) return undefined;
    throw new Error("目标聊天必填，请填写非零数字 ID 或 @username");
  }

  if (INTEGER_RE.test(raw)) {
    const targetId = Number(raw);
    if (!Number.isSafeInteger(targetId) || targetId === 0) {
      throw new Error("目标聊天必须是非零安全整数 ID 或 @username");
    }
    return targetId;
  }

  if (USERNAME_RE.test(raw)) return raw;

  throw new Error(
    "目标聊天格式无效，请填写非零数字 ID 或标准 @username（不支持裸用户名或 t.me 链接）",
  );
}

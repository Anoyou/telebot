export type AssistantPetIntent = "idle" | "awake" | "working" | "complete";

export type AssistantPetCell = {
  row: number;
  column: number;
};

export type AssistantPetBounds = {
  left: number;
  top: number;
  width: number;
  height: number;
};

const ANIMATIONS: Record<AssistantPetIntent, { row: number; durations: readonly number[] }> = {
  idle: { row: 0, durations: [280, 110, 110, 140, 140, 320] },
  awake: { row: 3, durations: [140, 140, 140, 280] },
  working: { row: 7, durations: [120, 120, 120, 120, 120, 220] },
  complete: { row: 8, durations: [180, 160, 160, 180, 160, 260] },
};

function frameForElapsed(durations: readonly number[], elapsed: number): number {
  const loopDuration = durations.reduce((sum, duration) => sum + duration, 0);
  let cursor = Math.max(0, elapsed) % loopDuration;
  for (let index = 0; index < durations.length; index += 1) {
    if (cursor < durations[index]) return index;
    cursor -= durations[index];
  }
  return 0;
}

export function assistantPetLookDirection(
  clientX: number,
  clientY: number,
  bounds: AssistantPetBounds,
  deadzone = 18,
): number | null {
  const dx = clientX - (bounds.left + bounds.width / 2);
  const dy = clientY - (bounds.top + bounds.height / 2);
  if (Math.hypot(dx, dy) < deadzone) return null;

  const clockwiseDegrees = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
  return Math.round(clockwiseDegrees / 22.5) % 16;
}

export function assistantPetCell(
  intent: AssistantPetIntent,
  elapsed: number,
  lookDirection: number | null,
  reduceMotion = false,
): AssistantPetCell {
  if ((intent === "idle" || intent === "awake") && lookDirection != null) {
    const normalized = ((lookDirection % 16) + 16) % 16;
    return {
      row: normalized < 8 ? 9 : 10,
      column: normalized % 8,
    };
  }

  const animation = ANIMATIONS[intent];
  return {
    row: animation.row,
    column: reduceMotion ? 0 : frameForElapsed(animation.durations, elapsed),
  };
}

export type AssistantPetIntent =
  | "idle"
  | "running-right"
  | "running-left"
  | "waving"
  | "jumping"
  | "failed"
  | "waiting"
  | "running"
  | "review";

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

export type AssistantPetDrawLayer = AssistantPetCell & {
  sourceHeightRatio: number;
  destinationX: number;
  destinationHeight: number;
  clearTopBeforeDraw?: boolean;
};

export type AssistantPetDrawPlan = {
  viewportHeight: number;
  layers: AssistantPetDrawLayer[];
};

export const ASSISTANT_PET_CANVAS_WIDTH = 192;
export const ASSISTANT_PET_CANVAS_HEIGHT = 208;
export const ASSISTANT_PET_COMPACT_CANVAS_HEIGHT = 150;

const ANIMATIONS: Record<AssistantPetIntent, { row: number; durations: readonly number[] }> = {
  idle: { row: 0, durations: [280, 110, 110, 140, 140, 320] },
  "running-right": { row: 1, durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  "running-left": { row: 2, durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  waving: { row: 3, durations: [140, 140, 140, 280] },
  jumping: { row: 4, durations: [140, 140, 140, 140, 280] },
  failed: { row: 5, durations: [140, 140, 140, 140, 140, 140, 140, 240] },
  waiting: { row: 6, durations: [150, 150, 150, 150, 150, 260] },
  running: { row: 7, durations: [120, 120, 120, 120, 120, 220] },
  review: { row: 8, durations: [150, 150, 150, 150, 150, 280] },
};

const WAVE_DYNAMIC_HEIGHT = 150;
const WAVE_FRAME_OFFSETS = [0, -3, 10, -1] as const;

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
  if ((intent === "idle" || intent === "waving") && lookDirection != null) {
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

export function assistantPetDrawPlan(
  cell: AssistantPetCell,
  compact = false,
): AssistantPetDrawPlan {
  if (compact) {
    return {
      viewportHeight: ASSISTANT_PET_COMPACT_CANVAS_HEIGHT,
      layers: [{
        ...cell,
        sourceHeightRatio: ASSISTANT_PET_COMPACT_CANVAS_HEIGHT / ASSISTANT_PET_CANVAS_HEIGHT,
        destinationX: 0,
        destinationHeight: ASSISTANT_PET_COMPACT_CANVAS_HEIGHT,
      }],
    };
  }

  if (cell.row === ANIMATIONS.waving.row && cell.column > 0) {
    return {
      viewportHeight: ASSISTANT_PET_CANVAS_HEIGHT,
      layers: [
        {
          row: ANIMATIONS.waving.row,
          column: 0,
          sourceHeightRatio: 1,
          destinationX: 0,
          destinationHeight: ASSISTANT_PET_CANVAS_HEIGHT,
        },
        {
          ...cell,
          sourceHeightRatio: WAVE_DYNAMIC_HEIGHT / ASSISTANT_PET_CANVAS_HEIGHT,
          destinationX: WAVE_FRAME_OFFSETS[cell.column] ?? 0,
          destinationHeight: WAVE_DYNAMIC_HEIGHT,
          clearTopBeforeDraw: true,
        },
      ],
    };
  }

  return {
    viewportHeight: ASSISTANT_PET_CANVAS_HEIGHT,
    layers: [{
      ...cell,
      sourceHeightRatio: 1,
      destinationX: 0,
      destinationHeight: ASSISTANT_PET_CANVAS_HEIGHT,
    }],
  };
}

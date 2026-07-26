export type AssistantPetIntent =
  | "idle"
  | "running-right"
  | "running-left"
  | "waving"
  | "jumping"
  | "failed"
  | "waiting"
  | "review";

export type AssistantPetCell = {
  row: number;
  column: number;
};

export type AssistantPetFrame = {
  cell: AssistantPetCell;
  nextCell: AssistantPetCell | null;
  blend: number;
};

export type AssistantPetBounds = {
  left: number;
  top: number;
  width: number;
  height: number;
};

export type AssistantPetState = {
  failed: boolean;
  celebrating: boolean;
  dragDirection: "left" | "right" | null;
  streaming: boolean;
  active: boolean;
};

export type AssistantPetDrawLayer = AssistantPetCell & {
  sourceX: number;
  sourceY: number;
  sourceWidth: number;
  sourceHeight: number;
  destinationX: number;
  destinationY: number;
  destinationWidth: number;
  destinationHeight: number;
  clearBeforeDraw?: boolean;
  flipX?: boolean;
};

export type AssistantPetDrawPlan = {
  viewportHeight: number;
  layers: AssistantPetDrawLayer[];
};

export type AssistantPetVisualMetrics = {
  alphaMass: number;
  centerX: number;
  baselineY: number;
};

export type AssistantPetDrawRegistration = {
  scale: number;
  destinationX: number;
  destinationY: number;
};

export const ASSISTANT_PET_CANVAS_WIDTH = 192;
export const ASSISTANT_PET_CANVAS_HEIGHT = 208;
export const ASSISTANT_PET_COMPACT_CANVAS_HEIGHT = 150;

const ANIMATIONS: Record<AssistantPetIntent, { row: number; durations: readonly number[] }> = {
  idle: { row: 0, durations: [280, 110, 110, 140, 140, 320] },
  "running-right": { row: 1, durations: [120, 120, 120, 120, 120, 120, 120, 120] },
  "running-left": { row: 2, durations: [120, 120, 120, 120, 120, 120, 120, 120] },
  waving: { row: 3, durations: [140, 140, 140, 280] },
  jumping: { row: 4, durations: [100, 90, 105, 150, 105, 90, 220] },
  failed: { row: 5, durations: [140, 140, 140, 140, 140, 140, 140, 240] },
  waiting: { row: 6, durations: [150, 150, 150, 150, 150, 260] },
  review: { row: 8, durations: [150, 150, 150, 150, 150, 280] },
};

const JUMP_FRAME_SCALE = 1.15;
const JUMP_FRAME_X = (ASSISTANT_PET_CANVAS_WIDTH - ASSISTANT_PET_CANVAS_WIDTH * JUMP_FRAME_SCALE) / 2;
const JUMP_FRAME_SOURCES = [
  { row: ANIMATIONS.idle.row, column: 3, scale: 1, destinationX: 0, destinationY: 0 },
  { row: ANIMATIONS.jumping.row, column: 0, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -30 },
  { row: ANIMATIONS.jumping.row, column: 1, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -25 },
  { row: ANIMATIONS.jumping.row, column: 2, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -6 },
  { row: ANIMATIONS.jumping.row, column: 3, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -27 },
  { row: ANIMATIONS.jumping.row, column: 4, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -30 },
  { row: ANIMATIONS.idle.row, column: 3, scale: 1, destinationX: 0, destinationY: 0 },
] as const;
const WAVE_FRAME_OFFSETS = [0, -3, 10, -1] as const;
const WAVE_FIXED_REGIONS = [
  { sourceX: 35, sourceY: 0, sourceWidth: 125, sourceHeight: 100 },
  { sourceX: 64, sourceY: 82, sourceWidth: 66, sourceHeight: 68 },
  { sourceX: 0, sourceY: 150, sourceWidth: 192, sourceHeight: 58 },
] as const;

function frameForElapsed(durations: readonly number[], elapsed: number) {
  const loopDuration = durations.reduce((sum, duration) => sum + duration, 0);
  let cursor = Math.max(0, elapsed) % loopDuration;
  for (let index = 0; index < durations.length; index += 1) {
    if (cursor < durations[index]) {
      return {
        index,
        progress: cursor / durations[index],
      };
    }
    cursor -= durations[index];
  }
  return { index: 0, progress: 0 };
}

function smoothStep(value: number) {
  const clamped = Math.min(Math.max(value, 0), 1);
  return clamped * clamped * (3 - 2 * clamped);
}

export function assistantPetIntentForState(state: AssistantPetState): AssistantPetIntent {
  if (state.failed) return "failed";
  if (state.celebrating) return "jumping";
  if (state.dragDirection === "left") return "running-left";
  if (state.dragDirection === "right") return "running-right";
  if (state.streaming) return "review";
  return state.active ? "waving" : "idle";
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
  return assistantPetFrameAt(intent, elapsed, lookDirection, reduceMotion).cell;
}

export function assistantPetFrameAt(
  intent: AssistantPetIntent,
  elapsed: number,
  lookDirection: number | null,
  reduceMotion = false,
): AssistantPetFrame {
  if ((intent === "idle" || intent === "waving") && lookDirection != null) {
    const normalized = ((lookDirection % 16) + 16) % 16;
    return {
      cell: {
        row: normalized < 8 ? 9 : 10,
        column: normalized % 8,
      },
      nextCell: null,
      blend: 0,
    };
  }

  const animation = ANIMATIONS[intent];
  const frame = reduceMotion
    ? { index: 0, progress: 0 }
    : frameForElapsed(animation.durations, elapsed);
  const cell = {
    row: animation.row,
    column: frame.index,
  };
  if (reduceMotion || (intent !== "running-right" && intent !== "running-left" && intent !== "jumping")) {
    return { cell, nextCell: null, blend: 0 };
  }

  const transitionStart = intent === "jumping" ? 0.48 : 0.4;
  return {
    cell,
    nextCell: {
      row: animation.row,
      column: (frame.index + 1) % animation.durations.length,
    },
    blend: smoothStep((frame.progress - transitionStart) / (1 - transitionStart)),
  };
}

export function assistantPetLookRegistration(
  metrics: AssistantPetVisualMetrics,
  targetAlphaMass: number,
): AssistantPetDrawRegistration {
  if (metrics.alphaMass <= 0 || targetAlphaMass <= 0) {
    return { scale: 1, destinationX: 0, destinationY: 0 };
  }
  const scale = Math.min(Math.max(Math.sqrt(targetAlphaMass / metrics.alphaMass), 0.92), 1.08);
  return {
    scale,
    destinationX: ASSISTANT_PET_CANVAS_WIDTH / 2 - metrics.centerX * scale,
    destinationY: ASSISTANT_PET_CANVAS_HEIGHT - metrics.baselineY * scale,
  };
}

export function assistantPetDrawPlan(
  cell: AssistantPetCell,
  compact = false,
  registration?: AssistantPetDrawRegistration,
): AssistantPetDrawPlan {
  const viewportHeight = compact
    ? ASSISTANT_PET_COMPACT_CANVAS_HEIGHT
    : ASSISTANT_PET_CANVAS_HEIGHT;

  if (!compact && registration && (cell.row === 9 || cell.row === 10)) {
    return {
      viewportHeight,
      layers: [{
        ...cell,
        sourceX: 0,
        sourceY: 0,
        sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
        sourceHeight: ASSISTANT_PET_CANVAS_HEIGHT,
        destinationX: registration.destinationX,
        destinationY: registration.destinationY,
        destinationWidth: ASSISTANT_PET_CANVAS_WIDTH * registration.scale,
        destinationHeight: ASSISTANT_PET_CANVAS_HEIGHT * registration.scale,
      }],
    };
  }

  if (cell.row === ANIMATIONS["running-left"].row) {
    return {
      viewportHeight,
      layers: [{
        row: ANIMATIONS["running-right"].row,
        column: cell.column,
        sourceX: 0,
        sourceY: 0,
        sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
        sourceHeight: viewportHeight,
        destinationX: 0,
        destinationY: 0,
        destinationWidth: ASSISTANT_PET_CANVAS_WIDTH,
        destinationHeight: viewportHeight,
        flipX: true,
      }],
    };
  }

  if (cell.row === ANIMATIONS.jumping.row) {
    const source = JUMP_FRAME_SOURCES[cell.column] ?? JUMP_FRAME_SOURCES[0];
    return {
      viewportHeight,
      layers: [{
        row: source.row,
        column: source.column,
        sourceX: 0,
        sourceY: 0,
        sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
        sourceHeight: viewportHeight,
        destinationX: source.destinationX,
        destinationY: source.destinationY,
        destinationWidth: ASSISTANT_PET_CANVAS_WIDTH * source.scale,
        destinationHeight: viewportHeight * source.scale,
      }],
    };
  }

  if (cell.row === ANIMATIONS.waving.row && cell.column > 0) {
    const fixedRegions = compact ? WAVE_FIXED_REGIONS.slice(0, 2) : WAVE_FIXED_REGIONS;
    return {
      viewportHeight,
      layers: [
        {
          ...cell,
          sourceX: 0,
          sourceY: 0,
          sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
          sourceHeight: viewportHeight,
          destinationX: WAVE_FRAME_OFFSETS[cell.column] ?? 0,
          destinationY: 0,
          destinationWidth: ASSISTANT_PET_CANVAS_WIDTH,
          destinationHeight: viewportHeight,
        },
        ...fixedRegions.map((region) => ({
          row: ANIMATIONS.waving.row,
          column: 0,
          ...region,
          destinationX: region.sourceX,
          destinationY: region.sourceY,
          destinationWidth: region.sourceWidth,
          destinationHeight: region.sourceHeight,
          clearBeforeDraw: true,
        })),
      ],
    };
  }

  return {
    viewportHeight,
    layers: [{
      ...cell,
      sourceX: 0,
      sourceY: 0,
      sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
      sourceHeight: viewportHeight,
      destinationX: 0,
      destinationY: 0,
      destinationWidth: ASSISTANT_PET_CANVAS_WIDTH,
      destinationHeight: viewportHeight,
    }],
  };
}

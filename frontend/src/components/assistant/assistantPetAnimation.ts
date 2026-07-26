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
  clipPath?: readonly AssistantPetDrawPoint[];
  flipX?: boolean;
};

export type AssistantPetDrawPoint = {
  x: number;
  y: number;
};

export type AssistantPetDrawPlan = {
  viewportHeight: number;
  layers: AssistantPetDrawLayer[];
};

export const ASSISTANT_PET_CANVAS_WIDTH = 192;
export const ASSISTANT_PET_CANVAS_HEIGHT = 208;
export const ASSISTANT_PET_COMPACT_CANVAS_HEIGHT = 150;

const ANIMATIONS: Record<AssistantPetIntent, {
  row: number;
  durations: readonly number[];
  frames?: readonly number[];
}> = {
  idle: {
    row: 0,
    durations: [520, 140, 110, 110, 110, 140],
    frames: [5, 4, 3, 2, 3, 4],
  },
  "running-right": { row: 1, durations: [84, 84, 84, 84, 84, 84, 84, 84] },
  "running-left": { row: 2, durations: [84, 84, 84, 84, 84, 84, 84, 84] },
  waving: { row: 3, durations: [140, 140, 140, 280], frames: [0, 1, 2, 0] },
  jumping: { row: 4, durations: [100, 90, 120, 90, 180] },
  failed: { row: 5, durations: [140, 140, 140, 140, 140, 140, 140, 240] },
  waiting: { row: 6, durations: [150, 150, 150, 150, 150, 260] },
  review: { row: 8, durations: [150, 150, 150, 150, 150, 280] },
};

const JUMP_FRAME_SCALE = 1.25;
const JUMP_FRAME_X = (ASSISTANT_PET_CANVAS_WIDTH - ASSISTANT_PET_CANVAS_WIDTH * JUMP_FRAME_SCALE) / 2;
const JUMP_FRAME_SOURCES = [
  { row: ANIMATIONS.jumping.row, column: 0, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -56 },
  { row: ANIMATIONS.jumping.row, column: 1, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -21 },
  { row: ANIMATIONS.jumping.row, column: 2, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -6 },
  { row: ANIMATIONS.jumping.row, column: 3, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -25 },
  { row: ANIMATIONS.jumping.row, column: 4, scale: JUMP_FRAME_SCALE, destinationX: JUMP_FRAME_X, destinationY: -58 },
] as const;
const LOOK_TARGET_HEIGHT = 190;
const LOOK_TARGET_LOWER_CENTER_X = 95;
const LOOK_BASELINE_Y = 202;
const LOOK_FRAME_METRICS = [
  { height: 196, lowerCenterX: 95.099 },
  { height: 196, lowerCenterX: 94.052 },
  { height: 196, lowerCenterX: 94.23 },
  { height: 195, lowerCenterX: 94.749 },
  { height: 194, lowerCenterX: 94.02 },
  { height: 190, lowerCenterX: 94.001 },
  { height: 186, lowerCenterX: 93.618 },
  { height: 183, lowerCenterX: 94.552 },
  { height: 190, lowerCenterX: 95.057 },
  { height: 191, lowerCenterX: 94.538 },
  { height: 191, lowerCenterX: 94.791 },
  { height: 189, lowerCenterX: 94.842 },
  { height: 186, lowerCenterX: 94.502 },
  { height: 185, lowerCenterX: 95.287 },
  { height: 185, lowerCenterX: 95.002 },
  { height: 186, lowerCenterX: 94.727 },
] as const;
const WAVE_ARM_REGISTRATION = [
  null,
  { x: -2, y: 0 },
  { x: 8, y: -4 },
] as const;
const WAVE_LOWER_ARM_FILL = {
  row: ANIMATIONS.waving.row,
  column: 1,
  sourceX: 57,
  sourceY: 108,
  sourceWidth: 30,
  sourceHeight: 42,
  destinationX: 55,
  destinationY: 108,
} as const;
const WAVE_ARM_CLIP_PATHS = [
  null,
  [
    { x: 37, y: 65 },
    { x: 56, y: 64 },
    { x: 63, y: 74 },
    { x: 63, y: 80 },
    { x: 72, y: 82 },
    { x: 74, y: 100 },
    { x: 71, y: 108 },
    { x: 60, y: 108 },
    { x: 56, y: 96 },
    { x: 53, y: 86 },
    { x: 41, y: 83 },
    { x: 37, y: 75 },
  ],
  [
    { x: 49, y: 66 },
    { x: 68, y: 65 },
    { x: 74, y: 74 },
    { x: 72, y: 83 },
    { x: 73, y: 98 },
    { x: 69, y: 107 },
    { x: 58, y: 107 },
    { x: 54, y: 95 },
    { x: 55, y: 85 },
    { x: 49, y: 80 },
  ],
] as const;

function translatePath(
  path: readonly AssistantPetDrawPoint[],
  x: number,
  y: number,
): AssistantPetDrawPoint[] {
  return path.map((point) => ({ x: point.x + x, y: point.y + y }));
}

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
  const phase = assistantPetLookPhase(clientX, clientY, bounds, deadzone);
  return phase == null ? null : Math.round(phase) % 16;
}

export function assistantPetLookPhase(
  clientX: number,
  clientY: number,
  bounds: AssistantPetBounds,
  deadzone = 18,
): number | null {
  const dx = clientX - (bounds.left + bounds.width / 2);
  const dy = clientY - (bounds.top + bounds.height / 2);
  if (Math.hypot(dx, dy) < deadzone) return null;

  const clockwiseDegrees = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
  return clockwiseDegrees / 22.5;
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
    const index = Math.floor(normalized);
    const blend = normalized - index;
    const nextIndex = (index + 1) % 16;
    return {
      cell: {
        row: index < 8 ? 9 : 10,
        column: index % 8,
      },
      nextCell: blend > 0.001
        ? { row: nextIndex < 8 ? 9 : 10, column: nextIndex % 8 }
        : null,
      blend,
    };
  }

  const animation = ANIMATIONS[intent];
  if (intent === "jumping") {
    const duration = animation.durations.reduce((sum, value) => sum + value, 0);
    if (reduceMotion || elapsed >= duration) {
      return {
        cell: { row: ANIMATIONS.idle.row, column: ANIMATIONS.idle.frames?.[0] ?? 0 },
        nextCell: null,
        blend: 0,
      };
    }
  }
  const frame = reduceMotion
    ? { index: 0, progress: 0 }
    : frameForElapsed(animation.durations, elapsed);
  return {
    cell: {
      row: animation.row,
      column: animation.frames?.[frame.index] ?? frame.index,
    },
    nextCell: null,
    blend: 0,
  };
}

export function assistantPetDrawPlan(
  cell: AssistantPetCell,
  compact = false,
): AssistantPetDrawPlan {
  const viewportHeight = compact
    ? ASSISTANT_PET_COMPACT_CANVAS_HEIGHT
    : ASSISTANT_PET_CANVAS_HEIGHT;

  if (!compact && (cell.row === 9 || cell.row === 10)) {
    const direction = (cell.row - 9) * 8 + cell.column;
    const metrics = LOOK_FRAME_METRICS[direction];
    if (metrics) {
      const scale = LOOK_TARGET_HEIGHT / metrics.height;
      return {
        viewportHeight,
        layers: [{
          ...cell,
          sourceX: 0,
          sourceY: 0,
          sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
          sourceHeight: ASSISTANT_PET_CANVAS_HEIGHT,
          destinationX: LOOK_TARGET_LOWER_CENTER_X - metrics.lowerCenterX * scale,
          destinationY: LOOK_BASELINE_Y - LOOK_BASELINE_Y * scale,
          destinationWidth: ASSISTANT_PET_CANVAS_WIDTH * scale,
          destinationHeight: ASSISTANT_PET_CANVAS_HEIGHT * scale,
        }],
      };
    }
  }

  if (cell.row === ANIMATIONS["running-right"].row || cell.row === ANIMATIONS["running-left"].row) {
    return {
      viewportHeight,
      layers: [{
        row: ANIMATIONS["running-left"].row,
        column: cell.column,
        sourceX: 0,
        sourceY: 0,
        sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
        sourceHeight: viewportHeight,
        destinationX: 0,
        destinationY: 0,
        destinationWidth: ASSISTANT_PET_CANVAS_WIDTH,
        destinationHeight: viewportHeight,
        flipX: cell.row === ANIMATIONS["running-right"].row,
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
    const registration = WAVE_ARM_REGISTRATION[cell.column];
    const clipPath = WAVE_ARM_CLIP_PATHS[cell.column];
    if (!registration || !clipPath) {
      return assistantPetDrawPlan({ row: ANIMATIONS.waving.row, column: 0 }, compact);
    }
    return {
      viewportHeight,
      layers: [
        {
          row: ANIMATIONS.waving.row,
          column: 0,
          sourceX: 0,
          sourceY: 0,
          sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
          sourceHeight: viewportHeight,
          destinationX: 0,
          destinationY: 0,
          destinationWidth: ASSISTANT_PET_CANVAS_WIDTH,
          destinationHeight: viewportHeight,
        },
        {
          ...WAVE_LOWER_ARM_FILL,
          destinationWidth: WAVE_LOWER_ARM_FILL.sourceWidth,
          destinationHeight: WAVE_LOWER_ARM_FILL.sourceHeight,
          clearBeforeDraw: true,
        },
        {
          ...cell,
          sourceX: 0,
          sourceY: 0,
          sourceWidth: ASSISTANT_PET_CANVAS_WIDTH,
          sourceHeight: viewportHeight,
          destinationX: registration.x,
          destinationY: registration.y,
          destinationWidth: ASSISTANT_PET_CANVAS_WIDTH,
          destinationHeight: viewportHeight,
          clipPath: translatePath(clipPath, registration.x, registration.y),
        },
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

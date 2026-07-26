import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";

import {
  ASSISTANT_SURFACE_ID,
  useAssistantDock,
} from "@/components/assistant/AssistantDock";
import petSpritesheetUrl from "@/assets/agent-pet-spritesheet.webp";
import { cn } from "@/lib/utils";
import {
  assistantPetCell,
  assistantPetDrawPlan,
  assistantPetLookDirection,
  ASSISTANT_PET_CANVAS_HEIGHT,
  ASSISTANT_PET_CANVAS_WIDTH,
  ASSISTANT_PET_COMPACT_CANVAS_HEIGHT,
  type AssistantPetIntent,
} from "./assistantPetAnimation";

type DockSide = "left" | "right";

type PetPosition = {
  x: number;
  y: number;
  side: DockSide;
};

type DragState = {
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
  lastX: number;
  moved: boolean;
};

const PET_WIDTH = 102;
const PET_HEIGHT = 114;
const PET_PEEK = 51;
const PET_TOP_GUTTER = 92;
const PET_BOTTOM_GUTTER = 24;
const PET_SNAP_DELAY = 1_800;
const PET_STORAGE_KEY = "telepilot:assistant-pet:v1";
const IDLE_BUBBLE_MIN_DELAY = 24_000;
const IDLE_BUBBLE_DELAY_RANGE = 18_000;
const IDLE_BUBBLE_VISIBLE_MS = 3_800;

type PetNotice = {
  text: string;
  tone: "idle" | "complete" | "failed";
};

const IDLE_NOTICES = ["我在这儿", "随时可以叫我", "需要我帮忙吗？"] as const;

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function dockedX(side: DockSide) {
  return side === "left" ? PET_PEEK - PET_WIDTH : window.innerWidth - PET_PEEK;
}

function emergedX(side: DockSide) {
  return side === "left" ? 10 : window.innerWidth - PET_WIDTH - 10;
}

function initialPosition(): PetPosition {
  const fallbackY = clamp(window.innerHeight * 0.42, PET_TOP_GUTTER, window.innerHeight - PET_HEIGHT - PET_BOTTOM_GUTTER);
  try {
    const saved = JSON.parse(window.localStorage.getItem(PET_STORAGE_KEY) || "null") as {
      side?: DockSide;
      yRatio?: number;
    } | null;
    const side = saved?.side === "left" ? "left" : "right";
    const y = typeof saved?.yRatio === "number"
      ? clamp(saved.yRatio * window.innerHeight, PET_TOP_GUTTER, window.innerHeight - PET_HEIGHT - PET_BOTTOM_GUTTER)
      : fallbackY;
    return { x: dockedX(side), y, side };
  } catch {
    return { x: dockedX("right"), y: fallbackY, side: "right" };
  }
}

export function AssistantPetSprite({
  compact = false,
  streaming = false,
  active = false,
  celebrating = false,
  failed = false,
  dragDirection = null,
  lookDirection = null,
}: {
  compact?: boolean;
  streaming?: boolean;
  active?: boolean;
  celebrating?: boolean;
  failed?: boolean;
  dragDirection?: DockSide | null;
  lookDirection?: number | null;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const intent: AssistantPetIntent = failed
    ? "failed"
    : celebrating
      ? "review"
      : dragDirection === "left"
        ? "running-left"
        : dragDirection === "right"
          ? "running-right"
          : streaming
            ? "running"
            : active
              ? "waving"
              : "idle";
  const intentRef = useRef(intent);
  const lookDirectionRef = useRef(lookDirection);
  const animationStartedAtRef = useRef(0);

  useEffect(() => {
    intentRef.current = intent;
    animationStartedAtRef.current = performance.now();
  }, [intent]);

  useEffect(() => {
    lookDirectionRef.current = lookDirection;
  }, [lookDirection]);

  useEffect(() => {
    const atlas = new Image();
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;

    let animationFrame = 0;
    let loaded = false;
    let sourceCellWidth = 0;
    let sourceCellHeight = 0;
    let lastCellKey = "";

    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";

    const drawCell = (cell: ReturnType<typeof assistantPetCell>) => {
      const plan = assistantPetDrawPlan(cell, compact);
      context.clearRect(0, 0, ASSISTANT_PET_CANVAS_WIDTH, plan.viewportHeight);

      for (const layer of plan.layers) {
        if (layer.clearTopBeforeDraw) {
          context.clearRect(0, 0, ASSISTANT_PET_CANVAS_WIDTH, layer.destinationHeight);
        }
        context.drawImage(
          atlas,
          layer.column * sourceCellWidth,
          layer.row * sourceCellHeight,
          sourceCellWidth,
          sourceCellHeight * layer.sourceHeightRatio,
          layer.destinationX,
          0,
          ASSISTANT_PET_CANVAS_WIDTH,
          layer.destinationHeight,
        );
      }
    };

    const render = (now: number) => {
      if (loaded) {
        const cell = assistantPetCell(
          intentRef.current,
          now - animationStartedAtRef.current,
          lookDirectionRef.current,
          reduceMotion.matches,
        );
        const cellKey = `${cell.row}:${cell.column}`;
        if (cellKey !== lastCellKey) {
          lastCellKey = cellKey;
          drawCell(cell);
        }
      }
      animationFrame = window.requestAnimationFrame(render);
    };

    const handleLoad = () => {
      if (atlas.naturalWidth % 8 !== 0 || atlas.naturalHeight % 11 !== 0) {
        console.warn("Agent 桌宠图集不是可整除的 8x11 网格", {
          width: atlas.naturalWidth,
          height: atlas.naturalHeight,
        });
        return;
      }
      sourceCellWidth = atlas.naturalWidth / 8;
      sourceCellHeight = atlas.naturalHeight / 11;
      if (atlas.naturalWidth !== 1536 || atlas.naturalHeight !== 2288) {
        console.warn("Agent 桌宠图集使用非标准尺寸，将按 8x11 动态推导单元格", {
          width: atlas.naturalWidth,
          height: atlas.naturalHeight,
        });
      }
      loaded = true;
      lastCellKey = "";
      animationStartedAtRef.current = performance.now();
    };

    atlas.addEventListener("load", handleLoad);
    atlas.src = petSpritesheetUrl;
    animationFrame = window.requestAnimationFrame(render);
    return () => {
      loaded = false;
      window.cancelAnimationFrame(animationFrame);
      atlas.removeEventListener("load", handleLoad);
    };
  }, [compact]);

  return (
    <span
      className={cn("assistant-pet-sprite-frame", compact && "assistant-pet-sprite-frame-compact")}
      data-assistant-pet-intent={intent}
      data-assistant-pet-compact={compact ? "true" : undefined}
      aria-hidden="true"
    >
      <canvas
        ref={canvasRef}
        className="assistant-pet-sprite-canvas"
        width={ASSISTANT_PET_CANVAS_WIDTH}
        height={compact ? ASSISTANT_PET_COMPACT_CANVAS_HEIGHT : ASSISTANT_PET_CANVAS_HEIGHT}
      />
    </span>
  );
}

export function AssistantPet() {
  const { collapsed, setCollapsed, streaming, outcomeSignal } = useAssistantDock();
  const [desktopVisible, setDesktopVisible] = useState(() => (
    typeof window !== "undefined" && window.matchMedia("(min-width: 640px)").matches
  ));
  const [position, setPosition] = useState<PetPosition | null>(null);
  const [dragging, setDragging] = useState(false);
  const [docked, setDocked] = useState(true);
  const [notice, setNotice] = useState<PetNotice | null>(null);
  const [lookDirection, setLookDirection] = useState<number | null>(null);
  const [dragDirection, setDragDirection] = useState<DockSide | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const snapTimerRef = useRef<number | null>(null);
  const outcomeTimerRef = useRef<number | null>(null);
  const outcomeSignalRef = useRef(outcomeSignal?.id ?? 0);
  const suppressClickRef = useRef(false);

  const clearSnapTimer = useCallback(() => {
    if (snapTimerRef.current != null) {
      window.clearTimeout(snapTimerRef.current);
      snapTimerRef.current = null;
    }
  }, []);

  const snapToNearestEdge = useCallback((current?: PetPosition | null) => {
    setPosition((value) => {
      const source = current ?? value;
      if (!source) return source;
      const side: DockSide = source.x + PET_WIDTH / 2 < window.innerWidth / 2 ? "left" : "right";
      const next = {
        x: dockedX(side),
        y: clamp(source.y, PET_TOP_GUTTER, window.innerHeight - PET_HEIGHT - PET_BOTTOM_GUTTER),
        side,
      };
      try {
        window.localStorage.setItem(PET_STORAGE_KEY, JSON.stringify({
          side,
          yRatio: next.y / window.innerHeight,
        }));
      } catch {
        // 无持久化权限时仍保持本轮吸附行为。
      }
      return next;
    });
    setDocked(true);
  }, []);

  const scheduleSnap = useCallback((delay = PET_SNAP_DELAY) => {
    clearSnapTimer();
    snapTimerRef.current = window.setTimeout(() => snapToNearestEdge(), delay);
  }, [clearSnapTimer, snapToNearestEdge]);

  const clearOutcomeTimer = useCallback(() => {
    if (outcomeTimerRef.current != null) {
      window.clearTimeout(outcomeTimerRef.current);
      outcomeTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    setPosition(initialPosition());
    return () => {
      clearSnapTimer();
      clearOutcomeTimer();
    };
  }, [clearOutcomeTimer, clearSnapTimer]);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 640px)");
    const update = () => setDesktopVisible(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!desktopVisible) return;
    const handleResize = () => {
      setPosition((current) => {
        if (!current) return current;
        const y = clamp(current.y, PET_TOP_GUTTER, window.innerHeight - PET_HEIGHT - PET_BOTTOM_GUTTER);
        return {
          ...current,
          x: docked ? dockedX(current.side) : clamp(current.x, 8, window.innerWidth - PET_WIDTH - 8),
          y,
        };
      });
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [desktopVisible, docked]);

  useEffect(() => {
    if (!position || !desktopVisible) return;
    clearSnapTimer();
    if (!collapsed) {
      setNotice(null);
      setDocked(false);
      const moveToSessionList = () => {
        if (dragRef.current) return;
        const anchor = document.querySelector<HTMLElement>("[data-assistant-session-anchor]");
        const rect = anchor?.getBoundingClientRect();
        setPosition((current) => {
          if (!current) return current;
          if (!rect || rect.width <= 0 || rect.height <= 0) {
            return { ...current, x: emergedX(current.side) };
          }
          return {
            x: clamp(rect.right - PET_WIDTH / 2, 8, window.innerWidth - PET_WIDTH - 8),
            y: clamp(rect.top + Math.min(150, rect.height * 0.28), PET_TOP_GUTTER, window.innerHeight - PET_HEIGHT - PET_BOTTOM_GUTTER),
            side: "left",
          };
        });
      };
      const timers = [0, 120, 320, 640, 1_000].map((delay) => window.setTimeout(moveToSessionList, delay));
      return () => timers.forEach((timer) => window.clearTimeout(timer));
    }
    scheduleSnap(900);
  }, [collapsed, desktopVisible]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!desktopVisible || !collapsed || !docked || dragging || streaming) return;
    let disposed = false;
    let bubbleTimer: number | null = null;
    let hideTimer: number | null = null;

    const scheduleBubble = () => {
      const delay = IDLE_BUBBLE_MIN_DELAY + Math.random() * IDLE_BUBBLE_DELAY_RANGE;
      bubbleTimer = window.setTimeout(() => {
        const text = IDLE_NOTICES[Math.floor(Math.random() * IDLE_NOTICES.length)] || IDLE_NOTICES[0];
        setNotice({ text, tone: "idle" });
        hideTimer = window.setTimeout(() => {
          setNotice((current) => current?.tone === "idle" ? null : current);
          if (!disposed) scheduleBubble();
        }, IDLE_BUBBLE_VISIBLE_MS);
      }, delay);
    };

    scheduleBubble();
    return () => {
      disposed = true;
      if (bubbleTimer != null) window.clearTimeout(bubbleTimer);
      if (hideTimer != null) window.clearTimeout(hideTimer);
      setNotice((current) => current?.tone === "idle" ? null : current);
    };
  }, [collapsed, desktopVisible, docked, dragging, streaming]);

  useEffect(() => {
    const outcomeId = outcomeSignal?.id ?? 0;
    if (outcomeId === outcomeSignalRef.current) return;
    outcomeSignalRef.current = outcomeId;
    if (!outcomeSignal) return;
    if (!desktopVisible || !collapsed || !position) return;

    const tone = outcomeSignal.status;
    clearOutcomeTimer();
    clearSnapTimer();
    setDocked(false);
    setNotice({ text: tone === "failed" ? "出错了" : "任务完成啦", tone });
    setPosition((current) => current ? { ...current, x: emergedX(current.side) } : current);
    outcomeTimerRef.current = window.setTimeout(() => {
      setNotice((current) => current?.tone === tone ? null : current);
      snapToNearestEdge();
      outcomeTimerRef.current = null;
    }, 3_600);
  }, [clearOutcomeTimer, clearSnapTimer, collapsed, desktopVisible, outcomeSignal, position, snapToNearestEdge]);

  useEffect(() => {
    if (!desktopVisible) return;
    let frame = 0;
    const handlePointer = (event: PointerEvent) => {
      if (dragging || !buttonRef.current) return;
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const rect = buttonRef.current?.getBoundingClientRect();
        if (!rect || !buttonRef.current) return;
        const nextDirection = assistantPetLookDirection(event.clientX, event.clientY, rect);
        setLookDirection((current) => current === nextDirection ? current : nextDirection);
      });
    };
    window.addEventListener("pointermove", handlePointer, { passive: true });
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("pointermove", handlePointer);
    };
  }, [desktopVisible, dragging]);

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || !position) return;
    clearSnapTimer();
    setLookDirection(null);
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: position.x,
      originY: position.y,
      lastX: event.clientX,
      moved: false,
    };
    setDocked(false);
    setDragging(true);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    const stepX = event.clientX - drag.lastX;
    drag.lastX = event.clientX;
    if (Math.hypot(dx, dy) > 5) drag.moved = true;
    if (Math.abs(stepX) >= 1) setDragDirection(stepX < 0 ? "left" : "right");
    setPosition((current) => current ? {
      ...current,
      x: clamp(drag.originX + dx, 8, window.innerWidth - PET_WIDTH - 8),
      y: clamp(drag.originY + dy, PET_TOP_GUTTER, window.innerHeight - PET_HEIGHT - PET_BOTTOM_GUTTER),
    } : current);
  };

  const finishDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    suppressClickRef.current = drag.moved;
    dragRef.current = null;
    setDragDirection(null);
    setDragging(false);
    if (drag.moved && collapsed) scheduleSnap();
  };

  const cancelDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    suppressClickRef.current = false;
    dragRef.current = null;
    setDragDirection(null);
    setDragging(false);
    if (collapsed) scheduleSnap(240);
  };

  if (!position || !desktopVisible) return null;

  return (
    <button
      ref={buttonRef}
      type="button"
      data-assistant-desktop-pet
      data-docked={docked ? position.side : "false"}
      data-side={position.side}
      data-dragging={dragging ? "true" : undefined}
      aria-label={collapsed ? "打开系统助手" : "关闭系统助手"}
      aria-expanded={!collapsed}
      aria-controls={ASSISTANT_SURFACE_ID}
      title={collapsed ? "拖动小助手，点击打开" : "关闭系统助手"}
      onClick={() => {
        if (suppressClickRef.current) {
          suppressClickRef.current = false;
          return;
        }
        setCollapsed(!collapsed);
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finishDrag}
      onPointerCancel={cancelDrag}
      className={cn(
        "assistant-pet fixed left-0 top-0 z-40 hidden h-[114px] w-[102px] select-none touch-none sm:block",
        dragging && "assistant-pet-dragging",
        !collapsed && "assistant-pet-awake",
        streaming && "assistant-pet-streaming",
        notice?.tone === "complete" && "assistant-pet-complete",
        notice?.tone === "failed" && "assistant-pet-failed",
      )}
      style={{
        transform: `translate3d(${position.x}px, ${position.y}px, 0)`,
      } as CSSProperties}
    >
      {notice ? (
        <span
          aria-hidden="true"
          data-assistant-pet-notice={notice.tone}
          className="assistant-pet-notice"
        >
          {notice.text}
        </span>
      ) : null}
      <AssistantPetSprite
        streaming={streaming}
        active={!collapsed}
        celebrating={notice?.tone === "complete"}
        failed={notice?.tone === "failed"}
        dragDirection={dragDirection}
        lookDirection={lookDirection}
      />
    </button>
  );
}

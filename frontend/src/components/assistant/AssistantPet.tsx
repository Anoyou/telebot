import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Send } from "lucide-react";

import { useAssistantDock } from "@/components/assistant/AssistantDock";
import { cn } from "@/lib/utils";

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
  moved: boolean;
};

const PET_WIDTH = 68;
const PET_HEIGHT = 76;
const PET_PEEK = 34;
const PET_TOP_GUTTER = 92;
const PET_BOTTOM_GUTTER = 24;
const PET_SNAP_DELAY = 1_800;
const PET_STORAGE_KEY = "telepilot:assistant-pet:v1";
const IDLE_BUBBLE_MIN_DELAY = 24_000;
const IDLE_BUBBLE_DELAY_RANGE = 18_000;
const IDLE_BUBBLE_VISIBLE_MS = 3_800;

type PetNotice = {
  text: string;
  tone: "idle" | "complete";
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

export function AssistantRobot({
  compact = false,
  streaming = false,
  active = false,
  celebrating = false,
}: {
  compact?: boolean;
  streaming?: boolean;
  active?: boolean;
  celebrating?: boolean;
}) {
  return (
    <span className={cn("assistant-pet-robot-frame", compact && "assistant-pet-robot-frame-compact")} aria-hidden="true">
      <span
        className="assistant-pet-robot"
        data-agent-pet-intent={celebrating ? "complete" : streaming ? "working" : active ? "awake" : "idle"}
      >
        <span className="assistant-pet-shadow" />
        <span className="assistant-pet-antenna"><span /></span>
        <span className="assistant-pet-head">
          <span className="assistant-pet-ear assistant-pet-ear-left" />
          <span className="assistant-pet-ear assistant-pet-ear-right" />
          <span className="assistant-pet-face">
            <span className="assistant-pet-eye assistant-pet-eye-left" />
            <span className="assistant-pet-eye assistant-pet-eye-right" />
            <span className="assistant-pet-mouth" />
          </span>
        </span>
        <span className="assistant-pet-neck" />
        <span className="assistant-pet-body">
          <span className={cn("assistant-pet-core", streaming && "assistant-pet-core-streaming")}>
            <Send />
          </span>
        </span>
        <span className="assistant-pet-arm assistant-pet-arm-left" />
        <span className="assistant-pet-arm assistant-pet-arm-right" />
        <span className="assistant-pet-foot assistant-pet-foot-left">
          <span className="assistant-pet-plume" />
        </span>
        <span className="assistant-pet-foot assistant-pet-foot-right">
          <span className="assistant-pet-plume" />
        </span>
      </span>
    </span>
  );
}

export function AssistantPet() {
  const { collapsed, setCollapsed, streaming, completionSignal } = useAssistantDock();
  const [desktopVisible, setDesktopVisible] = useState(() => (
    typeof window !== "undefined" && window.matchMedia("(min-width: 640px)").matches
  ));
  const [position, setPosition] = useState<PetPosition | null>(null);
  const [dragging, setDragging] = useState(false);
  const [docked, setDocked] = useState(true);
  const [notice, setNotice] = useState<PetNotice | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const snapTimerRef = useRef<number | null>(null);
  const completionTimerRef = useRef<number | null>(null);
  const completionSignalRef = useRef(completionSignal);
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

  const clearCompletionTimer = useCallback(() => {
    if (completionTimerRef.current != null) {
      window.clearTimeout(completionTimerRef.current);
      completionTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    setPosition(initialPosition());
    return () => {
      clearSnapTimer();
      clearCompletionTimer();
    };
  }, [clearCompletionTimer, clearSnapTimer]);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 640px)");
    const update = () => setDesktopVisible(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
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
  }, [docked]);

  useEffect(() => {
    if (!position || !desktopVisible) return;
    clearSnapTimer();
    if (!collapsed) {
      setNotice(null);
      setDocked(false);
      const moveToSessionList = () => {
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
    if (completionSignal === completionSignalRef.current) return;
    completionSignalRef.current = completionSignal;
    if (!desktopVisible || !collapsed || !position) return;

    clearCompletionTimer();
    clearSnapTimer();
    setDocked(false);
    setNotice({ text: "任务完成啦", tone: "complete" });
    setPosition((current) => current ? { ...current, x: emergedX(current.side) } : current);
    completionTimerRef.current = window.setTimeout(() => {
      setNotice((current) => current?.tone === "complete" ? null : current);
      snapToNearestEdge();
      completionTimerRef.current = null;
    }, 3_600);
  }, [clearCompletionTimer, clearSnapTimer, collapsed, completionSignal, desktopVisible, position, snapToNearestEdge]);

  useEffect(() => {
    let frame = 0;
    const handlePointer = (event: PointerEvent) => {
      if (dragging || !buttonRef.current) return;
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const rect = buttonRef.current?.getBoundingClientRect();
        if (!rect || !buttonRef.current) return;
        const dx = clamp((event.clientX - (rect.left + rect.width / 2)) / 28, -1, 1);
        const dy = clamp((event.clientY - (rect.top + rect.height / 2)) / 24, -1, 1);
        buttonRef.current.style.setProperty("--assistant-pet-eye-x", `${(dx * 1.8).toFixed(2)}px`);
        buttonRef.current.style.setProperty("--assistant-pet-eye-y", `${(dy * 1.35).toFixed(2)}px`);
        buttonRef.current.style.setProperty("--assistant-pet-tilt", `${(dx * 2.4).toFixed(2)}deg`);
      });
    };
    window.addEventListener("pointermove", handlePointer, { passive: true });
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("pointermove", handlePointer);
    };
  }, [dragging]);

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || !position) return;
    clearSnapTimer();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: position.x,
      originY: position.y,
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
    if (Math.hypot(dx, dy) > 5) drag.moved = true;
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
    setDragging(false);
    if (collapsed) scheduleSnap(240);
  };

  if (!position) return null;

  return (
    <button
      ref={buttonRef}
      type="button"
      data-assistant-desktop-pet
      data-docked={docked ? position.side : "false"}
      data-side={position.side}
      data-dragging={dragging ? "true" : undefined}
      aria-label={collapsed ? "打开系统助手" : "关闭系统助手"}
      aria-pressed={!collapsed}
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
        "assistant-pet fixed left-0 top-0 z-40 hidden h-[76px] w-[68px] select-none touch-none sm:block",
        dragging && "assistant-pet-dragging",
        !collapsed && "assistant-pet-awake",
        streaming && "assistant-pet-streaming",
        notice?.tone === "complete" && "assistant-pet-complete",
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
      <AssistantRobot streaming={streaming} active={!collapsed} celebrating={notice?.tone === "complete"} />
    </button>
  );
}

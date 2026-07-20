import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent,
  type ReactNode,
} from "react";
import { MessageCircle } from "lucide-react";
import { useLocation } from "react-router-dom";

import { cn } from "@/lib/utils";

type DockSide = "left" | "right";
type FloatingPosition = {
  side: DockSide | null;
  left: number | null;
  top: number | null;
};

type AssistantDockValue = {
  collapsed: boolean;
  mounted: boolean;
  setCollapsed: (collapsed: boolean) => void;
  setStreaming: (streaming: boolean) => void;
};

const AssistantDockContext = createContext<AssistantDockValue | null>(null);

export function AssistantDockProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const [collapsed, setCollapsedState] = useState(true);
  const [mounted, setMounted] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [position, setPosition] = useState<FloatingPosition>({
    side: "right",
    left: null,
    top: null,
  });
  const locationKeyRef = useRef(`${location.pathname}${location.search}${location.hash}`);
  const setCollapsed = useCallback((next: boolean) => {
    if (!next) {
      setMounted(true);
    }
    setCollapsedState(next);
    if (next) {
      window.requestAnimationFrame(() => {
        document.querySelector<HTMLButtonElement>("[data-assistant-tip]")?.focus({ preventScroll: true });
      });
    }
  }, []);

  useEffect(() => {
    const locationKey = `${location.pathname}${location.search}${location.hash}`;
    if (locationKey === locationKeyRef.current) return;
    locationKeyRef.current = locationKey;
    if (!collapsed) setCollapsed(true);
  }, [collapsed, location.hash, location.pathname, location.search, setCollapsed]);

  const value = useMemo(
    () => ({ collapsed, mounted, setCollapsed, setStreaming }),
    [collapsed, mounted, setCollapsed],
  );

  return (
    <AssistantDockContext.Provider value={value}>
      {children}
      {collapsed ? (
        <AssistantFloatingTip
          position={position}
          onPositionChange={setPosition}
          streaming={streaming}
          onExpand={() => {
            setCollapsed(false);
          }}
        />
      ) : null}
    </AssistantDockContext.Provider>
  );
}

export function useAssistantDock(): AssistantDockValue {
  const value = useContext(AssistantDockContext);
  if (!value) {
    throw new Error("useAssistantDock must be used inside AssistantDockProvider");
  }
  return value;
}

function AssistantFloatingTip({
  position,
  onPositionChange,
  streaming,
  onExpand,
}: {
  position: FloatingPosition;
  onPositionChange: (position: FloatingPosition) => void;
  streaming: boolean;
  onExpand: () => void;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dragRef = useRef<{
    pointerId: number;
    offsetX: number;
    offsetY: number;
    startX: number;
    startY: number;
    width: number;
    height: number;
    moved: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);

  const clampPosition = (left: number, top: number, width: number, height: number) => {
    const mobileBottomReserve = window.innerWidth < 640 ? 88 : 12;
    return {
      left: Math.max(8, Math.min(left, window.innerWidth - width - 8)),
      top: Math.max(8, Math.min(top, window.innerHeight - height - mobileBottomReserve)),
    };
  };

  const handlePointerDown = (event: PointerEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    event.currentTarget.setPointerCapture(event.pointerId);
    suppressClickRef.current = false;
    dragRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
      startX: event.clientX,
      startY: event.clientY,
      width: rect.width,
      height: rect.height,
      moved: false,
    };
  };

  const handlePointerMove = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const next = clampPosition(
      event.clientX - drag.offsetX,
      event.clientY - drag.offsetY,
      drag.width,
      drag.height,
    );
    if (Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) > 4) {
      drag.moved = true;
    }
    onPositionChange({ side: null, ...next });
  };

  const handlePointerUp = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (drag.moved) {
      const next = clampPosition(
        event.clientX - drag.offsetX,
        event.clientY - drag.offsetY,
        drag.width,
        drag.height,
      );
      onPositionChange({
        side: event.clientX < window.innerWidth / 2 ? "left" : "right",
        left: null,
        top: next.top,
      });
      suppressClickRef.current = true;
    }
    dragRef.current = null;
  };

  useEffect(() => {
    if (position.top == null) return;
    const keepInViewport = () => {
      const rect = buttonRef.current?.getBoundingClientRect();
      if (!rect) return;
      const next = clampPosition(rect.left, position.top ?? rect.top, rect.width, rect.height);
      const nextLeft = position.side == null ? next.left : null;
      if (nextLeft === position.left && next.top === position.top) return;
      onPositionChange({
        side: position.side,
        left: nextLeft,
        top: next.top,
      });
    };
    keepInViewport();
    window.addEventListener("resize", keepInViewport);
    window.visualViewport?.addEventListener("resize", keepInViewport);
    return () => {
      window.removeEventListener("resize", keepInViewport);
      window.visualViewport?.removeEventListener("resize", keepInViewport);
    };
  }, [onPositionChange, position.left, position.side, position.top]);

  const style: CSSProperties = position.top == null
    ? {}
    : { top: position.top };
  if (position.side == null && position.left != null) {
    style.left = position.left;
  }

  return (
    <button
      ref={buttonRef}
      type="button"
      data-assistant-tip
      aria-label={streaming ? "展开系统助手，当前正在调用" : "展开系统助手"}
      title="拖动可贴边，点击展开系统助手"
      style={style}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={() => {
        dragRef.current = null;
        suppressClickRef.current = false;
      }}
      onClick={() => {
        if (suppressClickRef.current) {
          suppressClickRef.current = false;
          return;
        }
        onExpand();
      }}
      className={cn(
        "fixed z-[75] flex h-11 touch-none select-none items-center gap-2 border border-primary/40 bg-card/95 text-sm font-semibold text-foreground shadow-lg shadow-black/15 active:scale-[0.97]",
        position.top == null && "bottom-[calc(5.25rem+env(safe-area-inset-bottom))] sm:bottom-6",
        position.side === "left" && "left-0 rounded-l-none rounded-r-full pl-2 pr-3",
        position.side === "right" && "right-0 rounded-l-full rounded-r-none pl-3 pr-2",
        position.side == null && "rounded-full px-3",
      )}
    >
      <span className="relative grid h-7 w-7 shrink-0 place-items-center rounded-full bg-primary/15 text-primary">
        <MessageCircle className="h-4 w-4" />
        {streaming ? (
          <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full border-2 border-card bg-success" />
        ) : null}
      </span>
      <span className="whitespace-nowrap text-xs">{streaming ? "调用中" : "助手"}</span>
    </button>
  );
}

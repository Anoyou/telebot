import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";
import {
  nextAssistantOutcomeSignal,
  shouldOpenAssistantDock,
  type AssistantOutcomeSignal,
  type AssistantOutcomeStatus,
} from "./assistantDockState";

type AssistantDockValue = {
  collapsed: boolean;
  mounted: boolean;
  streaming: boolean;
  outcomeSignal: AssistantOutcomeSignal | null;
  setCollapsed: (collapsed: boolean) => void;
  setStreaming: (streaming: boolean) => void;
  notifyOutcome: (status: AssistantOutcomeStatus) => void;
};

export const ASSISTANT_SURFACE_ID = "telepilot-assistant-surface";

const AssistantDockContext = createContext<AssistantDockValue | null>(null);

export function AssistantDockProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const initiallyOpen = shouldOpenAssistantDock(location.pathname, location.search);
  const [collapsed, setCollapsedState] = useState(!initiallyOpen);
  const [mounted, setMounted] = useState(initiallyOpen);
  const [streaming, setStreaming] = useState(false);
  const [outcomeSignal, setOutcomeSignal] = useState<AssistantDockValue["outcomeSignal"]>(null);
  const locationKeyRef = useRef(`${location.pathname}${location.search}${location.hash}`);
  const setCollapsed = useCallback((next: boolean) => {
    if (!next) {
      setMounted(true);
    }
    setCollapsedState(next);
    if (next) {
      window.requestAnimationFrame(() => {
        const triggers = Array.from(document.querySelectorAll<HTMLButtonElement>(
          "[data-assistant-mobile-button], [data-assistant-desktop-pet]",
        ));
        triggers.find((button) => button.offsetParent !== null)?.focus({ preventScroll: true });
      });
    }
  }, []);
  const notifyOutcome = useCallback((status: AssistantOutcomeStatus) => {
    setOutcomeSignal((current) => nextAssistantOutcomeSignal(current, status));
  }, []);

  useEffect(() => {
    const locationKey = `${location.pathname}${location.search}${location.hash}`;
    if (locationKey === locationKeyRef.current) return;
    locationKeyRef.current = locationKey;
    // 深链 /assistant?session=…：打开悬浮助手，不因路由切换而收起
    if (shouldOpenAssistantDock(location.pathname, location.search)) {
      setMounted(true);
      setCollapsedState(false);
      return;
    }
    if (!collapsed) setCollapsed(true);
  }, [collapsed, location.hash, location.pathname, location.search, setCollapsed]);

  const value = useMemo(
    () => ({
      collapsed,
      mounted,
      streaming,
      outcomeSignal,
      setCollapsed,
      setStreaming,
      notifyOutcome,
    }),
    [collapsed, mounted, notifyOutcome, outcomeSignal, setCollapsed, streaming],
  );

  return (
    <AssistantDockContext.Provider value={value}>
      {children}
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

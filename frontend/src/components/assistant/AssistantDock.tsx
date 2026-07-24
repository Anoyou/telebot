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

type AssistantDockValue = {
  collapsed: boolean;
  mounted: boolean;
  streaming: boolean;
  completionSignal: number;
  setCollapsed: (collapsed: boolean) => void;
  setStreaming: (streaming: boolean) => void;
  notifyCompletion: () => void;
};

const AssistantDockContext = createContext<AssistantDockValue | null>(null);

export function AssistantDockProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const [collapsed, setCollapsedState] = useState(true);
  const [mounted, setMounted] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [completionSignal, setCompletionSignal] = useState(0);
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
  const notifyCompletion = useCallback(() => {
    setCompletionSignal((value) => value + 1);
  }, []);

  useEffect(() => {
    const locationKey = `${location.pathname}${location.search}${location.hash}`;
    if (locationKey === locationKeyRef.current) return;
    locationKeyRef.current = locationKey;
    if (!collapsed) setCollapsed(true);
  }, [collapsed, location.hash, location.pathname, location.search, setCollapsed]);

  const value = useMemo(
    () => ({
      collapsed,
      mounted,
      streaming,
      completionSignal,
      setCollapsed,
      setStreaming,
      notifyCompletion,
    }),
    [collapsed, completionSignal, mounted, notifyCompletion, setCollapsed, streaming],
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

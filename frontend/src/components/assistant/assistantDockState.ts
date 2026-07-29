export type AssistantOutcomeStatus = "complete" | "failed";

export type AssistantOutcomeSignal = {
  id: number;
  status: AssistantOutcomeStatus;
};

export function shouldOpenAssistantDock(pathname: string, search: string): boolean {
  const deepSession = new URLSearchParams(search).get("session");
  return pathname === "/assistant" || Boolean(deepSession);
}

export function nextAssistantOutcomeSignal(
  current: AssistantOutcomeSignal | null,
  status: AssistantOutcomeStatus,
): AssistantOutcomeSignal {
  return {
    id: (current?.id ?? 0) + 1,
    status,
  };
}

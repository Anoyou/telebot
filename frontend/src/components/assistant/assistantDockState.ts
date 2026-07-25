export function shouldOpenAssistantDock(pathname: string, search: string): boolean {
  const deepSession = new URLSearchParams(search).get("session");
  return pathname === "/assistant" || Boolean(deepSession);
}

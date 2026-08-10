export const SUPPORTED_PROXY_TYPES = ["socks5", "http", "https"] as const;

export type SupportedProxyType = (typeof SUPPORTED_PROXY_TYPES)[number];

export interface ProxyReference {
  id: number;
  type: string;
}

export type ProxySelectionIssue = "missing" | "unsupported" | null;

export function normalizeSupportedProxyType(
  value: string,
): SupportedProxyType | null {
  const normalized = value.toLowerCase();
  return SUPPORTED_PROXY_TYPES.includes(normalized as SupportedProxyType)
    ? (normalized as SupportedProxyType)
    : null;
}

export function isSupportedProxyType(value: string): boolean {
  return normalizeSupportedProxyType(value) !== null;
}

export function proxySelectionIssue(
  proxies: readonly ProxyReference[],
  selectedId: string,
): ProxySelectionIssue {
  if (!selectedId) return null;
  const id = Number(selectedId);
  const selected = Number.isSafeInteger(id)
    ? proxies.find((proxy) => proxy.id === id)
    : undefined;
  if (!selected) return "missing";
  return isSupportedProxyType(selected.type) ? null : "unsupported";
}

export function proxySelectionNeedsLoadedList(
  selectedId: string,
  listLoaded: boolean,
): boolean {
  return Boolean(selectedId) && !listLoaded;
}

export function visibleProxyOptions<T extends ProxyReference>(
  proxies: readonly T[],
  selectedId: string,
): T[] {
  const selected = Number(selectedId);
  return proxies.filter(
    (proxy) => isSupportedProxyType(proxy.type) || proxy.id === selected,
  );
}

export function shouldClearCredentialsForMigration(
  sourceType: string,
  targetType: string,
): boolean {
  return !isSupportedProxyType(sourceType) && isSupportedProxyType(targetType);
}

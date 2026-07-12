export const queryKeys = {
  featureMatrix: ["matrix"] as const,
  ignoredPeers: (accountId: number | undefined) => ["ignored-peers", accountId] as const,
};

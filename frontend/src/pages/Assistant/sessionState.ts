export type SessionStateItem = {
  id: string;
};

export function removeSessionAndChooseNext<T extends SessionStateItem>(
  sessions: T[],
  activeId: string | null,
  deletedId: string,
): { sessions: T[]; activeId: string | null } {
  const remaining = sessions.filter((session) => session.id !== deletedId);
  return {
    sessions: remaining,
    activeId: activeId === deletedId ? remaining[0]?.id || null : activeId,
  };
}

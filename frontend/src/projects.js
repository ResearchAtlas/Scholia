// Projects and conversations as the interface names and moves them.

// Sensitivity levels from least to most strict (backend/app.py _STRICTNESS).
const STRICTNESS = { normal: 0, private: 1, local_only: 2 };

export function projectName(t, project) {
  return project?.kind === 'general' ? t('project.general') : project?.name ?? '';
}

export function conversationTitle(t, conversation) {
  return conversation?.title || t('conversation.untitled');
}

// The projects a conversation in `from` may move to: any other project at the same level or
// stricter (ticket 14). The backend refuses the rest.
export function moveTargets(projects, from) {
  const level = STRICTNESS[from?.sensitivity] ?? 0;
  return projects.filter((p) => p.id !== from?.id && (STRICTNESS[p.sensitivity] ?? 0) >= level);
}

// The latest turn can be continued when it was interrupted, or stopped at a limit or by a
// change to its project (backend/runs.py continue_turn).
export function continuable(turn) {
  return turn.status === 'interrupted' || (turn.status === 'cancelled' && ['limit', 'revoked'].includes(turn.cancel_reason));
}

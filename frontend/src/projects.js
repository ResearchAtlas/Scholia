// Projects and conversations as the interface names and moves them.

// Sensitivity levels from least to most strict (backend/app.py _STRICTNESS).
const STRICTNESS = { normal: 0, private: 1, local_only: 2 };

export function projectName(t, project) {
  return project?.kind === 'general' ? t('project.general') : project?.name ?? '';
}

export function conversationTitle(t, conversation) {
  return conversation?.title || t('conversation.untitled');
}

// How strict a project is: its level, and a review-locked project above an unlocked one.
const rank = (project) => (STRICTNESS[project?.sensitivity] ?? 0) * 2 + (project?.review_lock ? 1 : 0);

// The projects a conversation in `from` may move to: any other project at the same level or
// stricter (ticket 14), a review-locked one's only to another locked project. The backend
// refuses the rest.
export function moveTargets(projects, from) {
  return projects.filter((p) => p.id !== from?.id && rank(p) >= rank(from));
}

// The latest turn can be continued when it was interrupted, or stopped at a limit or by a
// change to its project (backend/runs.py continue_turn).
export function continuable(turn) {
  return turn.status === 'interrupted' || (turn.status === 'cancelled' && ['limit', 'revoked'].includes(turn.cancel_reason));
}

// The answers to what a project holds (slice-1 spec F1): my own research (Normal), unpublished
// work or personal data (Private, the suggested answer), or someone else's submission (the
// review-lock preset: Local only with the lock, and its venue).
export const HOLDS = ['own', 'private', 'review'];
export const SUGGESTED_HOLDS = 'private';

// A new project's protection for an answer, as POST /api/projects takes it.
export function holdsBody(holds, venue) {
  if (holds === 'review') return { sensitivity: 'local_only', review_lock: true, review_venue: venue?.trim() || null };
  return { sensitivity: holds === 'private' ? 'private' : 'normal' };
}

// The answer a project's protection gives, or null for Local only without the lock.
export function holdsOf(project) {
  if (project?.review_lock) return 'review';
  return { normal: 'own', private: 'private' }[project?.sensitivity] ?? null;
}

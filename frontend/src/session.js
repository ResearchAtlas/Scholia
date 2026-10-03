// This launch's session (backend/local_guard.py): the window's first URL carries it in its
// fragment, which never reaches a server. It is kept in this origin's session storage, so a
// reload keeps it and no other origin or port can read it, and the fragment is removed from
// the address bar.
export const SESSION_KEY = 'scholia.session';

export function takeSession(location, storage, history) {
  const match = /(?:^#|&)session=([A-Za-z0-9_-]{32,})(?:&|$)/.exec(location.hash ?? '');
  if (match) {
    try {
      storage.setItem(SESSION_KEY, match[1]);
    } catch {
      // ponytail: storage refused (a locked-down window): the secret lives for this page only.
    }
    history.replaceState(null, '', location.pathname + location.search);
    return match[1];
  }
  try {
    return storage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

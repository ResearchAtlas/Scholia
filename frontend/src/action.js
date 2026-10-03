// An action a dialog or form runs that can fail: its error stays beside it, in the interface
// language (the backend's message is never shown).
import { useState } from 'react';
import { ApiError } from './api.js';
import { errorText } from './text.js';
import { useT } from './i18n/index.js';

export function useAction() {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(null);
  async function run(action) {
    setBusy(true);
    setProblem(null);
    try {
      return await action();
    } catch (error) {
      setProblem(errorText(t, error instanceof ApiError ? error.code : 'internal'));
      return undefined;
    } finally {
      setBusy(false);
    }
  }
  return { busy, problem, run, reset: () => setProblem(null) };
}

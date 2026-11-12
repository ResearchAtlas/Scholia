// An action a dialog or form runs that can fail: its error stays beside it, in the interface
// language (the backend's message is never shown).
import { useContext, useState } from 'react';
import { ApiError } from './api.js';
import { errorText, money } from './text.js';
import { LanguageContext, useT } from './i18n/index.js';

// A failure's text; S1-33: a budget limit too low for its step says the least it needs, rounded up to the cent so
// typing it fits (PR 52 cycle 2).
export function problemText(t, error, language) {
  if (!(error instanceof ApiError)) return errorText(t, 'internal');
  if (error.neededUsd !== undefined) {
    return t('errors.limitNeeded', { needed: money(Math.ceil(error.neededUsd * 100 - 1e-9) / 100, language) });
  }
  return errorText(t, error.code);
}

export function useAction() {
  const t = useT();
  const language = useContext(LanguageContext);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(null);
  async function run(action) {
    setBusy(true);
    setProblem(null);
    try {
      return await action();
    } catch (error) {
      setProblem(problemText(t, error, language));
      return undefined;
    } finally {
      setBusy(false);
    }
  }
  return { busy, problem, run, reset: () => setProblem(null) };
}

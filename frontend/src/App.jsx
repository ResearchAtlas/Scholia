// i18n: migrated
// The app: the first-run setup, then the window's three columns. The language follows the
// [ui] language setting ("system" follows the system), and light or dark follows the system.
import { useCallback, useEffect, useState } from 'react';
import { LanguageContext, resolveLanguage, useT } from './i18n/index.js';
import { ApiError, get } from './api.js';
import { errorText } from './text.js';
import { FirstRun } from './components/FirstRun.jsx';
import { Shell } from './components/Shell.jsx';
import { DataFolderScreen } from './components/DataFolderScreen.jsx';
import { Button } from '@/components/ui/button';

function useSystemTheme() {
  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => document.documentElement.classList.toggle('dark', query.matches);
    apply();
    query.addEventListener('change', apply);
    return () => query.removeEventListener('change', apply);
  }, []);
}

export function App() {
  useSystemTheme();
  const [state, setState] = useState({ phase: 'loading' });
  const [language, setLanguage] = useState(() => resolveLanguage('system', navigator.language));

  const load = useCallback(async () => {
    try {
      const [health, setup, settings] = await Promise.all([get('/api/health'), get('/api/setup'), get('/api/settings')]);
      setLanguage(resolveLanguage(settings.values?.ui?.language ?? 'system', navigator.language));
      if (health.data_folder_problem) {
        setState({ phase: 'dataFolder', health });
      } else {
        setState({ phase: setup.needed ? 'setup' : 'ready', health, settings });
      }
    } catch (error) {
      setState({ phase: 'error', code: error instanceof ApiError ? error.code : 'internal' });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  return (
    <LanguageContext.Provider value={language}>
      {state.phase === 'loading' && <Splash />}
      {state.phase === 'error' && <Failure code={state.code} onRetry={load} />}
      {state.phase === 'dataFolder' && <DataFolderScreen health={state.health} />}
      {state.phase === 'setup' && <FirstRun onDone={load} />}
      {state.phase === 'ready' && <Shell health={state.health} settings={state.settings} />}
    </LanguageContext.Provider>
  );
}

function Splash() {
  const t = useT();
  return (
    <div className="grid h-full place-items-center" role="status" aria-live="polite">
      <span className="text-sm text-muted-foreground">{t('common.loading')}</span>
    </div>
  );
}

function Failure({ code, onRetry }) {
  const t = useT();
  return (
    <div className="grid h-full place-items-center p-6">
      <div className="max-w-sm text-center" role="alert">
        <p className="text-sm">{errorText(t, code)}</p>
        <Button className="mt-4" variant="outline" onClick={onRetry}>{t('common.retry')}</Button>
      </div>
    </div>
  );
}

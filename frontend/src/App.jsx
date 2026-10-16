// i18n: migrated
// The app: the first-run setup, then the window's three columns. The language follows the
// [ui] language setting ("system" follows the system), and light or dark follows the system.
// The window is drawn in a language once its catalog has loaded (i18n languageSwitch), and only
// the setting's language is loaded (the system's when the setting cannot be read): until then the
// window shows nothing. One that cannot load says so in that language (NOT_LOADED), with Try again
// and Use English, which sets the language to English. A change in Settings loads its catalog
// before it is saved, so the window changes language at once.
// A data folder Scholia will not open, or a damaged database, shows its own screen; health is
// read first, since a limited app serves little else.
import { useCallback, useEffect, useState } from 'react';
import { LanguageContext, NOT_LOADED, languageSwitch, resolveLanguage, useT } from './i18n/index.js';
import { ApiError, get, saveSettings } from './api.js';
import { errorText } from './text.js';
import { FirstRun } from './components/FirstRun.jsx';
import { Shell } from './components/Shell.jsx';
import { DataFolderScreen } from './components/DataFolderScreen.jsx';
import { DamagedDatabaseScreen } from './components/Backups.jsx';
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
  const [shown, setShown] = useState({ language: null, failed: null });
  const [languages] = useState(() => languageSwitch(setShown));
  const { language } = shown;

  const load = useCallback(async () => {
    try {
      const health = await get('/api/health');
      if (health.data_folder_problem || health.database_damaged) {
        const settings = await get('/api/settings').catch(() => null); // a limited app may not serve it
        await languages.want(resolveLanguage(settings?.values?.ui?.language ?? 'system', navigator.language));
        setState({ phase: health.data_folder_problem ? 'dataFolder' : 'damaged', health });
        return;
      }
      // The setting's language is drawn whenever the settings were read, even if the setup was not.
      const [setup, settings] = await Promise.allSettled([get('/api/setup'), get('/api/settings')]);
      if (settings.status === 'fulfilled') await languages.want(resolveLanguage(settings.value.values?.ui?.language ?? 'system', navigator.language));
      for (const answer of [setup, settings]) if (answer.status === 'rejected') throw answer.reason;
      setState({ phase: setup.value.needed ? 'setup' : 'ready', health, settings: settings.value });
    } catch (error) {
      await languages.fallback(resolveLanguage('system', navigator.language)); // the setting was not read
      setState({ phase: 'error', code: error instanceof ApiError ? error.code : 'internal' });
    }
  }, [languages]);

  // Use English, on the screen of a language that could not load: the setting becomes English
  // (saved once more if the settings changed meanwhile or the save failed), and English is drawn
  // even if it could not be saved, since the researcher chose it; the window then says it was not
  // saved, with Try again, which saves it again.
  const [englishUnsaved, setEnglishUnsaved] = useState(false);
  const saveEnglish = async () => {
    const save = () => saveSettings({ 'ui.language': 'en' }).catch(() => null);
    setEnglishUnsaved(!(await save() || await save()));
  };
  const chooseEnglish = async () => {
    await saveEnglish();
    await languages.want('en');
  };

  useEffect(() => {
    load();
  }, [load]);

  // A request found the app limited while it was open (api.js): if health says the database is
  // damaged, the restore screen replaces the window.
  useEffect(() => {
    const check = () => get('/api/health').then((health) => {
      if (health.database_damaged) setState({ phase: 'damaged', health });
    }).catch(() => {});
    window.addEventListener('scholia:limited', check);
    return () => window.removeEventListener('scholia:limited', check);
  }, []);

  useEffect(() => {
    if (language) document.documentElement.lang = language;
  }, [language]);

  if (shown.failed) return <LanguageNotLoaded language={shown.failed} onEnglish={chooseEnglish} />;
  if (!language) return null;
  return (
    <LanguageContext.Provider value={language}>
      {englishUnsaved && <EnglishNotSaved onRetry={saveEnglish} />}
      {state.phase === 'loading' && <Splash />}
      {state.phase === 'error' && <Failure code={state.code} onRetry={load} />}
      {state.phase === 'dataFolder' && <DataFolderScreen health={state.health} />}
      {state.phase === 'damaged' && <DamagedDatabaseScreen onRestored={load} />}
      {state.phase === 'setup' && <FirstRun onDone={load} />}
      {state.phase === 'ready' && <Shell health={state.health} settings={state.settings}
        onLanguage={(setting) => languages.want(resolveLanguage(setting, navigator.language))} />}
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

// English chosen on that screen but not saved as the setting: said over the window, which keeps its
// layout, with Try again.
function EnglishNotSaved({ onRetry }) {
  const t = useT();
  return (
    <div role="alert" className="fixed inset-x-4 bottom-4 z-50 mx-auto flex max-w-lg items-center gap-3 rounded-md border bg-background px-3 py-3 text-sm text-destructive shadow-md">
      <p className="flex-1">{t('errors.english_not_saved')}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>{t('common.retry')}</Button>
    </div>
  );
}

// A language whose catalog could not be loaded, said in that language from the few strings kept
// with the window (NOT_LOADED), since its catalog is what could not load. Try again reloads the
// window, as for the other parts that could not load.
function LanguageNotLoaded({ language, onEnglish }) {
  const text = NOT_LOADED[language];
  return (
    <div className="grid h-full place-items-center p-6" lang={language}>
      <div role="alert" className="max-w-sm space-y-3 rounded-md bg-destructive/10 px-3 py-3 text-sm text-destructive">
        <p>{text.message}</p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => window.location.reload()}>{text.retry}</Button>
          <Button variant="outline" size="sm" onClick={onEnglish}>{text.english}</Button>
        </div>
      </div>
    </div>
  );
}

// i18n: migrated
// First-run setup (S1): one screen for the OpenRouter key, with a link to how to get one,
// then the first project's name and what it will hold (F1). Other providers are added later in Settings.
import { useState } from 'react';
import { ArrowRight, KeyRound } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { ApiError, post } from '../api.js';
import { errorText } from '../text.js';
import { SUGGESTED_HOLDS, holdsBody } from '../projects.js';
import { HoldsChoice } from './Governance.jsx';
import { Mark } from './Mark.jsx';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

const KEY_HELP = 'https://openrouter.ai/keys';

export function FirstRun({ onDone }) {
  const t = useT();
  const [step, setStep] = useState('key');
  const [key, setKey] = useState('');
  const [name, setName] = useState('');
  const [holds, setHolds] = useState(SUGGESTED_HOLDS);
  const [venue, setVenue] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(null);
  const [warning, setWarning] = useState(null);

  async function saveKey(event) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const saved = await post('/api/setup', { openrouter_key: key });
      setKey('');
      if (saved.warning) setWarning(t('setup.storeWarning'));
      setStep('project');
    } catch (error) {
      setProblem(errorText(t, error instanceof ApiError ? error.code : 'internal'));
    } finally {
      setBusy(false);
    }
  }

  async function createProject(event) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      if (name.trim()) await post('/api/projects', { name, ...holdsBody(holds, venue) });
      onDone();
    } catch (error) {
      setProblem(errorText(t, error instanceof ApiError ? error.code : 'internal'));
      setBusy(false);
    }
  }

  return (
    <main className="grid h-full place-items-center overflow-y-auto bg-gradient-to-b from-brand-soft via-background to-background p-6">
      <div className="w-full max-w-md animate-fade-up rounded-2xl border bg-card p-8 shadow-xl shadow-brand/5">
        <Mark className="size-11 text-base shadow-lg shadow-brand/20" />
        {step === 'key' ? (
          <form onSubmit={saveKey}>
            <h1 className="mt-5 text-xl font-semibold tracking-tight">{t('setup.welcome')}</h1>
            <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{t('setup.intro')}</p>
            <label htmlFor="key" className="mt-6 block text-sm font-medium">{t('setup.keyLabel')}</label>
            <div className="relative mt-1.5">
              <KeyRound className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input id="key" type="password" autoComplete="off" spellCheck={false} value={key} required
                onChange={(event) => setKey(event.target.value)} className="h-10 pl-9 font-mono" autoFocus />
            </div>
            <div className="mt-2 flex flex-wrap justify-between gap-2 text-xs text-muted-foreground">
              <a href={KEY_HELP} target="_blank" rel="noreferrer noopener" className="text-brand underline-offset-2 hover:underline">
                {t('setup.keyHelp')}
              </a>
              <span>{t('setup.otherProviders')}</span>
            </div>
            {problem && <p role="alert" className="mt-4 text-sm text-destructive">{problem}</p>}
            <Button type="submit" className="mt-6 w-full" disabled={busy || !key.trim()}>
              {t('setup.continue')}<ArrowRight aria-hidden="true" />
            </Button>
          </form>
        ) : (
          <form onSubmit={createProject}>
            <h1 className="mt-5 text-xl font-semibold tracking-tight">{t('project.firstTitle')}</h1>
            {warning && <p role="status" className="mt-3 rounded-lg bg-warning/10 px-3 py-2 text-sm text-warning">{warning}</p>}
            <label htmlFor="project" className="mt-5 block text-sm font-medium">{t('project.nameLabel')}</label>
            <Input id="project" value={name} maxLength={200} onChange={(event) => setName(event.target.value)}
              placeholder={t('project.namePlaceholder')} className="mt-1.5 h-10" autoFocus />
            <div className="mt-5">
              <HoldsChoice value={holds} onChange={setHolds} venue={venue} onVenue={setVenue} name="first-project-holds" />
            </div>
            {problem && <p role="alert" className="mt-4 text-sm text-destructive">{problem}</p>}
            <div className="mt-6 flex gap-2">
              <Button type="submit" className="flex-1" disabled={busy || !name.trim()}>{t('project.create')}</Button>
              <Button type="button" variant="ghost" disabled={busy} onClick={onDone}>{t('project.skip')}</Button>
            </div>
          </form>
        )}
      </div>
    </main>
  );
}

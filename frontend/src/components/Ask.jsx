// i18n: migrated
// The shared confirmation (slice-1 spec section 6.2): a question, one to three options and a text
// box unless its kind rules one out, naming its project (from the ask itself, so wherever it is shown)
// and action. The conversation, the Library panel and the background-run list all show it with this
// component, and answer it through the one ask endpoint (backend/asks.py); an answer that is no
// longer possible says why.
import { useContext, useId, useState } from 'react';
import { MessageCircleQuestion } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { post } from '../api.js';
import { useAction } from '../action.js';
import { visible } from '../text.js';
import { projectName } from '../projects.js';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ModelDetails } from './LocalHelper.jsx'; // S1-17: the search model's offer

// A catalog entry when there is one, else what was given.
function named(t, key, fallback) {
  const text = t(key);
  return text === key ? fallback : text;
}

export function Ask({ ask, onAnswered }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const textId = useId();
  const [text, setText] = useState('');
  const { busy, problem, run } = useAction();
  const services = new Intl.ListFormat(language, { type: 'conjunction' })
    .format((ask.params?.services ?? []).map((s) => named(t, `ask.service.${s}`, s)));
  // A kind the app defines is asked in its own words; another (the agent's, S1-19) carries its question.
  const questionKey = `ask.${ask.kind}.question`;
  const asked = t(questionKey, { count: ask.params?.identifiers ?? 0, services });
  const question = asked === questionKey ? ask.params?.question ?? '' : asked;
  const body = named(t, `ask.${ask.kind}.body`, '');

  async function answer(reply) {
    const done = await run(() => post(`/api/runs/${encodeURIComponent(ask.run_id)}/asks/${encodeURIComponent(ask.ask_id)}`, reply));
    if (done) onAnswered?.(done);
  }

  return (
    <section role="group" aria-label={t('ask.label')}
      className="grid grid-cols-1 gap-3 rounded-xl border border-brand/30 bg-brand-soft/60 p-4 text-sm">
      <div className="flex gap-2.5">
        <MessageCircleQuestion className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden="true" />
        <div className="grid min-w-0 gap-1">
          <p className="truncate text-xs text-muted-foreground">
            {t('ask.project', { name: projectName(t, { kind: ask.project_kind, name: ask.project_name }) })}
          </p>
          <p className="font-medium leading-snug">{question}</p>
          {body && <p className="leading-relaxed text-muted-foreground">{body}</p>}
          {ask.kind === 'model_download' && (
            <details className="text-xs">
              <summary className="w-fit cursor-pointer select-none rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
                {t('library.details')}
              </summary>
              <ModelDetails />
            </details>
          )}
        </div>
      </div>
      {ask.text_box && (
        <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); if (visible(text)) answer({ text }); }}>
          <label htmlFor={textId} className="sr-only">{t('ask.textLabel')}</label>
          <Input id={textId} value={text} onChange={(event) => setText(event.target.value)} disabled={busy} className="h-8" />
          <Button type="submit" size="sm" variant="outline" disabled={busy || !visible(text)}>{t('ask.send')}</Button>
        </form>
      )}
      {problem && <p role="alert" className="text-destructive">{problem}</p>}
      <div className="flex flex-wrap gap-2">
        {ask.options.map((option, i) => (
          <Button key={option} type="button" size="sm" variant={i === 0 ? 'default' : 'outline'} disabled={busy}
            className={i === 0 ? 'bg-brand text-brand-foreground hover:bg-brand/90' : undefined}
            onClick={() => answer({ option })}>
            {named(t, `ask.${ask.kind}.option.${option}`, option)}
          </Button>
        ))}
      </div>
    </section>
  );
}

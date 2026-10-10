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
import { paperLine } from '../references.js';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

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
  // A Local only lookup of a reference file's papers says so (S1-14): its answer covers those papers.
  const body = named(t, `ask.${ask.kind}.body${ask.params?.batch === 'reference_file' ? 'ReferenceFile' : ''}`, '');

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
        </div>
      </div>
      {ask.text_box && (
        <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); if (visible(text)) answer({ text }); }}>
          <label htmlFor={textId} className="sr-only">{t('ask.textLabel')}</label>
          <Input id={textId} value={text} onChange={(event) => setText(event.target.value)} disabled={busy} className="h-8" />
          <Button type="submit" size="sm" variant="outline" disabled={busy || !visible(text)}>{t('ask.send')}</Button>
        </form>
      )}
      {ask.kind === 'merge' && <MergeComparison ask={ask} />}
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

// A merge question's two papers (S1-14), as the backend reads them when it lists the question: the
// project's paper and the reference file's record, and what made them look like one paper.
function MergeComparison({ ask }) {
  const t = useT();
  const { paper, record, matched } = ask.params ?? {};
  if (!paper || !record) return null;
  return (
    <div className="grid grid-cols-1 gap-2">
      {[['ask.merge.paper', paper], ['ask.merge.record', record]].map(([label, item]) => (
        <div key={label} className="grid min-w-0 gap-0.5 rounded-lg border bg-background/70 px-3 py-2">
          <p className="text-xs font-medium text-muted-foreground">{t(label)}</p>
          <p className="line-clamp-2 break-words font-medium" title={item.title}>{item.title}</p>
          <p className="truncate text-xs text-muted-foreground" title={paperLine(t, item)}>
            {paperLine(t, item) || t('ask.merge.noDetails')}
          </p>
        </div>
      ))}
      {matched && <p className="text-xs text-muted-foreground">{t(`ask.merge.matched.${matched}`)}</p>}
    </div>
  );
}

// i18n: migrated
// Details under an answer (S1-19b; slice-1 spec F11 and F4): what the run did, from its trace alone:
// the route and how it was chosen, the retrieval, each step with its estimated cost ("about $x") and
// what it was charged, the exact text each call sent, what was saved, and the full citation check.
// Loaded when first opened (Parts.jsx).
import { useContext, useEffect, useState } from 'react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, getBlob } from '../api.js';
import { errorText, money } from '../text.js';
import { readTrace, summarize } from '../trace.js';
import { LoadState } from './fields.jsx';

const SUPPORT = { supported: 'cite.supported', partly: 'cite.partly', unsupported: 'cite.unsupported', unchecked: 'cite.unchecked' };
const EXISTENCE = { ok: 'trace.exists', not_found: 'cite.notFound', not_read_in_run: 'cite.notRead', source_removed: 'cite.removed' };
const PHASE = { answer: 'trace.phaseAnswer', judge: 'trace.phaseJudge', route: 'trace.phaseRoute' };

export function Trace({ turn }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const [trace, setTrace] = useState(null);
  const [problem, setProblem] = useState(null);
  const [tries, setTries] = useState(0);
  useEffect(() => {
    let live = true;
    setProblem(null);
    readTrace(turn.run_id).then((events) => live && setTrace(summarize(events)),
      (error) => live && setProblem(error instanceof ApiError ? error.code : 'internal'));
    return () => { live = false; };
  }, [turn.run_id, tries]);
  if (!trace) return <LoadState problem={problem} onRetry={() => setTries((n) => n + 1)} />;
  const about = (value) => (value === null ? null : t('trace.about', { cost: money(value, language) }));
  return (
    <div className="space-y-3 rounded-md border bg-muted/30 p-3 text-xs">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">{t('trace.model')}</dt><dd className="break-all">{trace.route ?? '—'}</dd>
        {trace.retrieval && <>
          <dt className="text-muted-foreground">{t('trace.route')}</dt>
          <dd>{t(`trace.route.${trace.retrieval.route}`)} · {t(`trace.decided.${trace.retrieval.decided_by}`)}</dd>
        </>}
        {trace.search && <>
          <dt className="text-muted-foreground">{t('trace.search')}</dt>
          <dd>{t('trace.passages', { count: trace.search.passages })} · {t(`trace.mode.${trace.search.mode}`)}
            {trace.search.rerank === 'fallback' ? ` · ${t('trace.rerankFallback')}` : ''}</dd>
        </>}
        <dt className="text-muted-foreground">{t('trace.saved')}</dt>
        <dd>{turn.answer && turn.result_saved ? t('trace.savedYes') : t('trace.savedNo')}</dd>
      </dl>
      <ol className="space-y-2">
        {trace.steps.map((step, i) => (
          <li key={i} className="space-y-1">
            <p className="font-medium">{t(PHASE[step.phase] ?? 'trace.phaseOther')}
              {step.estimate !== null && <> · <span className="font-normal text-muted-foreground">{about(step.estimate)}</span></>}</p>
            {step.cut && <p className="text-muted-foreground">{t('turn.cut')}</p>}
            {step.attempts.map((attempt, j) => <Attempt key={j} runId={turn.run_id} attempt={attempt} />)}
          </li>
        ))}
      </ol>
      {trace.checks.length > 0 && (
        <div className="space-y-1">
          <p className="font-medium">{t('trace.checks')}</p>
          <ul className="space-y-1">
            {trace.checks.map((check, i) => (
              <li key={i} className="grid gap-0.5">
                <span>{check.sentence}</span>
                <span className="text-muted-foreground">{check.marker} · {t(EXISTENCE[check.existence] ?? 'cite.notFound')}
                  {check.existence === 'ok' ? ` · ${t(SUPPORT[check.support] ?? 'cite.unchecked')}` : ''}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// One attempt: its outcome and charge, and on request the exact text it sent.
function Attempt({ runId, attempt }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const [text, setText] = useState(null);
  const [problem, setProblem] = useState(null);
  async function show() {
    setProblem(null);
    try {
      setText(await (await getBlob(`/api/runs/${encodeURIComponent(runId)}/trace/bodies/${attempt.body}`)).text());
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
    }
  }
  const charged = attempt.cost !== null ? (attempt.charge === 'reported' ? money(attempt.cost, language)
    : t('trace.about', { cost: money(attempt.cost, language) })) : null;
  return (
    <div className="space-y-1 pl-3">
      <p className="text-muted-foreground">{attempt.outcome === 'ok' ? t('trace.outcomeOk')
        : t('trace.outcomeFailed', { reason: errorText(t, attempt.outcome) })}{charged ? ` · ${t('trace.charged', { cost: charged })}` : ''}</p>
      {attempt.body && text === null && (
        <button type="button" onClick={show}
          className="rounded-sm text-brand underline-offset-2 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
          {t('trace.showSent')}</button>
      )}
      {problem && <p role="alert" className="text-destructive">{errorText(t, problem)}</p>}
      {text !== null && (
        <pre aria-label={t('trace.sent')} className="scroll-thin max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-[11px]">
          {text}</pre>
      )}
    </div>
  );
}

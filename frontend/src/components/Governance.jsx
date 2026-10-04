// i18n: migrated
// Governance (slice-1 spec F1, sections 6.4 and 10; ticket 18): what a project holds and its
// protection, the OpenRouter key's data-settings confirmation, declared model servers on this
// Mac, the Private allowlist and the audit log. Scholia cannot verify a confirmation or a
// declaration, and says so where each is made.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { ExternalLink, Lock, ShieldCheck } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, confirmedChange, del, get, post, put } from '../api.js';
import { HOLDS, holdsOf, tightens } from '../projects.js';
import { forgetModels } from '../settings.js';
import { auditDetail, auditEvent } from '../audit.js';
import { CommitField, Field, LoadState, Problem, Section, Segmented } from './fields.jsx';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

const KEY_SETTINGS = { privacyLink: 'https://openrouter.ai/settings/privacy',
  observabilityLink: 'https://openrouter.ai/settings/observability' };
const LEVEL_OF = { own: 'normal', private: 'private' };
const codeOf = (error) => (error instanceof ApiError ? error.code : 'internal');

// The three answers to what a project holds, each with what it means. The review answer asks
// for the submission's venue when onVenue is given; lockedOut answers cannot be chosen.
export function HoldsChoice({ value, onChange, venue, onVenue, disabled, lockedOut = [], name = 'holds' }) {
  const t = useT();
  return (
    <fieldset className="grid gap-2" disabled={disabled}>
      <legend className="mb-1.5 text-sm font-medium">{t('protection.question')}</legend>
      {HOLDS.map((option) => {
        const out = lockedOut.includes(option);
        return (
          <label key={option} className={cn('flex gap-2.5 rounded-lg border px-3 py-2.5 text-sm transition-colors',
            out ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:bg-accent', value === option && 'border-brand bg-brand-soft')}>
            <input type="radio" name={name} value={option} checked={value === option} disabled={out}
              onChange={() => onChange(option)} className="mt-0.5 accent-[hsl(var(--brand))]" />
            <span className="min-w-0">
              <span className="block font-medium">{t(`protection.${option}`)}</span>
              <span className="block text-xs leading-relaxed text-muted-foreground">{t(`protection.${option}Hint`)}</span>
            </span>
          </label>
        );
      })}
      {value === 'review' && onVenue && (
        <Field label={t('protection.venue')} htmlFor={`${name}-venue`}>
          <Input id={`${name}-venue`} value={venue} maxLength={200} placeholder={t('protection.venuePlaceholder')}
            onChange={(event) => onVenue(event.target.value)} />
        </Field>
      )}
    </fieldset>
  );
}

// A question the researcher answers before a change goes ahead: ask(title, body, confirm)
// resolves to whether they confirmed.
function useConfirm() {
  const t = useT();
  const [asking, setAsking] = useState(null);
  const ask = useCallback((title, body, confirm) => new Promise((resolve) => setAsking({ title, body, confirm, resolve })), []);
  const done = (yes) => {
    asking?.resolve(yes);
    setAsking(null);
  };
  const dialog = (
    <Dialog open={Boolean(asking)} onOpenChange={(open) => { if (!open) done(false); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{asking?.title}</DialogTitle>
          <DialogDescription>{asking?.body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => done(false)}>{t('common.cancel')}</Button>
          <Button type="button" onClick={() => done(true)}>{asking?.confirm ?? t('protection.confirm')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
  return [ask, dialog];
}

// One change at a time, with its failure shown; a change that went ahead runs after().
function useChange(after) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(null);
  const run = async (change) => {
    setBusy(true);
    setProblem(null);
    try {
      const done = await change();
      if (done) await after?.(done);
      return done;
    } catch (error) {
      setProblem(codeOf(error));
      return null;
    } finally {
      setBusy(false);
    }
  };
  return { busy, problem, run };
}

// This project's protection, on the This project page: the same question as at its creation. A
// stricter answer applies at once; a less strict one, and lifting the review lock, ask first.
export function ProjectProtection({ project, onProjectChanged }) {
  const t = useT();
  const [ask, dialog] = useConfirm();
  const { busy, problem, run } = useChange(async () => {
    forgetModels(); // what the model picker offers here follows the protection
    await onProjectChanged();
  });
  const [applying, setApplying] = useState(false); // a stricter answer, until it has applied
  const base = `/api/projects/${project.id}`;
  const choose = (holds) => run(async () => {
    setApplying(tightens(project, holds));
    try {
      if (holds === 'review') return await post(`${base}/review-lock`, { locked: true });
      const level = LEVEL_OF[holds];
      return await confirmedChange('POST', `${base}/sensitivity`, { level },
        () => ask(t('protection.confirmTitle'), t(`protection.loosen.${level}`)));
    } finally {
      setApplying(false);
    }
  });
  const unlock = () => run(() => confirmedChange('POST', `${base}/review-lock`, { locked: false },
    () => ask(t('protection.unlockTitle'), t('protection.unlockBody'))));
  const holds = holdsOf(project);
  return (
    <Section title={t('protection.title')} hint={t('protection.hint')}>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className="inline-flex items-center gap-1.5">
          <ShieldCheck className="size-4 text-brand" aria-hidden="true" />
          {t('protection.current', { level: t(`level.${project.sensitivity}`) })}
        </span>
        {project.review_lock && (
          <span className="inline-flex items-center gap-1.5 text-warning">
            <Lock className="size-3.5" aria-hidden="true" />
            {project.review_venue ? t('protection.lockedVenue', { venue: project.review_venue }) : t('protection.locked')}
          </span>
        )}
      </p>
      <Problem code={problem} />
      {applying && <p role="status" className="text-xs text-muted-foreground">{t('protection.applying')}</p>}
      <HoldsChoice value={holds} onChange={choose} disabled={busy} name="project-holds"
        lockedOut={project.review_lock ? ['own', 'private'] : []} />
      {holds === null && <p className="text-xs text-muted-foreground">{t('protection.localOnlyNow')}</p>}
      {project.review_lock && (
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">{t('protection.lockFirst')}</p>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-56 flex-1">
              <Field label={t('protection.venue')} htmlFor="project-review-venue">
                <CommitField id="project-review-venue" value={project.review_venue} allowEmpty
                  placeholder={t('protection.venuePlaceholder')}
                  onCommit={(venue) => run(() => post(`${base}/review-lock`, { locked: true, venue }))} />
              </Field>
            </div>
            <Button variant="outline" size="sm" className="h-9" disabled={busy} onClick={unlock}>{t('protection.unlock')}</Button>
          </div>
        </div>
      )}
      {dialog}
    </Section>
  );
}

const day = (language, at) => (at ? new Intl.DateTimeFormat(language, { dateStyle: 'medium' }).format(new Date(at)) : '');

// The researcher's confirmation of the OpenRouter key's data settings, which Private projects
// need (section 6.4). Scholia links to the settings and says it cannot check them.
export function KeyConfirmation({ provider, onChanged }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const { busy, problem, run } = useChange(async () => {
    forgetModels();
    await onChanged();
  });
  const confirmation = provider.key_confirmation;
  if (!confirmation) return null;
  const current = confirmation.status === 'current';
  return (
    <section className="mt-5 space-y-2.5 rounded-lg border bg-muted/30 p-3" aria-labelledby={`key-confirmation-${provider.name}`}>
      <h5 id={`key-confirmation-${provider.name}`} className="flex items-center gap-1.5 text-sm font-medium">
        <ShieldCheck className="size-3.5" aria-hidden="true" />{t('keyConfirm.title')}
      </h5>
      <p role="status" className={cn('text-xs', current ? 'text-success' : 'text-warning')}>
        {t(`keyConfirm.status.${confirmation.status}`, { date: day(language, confirmation.expires_at) })}
      </p>
      <div className="space-y-1.5 text-xs leading-relaxed text-muted-foreground">
        <p>{t('privacy.key.intro')}</p>
        <ul className="list-disc space-y-1 pl-5">
          {['logging', 'improvement', 'broadcast'].map((item) => <li key={item}>{t(`privacy.key.${item}`)}</li>)}
        </ul>
        <p>{t('privacy.key.unverifiable')}</p>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        {Object.entries(KEY_SETTINGS).map(([label, href]) => (
          <a key={label} href={href} target="_blank" rel="noreferrer noopener"
            className="inline-flex items-center gap-1 text-brand underline-offset-2 hover:underline">
            {t(`keyConfirm.${label}`)}<ExternalLink className="size-3" aria-hidden="true" />
          </a>
        ))}
        <Button size="sm" variant={current ? 'outline' : 'default'} className="ml-auto h-8" disabled={busy}
          onClick={() => run(() => post('/api/key-attestations', // for the key and the statement this card shows
            { provider: provider.name, statement: confirmation.statement, key: confirmation.key }))}>
          {current ? t('keyConfirm.again') : t('keyConfirm.confirm')}
        </Button>
      </div>
      {problem && <Problem code={problem} />}
    </section>
  );
}

// The researcher's declaration that a server on this Mac runs its models here (section 6.4;
// ticket 64), which Private and Local only projects need to use it.
export function LocalDeclaration({ provider, onChanged }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const { busy, problem, run } = useChange(async () => {
    forgetModels();
    await onChanged();
  });
  if (!provider.local) return null;
  const declared = Boolean(provider.declared_at);
  return (
    <section className="mt-5 space-y-2.5 rounded-lg border bg-muted/30 p-3" aria-labelledby={`declaration-${provider.name}`}>
      <h5 id={`declaration-${provider.name}`} className="flex items-center gap-1.5 text-sm font-medium">
        <ShieldCheck className="size-3.5" aria-hidden="true" />{t('localDeclare.title')}
      </h5>
      <p role="status" className={cn('text-xs', declared ? 'text-success' : 'text-muted-foreground')}>
        {declared ? t('localDeclare.declared', { date: day(language, provider.declared_at) }) : t('localDeclare.undeclared')}
      </p>
      <div className="space-y-1.5 text-xs leading-relaxed text-muted-foreground">
        <p className="font-medium text-foreground">{t('privacy.local.statement')}</p>
        <p>{t('privacy.local.unverifiable')}</p>
        <p>{t('privacy.local.private')}</p>
      </div>
      <div className="flex justify-end">
        <Button size="sm" variant={declared ? 'outline' : 'default'} className="h-8" disabled={busy}
          onClick={() => run(() => (declared ? del(`/api/local-declarations/${encodeURIComponent(provider.name)}`)
            : post('/api/local-declarations', { provider: provider.name, origin: provider.origin })))}>
          {declared ? t('localDeclare.withdraw') : t('localDeclare.declare')}
        </Button>
      </div>
      {problem && <Problem code={problem} />}
    </section>
  );
}

const MODEL = 'openrouter:';

// The Private allowlist under Advanced: the shipped dated entries and the researcher's own, each
// with its terms, the date it was checked (flagged after six months) and its exceptions.
export function PrivateAllowlist() {
  const t = useT();
  const language = useContext(LanguageContext);
  const [list, setList] = useState(null);
  const [read, setRead] = useState(null); // the last read's error
  const [adding, setAdding] = useState('');
  const { busy, problem, run } = useChange((changed) => {
    setList(changed);
    forgetModels();
  });
  const load = useCallback(() => get('/api/private-routes').then((data) => {
    setList(data);
    setRead(null);
  }).catch((error) => setRead(codeOf(error))), []);
  useEffect(() => {
    load();
  }, [load]);
  const change = (key, body) => run(() => put(`/api/private-routes/${encodeURIComponent(key)}`, body));
  if (!list) return <Section title={t('allowlist.title')}><LoadState problem={read} onRetry={load} /></Section>;
  // Shipped dates are days, read as such wherever the researcher is.
  const dates = new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeZone: 'UTC' });
  return (
    <Section title={t('allowlist.title')} hint={t('allowlist.hint', { date: dates.format(new Date(list.checked_on)) })}>
      <Problem code={problem} />
      <ul className="divide-y rounded-lg border">
        {list.routes.map((route) => {
          const model = route.route_key.slice(MODEL.length);
          const name = model === '*' ? t('allowlist.every') : model;
          return (
            <li key={route.route_key} className="space-y-2 px-3 py-3 text-sm">
              <div className="flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <p className={cn('truncate font-medium', model !== '*' && 'font-mono text-[13px]')} title={name}>{name}</p>
                  <p className="text-xs text-muted-foreground">
                    {t(`allowlist.source.${route.source}`)} · {t('allowlist.checked', { date: route.checked_on ? dates.format(new Date(route.checked_on)) : '' })}
                    {route.terms_url && <> · <a href={route.terms_url} target="_blank" rel="noreferrer noopener"
                      className="text-brand underline-offset-2 hover:underline">{t('allowlist.terms')}</a></>}
                  </p>
                </div>
                <Segmented label={t('allowlist.onOff', { route: name })} value={route.enabled ? 'on' : 'off'} disabled={busy}
                  options={[{ value: 'on', label: t('providers.on') }, { value: 'off', label: t('providers.off') }]}
                  onChange={(value) => change(route.route_key, { enabled: value === 'on' })} />
              </div>
              {route.stale && <p className="text-xs text-warning">{t('allowlist.stale')}</p>}
              {route.exceptions.length > 0 && (
                <ul className="list-disc space-y-0.5 pl-5 text-xs leading-relaxed text-muted-foreground">
                  {route.exceptions.map((code) => <li key={code}>{t(`allowlist.exception.${code}`)}</li>)}
                </ul>
              )}
              <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" disabled={busy}
                onClick={() => change(route.route_key, { rechecked: true })}>{t('allowlist.recheck')}</Button>
            </li>
          );
        })}
      </ul>
      <form className="space-y-1.5" onSubmit={async (event) => {
        event.preventDefault();
        if (await change(`${MODEL}${adding.trim()}`, { enabled: true })) setAdding('');
      }}>
        <label htmlFor="allowlist-add" className="text-sm font-medium">{t('allowlist.addLabel')}</label>
        <div className="flex gap-2">
          <Input id="allowlist-add" value={adding} onChange={(event) => setAdding(event.target.value)}
            placeholder={t('allowlist.addPlaceholder')} className="h-9 font-mono text-[13px]" spellCheck={false} />
          <Button type="submit" variant="outline" size="sm" className="h-9" disabled={busy || !adding.trim()}>{t('allowlist.add')}</Button>
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">{t('allowlist.addHint')}</p>
      </form>
    </Section>
  );
}

const PAGE = 50;

// The audit log under Advanced, for this project or everything, newest first; export, and
// clearing, which asks first and leaves a record.
export function AuditLog({ project }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const [scope, setScope] = useState(project ? 'project' : 'all');
  const [page, setPage] = useState(null); // { entries, next, allowed_off_this_mac }
  const [read, setRead] = useState(null);
  const [exported, setExported] = useState(null);
  const [ask, dialog] = useConfirm();
  const projectId = scope === 'project' ? project?.id : null;
  const fetchPage = useCallback((before) => {
    const query = new URLSearchParams({ limit: String(PAGE), ...(projectId ? { project_id: projectId } : {}),
      ...(before ? { before: String(before) } : {}) });
    return get(`/api/audit?${query}`);
  }, [projectId]);
  const reads = useRef(0); // each fresh read of the log; a page asked for under an earlier one is dropped
  const load = useCallback(() => {
    const mine = ++reads.current;
    return fetchPage().then((data) => {
      if (mine !== reads.current) return;
      setPage(data);
      setRead(null);
    }).catch((error) => { if (mine === reads.current) setRead(codeOf(error)); });
  }, [fetchPage]);
  const { busy, problem, run } = useChange(load);
  useEffect(() => {
    setPage(null);
    load();
  }, [load]);
  const older = () => run(async () => {
    const mine = reads.current;
    const next = await fetchPage(page.next);
    if (mine === reads.current) { // still the same scope and read: the older page continues it
      setPage((current) => current && ({ ...next, entries: [...current.entries, ...next.entries] }));
    }
    return false; // nothing changed to read again
  });
  const exportLog = () => run(async () => {
    const done = await post('/api/audit/export', projectId ? { project_id: projectId } : {});
    setExported(done.path);
    return done;
  });
  const clear = () => run(() => confirmedChange('DELETE', '/api/audit', undefined,
    () => ask(t('audit.clearTitle'), t('audit.clearBody'), t('audit.clearConfirm'))));
  const dates = new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' });
  return (
    <Section title={t('audit.title')} hint={t('audit.hint')}>
      <div className="flex flex-wrap items-center gap-2">
        {project && (
          <Segmented label={t('audit.show')} value={scope} onChange={setScope}
            options={[{ value: 'project', label: t('audit.thisProject') }, { value: 'all', label: t('audit.everything') }]} />
        )}
        <div className="ml-auto flex gap-2">
          <Button size="sm" variant="outline" className="h-8" disabled={busy} onClick={exportLog}>{t('audit.export')}</Button>
          <Button size="sm" variant="outline" className="h-8 text-destructive hover:text-destructive" disabled={busy}
            onClick={clear}>{t('audit.clear')}</Button>
        </div>
      </div>
      <Problem code={problem} />
      {exported && <p role="status" className="break-all text-xs text-muted-foreground">{t('audit.exported', { path: exported })}</p>}
      {!page ? <LoadState problem={read} onRetry={load} /> : (
        <div className="space-y-2">
          {page.allowed_off_this_mac != null && <p className="text-xs">{t('audit.sent', { count: page.allowed_off_this_mac })}</p>}
          {page.entries.length === 0 && <p className="text-sm text-muted-foreground">{t('audit.empty')}</p>}
          {page.entries.length > 0 && (
            <ol className="scroll-thin max-h-96 divide-y overflow-y-auto rounded-lg border">
              {page.entries.map((entry) => <AuditEntry key={entry.seq} entry={entry} dates={dates} />)}
            </ol>
          )}
          {page.next && <Button variant="ghost" size="sm" disabled={busy} onClick={older}>{t('audit.more')}</Button>}
        </div>
      )}
      {dialog}
    </Section>
  );
}

function AuditEntry({ entry, dates }) {
  const t = useT();
  const event = auditEvent(t, entry);
  const at = dates.format(new Date(entry.at));
  const detail = auditDetail(t, entry, dates);
  return (
    <li className="grid gap-0.5 px-3 py-2 text-sm">
      <div className="flex items-baseline justify-between gap-3">
        <span className="truncate font-medium">{event}</span>
        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{at}</span>
      </div>
      {detail && <p className="break-all font-mono text-[11px] text-muted-foreground">{detail}</p>}
    </li>
  );
}

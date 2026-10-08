// i18n: migrated
// The conversation (S5): its turns, the live turn with Stop, Continue after an
// interruption, a limit or a project change, and the message box. Model output is shown
// as Markdown with no raw HTML; its images become links (links.js). Files attached here are
// added to the project's Library, and the questions their lookups ask are shown here (F3a,
// section 6.2: a confirmation appears where its work started).
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import { ArrowUp, BookOpen, FileText, Loader2, PanelLeftOpen, Paperclip, RotateCw, Square } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, get, post } from '../api.js';
import { clear, send, stop, unsavedAnswer, useLiveTurn } from '../live.js';
import { imageAsLink, safeHref } from '../links.js';
import { errorText, money } from '../text.js';
import { continuable, conversationTitle } from '../projects.js';
import { ModelPicker, currentChoice, readChoice } from './ModelPicker.jsx';
import { messageRoute } from '../settings.js';
import { ACCEPT, libraryChanged } from '../library.js';
import { addTo } from './Library.jsx';
import { Ask } from './Ask.jsx';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const POLL_MS = 1000; // between reads while a turn is settling, and after a failed read
const ASKS_MS = 1500; // between reads of this conversation's questions, while files attached here are looked up

// The questions raised by work started in this conversation, read again while that work goes on.
function useConversationAsks(conversationId, watching) {
  const [asks, setAsks] = useState([]);
  const [working, setWorking] = useState(false); // work started here still runs
  const [reads, setReads] = useState(0);
  const load = useCallback(async () => {
    if (!conversationId) return;
    try {
      const found = await get(`/api/asks?conversation_id=${encodeURIComponent(conversationId)}`);
      setAsks(found.asks);
      setWorking(found.working > 0);
    } catch {
      // read again at the next turn
    } finally {
      setReads((n) => n + 1);
    }
  }, [conversationId]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!watching && !working && !asks.length) return undefined;
    const timer = setTimeout(load, ASKS_MS);
    return () => clearTimeout(timer);
  }, [watching, working, asks.length, reads, load]);
  return { asks, load };
}

export function ConversationView({ conversation, projectId, panel, showSidebarButton, onShowSidebar, onPanel, onCreated,
  onLibrary }) {
  const t = useT();
  const [draft, setDraft] = useState(null); // a new conversation, made at its first message
  const id = conversation?.id ?? draft;
  const live = useLiveTurn(id);
  const [turns, setTurns] = useState(conversation ? null : []);
  const [failed, setFailed] = useState(false);
  const [problem, setProblem] = useState(null);
  const [reads, setReads] = useState(0); // each read, failed or not, so polling goes on
  const end = useRef(null);
  const [attached, setAttached] = useState(null); // what the last attach said, and the lookup it started
  const { asks, load: loadAsks } = useConversationAsks(conversation?.id, attached?.lookup);
  const here = useRef(false); // still shown: a draft admitted after the researcher left selects nothing
  useEffect(() => {
    here.current = true;
    return () => { here.current = false; };
  }, []);

  const load = useCallback(async () => { // whether the conversation was read
    if (!id) return false;
    try {
      setTurns((await get(`/api/conversations/${id}`)).turns);
      setFailed(false);
      return true;
    } catch {
      setFailed(true);
      return false;
    } finally {
      setReads((n) => n + 1);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  // A finished live turn hands over to its saved record once that is read, and a saved turn
  // that still reads running (its stream ended early, or it is stopping) is read again until
  // it settles. A failed read is tried again after a pause.
  const handoff = Boolean(live?.done);
  const waiting = handoff || (!live && (turns ?? []).some((turn) => turn.status === 'running'));
  useEffect(() => {
    if (!waiting) return undefined;
    const timer = setTimeout(async () => {
      if ((await load()) && handoff) clear(id);
    }, handoff && !failed ? 0 : POLL_MS);
    return () => clearTimeout(timer);
  }, [waiting, handoff, failed, reads, id, load]);

  useEffect(() => {
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    end.current?.scrollIntoView({ block: 'end', behavior: still ? 'auto' : 'smooth' });
  }, [turns, live?.answer, live?.text]);

  async function submit(text) {
    setProblem(null);
    try {
      const target = id ?? (await post('/api/conversations', { project_id: projectId })).id;
      setDraft(target); // shown here from now on, so Stop works while it is admitted
      await readChoice(); // the model chosen at an earlier launch, before anything is sent
      await send(target, `/api/conversations/${target}/message/stream`, { content: text, ...messageRoute(currentChoice()) }, text);
      if (!conversation && here.current) onCreated(target);
      return true;
    } catch (error) {
      setProblem(errorText(t, error instanceof ApiError ? error.code : 'internal'));
      return false;
    }
  }

  // Attached files join the project's Library, named as coming from this conversation, so the
  // question their lookup may ask is shown here. Before its first message a conversation does not
  // exist yet: the files are added as from the Library, which opens to show them.
  async function attach(files) {
    setProblem(null);
    setAttached(null);
    const notes = [];
    const result = await addTo(projectId, files, t, (text) => notes.push(text), id ? { conversationId: id } : {});
    if (result) {
      libraryChanged(projectId);
      notes.unshift(t('conversation.attached', { count: result.materials.length }));
      if (!id) onLibrary?.();
    }
    if (notes.length) setAttached({ text: notes.join(' '), lookup: id && result ? result.lookup_run_id : null });
    if (result && id) setTimeout(loadAsks, 300);
  }

  // The watch on an attached batch's lookup ends with it.
  useEffect(() => {
    if (!attached?.lookup) return undefined;
    let live = true;
    const timer = setInterval(async () => {
      try {
        const [row] = (await get(`/api/activity?run_id=${encodeURIComponent(attached.lookup)}`)).runs;
        if (live && row && row.status !== 'running') setAttached((current) => current && { ...current, lookup: null });
      } catch {
        // looked at again at the next turn
      }
    }, ASKS_MS);
    return () => { live = false; clearInterval(timer); };
  }, [attached?.lookup]);

  async function resume(turn) {
    setProblem(null);
    try {
      await readChoice();
      await send(id, `/api/runs/${turn.run_id}/continue`, messageRoute(currentChoice()), turn.message?.text ?? '');
    } catch (error) {
      setProblem(errorText(t, error instanceof ApiError ? error.code : 'internal'));
    }
  }

  // An answer shown but not saved stays shown, marked unsaved, for as long as the window is open.
  const shown = (turns ?? []).filter((turn) => !(live && !live.done && turn.status === 'running')).map((turn) => {
    const unsaved = !turn.answer && unsavedAnswer(turn.run_id);
    return unsaved ? { ...turn, answer: { text: unsaved }, result_saved: false } : turn;
  });
  const running = Boolean(live && !live.done) || shown.some((turn) => turn.status === 'running');
  const latest = shown.at(-1);
  const saved = live?.done && shown.some((turn) => turn.run_id === live.runId);

  return (
    <section className="flex h-full flex-col" aria-label={conversationTitle(t, conversation)}>
      <header className="flex h-12 shrink-0 items-center gap-1 border-b px-3">
        {showSidebarButton && (
          <Button variant="ghost" size="icon" className="size-8" onClick={onShowSidebar} aria-label={t('sidebar.show')}>
            <PanelLeftOpen aria-hidden="true" />
          </Button>
        )}
        <h1 className="min-w-0 flex-1 truncate px-1 text-sm font-medium" title={conversation ? conversationTitle(t, conversation) : undefined}>
          {conversation ? conversationTitle(t, conversation) : ''}
        </h1>
        <PanelButton icon={BookOpen} label={t('panel.library')} pressed={panel === 'library'} onClick={(event) => onPanel('library', event.currentTarget)} />
        <PanelButton icon={FileText} label={t('panel.manuscript')} pressed={panel === 'manuscript'} onClick={(event) => onPanel('manuscript', event.currentTarget)} />
      </header>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-5 py-6">
          {failed && <p role="alert" className="text-sm text-destructive">{t('conversation.loadFailed')}</p>}
          {turns === null && !failed && <p className="text-sm text-muted-foreground" role="status">{t('common.loading')}</p>}
          {turns !== null && shown.length === 0 && !live && <Empty />}
          <ol className="space-y-8">
            {shown.map((turn) => (
              <Turn key={turn.run_id} turn={turn}
                onContinue={turn === latest && continuable(turn) && !running ? () => resume(turn) : null} />
            ))}
            {live && !saved && <LiveTurn turn={live} />}
          </ol>
          <div ref={end} />
        </div>
      </div>

      {(asks.length > 0 || attached) && (
        <div className="mx-auto grid w-full max-w-3xl shrink-0 gap-2 px-4 pb-2">
          {attached && (
            <p role="status" className="flex flex-wrap items-center gap-x-2 text-sm text-muted-foreground">
              {attached.text}
              {onLibrary && <button type="button" onClick={onLibrary}
                className="rounded-sm text-brand underline-offset-2 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
                {t('conversation.showLibrary')}</button>}
            </p>
          )}
          {asks.map((ask) => <Ask key={ask.ask_id} ask={ask} onAnswered={loadAsks} />)}
        </div>
      )}
      <Composer running={running} problem={problem} onSend={submit} projectId={projectId} onAttach={attach}
        onStop={() => Promise.resolve(stop(id, live?.runId ?? shown.find((turn) => turn.status === 'running')?.run_id))
          .then(load).catch(() => {})} />
    </section>
  );
}

function PanelButton({ icon: Icon, label, pressed, onClick }) {
  return (
    <Button variant="ghost" size="sm" aria-pressed={pressed} onClick={onClick}
      className={cn('h-8 gap-1.5 text-muted-foreground', pressed && 'bg-accent text-foreground')}>
      <Icon aria-hidden="true" /><span className="hidden sm:inline">{label}</span>
    </Button>
  );
}

function Empty() {
  const t = useT();
  return (
    <div className="mx-auto mt-[12vh] max-w-md animate-fade-up text-center">
      <h2 className="text-2xl font-semibold tracking-tight">{t('conversation.emptyTitle')}</h2>
      <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{t('conversation.emptyBody')}</p>
    </div>
  );
}

function Question({ text }) {
  const t = useT();
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-muted px-4 py-2.5 text-[15px] leading-relaxed">
        <span className="sr-only">{t('conversation.you')}: </span>{text}
      </div>
    </div>
  );
}

function Working() {
  const t = useT();
  return (
    <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
      <Loader2 className="size-4 animate-spin text-brand" aria-hidden="true" />{t('turn.working')}
    </p>
  );
}

function Turn({ turn, onContinue }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const cost = turn.cost_usd > 0 ? money(turn.cost_usd, language) : null;
  const note = {
    interrupted: t('turn.interrupted'),
    // an answer that was shown but not saved says so below it instead
    failed: turn.answer && turn.reason_code === 'save_failed' ? null : t('turn.failed', { reason: errorText(t, turn.reason_code) }),
  }[turn.status] ?? (turn.status === 'cancelled'
    ? { limit: t('turn.limit'), revoked: t('turn.revoked') }[turn.cancel_reason] ?? t('turn.stopped')
    : null);
  return (
    <li className="space-y-3">
      <Question text={turn.message?.text ?? ''} />
      {turn.status === 'running' && <Working />}
      {turn.answer?.text && <Answer text={turn.answer.text} />}
      {turn.answer && !turn.result_saved && <p className="text-xs text-warning">{t('turn.unsaved')}</p>}
      {(note || cost || onContinue) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
          {note && <span className={cn(turn.status === 'failed' && 'text-destructive')}>{note}</span>}
          {cost && <span>{t('common.cost', { cost })}</span>}
          {onContinue && (
            <Button size="sm" variant="outline" className="h-7" onClick={onContinue}>
              <RotateCw aria-hidden="true" />{t('turn.continue')}
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

function LiveTurn({ turn }) {
  const t = useT();
  return (
    <li className="space-y-3">
      <Question text={turn.text} />
      {turn.answer ? <Answer text={turn.answer} /> : !turn.done && <Working />}
      {turn.answer && turn.resultSaved === false && <p className="text-xs text-warning">{t('turn.unsaved')}</p>}
      {turn.error && <p className="text-xs text-destructive">{t('turn.failed', { reason: errorText(t, turn.error) })}</p>}
      {turn.limit && <p className="text-xs text-muted-foreground">{t('turn.limit')}</p>}
    </li>
  );
}

function Answer({ text }) {
  const t = useT();
  return (
    <div className="prose-answer break-words" aria-label={t('conversation.scholia')}>
      <Markdown skipHtml components={{
        a: ({ href, children }) => {
          const safe = safeHref(href);
          return safe ? <a href={safe} target="_blank" rel="noreferrer noopener">{children}</a> : <span>{children}</span>;
        },
        img: ({ src, alt }) => { // a placeholder with its alt text and host (PR02A), never loaded
          const { href, label, host } = imageAsLink(src, alt);
          const text = label ? t('turn.image', { label }) : t('turn.imageUnnamed');
          return href ? (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {text} <span className="text-muted-foreground">{t('turn.imageFrom', { host })}</span>
            </a>
          ) : <span>{text}</span>;
        },
      }}>{text}</Markdown>
    </div>
  );
}

function Composer({ running, problem, onSend, onStop, projectId, onAttach }) {
  const t = useT();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const box = useRef(null);
  const files = useRef(null);

  useEffect(() => {
    const area = box.current;
    if (!area) return;
    area.style.height = 'auto';
    area.style.height = `${Math.min(area.scrollHeight, 240)}px`;
  }, [text]);

  async function submit(event) {
    event?.preventDefault();
    if (running || busy || !text.trim()) return;
    setBusy(true);
    const sent = text;
    setText('');
    if (!(await onSend(sent))) setText(sent);
    setBusy(false);
    box.current?.focus();
  }

  function key(event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  }

  return (
    <form onSubmit={submit} className="shrink-0 px-4 pb-4">
      <div className="mx-auto w-full max-w-3xl">
        {problem && <p role="alert" className="mb-2 text-sm text-destructive">{problem}</p>}
        <div className="rounded-2xl border bg-card p-2 shadow-xs transition-shadow focus-within:border-brand/50 focus-within:shadow-md focus-within:shadow-brand/5">
          <label htmlFor="composer" className="sr-only">{t('composer.label')}</label>
          <textarea id="composer" ref={box} rows={1} value={text} placeholder={t('composer.placeholder')}
            onChange={(event) => setText(event.target.value)} onKeyDown={key}
            className="scroll-thin block max-h-60 min-h-9 w-full resize-none bg-transparent px-2 py-1.5 text-[15px] leading-relaxed outline-hidden placeholder:text-muted-foreground" />
          <div className="mt-1 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1">
            <input ref={files} type="file" multiple accept={ACCEPT} className="hidden" aria-hidden="true" tabIndex={-1}
              data-testid="composer-files" onChange={(event) => { onAttach(event.target.files); event.target.value = ''; }} />
            <Button type="button" variant="ghost" size="icon" className="size-8 shrink-0 text-muted-foreground"
              disabled={!projectId} onClick={() => files.current?.click()} aria-label={t('composer.attach')} title={t('composer.attach')}>
              <Paperclip aria-hidden="true" />
            </Button>
            <ModelPicker projectId={projectId} />
          </div>
          {running ? (
            <Button type="button" size="icon" variant="secondary" className="size-9 shrink-0 rounded-xl" onClick={onStop} aria-label={t('composer.stop')}>
              <Square className="fill-current" aria-hidden="true" />
            </Button>
          ) : (
            <Button type="submit" size="icon" className="size-9 shrink-0 rounded-xl bg-brand text-brand-foreground hover:bg-brand/90"
              disabled={busy || !text.trim()} aria-label={t('composer.send')}>
              <ArrowUp aria-hidden="true" />
            </Button>
          )}
          </div>
        </div>
        <p className="mt-1.5 px-2 text-[11px] text-muted-foreground">{t('composer.hint')}</p>
      </div>
    </form>
  );
}

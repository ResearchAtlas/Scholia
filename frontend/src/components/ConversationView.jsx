// i18n: migrated
// The conversation (S5): its turns, the live turn with Stop, Continue after an
// interruption, a limit or a project change, and the message box. Model output is shown
// as Markdown with no raw HTML; its images become links (links.js). Files attached here are
// added to the project's Library, and the questions their lookups ask are shown here (F3a,
// section 6.2: a confirmation appears where its work started). S1-19b: an answer's citations are chips
// that open the paper at its passage, its notices say what its retrieval and checks could not do, and
// Details shows its trace; papers chosen in the Library or with @ go with the next message.
import { useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { LeftOff, Markdown, Trace, markdownReady } from './Parts.jsx';
import { ArrowUp, BookOpen, FileText, Loader2, PanelLeftOpen, Paperclip, RotateCw, Square, X } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, get, post } from '../api.js';
import { NOTICES, clear, limitText, send, stop, unsavedAnswer, useLiveTurn } from '../live.js';
import { errorText, money, toolName } from '../text.js';
import { continuable, conversationTitle } from '../projects.js';
import { ModelPicker, currentChoice, readChoice } from './ModelPicker.jsx';
import { messageRoute } from '../settings.js';
import { ACCEPT, afterAsksRead, asksChanged, choosePapers, chosenPapers, followAsks, libraryChanged, libraryEvents, newest, NO_ASKS, openPaperAt,
  readGuard, readGuardedAsks, pollsAsks, watchChosen } from '../library.js';
import { addTo } from './Library.jsx';
import { actionLines, composerFill } from '../memory.js'; // S1-21a
import { Ask } from './Ask.jsx';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const POLL_MS = 1000; // between reads while a turn is settling, and after a failed read
const ASKS_MS = 1500; // between reads of this conversation's questions, while files attached here are looked up

// The questions raised by work started in this conversation, read again while that work goes on, or
// until a read succeeds after one failed (afterRead, pollsAsks). Only the newest read's answer is
// shown, and a change of conversation clears what was shown. S1-19c: a library change drops every question shown
// and every read asked before it, as for the conversation's own turns (readGuardedAsks).
function useConversationAsks(conversationId, watching, projectId) {
  const [known, setKnown] = useState(NO_ASKS);
  const [reads, setReads] = useState(0);
  const [asked] = useState(newest);
  const guard = useRef(null);
  if (guard.current === null) guard.current = readGuard();
  const load = useCallback(() => readGuardedAsks(guard.current, asked, conversationId, (found, changed) => {
    setKnown((current) => afterAsksRead(current, found, changed));
    setReads((n) => n + 1);
  }), [conversationId, asked]);
  useEffect(() => { setKnown(NO_ASKS); load(); }, [load]);
  useEffect(() => {
    const changed = (event) => {
      if (event.detail !== null && event.detail !== projectId) return;
      guard.current.libraryChanged(); // first: a read asked before this is dropped when it arrives
      setKnown({ ...NO_ASKS, unsure: true }); // nothing shown until a read succeeds; looked at again meanwhile
      load();
    };
    libraryEvents.addEventListener('changed', changed);
    return () => libraryEvents.removeEventListener('changed', changed);
  }, [projectId, load]);
  useEffect(() => followAsks(conversationId, load), [conversationId, load]); // files attached here were added
  const polls = pollsAsks(known, watching);
  useEffect(() => {
    if (!polls) return undefined;
    const timer = setTimeout(load, ASKS_MS);
    return () => clearTimeout(timer);
  }, [polls, reads, load]);
  return { asks: known.asks, load };
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
  // Read for the conversation attach() tags its files with: a draft's too, before the window knows it.
  const { asks, load: loadAsks } = useConversationAsks(id, attached?.lookup, projectId);
  const here = useRef(false); // still shown: a draft admitted after the researcher left selects nothing
  useEffect(() => {
    here.current = true;
    return () => { here.current = false; };
  }, []);

  // Each library change starts a new generation: a read asked before it is dropped when it arrives, and
  // until a read of the new one succeeds nothing read before it is shown (stale), its failure saying so.
  const guard = useRef(null); // which reads may be shown (library.js readGuard)
  if (guard.current === null) guard.current = readGuard();
  const [stale, setStale] = useState(false);
  const load = useCallback(async () => { // whether the conversation was read
    if (!id) return false;
    const settle = guard.current.ask();
    try {
      // The Markdown renderer loads beside the first read, so answers show formatted with their turns.
      const [read] = await Promise.all([get(`/api/conversations/${id}`), markdownReady()]);
      if (!settle(true)) return false; // asked before the library changed: never shown
      setTurns(read.turns);
      setFailed(false);
      setStale(false);
      return true;
    } catch {
      if (settle(false)) setFailed(true);
      return false;
    } finally {
      setReads((n) => n + 1);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);
  // A paper deleted changes what this conversation's citations may show (Harold, 2026-10-21: no client-held
  // derived state): everything read before is dropped at once and read again, so a deleted paper's text never
  // stays on screen (backend/provenance.py); a failed read is tried again after a pause.
  useEffect(() => {
    const changed = (event) => {
      if ((event.detail !== projectId && event.detail !== null) || !id) return; // a conversation not yet created has read nothing
      guard.current.libraryChanged(); // first: every read asked before this is dropped when it arrives
      setTurns(null);
      setStale(true);
      setFailed(false);
      load();
    };
    libraryEvents.addEventListener('changed', changed); // a memory change too: a record its turns read back
    return () => libraryEvents.removeEventListener('changed', changed);
  }, [projectId, load]);
  useEffect(() => {
    if (!(stale && failed)) return undefined;
    const timer = setTimeout(load, POLL_MS);
    return () => clearTimeout(timer);
  }, [stale, failed, reads, load]);

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
      const papers = chosenPapers(projectId);
      await send(target, `/api/conversations/${target}/message/stream`, { content: text, ...messageRoute(currentChoice()),
        ...(papers.length ? { material_ids: papers.map((paper) => paper.id) } : {}) }, text);
      if (papers.length) choosePapers(projectId, []); // they went with this message
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
    if (result && id) asksChanged(id); // the view showing it now reads its questions, this one or its successor
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
    return unsaved ? { ...turn, answer: unsaved, result_saved: false } : turn;
  });
  const running = Boolean(live && !live.done) || shown.some((turn) => turn.status === 'running');
  const latest = shown.at(-1);
  const saved = live?.done && shown.some((turn) => turn.run_id === live.runId);
  // A cited paper opens in the Library panel at its passage.
  const openCited = (citation) => {
    if (!citation.material_id) return;
    openPaperAt(projectId, citation.material_id, citation.passage_id ? { id: citation.passage_id, ordinal: citation.ordinal } : null);
    if (panel !== 'library') onPanel('library');
  };

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
          {failed && <p role="alert" className="text-sm text-destructive">{t(stale ? 'conversation.reloadFailed' : 'conversation.loadFailed')}</p>}
          {turns === null && !failed && <p className="text-sm text-muted-foreground" role="status">{t(stale ? 'conversation.reloading' : 'common.loading')}</p>}
          {projectId && <LeftOff projectId={projectId} />}
          {turns !== null && shown.length === 0 && !live && <Empty />}
          <ol className="space-y-8">
            {shown.map((turn) => (
              <Turn key={turn.run_id} turn={turn} onOpen={openCited}
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

function Working({ tool }) {
  const t = useT();
  return (
    <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
      <Loader2 className="size-4 animate-spin text-brand" aria-hidden="true" />
      {tool ? t('turn.usingTool', { tool: toolName(t, tool) }) : t('turn.working')}
    </p>
  );
}

function Turn({ turn, onContinue, onOpen }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const [details, setDetails] = useState(false);
  const cost = turn.cost_usd > 0 ? money(turn.cost_usd, language) : null;
  const note = {
    interrupted: t('turn.interrupted'),
    // an answer that was shown but not saved says so below it instead
    failed: turn.answer && turn.reason_code === 'save_failed' ? null : t('turn.failed', { reason: errorText(t, turn.reason_code) }),
  }[turn.status] ?? (turn.status === 'cancelled'
    ? { limit: t(limitText(turn.reason_code)), revoked: t('turn.revoked') }[turn.cancel_reason] ?? t('turn.stopped')
    : null);
  return (
    <li className="space-y-3">
      <Question text={turn.message?.text ?? ''} />
      <MemoryNotes items={turn.memory} />
      {turn.status === 'running' && <Working />}
      {turn.answer?.text && <Answer text={turn.answer.text} citations={turn.result_saved ? turn.citations ?? [] : null} onOpen={onOpen} />}
      {turn.answer && !turn.result_saved && <p className="text-xs text-warning">{t('turn.unsaved')}</p>}
      {turn.answer?.cut && <p className="text-xs text-muted-foreground">{t('turn.cut')}</p>}
      <Notices notices={turn.answer?.notices} />
      {(note || cost || onContinue || turn.answer) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
          {note && <span className={cn(turn.status === 'failed' && 'text-destructive')}>{note}</span>}
          {cost && <span>{t('common.cost', { cost })}</span>}
          {turn.answer && turn.status !== 'running' && (
            <button type="button" aria-expanded={details} onClick={() => setDetails((open) => !open)}
              className="rounded-sm underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
              {t('turn.details')}</button>
          )}
          {onContinue && (
            <Button size="sm" variant="outline" className="h-7" onClick={onContinue}>
              <RotateCw aria-hidden="true" />{t('turn.continue')}
            </Button>
          )}
        </div>
      )}
      {details && <Trace turn={turn} />}
    </li>
  );
}

// S1-21a: what the turn's memory command or the agent's note did, read back from the record (memory.js).
function MemoryNotes({ items }) {
  const t = useT();
  const lines = (items ?? []).flatMap((item) => actionLines(t, item));
  if (!lines.length) return null;
  return (
    <ul className="flex flex-col items-end gap-0.5 text-xs text-muted-foreground" aria-label={t('memory.title')}>
      {lines.map((line, i) => <li key={i} className="max-w-[85%] break-words">{line}</li>)}
    </ul>
  );
}

// What the answer could not do, in words (live.js NOTICES).
function Notices({ notices }) {
  const t = useT();
  const shown = (notices ?? []).filter((notice) => NOTICES[notice]);
  if (!shown.length) return null;
  return (
    <ul className="space-y-0.5 text-xs text-muted-foreground">
      {shown.map((notice) => <li key={notice} className={cn(notice === 'retracted' && 'text-destructive')}>{t(NOTICES[notice])}</li>)}
    </ul>
  );
}

function LiveTurn({ turn }) {
  const t = useT();
  return (
    <li className="space-y-3">
      <Question text={turn.text} />
      <MemoryNotes items={turn.memory} />
      {turn.answer ? <Answer text={turn.answer} /> : !turn.done && <Working tool={turn.tool} />}
      {turn.answer && turn.resultSaved === false && <p className="text-xs text-warning">{t('turn.unsaved')}</p>}
      {turn.answer && turn.cut && <p className="text-xs text-muted-foreground">{t('turn.cut')}</p>}
      {turn.answer && <Notices notices={turn.notices} />}
      {turn.error && <p className="text-xs text-destructive">{t('turn.failed', { reason: errorText(t, turn.error) })}</p>}
      {turn.limit && <p className="text-xs text-muted-foreground">{t(limitText(turn.limit))}</p>}
    </li>
  );
}

function Answer({ text, citations = null, onOpen }) {
  const t = useT();
  return (
    <div className="prose-answer break-words" aria-label={t('conversation.scholia')}>
      <Markdown text={text} citations={citations} t={t} onOpen={onOpen} />
    </div>
  );
}

function Composer({ running, problem, onSend, onStop, projectId, onAttach }) {
  const t = useT();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const box = useRef(null);
  const files = useRef(null);
  const chosen = useSyncExternalStore(watchChosen, () => chosenPapers(projectId));
  const mention = useMention(projectId, text);
  useEffect(() => { // S1-21a: Where we left off's next step, put in the box; the researcher sends it
    const fill = (event) => { setText(event.detail); box.current?.focus(); };
    composerFill.addEventListener('fill', fill);
    return () => composerFill.removeEventListener('fill', fill);
  }, []);

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
    if (mention.key(event, (paper) => { setText(mention.without(text)); choosePapers(projectId, [...chosen, paper]); })) return;
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  }

  return (
    <form onSubmit={submit} className="shrink-0 px-4 pb-4">
      <div className="mx-auto w-full max-w-3xl">
        {problem && <p role="alert" className="mb-2 text-sm text-destructive">{problem}</p>}
        <div className="relative rounded-2xl border bg-card p-2 shadow-xs transition-shadow focus-within:border-brand/50 focus-within:shadow-md focus-within:shadow-brand/5">
          {chosen.length > 0 && (
            <ul className="flex flex-wrap gap-1 px-1 pb-1" aria-label={t('composer.chosen')}>
              {chosen.map((paper) => (
                <li key={paper.id} className="flex max-w-60 items-center gap-1 rounded-full bg-brand-soft py-0.5 pl-2 pr-1 text-xs text-brand">
                  <span className="truncate" title={paper.title}>{paper.title}</span>
                  <button type="button" aria-label={t('composer.unchoose', { title: paper.title })}
                    onClick={() => choosePapers(projectId, chosen.filter((other) => other.id !== paper.id))}
                    className="rounded-full p-0.5 hover:bg-brand/15 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
                    <X className="size-3" aria-hidden="true" /></button>
                </li>
              ))}
            </ul>
          )}
          {mention.open && (
            <ul id="composer-mentions" role="listbox" aria-label={t('composer.mentions')}
              className="absolute bottom-full left-2 right-2 z-20 mb-1 max-h-60 overflow-y-auto rounded-md border bg-popover p-1 text-sm shadow-md">
              {mention.papers.length === 0 && <li className="px-2 py-1.5 text-muted-foreground">{t('composer.noMentions')}</li>}
              {mention.papers.map((paper, i) => (
                <li key={paper.id} id={`composer-mention-${i}`} role="option" aria-selected={i === mention.active}
                  onMouseDown={(event) => { event.preventDefault(); setText(mention.without(text)); choosePapers(projectId, [...chosen, paper]); }}
                  className={cn('cursor-pointer truncate rounded-sm px-2 py-1.5', i === mention.active && 'bg-accent')}>{paper.title}</li>
              ))}
            </ul>
          )}
          <label htmlFor="composer" className="sr-only">{t('composer.label')}</label>
          <textarea id="composer" ref={box} rows={1} value={text} placeholder={t('composer.placeholder')}
            role={mention.open ? 'combobox' : undefined} aria-expanded={mention.open ? true : undefined}
            aria-controls={mention.open ? 'composer-mentions' : undefined} aria-autocomplete={mention.open ? 'list' : undefined}
            aria-activedescendant={mention.open && mention.papers.length ? `composer-mention-${mention.active}` : undefined}
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

// S1-19b: an @ in the message box offers the project's papers by title; the one chosen goes with the
// message (its id), and the @ and what followed it are taken out of the text.
const MENTION = /(^|\s)@([^\s@]*)$/u;

function useMention(projectId, text) {
  const [read, setRead] = useState(null); // { projectId, papers }: the project's papers, read when an @ is first typed
  const [picked, setPicked] = useState({ query: '', index: 0 }); // the highlighted paper, for the query it was moved at
  const [closed, setClosed] = useState(null); // the text an Escape closed the list at
  const typed = projectId && closed !== text ? MENTION.exec(text) : null;
  const papers = read?.projectId === projectId ? read.papers : null;
  const wanted = Boolean(typed) && !papers;
  useEffect(() => {
    if (!wanted) return undefined;
    let live = true;
    get(`/api/projects/${encodeURIComponent(projectId)}/materials`).then(
      (found) => live && setRead({ projectId, papers: found.materials.map((m) => ({ id: m.id, title: m.title })) }),
      () => live && setRead({ projectId, papers: [] }));
    return () => { live = false; };
  }, [wanted, projectId]);
  const query = typed?.[2].toLocaleLowerCase() ?? '';
  const matching = (papers ?? []).filter((paper) => (paper.title ?? '').toLocaleLowerCase().includes(query)).slice(0, 8);
  const open = Boolean(typed && papers);
  const active = picked.query === query && picked.index < matching.length ? picked.index : 0;
  return {
    open, papers: matching, active,
    without: (current) => current.replace(MENTION, '$1'),
    key(event, choose) { // whether the list took the key
      if (!open) return false;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        setPicked({ query, index: matching.length ? (active + step + matching.length) % matching.length : 0 });
        return true;
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && matching.length && !event.nativeEvent.isComposing) {
        event.preventDefault();
        choose(matching[active]);
        return true;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        setClosed(text);
        return true;
      }
      return false;
    },
  };
}

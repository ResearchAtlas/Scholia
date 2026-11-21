// i18n: migrated
// The window (slice-1 spec section 3): the sidebar, the conversation, and the side panel,
// with resizable, remembered dividers. Under 1,000 pixels the sidebar becomes a drawer, and
// when the conversation and the panel do not both fit, the panel slides over the
// conversation. The layout is kept in [ui.layout]; each project keeps its open panel. Files
// dropped anywhere on the window are added to the open project, whose Library then opens (F3a).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as Drawer from '@radix-ui/react-dialog';
import { useT } from '../i18n/index.js';
import { ApiError, get, saveSettings } from '../api.js';
import { DEFAULTS, LIMITS, clamp, columns, fromSettings, panelShareAt, toSettings } from '../layout.js';
import { errorText } from '../text.js';
import { deleted, forget, forgotten, places, recheck, subscribe } from '../live.js';
import { libraryChanged, newest } from '../library.js';
import { generation, useRead } from '../libraryRead.js';
import { projectName } from '../projects.js';
import { addTo } from './Library.jsx';
import { Divider } from './Divider.jsx';
import { Sidebar } from './Sidebar.jsx';
import { ConversationView } from './ConversationView.jsx';
import { SidePanel } from './SidePanel.jsx';
import { Settings } from './Parts.jsx';
import { PanelLeftOpen } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const CURRENT = 'scholia.project';
const RETRY_MS = 1000; // between reads of a list that could not be read
const NONE = [];

// A list that could not be read is read again after a pause, as a conversation is.
function useRetry({ error, reload }) {
  useEffect(() => {
    if (!error) return undefined;
    const timer = setTimeout(reload, RETRY_MS);
    return () => clearTimeout(timer);
  }, [error, reload]);
}

function useWindowWidth() {
  const [width, setWidth] = useState(window.innerWidth);
  useEffect(() => {
    const resize = () => setWidth(window.innerWidth);
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);
  return width;
}

function remembered() {
  try {
    return localStorage.getItem(CURRENT);
  } catch {
    return null;
  }
}

function remember(projectId) {
  try {
    localStorage.setItem(CURRENT, projectId);
  } catch {
    // ponytail: a private window forgets which project was open
  }
}

export function Shell({ health, settings, onLanguage }) {
  const t = useT();
  const width = useWindowWidth();
  const [layout, setLayout] = useState(() => fromSettings(settings.values));
  const [drawer, setDrawer] = useState(false);
  // The open panel. undefined: not decided yet (none is drawn): the project's setting decides it when read, unless
  // the researcher chose one meanwhile; a read decides only what is not decided (design review U).
  const [chosen, setPanel] = useState(undefined);
  const panel = chosen ?? 'none';
  // What the window shows: a project and one of its conversations. undefined: not decided yet (nothing read yet, or
  // what was shown was removed), and then no conversation, so no message box, is drawn; a conversation null: a new
  // one. A list decides only what is not decided; after that only the researcher's own actions change it, never a
  // read (design review S).
  const [lost] = useState(() => new Set()); // the projects this window knows deleted (a conversation's: live.js forgotten)
  const [open, setRaw] = useState({ projectId: undefined, conversationId: undefined, visit: 0 });
  // Nothing known deleted is open, whatever chose it and whether the choice or the knowledge came first: every change of
  // what is open passes here, and knowledge has what is open looked at again (left, below) (design review AE).
  const setOpen = useCallback((change) => setRaw((now) => {
    const next = change(now);
    return lost.has(next.projectId) ? { ...next, projectId: undefined, conversationId: undefined }
      : forgotten(next.conversationId) ? { ...next, conversationId: undefined } : next;
  }), [lost]);
  const { projectId, conversationId } = open;
  // The researcher's choice of another project (its conversation not decided yet) or conversation is a new visit; the
  // open one chosen again changes nothing.
  const openProject = useCallback((id) => setOpen((now) => (now.projectId === id ? now
    : { projectId: id, conversationId: undefined, visit: now.visit + 1 })), [setOpen]);
  const openConversation = useCallback((id) => setOpen((now) => (now.conversationId === id ? now
    : { ...now, conversationId: id, visit: now.visit + 1 })), [setOpen]);
  // An answer that comes later (a removal known to have happened, a project made, a first message admitted) changes
  // what is open only in the visit it was asked in: after any choice of the researcher's since, it changes nothing
  // (design review U). The conversation's view is keyed by the visit, so no answer draws the message box again.
  const answered = (change) => setOpen((now) => (now.visit === open.visit ? change(now) : now));
  // The lists, through the Library's read (libraryRead.js): shown only when read since the last removal of something
  // they name (a deletion's request settled or became known, a conversation moved), never one asked before it, and
  // nothing old after a read that failed.
  const [removals] = useState(() => ({ projects: generation(), conversations: generation() }));
  // What is known of a deletion wins over everything read before it was known, and there is one place it lands, however
  // it was learned (its DELETE answered; its own record answered 404, to the Shell, a view or live.js): no list asked
  // before now is shown or chosen from (first: the request may have settled before the engine deleted), every other
  // reader drops what it read and reads again (the one change event), live.js asks for what it holds, and what is open
  // is looked at again, whichever visit asked and whether or not its Sidebar is drawn: setOpen leaves what is known
  // deleted, now and at any later choice, so a choice that lands after the knowledge opens nothing deleted either
  // (design reviews Z, AD and AE; rider N).
  const left = useCallback((kind, id) => {
    if (kind === 'project') { lost.add(id); removals.projects.move(); }
    removals.conversations.move();
    libraryChanged(null);
    recheck();
    setOpen((now) => now);
  }, [removals, lost, setOpen]);
  // A conversation's passes through live.js, which says 'deleted' once for each (below): what live.js learns by its own
  // reads lands in the same place.
  const gone = useCallback((kind, id) => (kind === 'conversation' ? forget(id) : left(kind, id)), [left]);
  // and a project whose deletion failed is asked for, and left if it answers 404; a read that gets no answer (the
  // backend down, or failing) is asked again while that project is open, so a lost answer cannot leave the window in a
  // project that is gone (the rider N review, P2)
  const openNow = useRef(open);
  openNow.current = open;
  const asking = useRef(new Map()); // project id -> its next ask: one chain per project, none once the Shell is gone
  useEffect(() => {
    const timers = asking.current;
    timers.drawn = true; // set again on every setup: StrictMode runs setup, cleanup and setup
    return () => { timers.drawn = false; for (const timer of timers.values()) clearTimeout(timer); timers.clear(); };
  }, []);
  const askProject = useCallback(function ask(id) {
    const timers = asking.current;
    clearTimeout(timers.get(id));
    timers.delete(id);
    if (lost.has(id)) return; // nothing known deleted is asked for again (design review AE)
    get(`/api/projects/${encodeURIComponent(id)}`).catch((error) => {
      if (deleted(error)) gone('project', id);
      else if (timers.drawn && !(error instanceof ApiError && error.status > 0 && error.status < 500)) { // no answer
        clearTimeout(timers.get(id));
        timers.set(id, setTimeout(() => { // asked again only while that project is still the one open
          timers.delete(id);
          if (openNow.current.projectId === id) ask(id);
        }, RETRY_MS));
      }
    });
  }, [gone, lost]);
  const projectsRead = useRead(removals.projects, 'projects', () => get('/api/projects'));
  const conversationsRead = useRead(removals.conversations, projectId ?? null,
    () => get(`/api/conversations?project_id=${encodeURIComponent(projectId)}`));
  useRetry(projectsRead);
  useRetry(conversationsRead);
  // and no list names what is known deleted, whenever it was read (a list read since names it only after another
  // client's restore: limit L4); each knowledge moves the generations, so a read after it is derived anew
  const projects = useMemo(() => projectsRead.value?.projects.filter((p) => !lost.has(p.id)) ?? null, [projectsRead.value, lost]);
  const conversations = useMemo(() => conversationsRead.value?.conversations.filter((c) => !forgotten(c.id)) ?? null,
    [conversationsRead.value]);
  const loadConversations = conversationsRead.reload;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [notice, setNotice] = useState(null);
  const [dropping, setDropping] = useState(false); // files are dragged over the window
  const saved = useRef(layout);
  const opener = useRef(null); // what opened an overlaid panel, focused again when it closes

  const fail = useCallback((error) => {
    setNotice(errorText(t, error instanceof ApiError ? error.code : 'internal'));
  }, [t]);

  // a list that could not be read is also said once, as the window's other failures are (it is read again meanwhile)
  useEffect(() => { if (projectsRead.error) fail(projectsRead.error); }, [Boolean(projectsRead.error), fail]);
  useEffect(() => { if (conversationsRead.error) fail(conversationsRead.error); }, [Boolean(conversationsRead.error), fail]);

  useEffect(() => { // no project decided: the one open before, or the first
    if (projectId !== undefined || !projects) return;
    const wanted = remembered();
    openProject((projects.find((p) => p.id === wanted) ?? projects.find((p) => p.kind !== 'general') ?? projects[0])?.id ?? null);
  }, [projects, projectId, openProject]);
  useEffect(() => { // no conversation decided: the project's first, or a new one (which needs no list) when it has none
    // or its list cannot be read
    if (!projectId || conversationId !== undefined || !(conversations || conversationsRead.error)) return;
    const first = conversations?.[0]?.id ?? null;
    setOpen((now) => (now.projectId === projectId && now.conversationId === undefined ? { ...now, conversationId: first } : now));
  }, [projectId, conversationId, conversations, conversationsRead.error]);

  useEffect(() => { // the list shows which conversations run
    const timers = new Set();
    const unsubscribe = subscribe((id, turn, type) => {
      if (type === 'deleted') {
        left('conversation', id);
        // the rider: it may have gone with its project, whose deletion this window was not told of: asked, never assumed
        if (openNow.current.projectId) askProject(openNow.current.projectId);
      }
      if (type !== 'run_started' && type !== 'run_finished') return;
      loadConversations();
      // ponytail: a title run follows a first answer; one later look shows its title, poll if titles lag
      if (type === 'run_finished') {
        const timer = setTimeout(() => { timers.delete(timer); loadConversations(); }, 4000);
        timers.add(timer);
      }
    });
    return () => { unsubscribe(); timers.forEach(clearTimeout); };
  }, [loadConversations, left, askProject]);

  useEffect(() => {
    if (!projectId) return undefined;
    remember(projectId);
    setPanel(undefined);
    let asked = true; // until the project is left: a read asked at an earlier visit decides nothing
    get(`/api/settings?project_id=${encodeURIComponent(projectId)}`)
      .then((loaded) => asked && setPanel((now) => now ?? loaded.values?.ui?.panel ?? 'none'))
      .catch(() => {});
    return () => { asked = false; };
  }, [projectId]);

  const saveLayout = useCallback((next) => {
    const before = toSettings(saved.current);
    const after = toSettings(next);
    const changed = Object.fromEntries(Object.entries(after).filter(([key, value]) => before[key] !== value));
    if (!Object.keys(changed).length) return;
    saved.current = next;
    saveSettings(changed).catch(fail);
  }, [fail]);

  // S1-19c: the help skill's open_place (live.js): the Library panel, or a Settings page; it only opens it.
  const [settingsPage, setSettingsPage] = useState(null);
  useEffect(() => {
    const open = (event) => {
      const place = event.detail;
      if (place === 'library') { setPanel('library'); return; }
      setSettingsPage(place.slice('settings.'.length));
      setSettingsOpen(true);
    };
    places.addEventListener('open', open);
    return () => places.removeEventListener('open', open);
  }, []);

  const togglePanel = useCallback((which, from) => {
    const next = panel === which ? 'none' : which;
    if (from) opener.current = from;
    setPanel(next);
    if (next === 'none') { // focus goes back where it came from, or to the message box
      requestAnimationFrame(() => (opener.current?.isConnected ? opener.current : document.getElementById('composer'))?.focus());
    }
    if (projectId) saveSettings({ 'ui.panel': next }, projectId).catch(fail);
  }, [panel, projectId, fail]);

  // Files dropped on the window go to the open project; its Library opens to show them and any
  // question their lookup asks (a drop in the Library itself is the Library's, and goes no further:
  // the window's overlay is cleared on the way down, at every drop).
  const hasFiles = (event) => Boolean(event.dataTransfer?.types?.includes('Files')) && !settingsOpen && Boolean(projectId);
  // Only the newest drop says how it ended: an earlier one that answers late says nothing over it (design review O's
  // Option 1, as the Library's own Add).
  const [newestDrop] = useState(newest);
  async function dropFiles(files) {
    const current = newestDrop();
    if (panel !== 'library') togglePanel('library');
    if (await addTo(projectId, files, t, (text) => { if (current()) setNotice(text); })) libraryChanged(projectId);
  }

  const view = columns({ width, ...layout, panelOpen: panel !== 'none' });
  const showSidebar = () => (view.narrow ? setDrawer(true)
    : (() => { const next = { ...layout, sidebarOpen: true }; setLayout(next); saveLayout(next); })());
  const overlaid = panel !== 'none' && view.overlay; // the panel covers the conversation, which is inert meanwhile

  function newConversation() { // a blank conversation; it is made at its first message
    openConversation(null);
    setDrawer(false);
  }

  const sidebar = (
    <Sidebar
      projects={projects ?? NONE}
      projectId={projectId}
      conversations={conversations}
      problem={conversationsRead.error && errorText(t, conversationsRead.error instanceof ApiError ? conversationsRead.error.code : 'internal')}
      conversationId={conversationId}
      onProject={(id) => { openProject(id); setDrawer(false); }}
      onProjectCreated={(id, asked) => {
        if (asked) answered((now) => ({ projectId: id, conversationId: undefined, visit: now.visit + 1 }));
        projectsRead.reload();
      }}
      // A removal's request settled (removed: it is known to be gone; else it may be): nothing the lists read before is
      // shown, and what the researcher removed is left at once, in their own action; what may be there still stays open.
      onProjectDeleted={(id, removed) => {
        removals.projects.move();
        removals.conversations.move();
        recheck(); // live.js: a turn it holds of a conversation that went with the project
        if (removed) gone('project', id);
        else askProject(id); // not known (its request failed; it may have happened): asked, and left if it is gone (rider N)
      }}
      onConversation={(id) => { openConversation(id); setDrawer(false); }}
      onConversationsChanged={loadConversations}
      onConversationDeleted={(id, removed) => {
        removals.conversations.move();
        recheck(); // not known (its request failed): live.js asks
        if (removed) gone('conversation', id);
      }}
      onConversationRemoved={(id, asked) => { // moved: it still exists, so it is left only in the visit the move was asked in (rule V, rider W)
        removals.conversations.move();
        if (asked) answered((now) => (now.conversationId === id ? { ...now, conversationId: undefined } : now));
      }}
      onNewConversation={newConversation}
      onSettings={() => { setDrawer(false); setSettingsOpen(true); }}
      onHide={view.narrow ? null : () => { const next = { ...layout, sidebarOpen: false }; setLayout(next); saveLayout(next); }}
    />
  );

  const conversation = useMemo(() => conversations?.find((c) => c.id === conversationId) ?? null,
    [conversations, conversationId]);

  const project = projects?.find((p) => p.id === projectId);
  return (
    <div className="relative flex h-full"
      onDragOver={(event) => { if (hasFiles(event)) { event.preventDefault(); setDropping(true); } }}
      onDragLeave={(event) => { if (!event.relatedTarget) setDropping(false); }}
      onDropCapture={() => setDropping(false)}
      onDrop={(event) => { if (!hasFiles(event)) return; event.preventDefault(); dropFiles(event.dataTransfer.files); }}>
      {dropping && (
        <div aria-hidden="true" className="pointer-events-none fixed inset-3 z-50 grid place-items-center rounded-2xl border-2 border-dashed border-brand bg-brand-soft/70 animate-in fade-in-0">
          <p className="rounded-lg bg-background/90 px-4 py-2 text-sm font-medium shadow-sm">{t('library.dropOnWindow', { name: projectName(t, project) })}</p>
        </div>
      )}
      {view.sidebarShown && <>
        <div className="h-full shrink-0" style={{ width: view.sidebar }}>{sidebar}</div>
        <Divider
          label={t('divider.sidebar')} value={view.sidebar} min={LIMITS.sidebarMin} max={LIMITS.sidebarMax}
          onMove={({ x, step }) => setLayout((current) => ({
            ...current,
            sidebarWidth: clamp(x ?? current.sidebarWidth + step, LIMITS.sidebarMin, LIMITS.sidebarMax),
          }))}
          onDone={() => setLayout((current) => { saveLayout(current); return current; })}
          onReset={() => { const next = { ...layout, sidebarWidth: DEFAULTS.sidebarWidth }; setLayout(next); saveLayout(next); }}
        />
      </>}
      <Drawer.Root open={view.narrow && drawer} onOpenChange={setDrawer}>
        <Drawer.Portal>
          <Drawer.Overlay className="fixed inset-0 z-40 bg-black/40 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
          <Drawer.Content aria-describedby={undefined}
            className="fixed inset-y-0 left-0 z-40 w-72 max-w-[85vw] shadow-2xl outline-hidden data-[state=open]:animate-in data-[state=open]:slide-in-from-left">
            <Drawer.Title className="sr-only">{t('sidebar.projects')}</Drawer.Title>
            {sidebar}
          </Drawer.Content>
        </Drawer.Portal>
      </Drawer.Root>
      <div className="flex min-w-0 flex-1 flex-col bg-background">
        <div className="relative flex min-h-0 flex-1">
          <div className="min-w-0 flex-1" inert={overlaid || undefined}>
            {!projectId || conversationId === undefined ? (
              <div className="flex items-start gap-1 p-3">
                {/* the sidebar (projects, Settings) stays reachable while nothing is decided, in a narrow window too */}
                {(view.narrow || !view.sidebarShown) && (
                  <Button variant="ghost" size="icon" className="size-8" onClick={showSidebar} aria-label={t('sidebar.show')}>
                    <PanelLeftOpen aria-hidden="true" />
                  </Button>
                )}
                {projectsRead.error
                  ? <p role="alert" className="p-3 text-sm text-destructive">{errorText(t, projectsRead.error instanceof ApiError ? projectsRead.error.code : 'internal')}</p>
                  : <p role="status" className="p-3 text-sm text-muted-foreground">{t('common.loading')}</p>}
              </div>
            ) : <ConversationView
              key={open.visit}
              conversationId={conversationId}
              conversation={conversation}
              projectId={projectId}
              panel={panel}
              showSidebarButton={view.narrow || !view.sidebarShown}
              onShowSidebar={showSidebar}
              onPanel={togglePanel}
              onCreated={(id) => { answered((now) => ({ ...now, conversationId: id })); loadConversations(); }}
              onLibrary={() => panel !== 'library' && togglePanel('library')}
              onNoProject={() => askProject(projectId)}
            />}
          </div>
          {panel !== 'none' && !view.overlay && (
            <Divider
              label={t('divider.panel')} value={view.panel} min={LIMITS.panelMin} max={width - view.sidebar - LIMITS.conversationMin}
              onMove={({ x, step }) => setLayout((current) => ({
                ...current,
                panelShare: x !== undefined
                  ? panelShareAt(x, { width, sidebar: view.sidebar })
                  : panelShareAt(width - (view.panel - step), { width, sidebar: view.sidebar }),
              }))}
              onDone={() => setLayout((current) => { saveLayout(current); return current; })}
              onReset={() => { const next = { ...layout, panelShare: DEFAULTS.panelShare }; setLayout(next); saveLayout(next); }}
            />
          )}
          {panel !== 'none' && (
            <div className={cn('shrink-0 bg-background', view.overlay && 'absolute inset-y-0 right-0 z-30 border-l shadow-2xl')}
              style={{ width: view.panel }}>
              <SidePanel which={panel} overlay={overlaid} onClose={() => togglePanel(panel)} project={project} />
            </div>
          )}
        </div>
      </div>
      <Settings open={settingsOpen} onOpenChange={(open) => { setSettingsOpen(open); if (!open) setSettingsPage(null); }}
        startPage={settingsPage} health={health} onLanguage={onLanguage}
        project={project} onProjectChanged={projectsRead.reload} />
      {notice && (
        <div role="alert" className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 animate-fade-up rounded-lg border bg-card px-4 py-2.5 text-sm shadow-lg"
          onClick={() => setNotice(null)}>
          {notice}
        </div>
      )}
    </div>
  );
}

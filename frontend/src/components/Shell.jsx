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
import { places, subscribe } from '../live.js';
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
  const [panel, setPanel] = useState('none');
  // What the window shows: a project and one of its conversations. undefined: not decided yet (nothing read yet, or
  // what was shown was removed), and then no conversation, so no message box, is drawn; a conversation null: a new
  // one. A list decides only what is not decided; after that only the researcher's own actions change it, never a
  // read (design review S).
  const [open, setOpen] = useState({ projectId: undefined, conversationId: undefined });
  const { projectId, conversationId } = open;
  // another project: its conversation not decided yet (the open one chosen again changes nothing)
  const openProject = useCallback((id) => setOpen((now) => (now.projectId === id ? now
    : { projectId: id, conversationId: undefined })), []);
  const openConversation = useCallback((id) => setOpen((now) => ({ ...now, conversationId: id })), []);
  // The lists, through the Library's read (libraryRead.js): shown only when read since the last removal of something
  // they name (a deletion's request settled, a conversation moved), never one asked before it, and nothing old after
  // a read that failed.
  const [removals] = useState(() => ({ projects: generation(), conversations: generation() }));
  const projectsRead = useRead(removals.projects, 'projects', () => get('/api/projects'));
  const conversationsRead = useRead(removals.conversations, projectId ?? null,
    () => get(`/api/conversations?project_id=${encodeURIComponent(projectId)}`));
  useRetry(projectsRead);
  useRetry(conversationsRead);
  const projects = projectsRead.value?.projects ?? null;
  const conversations = conversationsRead.value?.conversations ?? null;
  const loadConversations = conversationsRead.reload;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [notice, setNotice] = useState(null);
  const [dropping, setDropping] = useState(false); // files are dragged over the window
  const saved = useRef(layout);
  const shown = useRef(null); // the project shown now: a list read for another is dropped
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
      if (type !== 'run_started' && type !== 'run_finished') return;
      loadConversations();
      // ponytail: a title run follows a first answer; one later look shows its title, poll if titles lag
      if (type === 'run_finished') {
        const timer = setTimeout(() => { timers.delete(timer); loadConversations(); }, 4000);
        timers.add(timer);
      }
    });
    return () => { unsubscribe(); timers.forEach(clearTimeout); };
  }, [loadConversations]);

  useEffect(() => {
    if (!projectId) return;
    remember(projectId);
    shown.current = projectId;
    setPanel('none');
    get(`/api/settings?project_id=${encodeURIComponent(projectId)}`)
      .then((loaded) => shown.current === projectId && setPanel(loaded.values?.ui?.panel ?? 'none'))
      .catch(() => {});
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
      onProjectCreated={(id) => { openProject(id); projectsRead.reload(); }}
      // A removal's request settled (removed: it is known to be gone; else it may be): nothing the lists read before is
      // shown, and what the researcher removed is left at once, in their own action; what may be there still stays open.
      onProjectDeleted={(id, removed) => {
        removals.projects.move();
        removals.conversations.move();
        if (removed) setOpen((now) => (now.projectId === id ? { projectId: undefined, conversationId: undefined } : now));
      }}
      onConversation={(id) => { openConversation(id); setDrawer(false); }}
      onConversationsChanged={loadConversations}
      onConversationRemoved={(id, removed) => {
        removals.conversations.move();
        if (removed) setOpen((now) => (now.conversationId === id ? { ...now, conversationId: undefined } : now));
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
              key={`${projectId}:${conversationId ?? 'new'}`}
              conversationId={conversationId}
              conversation={conversation}
              projectId={projectId}
              panel={panel}
              showSidebarButton={view.narrow || !view.sidebarShown}
              onShowSidebar={showSidebar}
              onPanel={togglePanel}
              onCreated={(id) => { openConversation(id); loadConversations(); }}
              onLibrary={() => panel !== 'library' && togglePanel('library')}
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

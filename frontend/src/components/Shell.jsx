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
import { subscribe } from '../live.js';
import { libraryChanged } from '../library.js';
import { projectName } from '../projects.js';
import { addTo } from './Library.jsx';
import { Divider } from './Divider.jsx';
import { Sidebar } from './Sidebar.jsx';
import { ConversationView } from './ConversationView.jsx';
import { SidePanel } from './SidePanel.jsx';
import { Settings } from './Settings.jsx';
import { cn } from '@/lib/utils';

const CURRENT = 'scholia.project';

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
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(null);
  const [panel, setPanel] = useState('none');
  const [conversations, setConversations] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [notice, setNotice] = useState(null);
  const [dropping, setDropping] = useState(false); // files are dragged over the window
  const saved = useRef(layout);
  const shown = useRef(null); // the project shown now: a list read for another is dropped
  const opener = useRef(null); // what opened an overlaid panel, focused again when it closes

  const fail = useCallback((error) => {
    setNotice(errorText(t, error instanceof ApiError ? error.code : 'internal'));
  }, [t]);

  // Only the newest read of the projects is shown, with the newest project asked for.
  const projectReads = useRef({ count: 0, prefer: undefined });
  const loadProjects = useCallback(async (prefer) => {
    const reads = projectReads.current;
    const read = ++reads.count;
    if (prefer !== undefined) reads.prefer = prefer;
    let listed;
    try {
      ({ projects: listed } = await get('/api/projects'));
    } catch (error) {
      if (read === reads.count) reads.prefer = undefined; // a failed read's preference does not outlive it
      throw error;
    }
    if (read !== reads.count) return listed; // a newer read is under way
    const wanted = reads.prefer ?? remembered();
    reads.prefer = undefined;
    setProjects(listed);
    const chosen = listed.find((p) => p.id === wanted) ?? listed.find((p) => p.kind !== 'general') ?? listed[0];
    setProjectId(chosen?.id ?? null);
    return listed;
  }, []);

  const loadConversations = useCallback(async () => {
    const id = shown.current;
    if (!id) return null;
    const { conversations: listed } = await get(`/api/conversations?project_id=${encodeURIComponent(id)}`);
    if (shown.current !== id) return null;
    setConversations(listed);
    return listed;
  }, []);

  useEffect(() => {
    loadProjects().catch(fail);
  }, [loadProjects, fail]);

  useEffect(() => { // the list shows which conversations run
    const timers = new Set();
    const unsubscribe = subscribe((id, turn, type) => {
      if (type !== 'run_started' && type !== 'run_finished') return;
      loadConversations().catch(() => {});
      // ponytail: a title run follows a first answer; one later look shows its title, poll if titles lag
      if (type === 'run_finished') {
        const timer = setTimeout(() => { timers.delete(timer); loadConversations().catch(() => {}); }, 4000);
        timers.add(timer);
      }
    });
    return () => { unsubscribe(); timers.forEach(clearTimeout); };
  }, [loadConversations]);

  useEffect(() => {
    if (!projectId) return;
    remember(projectId);
    shown.current = projectId;
    setConversations([]);
    setConversationId(null);
    setPanel('none');
    loadConversations().then((listed) => listed && setConversationId(listed[0]?.id ?? null)).catch(fail);
    get(`/api/settings?project_id=${encodeURIComponent(projectId)}`)
      .then((loaded) => shown.current === projectId && setPanel(loaded.values?.ui?.panel ?? 'none'))
      .catch(() => {});
  }, [projectId, loadConversations, fail]);

  const saveLayout = useCallback((next) => {
    const before = toSettings(saved.current);
    const after = toSettings(next);
    const changed = Object.fromEntries(Object.entries(after).filter(([key, value]) => before[key] !== value));
    if (!Object.keys(changed).length) return;
    saved.current = next;
    saveSettings(changed).catch(fail);
  }, [fail]);

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
  // question their lookup asks (a drop in the Library itself is the Library's).
  const hasFiles = (event) => Boolean(event.dataTransfer?.types?.includes('Files')) && !settingsOpen && Boolean(projectId);
  async function dropFiles(files) {
    if (panel !== 'library') togglePanel('library');
    if (await addTo(projectId, files, t, setNotice)) libraryChanged(projectId);
  }

  const view = columns({ width, ...layout, panelOpen: panel !== 'none' });
  const overlaid = panel !== 'none' && view.overlay; // the panel covers the conversation, which is inert meanwhile

  function newConversation() { // a blank conversation; it is made at its first message
    setConversationId(null);
    setDrawer(false);
  }

  const sidebar = (
    <Sidebar
      projects={projects}
      projectId={projectId}
      conversations={conversations}
      conversationId={conversationId}
      onProject={(id) => { setProjectId(id); setDrawer(false); }}
      onProjectsChanged={(prefer) => loadProjects(prefer).catch(fail)}
      onConversation={(id) => { setConversationId(id); setDrawer(false); }}
      onConversationsChanged={async (removed) => {
        const listed = await loadConversations().catch(fail);
        if (removed && removed === conversationId) setConversationId(listed?.[0]?.id ?? null);
      }}
      onNewConversation={newConversation}
      onSettings={() => { setDrawer(false); setSettingsOpen(true); }}
      onHide={view.narrow ? null : () => { const next = { ...layout, sidebarOpen: false }; setLayout(next); saveLayout(next); }}
    />
  );

  const conversation = useMemo(() => conversations.find((c) => c.id === conversationId) ?? null,
    [conversations, conversationId]);

  const project = projects.find((p) => p.id === projectId);
  return (
    <div className="relative flex h-full"
      onDragOver={(event) => { if (hasFiles(event)) { event.preventDefault(); setDropping(true); } }}
      onDragLeave={(event) => { if (!event.relatedTarget) setDropping(false); }}
      onDrop={(event) => { if (!hasFiles(event)) return; event.preventDefault(); setDropping(false); dropFiles(event.dataTransfer.files); }}>
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
            <ConversationView
              key={`${projectId}:${conversationId ?? 'new'}`}
              conversation={conversation}
              projectId={projectId}
              panel={panel}
              showSidebarButton={view.narrow || !view.sidebarShown}
              onShowSidebar={() => (view.narrow ? setDrawer(true)
                : (() => { const next = { ...layout, sidebarOpen: true }; setLayout(next); saveLayout(next); })())}
              onPanel={togglePanel}
              onCreated={(id) => { setConversationId(id); loadConversations().catch(fail); }}
              onLibrary={() => panel !== 'library' && togglePanel('library')}
            />
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
      <Settings open={settingsOpen} onOpenChange={setSettingsOpen} health={health} onLanguage={onLanguage}
        project={project} onProjectChanged={() => loadProjects(projectId).catch(fail)} />
      {notice && (
        <div role="alert" className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 animate-fade-up rounded-lg border bg-card px-4 py-2.5 text-sm shadow-lg"
          onClick={() => setNotice(null)}>
          {notice}
        </div>
      )}
    </div>
  );
}

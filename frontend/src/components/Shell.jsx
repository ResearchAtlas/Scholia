// i18n: migrated
// The window (slice-1 spec section 3): the sidebar, the conversation, and the side panel,
// with resizable, remembered dividers. Under 1,000 pixels the sidebar becomes a drawer, and
// when the conversation and the panel do not both fit, the panel slides over the
// conversation. The layout is kept in [ui.layout]; each project keeps its open panel.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as Drawer from '@radix-ui/react-dialog';
import { useT } from '../i18n/index.js';
import { ApiError, get, saveSettings } from '../api.js';
import { DEFAULTS, LIMITS, clamp, columns, fromSettings, panelShareAt, toSettings } from '../layout.js';
import { errorText } from '../text.js';
import { subscribe } from '../live.js';
import { Divider } from './Divider.jsx';
import { Sidebar } from './Sidebar.jsx';
import { ConversationView } from './ConversationView.jsx';
import { SidePanel } from './SidePanel.jsx';
import { SettingsDialog } from './SettingsDialog.jsx';
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

export function Shell({ health, settings }) {
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
  const saved = useRef(layout);

  const fail = useCallback((error) => {
    setNotice(errorText(t, error instanceof ApiError ? error.code : 'internal'));
  }, [t]);

  const loadProjects = useCallback(async (prefer) => {
    const { projects: listed } = await get('/api/projects');
    setProjects(listed);
    const wanted = prefer ?? remembered();
    const chosen = listed.find((p) => p.id === wanted) ?? listed.find((p) => p.kind !== 'general') ?? listed[0];
    setProjectId(chosen?.id ?? null);
    return listed;
  }, []);

  const loadConversations = useCallback(async (id = projectId) => {
    if (!id) return [];
    const { conversations: listed } = await get(`/api/conversations?project_id=${encodeURIComponent(id)}`);
    setConversations(listed);
    return listed;
  }, [projectId]);

  useEffect(() => {
    loadProjects().catch(fail);
  }, [loadProjects, fail]);

  useEffect(() => subscribe((id, turn, type) => { // the list shows which conversations run
    if (type !== 'run_started' && type !== 'run_finished') return;
    loadConversations().catch(() => {});
    // ponytail: a title run follows a first answer; one later look shows its title, poll if titles lag
    if (type === 'run_finished') setTimeout(() => loadConversations().catch(() => {}), 4000);
  }), [loadConversations]);

  useEffect(() => {
    if (!projectId) return;
    remember(projectId);
    setConversationId(null);
    setPanel('none');
    loadConversations(projectId).then((listed) => setConversationId(listed[0]?.id ?? null)).catch(fail);
    get(`/api/settings?project_id=${encodeURIComponent(projectId)}`)
      .then((loaded) => setPanel(loaded.values?.ui?.panel ?? 'none'))
      .catch(() => setPanel('none'));
  }, [projectId, loadConversations, fail]);

  const saveLayout = useCallback((next) => {
    const before = toSettings(saved.current);
    const after = toSettings(next);
    const changed = Object.fromEntries(Object.entries(after).filter(([key, value]) => before[key] !== value));
    if (!Object.keys(changed).length) return;
    saved.current = next;
    saveSettings(changed).catch(fail);
  }, [fail]);

  const togglePanel = useCallback((which) => {
    const next = panel === which ? 'none' : which;
    setPanel(next);
    if (projectId) saveSettings({ 'ui.panel': next }, projectId).catch(fail);
  }, [panel, projectId, fail]);

  const view = columns({ width, ...layout, panelOpen: panel !== 'none' });

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

  return (
    <div className="flex h-full">
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
            className="fixed inset-y-0 left-0 z-40 w-72 max-w-[85vw] shadow-2xl outline-none data-[state=open]:animate-in data-[state=open]:slide-in-from-left">
            <Drawer.Title className="sr-only">{t('sidebar.projects')}</Drawer.Title>
            {sidebar}
          </Drawer.Content>
        </Drawer.Portal>
      </Drawer.Root>
      <div className="flex min-w-0 flex-1 flex-col bg-background">
        <div className="relative flex min-h-0 flex-1">
          <div className="min-w-0 flex-1">
            <ConversationView
              key={conversationId ?? 'none'}
              conversation={conversation}
              projectId={projectId}
              panel={panel}
              showSidebarButton={view.narrow || !view.sidebarShown}
              onShowSidebar={() => (view.narrow ? setDrawer(true)
                : (() => { const next = { ...layout, sidebarOpen: true }; setLayout(next); saveLayout(next); })())}
              onPanel={togglePanel}
              onCreated={(id) => { setConversationId(id); loadConversations().catch(fail); }}
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
              <SidePanel which={panel} onClose={() => togglePanel(panel)} />
            </div>
          )}
        </div>
      </div>
      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} health={health} />
      {notice && (
        <div role="alert" className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 animate-fade-up rounded-lg border bg-card px-4 py-2.5 text-sm shadow-lg"
          onClick={() => setNotice(null)}>
          {notice}
        </div>
      )}
    </div>
  );
}

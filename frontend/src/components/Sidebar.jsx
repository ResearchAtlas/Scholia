// i18n: migrated
// The sidebar (slice-1 spec section 3): the project switcher, with deleting the current
// project, a new conversation, the project's conversations with rename, move and delete
// (the delete dialogs of S14), and Settings.
import { useEffect, useState } from 'react';
import { Check, ChevronsUpDown, Loader2, MoreHorizontal, PanelLeftClose, Pencil, Plus, Settings, SquarePen,
  Trash2, FolderInput } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { post, put } from '../api.js';
import { useAction } from '../action.js';
import { conversationTitle, moveTargets, projectName } from '../projects.js';
import { Mark } from './Mark.jsx';
import { DeleteDialog } from './DeleteDialog.jsx';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger }
  from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';

export function Sidebar({ projects, projectId, conversations, conversationId, onProject, onProjectsChanged,
  onConversation, onConversationsChanged, onNewConversation, onSettings, onHide }) {
  const t = useT();
  const [dialog, setDialog] = useState(null); // { kind, conversation? }
  const project = projects.find((p) => p.id === projectId);

  return (
    <nav className="flex h-full flex-col bg-sidebar" aria-label={t('sidebar.projects')}>
      <div className="flex items-center gap-1 p-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" aria-label={t('sidebar.switchProject')}
              className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-accent">
              <Mark className="size-7 text-sm" />
              <span className="min-w-0 flex-1 truncate text-sm font-semibold" title={projectName(t, project)}>{projectName(t, project)}</span>
              <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-64">
            {projects.map((p) => (
              <DropdownMenuItem key={p.id} onSelect={() => onProject(p.id)}>
                <span className="min-w-0 flex-1 truncate">{projectName(t, p)}</span>
                {p.id === projectId && <Check className="size-4" aria-hidden="true" />}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => setDialog({ kind: 'project' })}>
              <Plus aria-hidden="true" />{t('sidebar.newProject')}
            </DropdownMenuItem>
            {project && project.kind !== 'general' && (
              <DropdownMenuItem className="text-destructive focus:text-destructive"
                onSelect={() => setDialog({ kind: 'deleteProject', project })}>
                <Trash2 aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate">{t('sidebar.deleteProject', { name: projectName(t, project) })}</span>
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
        {onHide && (
          <Button variant="ghost" size="icon" className="size-8 shrink-0" onClick={onHide} aria-label={t('sidebar.hide')}>
            <PanelLeftClose aria-hidden="true" />
          </Button>
        )}
      </div>

      <div className="px-2 pb-2">
        <Button variant="outline" className="w-full justify-start gap-2 bg-background/60" onClick={onNewConversation}>
          <SquarePen aria-hidden="true" />{t('sidebar.newConversation')}
        </Button>
      </div>

      <h2 className="px-4 pb-1 pt-2 text-xs font-medium text-muted-foreground">{t('sidebar.conversations')}</h2>
      <ul className="scroll-thin min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
        {conversations.length === 0 && (
          <li className="px-2 py-3 text-sm text-muted-foreground">{t('sidebar.noConversations')}</li>
        )}
        {conversations.map((c) => (
          <li key={c.id} className="group relative">
            <button type="button" onClick={() => onConversation(c.id)} aria-current={c.id === conversationId ? 'page' : undefined}
              className={cn('flex w-full items-center gap-2 rounded-md py-1.5 pl-2.5 pr-8 text-left text-sm transition-colors hover:bg-accent',
                c.id === conversationId && 'bg-accent font-medium')}>
              <span className="min-w-0 flex-1 truncate" title={conversationTitle(t, c)}>{conversationTitle(t, c)}</span>
              {c.running && <Loader2 className="size-3.5 shrink-0 animate-spin text-brand" aria-label={t('sidebar.running')} />}
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button type="button" aria-label={t('sidebar.conversationActions', { title: conversationTitle(t, c) })}
                  className="absolute right-1 top-1/2 grid size-6 -translate-y-1/2 place-items-center rounded text-muted-foreground opacity-0 hover:bg-background focus-visible:opacity-100 group-hover:opacity-100 data-[state=open]:opacity-100">
                  <MoreHorizontal className="size-4" aria-hidden="true" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => setDialog({ kind: 'rename', conversation: c })}>
                  <Pencil aria-hidden="true" />{t('common.rename')}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setDialog({ kind: 'move', conversation: c })}>
                  <FolderInput aria-hidden="true" />{t('common.move')}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem className="text-destructive focus:text-destructive"
                  onSelect={() => setDialog({ kind: 'delete', conversation: c })}>
                  <Trash2 aria-hidden="true" />{t('common.delete')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </li>
        ))}
      </ul>

      <div className="border-t p-2">
        <Button variant="ghost" className="w-full justify-start gap-2" onClick={onSettings}>
          <Settings aria-hidden="true" />{t('sidebar.settings')}
        </Button>
      </div>

      <NewProjectDialog open={dialog?.kind === 'project'} onClose={() => setDialog(null)}
        onCreated={(created) => { setDialog(null); onProjectsChanged(created.id); }} />
      <RenameDialog conversation={dialog?.kind === 'rename' ? dialog.conversation : null} onClose={() => setDialog(null)}
        onDone={() => { setDialog(null); onConversationsChanged(); }} />
      <MoveDialog conversation={dialog?.kind === 'move' ? dialog.conversation : null} projects={projects} from={project}
        onClose={() => setDialog(null)} onDone={(moved) => { setDialog(null); onConversationsChanged(moved); }} />
      <DeleteDialog onClose={() => setDialog(null)}
        target={dialog?.kind === 'delete' ? { kind: 'conversation', id: dialog.conversation.id,
          title: t('conversation.deleteTitle'), body: t('conversation.deleteBody') }
          : dialog?.kind === 'deleteProject' ? { kind: 'project', id: dialog.project.id,
            title: t('project.deleteTitle', { name: projectName(t, dialog.project) }), body: t('project.deleteBody') }
            : null}
        onDone={(deleted) => {
          const kind = dialog?.kind;
          setDialog(null);
          if (kind === 'deleteProject') onProjectsChanged(); // another project is shown
          else onConversationsChanged(deleted);
        }} />
    </nav>
  );
}

function NewProjectDialog({ open, onClose, onCreated }) {
  const t = useT();
  const [name, setName] = useState('');
  const { busy, problem, run, reset } = useAction();
  async function submit(event) {
    event.preventDefault();
    const created = await run(() => post('/api/projects', { name }));
    if (created) { setName(''); onCreated(created); }
  }
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) { reset(); onClose(); } }}>
      <DialogContent className="max-w-md">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader><DialogTitle>{t('project.newTitle')}</DialogTitle></DialogHeader>
          <div className="grid gap-1.5">
            <label htmlFor="new-project" className="text-sm font-medium">{t('project.nameLabel')}</label>
            <Input id="new-project" value={name} maxLength={200} autoFocus placeholder={t('project.namePlaceholder')}
              onChange={(event) => setName(event.target.value)} />
          </div>
          {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" disabled={busy || !name.trim()}>{t('project.create')}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RenameDialog({ conversation, onClose, onDone }) {
  const t = useT();
  const [title, setTitle] = useState('');
  const { busy, problem, run, reset } = useAction();
  useEffect(() => setTitle(conversation?.title ?? ''), [conversation]);
  async function submit(event) {
    event.preventDefault();
    if (await run(() => put(`/api/conversations/${conversation.id}`, { title }))) onDone();
  }
  return (
    <Dialog open={Boolean(conversation)} onOpenChange={(next) => {
      if (next) return;
      reset();
      onClose();
    }}>
      <DialogContent className="max-w-md">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader><DialogTitle>{t('conversation.renameTitle')}</DialogTitle></DialogHeader>
          <div className="grid gap-1.5">
            <label htmlFor="rename" className="text-sm font-medium">{t('conversation.titleLabel')}</label>
            <Input id="rename" value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} />
          </div>
          {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" disabled={busy || !title.trim()}>{t('common.save')}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function MoveDialog({ conversation, projects, from, onClose, onDone }) {
  const t = useT();
  const [target, setTarget] = useState(null);
  const { busy, problem, run, reset } = useAction();
  const targets = moveTargets(projects, from);
  useEffect(() => setTarget(null), [conversation]); // each move starts with no destination chosen
  async function submit(event) {
    event.preventDefault();
    if (await run(() => post(`/api/conversations/${conversation.id}/move`, { project_id: target }))) onDone(conversation.id);
  }
  return (
    <Dialog open={Boolean(conversation)} onOpenChange={(next) => {
      if (next) return;
      reset();
      setTarget(null);
      onClose();
    }}>
      <DialogContent className="max-w-md">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>{t('conversation.moveTitle')}</DialogTitle>
            <DialogDescription>{t('conversation.moveHint')}</DialogDescription>
          </DialogHeader>
          {targets.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t('conversation.noTargets')}</p>
          ) : (
            <fieldset className="grid max-h-64 gap-1 overflow-y-auto">
              <legend className="sr-only">{t('conversation.moveTarget')}</legend>
              {targets.map((p) => (
                <label key={p.id} className={cn('flex cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 text-sm transition-colors hover:bg-accent',
                  target === p.id && 'border-brand bg-brand-soft')}>
                  <input type="radio" name="target" value={p.id} checked={target === p.id}
                    onChange={() => setTarget(p.id)} className="accent-[hsl(var(--brand))]" />
                  <span className="truncate">{projectName(t, p)}</span>
                </label>
              ))}
            </fieldset>
          )}
          {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" disabled={busy || !target}>{t('common.move')}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

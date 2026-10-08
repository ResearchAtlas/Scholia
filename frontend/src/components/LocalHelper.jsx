// i18n: migrated
// The local model helper under Settings, then Advanced (S12), and the model consent screen (S13;
// backend/local_helper.py): the search model's status, its download from Hugging Face or ModelScope
// once the researcher has seen its size, source and hash and chosen Download, its import from a
// file for offline installs, the helper's state, and whether search is keyword-only. A Local only
// project offers no download: its note (LocalOnlySearchNote) points to the import here, and while it
// is the current project this section offers the import only.
import { useCallback, useContext, useEffect, useId, useState } from 'react';
import { CheckCircle2, Cpu, Download, FileInput, FolderOpen, RotateCcw, TriangleAlert } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, del, get, post } from '../api.js';
import { errorText } from '../text.js';
import { fileSize } from '../backups.js';
import { downloadOffered, downloading, helperState, keywordOnlyReason, modelFilePicker, pollDelay, preferredSource, progress,
  SOURCES } from '../helper.js';
import { useAction } from '../action.js';
import { LoadState, Problem, Section, Segmented } from './fields.jsx';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

// The helper's status, read again while it is shown: often during a download or a start. The next
// read is set from the status last shown, which an action's answer also updates.
function useHelperStatus() {
  const [status, setStatus] = useState(null);
  const [problem, setProblem] = useState(null);
  const [reads, setReads] = useState(0);
  const load = useCallback(() => get('/api/helper').then((data) => {
    setStatus(data);
    setProblem(null);
  }).catch((error) => {
    setProblem(error instanceof ApiError ? error.code : 'internal');
  }).finally(() => setReads((n) => n + 1)), []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const timer = setTimeout(load, pollDelay(status));
    return () => clearTimeout(timer);
  }, [status, reads, load]);
  return { status, problem, load, setStatus };
}

// `project` is the current project, if any: the download request names it, and a Local only one is
// offered the import only.
export function LocalHelperSection({ project }) {
  const t = useT();
  const { status, problem, load, setStatus } = useHelperStatus();
  const [consent, setConsent] = useState(false);
  const [importing, setImporting] = useState(false);
  const cancelling = useAction();
  const restarting = useAction();
  const model = status?.models?.[0];
  const offered = downloadOffered(project);

  return (
    <Section title={t('helper.title')} hint={t('helper.hint')}>
      {!status && <LoadState problem={problem} onRetry={load} />}
      {status && model && (
        <>
          <Problem code={problem} />
          <div className="divide-y rounded-lg border">
            <ModelRow model={model} status={status} />
            {downloading(status) && (
              <DownloadProgress status={status} busy={cancelling.busy} onCancel={async () => {
                const data = await cancelling.run(() => del('/api/helper/models/download'));
                if (data) setStatus(data);
              }} />
            )}
            <HelperRow status={status} busy={restarting.busy} onStartAgain={async () => {
              const data = await restarting.run(() => post('/api/helper/restart'));
              if (data) setStatus(data);
            }} />
            <SearchRow status={status} offered={offered} />
          </div>
          {(cancelling.problem || restarting.problem) && (
            <p role="alert" className="text-sm text-destructive">{cancelling.problem || restarting.problem}</p>
          )}
          {needsModel(status) && !offered && <LocalOnlyNote />}
          {needsModel(status) && (
            <div className="flex flex-wrap gap-2">
              {offered && (
                <Button disabled={downloading(status)} onClick={() => setConsent(true)}>
                  <Download aria-hidden="true" />{t('helper.download')}
                </Button>
              )}
              <Button variant="outline" aria-expanded={importing} disabled={downloading(status)}
                onClick={() => setImporting((open) => !open)}>
                <FileInput aria-hidden="true" />{t('helper.import')}
              </Button>
            </div>
          )}
          {importing && needsModel(status) && (
            <ImportForm model={model} onDone={(data) => { setStatus(data); setImporting(false); }} />
          )}
          {offered && (
            <ModelConsent open={consent} status={status} project={project} onClose={() => setConsent(false)}
              onStarted={(data) => { setStatus(data); setConsent(false); }} />
          )}
        </>
      )}
    </Section>
  );
}

// The model is missing, or what is in place is not the pinned file.
function needsModel(status) {
  return !status.models[0].installed || status.search?.reason === 'model_changed';
}

function ModelRow({ model, status }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const download = status.download;
  const failed = download && ['failed', 'cancelled'].includes(download.state) && needsModel(status);
  return (
    <div className="grid gap-2 px-3 py-3 text-sm">
      <div className="flex items-start gap-3">
        <Cpu className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium" title={model.name}>{model.name}</p>
          <p className="truncate text-xs text-muted-foreground">
            {t('helper.modelDetail', { size: fileSize(model.size, language), license: model.license })}
          </p>
        </div>
        <span className={cn('shrink-0 rounded-full px-2 py-0.5 text-xs', needsModel(status)
          ? 'bg-muted text-muted-foreground' : 'bg-success/10 text-success')}>
          {t(needsModel(status) ? (model.installed ? 'helper.modelChanged' : 'helper.notInstalled') : 'helper.installed')}
        </span>
      </div>
      {failed && (
        <p role="alert" className={cn('text-sm', download.state === 'failed' ? 'text-destructive' : 'text-muted-foreground')}>
          {download.state === 'cancelled' ? t('helper.downloadCancelled') : errorText(t, download.problem)}
        </p>
      )}
    </div>
  );
}

function DownloadProgress({ status, busy, onCancel }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const download = status.download;
  const share = progress(download);
  const percent = new Intl.NumberFormat(language, { style: 'percent', maximumFractionDigits: 0 }).format(share);
  const label = t('helper.downloading', { source: t(`helper.source.${download.source}`) });
  return (
    <div className="grid gap-2 px-3 py-3 text-sm">
      <div className="flex items-center justify-between gap-3">
        <span className="min-w-0 truncate">{label}</span>
        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
          {t('helper.downloaded', { done: fileSize(download.received, language), total: fileSize(download.total, language) })}
        </span>
      </div>
      <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={Math.round(share * 100)} aria-valuetext={percent} className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-brand transition-[width] duration-200" style={{ width: `${share * 100}%` }} />
      </div>
      <Button size="sm" variant="outline" className="h-7 justify-self-start" disabled={busy} onClick={onCancel}>
        {t('helper.cancelDownload')}
      </Button>
    </div>
  );
}

function HelperRow({ status, busy, onStartAgain }) {
  const t = useT();
  return (
    <div className="flex items-center gap-3 px-3 py-2.5 text-sm">
      <span className="text-muted-foreground">{t('helper.helperLabel')}</span>
      <span className="min-w-0 flex-1">{t(`helper.state.${helperState(status)}`)}</span>
      {status.helper?.state === 'failed' && (
        <Button size="sm" variant="outline" className="h-7 shrink-0" disabled={busy} onClick={onStartAgain}>
          <RotateCcw aria-hidden="true" />{t('helper.startAgain')}
        </Button>
      )}
    </div>
  );
}

function SearchRow({ status, offered }) {
  const t = useT();
  const reason = keywordOnlyReason(status, offered);
  return (
    <p role="status" className={cn('flex gap-2 px-3 py-2.5 text-sm leading-relaxed', reason && 'text-warning')}>
      {reason ? <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        : <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />}
      <span className="min-w-0">{reason ? t('helper.keywordOnly', { reason: t(reason) }) : t('helper.searchHybrid')}</span>
    </p>
  );
}

// The model consent screen (S13): what is downloaded, from where, its size and SHA-256, and where
// it is kept. Cancel sends nothing; Download starts the one download the researcher asked for, naming
// the current project so that the backend refuses it for a Local only one.
function ModelConsent({ open, status, project, onClose, onStarted }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const [source, setSource] = useState('huggingface');
  const { busy, problem, run, reset } = useAction();
  useEffect(() => { if (open) setSource(preferredSource(status)); }, [open]); // as the screen opens, not as it polls
  const model = status.models[0];
  const close = () => { if (!busy) { reset(); onClose(); } };

  async function start() {
    const data = await run(() => post('/api/helper/models/download',
      { model: model.id, source, ...(project ? { project_id: project.id } : {}) }));
    if (data) onStarted(data);
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('helper.consentTitle')}</DialogTitle>
          <DialogDescription>{t('helper.consentBody')}</DialogDescription>
        </DialogHeader>
        <dl className="grid gap-3 text-sm">
          <Detail label={t('helper.consentModel')}>{model.name}</Detail>
          <Detail label={t('helper.consentSize')}>
            {t('helper.consentSizeValue', { size: fileSize(model.size, language),
              bytes: new Intl.NumberFormat(language).format(model.size) })}
          </Detail>
          <div className="grid gap-1.5">
            <dt className="text-xs text-muted-foreground">{t('helper.consentSource')}</dt>
            <dd className="grid gap-1.5">
              <div>
                <Segmented label={t('helper.consentSource')} value={source} disabled={busy} onChange={setSource}
                  options={SOURCES.filter((name) => model.sources[name]).map((name) => ({ value: name,
                    label: status.recommended_source === name ? t('helper.sourceRecommended', { source: t(`helper.source.${name}`) })
                      : t(`helper.source.${name}`) }))} />
              </div>
              {status.recommended_source === 'modelscope' && (
                <p className="text-xs text-muted-foreground">{t('helper.recommendModelScope')}</p>
              )}
              <p className="break-all font-mono text-xs text-muted-foreground">{model.sources[source]}</p>
            </dd>
          </div>
          <Detail label={t('helper.consentHash')}><span className="break-all font-mono text-[11px]">{model.sha256}</span></Detail>
          <Detail label={t('helper.consentLicense')}>{model.license}</Detail>
          <Detail label={t('helper.consentStored')}><span className="break-all font-mono text-xs">{model.folder}</span></Detail>
        </dl>
        <p className="text-xs leading-relaxed text-muted-foreground">{t('helper.consentNote')}</p>
        {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
        <DialogFooter>
          <Button type="button" variant="ghost" disabled={busy} onClick={close}>{t('common.cancel')}</Button>
          <Button type="button" disabled={busy} onClick={start}>
            <Download aria-hidden="true" />{t('helper.consentDownload')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Detail({ label, children }) {
  return (
    <div className="grid gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

// Import for offline installs: the model file named by its full path, typed or picked in the app's
// window. Scholia copies it only if its size and SHA-256 match the pin.
function ImportForm({ model, onDone }) {
  const t = useT();
  const id = useId();
  const [path, setPath] = useState('');
  const [pick, setPick] = useState(() => modelFilePicker());
  const { busy, problem, run } = useAction();
  useEffect(() => { // pywebview adds its bridge after the page has loaded
    const ready = () => setPick(() => modelFilePicker());
    window.addEventListener('pywebviewready', ready);
    return () => window.removeEventListener('pywebviewready', ready);
  }, []);

  async function submit(event) {
    event.preventDefault();
    const data = await run(() => post('/api/helper/models/import', { model: model.id, path: path.trim() }));
    if (data) onDone(data);
  }

  return (
    <form onSubmit={submit} className="grid gap-3 rounded-lg border px-3 py-3">
      <div className="grid gap-1.5">
        <label htmlFor={id} className="text-sm font-medium">{t('helper.importLabel')}</label>
        <div className="flex gap-2">
          <Input id={id} value={path} spellCheck={false} autoComplete="off" className="font-mono text-xs md:text-xs"
            placeholder={t('helper.importPlaceholder')} aria-describedby={`${id}-hint`}
            onChange={(event) => setPath(event.target.value)} />
          {pick && (
            <Button type="button" variant="outline" className="shrink-0" onClick={async () => {
              const chosen = await pick().catch(() => null);
              if (chosen) setPath(chosen);
            }}><FolderOpen aria-hidden="true" />{t('folder.choose')}</Button>
          )}
        </div>
        <p id={`${id}-hint`} className="text-xs leading-relaxed text-muted-foreground">
          {t('helper.importHint', { file: model.file })}
        </p>
      </div>
      {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
      {busy && <p role="status" className="text-sm text-muted-foreground">{t('helper.importing')}</p>}
      <Button type="submit" variant="outline" className="justify-self-start" disabled={busy || !path.trim()}>
        {t('helper.importConfirm')}
      </Button>
    </form>
  );
}

// In a Local only project, while the search model is not in place: search is keyword-only, and the
// project offers no download (ticket 71); the model can be imported under Settings, Advanced.
export function LocalOnlySearchNote({ onOpenAdvanced }) {
  const { status } = useHelperStatus();
  if (!status?.models?.length || !needsModel(status)) return null;
  return <LocalOnlyNote onOpenAdvanced={onOpenAdvanced} />;
}

// The note itself, under This project with the way to Advanced, and in Advanced above the import.
function LocalOnlyNote({ onOpenAdvanced }) {
  const t = useT();
  return (
    <div role="note" className="grid gap-2 rounded-md border border-warning/40 px-3 py-2.5 text-sm">
      <p className="font-medium">{t('helper.localOnlyTitle')}</p>
      <p className="leading-relaxed text-muted-foreground">{t('helper.localOnlyBody')}</p>
      {onOpenAdvanced && (
        <Button variant="outline" size="sm" className="h-7 justify-self-start" onClick={onOpenAdvanced}>
          {t('helper.localOnlyOpen')}
        </Button>
      )}
    </div>
  );
}

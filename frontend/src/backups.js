// Backups, restore, project export and deletion as the interface asks for them
// (backend/backups.py, backend/app.py's DELETE endpoints, backend/desktop.py WindowApi).

// The sensitivity levels whose projects make a full backup or an export encrypted.
export const SENSITIVE = ['private', 'local_only'];

export function needsPassphrase(projects) {
  return projects.some((project) => SENSITIVE.includes(project?.sensitivity));
}

// The DELETE request for a project or conversation, with the delete dialog's choices:
// "Delete everywhere including backups" and, under Details, "Remove all trace".
export function deletePath(kind, id, { everywhere = false, removeAllTrace = false } = {}) {
  const base = `/api/${kind === 'project' ? 'projects' : 'conversations'}/${encodeURIComponent(id)}`;
  const query = new URLSearchParams();
  if (everywhere) query.set('purge_backups', 'true');
  if (removeAllTrace) query.set('remove_all_trace', 'true');
  const text = query.toString();
  return text ? `${base}?${text}` : base;
}

// What a deletion that succeeded must still say, as catalog keys: older backups it could not
// clear (Delete everywhere), and files of the project it could not remove (retried at launch).
export function deletionNotices(result) {
  return [result?.purge_failed && 'delete.purgeFailed', result?.staging_left && 'delete.stagingLeft',
    result?.files_left && 'delete.filesLeft'].filter(Boolean);
}

// What a restore that was done could not record, as catalog keys (the backend's not_recorded).
// Its schema version alone is not the researcher's concern.
const NOT_RECORDED = {
  audit: 'backups.notRecordedAudit',
  missing_files: 'backups.notRecordedMissingFiles',
  journal: 'backups.notRecordedJournal',
};

export function restoreNotes(result) {
  return (result?.not_recorded ?? []).map((name) => NOT_RECORDED[name]).filter(Boolean);
}

// A restore request: an automatic backup by its id, or a full backup file with its passphrase.
export function restoreBody({ generation, file, passphrase }) {
  if (generation) return { generation };
  return { file: file.trim(), ...(passphrase ? { passphrase } : {}) };
}

// A size in bytes as the interface shows it, such as "1.2 MB" (never in bytes: "0.5 kB").
export function fileSize(bytes, language) {
  const units = ['kilobyte', 'megabyte', 'gigabyte'];
  let value = Math.max(0, Number(bytes) || 0) / 1000;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return new Intl.NumberFormat(language, { style: 'unit', unit: units[unit], unitDisplay: 'short',
    maximumFractionDigits: 1 }).format(value);
}

// The window's folder picker, or null where there is none (a browser). In the app's window,
// pywebview adds window.pywebview.api once the page has loaded ("pywebviewready").
export function folderPicker(win = globalThis.window) {
  const api = win?.pywebview?.api;
  return typeof api?.choose_folder === 'function' ? () => api.choose_folder() : null;
}

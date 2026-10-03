// How the audit log view shows an entry's details in the interface language. The log holds
// codes, ids, origins, counts and times, never content (ticket 18): each known field, level,
// yes or no, and refusal reason is shown through the catalogs; a code this version does not name,
// and a value that is the researcher's own (a file or backup's name), is shown as it is.
const LEVEL_FIELDS = new Set(['from', 'to', 'sensitivity']);
const TIME_FIELDS = new Set(['expires_at', 'declared_at']);

// A code's text from the catalog under prefix, or the code itself when it has none.
export function named(t, prefix, code) {
  const key = `${prefix}.${code}`;
  const text = t(key);
  return text === key ? String(code) : text;
}

function shown(t, key, value, dates) {
  if (typeof value === 'boolean') return t(value ? 'audit.value.yes' : 'audit.value.no');
  if (LEVEL_FIELDS.has(key)) return named(t, 'level', value);
  if (key === 'stored_in') return named(t, 'audit.storedIn', value);
  if (key === 'kind') return named(t, 'audit.object', value); // what a deletion deleted
  if (key === 'source') return named(t, 'audit.backupSource', value); // what a restore put in place
  if (TIME_FIELDS.has(key) && typeof value === 'string') return dates.format(new Date(value));
  if (Array.isArray(value)) return String(value.length); // the settings files a full backup left out: how many
  if (value && typeof value === 'object') { // the records a deletion removed, by table: their count
    return String(Object.values(value).reduce((sum, n) => sum + (Number(n) || 0), 0));
  }
  return String(value);
}

// One line of an entry's details: a request's decision, its reason, the kind of destination and
// its origin; or each other field, by its name, with its value.
export function auditDetail(t, entry, dates) {
  const data = entry.data ?? {};
  if (entry.event === 'outbound') {
    return [data.decision === 'allow' ? t('audit.allowed') : t('audit.refused', { reason: named(t, 'audit.reason', data.reason) }),
      data.kind ? named(t, 'audit.kind', data.kind) : null, data.destination].filter(Boolean).join(' · ');
  }
  return Object.entries(data).filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => t('audit.pair', { field: named(t, 'audit.field', key), value: shown(t, key, value, dates) }))
    .join(' · ');
}

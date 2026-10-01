// i18n: migrated
// Fixture: every kind of raw UI string the raw-string check must flag.
export function RawStrings({ busy, count, t }) {
  return (
    <div className="flex gap-2" title="Raw title">
      Raw text
      {'Raw child'}
      {busy ? 'Raw branch' : t('common.cancel')}
      {count === 0 && 'Raw fallback'}
      {`Raw template ${count}`}
      <input placeholder={'Raw placeholder'} aria-label={busy ? t('x') : 'Raw label'} />
      <img alt={`Raw alt`} src="/x.png" />
      <>原始文本</>
      {busy && `Raw template branch ${count}`}
    </div>
  );
}

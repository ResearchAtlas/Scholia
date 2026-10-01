// i18n: migrated
// Fixture: a migrated component whose visible text all goes through t().
export function Clean({ busy, count, failed, ready, status, t }) {
  return (
    <div className="flex gap-2" data-state={busy ? 'busy' : 'idle'} title={t('sidebar.projects')}>
      {t('library.materialCount', { count })}
      {busy ? t('common.cancel') : null}
      {status === 'done' && <span>{t('sidebar.conversations')}</span>}
      <input type="text" placeholder={t('sidebar.newConversation')} />
      <span>{count} · 42 / {count}</span>
      {ready ? (failed ? t('common.cancel') : busy && t('sidebar.projects')) : ''}
      {(count, t('sidebar.conversations'))}
    </div>
  );
}

// i18n: migrated
// The settings pages' controls. Every choice has at most three options, or a text box
// (slice-1 spec F12): a typed value with suggestions counts as a text box. A text or number
// is saved when it is committed (Enter, or leaving the field), and a choice when it is made.
import { useEffect, useId, useRef, useState } from 'react';
import { RotateCcw } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { errorText } from '../text.js';
import { Button } from '@/components/ui/button';
import { commitDecision } from '../settings.js';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

export function Section({ title, hint, children }) {
  return (
    <section className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold">{title}</h3>
        {hint && <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{hint}</p>}
      </div>
      {children}
    </section>
  );
}

export function Field({ label, hint, htmlFor, children, aside }) {
  return (
    <div className="grid gap-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={htmlFor} className="text-sm font-medium">{label}</label>
        {aside}
      </div>
      {children}
      {hint && <p className="text-xs leading-relaxed text-muted-foreground">{hint}</p>}
    </div>
  );
}

// One choice among at most three, as a row of buttons.
export function Segmented({ label, options, value, onChange, disabled }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-lg border bg-muted/40 p-0.5">
      {options.map((option) => (
        <button key={option.value} type="button" role="radio" aria-checked={value === option.value} disabled={disabled}
          onClick={() => value !== option.value && onChange(option.value)}
          className={cn('rounded-md px-3 py-1.5 text-sm transition-colors disabled:opacity-50',
            value === option.value ? 'bg-background font-medium shadow-xs' : 'text-muted-foreground hover:text-foreground')}>
          {option.label}
        </button>
      ))}
    </div>
  );
}

// A text or a whole number, committed on Enter or on leaving the field. suggestions are
// offered under it; an empty field commits null (back to the default) when allowEmpty.
export function CommitField({ id, value, onCommit, type = 'text', min, above, step, suggestions, placeholder, allowEmpty,
  className, inputMode }) {
  const listId = useId();
  const [draft, setDraft] = useState(value ?? '');
  const edited = useRef(false); // typed in since the last commit
  useEffect(() => { // a new value replaces the draft unless the researcher has typed since
    if (!edited.current) setDraft(value ?? '');
  }, [value]);

  // Saves what was typed. Leaving the field saves only after typing; Enter always saves, so a
  // refused save can be tried again. Saving a value that did not change is harmless.
  function commit(asked) {
    if (!edited.current && !asked) return;
    edited.current = false;
    const decided = commitDecision(String(draft).trim(), { type, min, above, step, allowEmpty });
    if (decided.reject) {
      setDraft(value ?? '');
      if (decided.invalid) onCommit(undefined, 'invalid_request');
      return;
    }
    onCommit(decided.out);
  }

  return (
    <>
      <Input id={id} type={type === 'number' ? 'text' : type} inputMode={inputMode ?? (type === 'number' ? 'decimal' : undefined)}
        value={draft} placeholder={placeholder} list={suggestions ? listId : undefined} className={cn('h-9', className)}
        onChange={(event) => { edited.current = true; setDraft(event.target.value); }} onBlur={() => commit(false)}
        onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); commit(true); } }} />
      {suggestions && (
        <datalist id={listId}>
          {suggestions.map((suggestion) => <option key={suggestion} value={suggestion} />)}
        </datalist>
      )}
    </>
  );
}

export function Restore({ onClick, disabled }) {
  const t = useT();
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground disabled:opacity-40">
      <RotateCcw className="size-3" aria-hidden="true" />{t('settings.restore')}
    </button>
  );
}

// Values the file holds that cannot be used, by key and line; each falls back to its default
// (ticket 14).
export function FileProblems({ problems }) {
  const t = useT();
  if (!problems?.length) return null;
  return (
    <ul role="status" className="space-y-1 rounded-md bg-warning/10 px-3 py-2 text-sm text-warning">
      {problems.map((problem, i) => (
        <li key={i}>{problem.key ? t('settings.problemValue', { key: problem.key, line: problem.line })
          : problem.line ? t('settings.problemFile', { line: problem.line }) : t('settings.problemFileUnread')}</li>
      ))}
    </ul>
  );
}

// A failed change or read, in the interface language.
export function Problem({ code }) {
  const t = useT();
  if (!code) return null;
  return <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
    {code === 'settings_changed' ? t('settings.changedOnDisk') : errorText(t, code)}
  </p>;
}

// Loading, or, when the first read failed, why, with a way to try again.
export function LoadState({ problem, onRetry }) {
  const t = useT();
  if (!problem) return <p className="text-sm text-muted-foreground" role="status">{t('common.loading')}</p>;
  return (
    <div role="alert" className="space-y-3 rounded-md bg-destructive/10 px-3 py-3 text-sm text-destructive">
      <p>{errorText(t, problem)}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>{t('common.retry')}</Button>
    </div>
  );
}

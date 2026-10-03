// i18n: migrated
// The settings pages' controls. Every choice has at most three options, or a text box
// (slice-1 spec F12): a typed value with suggestions counts as a text box. A text or number
// is saved when it is committed (Enter, or leaving the field), and a choice when it is made.
import { useEffect, useId, useRef, useState } from 'react';
import { RotateCcw } from 'lucide-react';
import { useT } from '../i18n/index.js';
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
            value === option.value ? 'bg-background font-medium shadow-sm' : 'text-muted-foreground hover:text-foreground')}>
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
  const sent = useRef(null); // the text last committed, until the value catches up with it
  useEffect(() => { // a new value replaces the draft unless the researcher has typed or committed since
    if (edited.current || (sent.current !== null && String(value ?? '') !== sent.current)) return;
    sent.current = null;
    setDraft(value ?? '');
  }, [value]);

  function commit() {
    edited.current = false;
    const text = String(draft).trim();
    let out = text; // what is saved: null (back to the default), a number, or the text
    if (!text) {
      if (!allowEmpty) {
        setDraft(sent.current ?? value ?? '');
        return;
      }
      out = null;
    } else if (type === 'number') {
      const number = Number(text);
      if (!Number.isFinite(number) || (min !== undefined && number < min) || (above !== undefined && number <= above)
          || (step === 1 && !Number.isInteger(number))) {
        setDraft(sent.current ?? value ?? '');
        onCommit(undefined, 'invalid_request');
        return;
      }
      out = number;
    }
    const shown = String(out ?? '');
    if (shown === (sent.current ?? String(value ?? ''))) return; // nothing new since the last commit
    sent.current = shown; // compared as the saved value will read
    Promise.resolve(onCommit(out)).then((saved) => { // not saved (refused): the same text may be committed again
      if (saved === false && sent.current === shown) sent.current = null;
    });
  }

  return (
    <>
      <Input id={id} type={type === 'number' ? 'text' : type} inputMode={inputMode ?? (type === 'number' ? 'decimal' : undefined)}
        value={draft} placeholder={placeholder} list={suggestions ? listId : undefined} className={cn('h-9', className)}
        onChange={(event) => { edited.current = true; setDraft(event.target.value); }} onBlur={commit}
        onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); commit(); } }} />
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

// Small text helpers shared by the components.

// An API error's text in the interface language: its code's own entry, else the generic one.
export function errorText(t, code) {
  const key = `errors.${code}`;
  const text = t(key);
  return text === key ? t('errors.internal') : text;
}

// A cost in dollars, as the interface shows it ("about $0.0042"); never shown as $0 when
// something was spent but rounds to nothing.
export function money(value, language) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  const digits = value > 0 && value < 0.01 ? 4 : 2;
  return new Intl.NumberFormat(language, { style: 'currency', currency: 'USD', minimumFractionDigits: digits,
    maximumFractionDigits: digits }).format(value);
}

// The text trimmed, or null when it has no visible character, as the backend reads names and
// defaults (backend/settings.py visible): only separators, format and control characters,
// marks, or the few letters that draw nothing (Hangul fillers, the blank Braille pattern).
const BLANK_LETTERS = new Set(['\u115f', '\u1160', '\u3164', '\uffa0', '\u2800']);

export function visible(text) {
  if (typeof text !== 'string' || ![...text].some((c) => !/[\p{C}\p{M}\p{Z}]/u.test(c) && !BLANK_LETTERS.has(c))) return null;
  return text.trim();
}

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

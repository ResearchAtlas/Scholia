// The interface's translation layer: t(key, params) over the JSON catalogs.
// Catalog values are strings with {name} placeholders, or an object of plural
// forms ("one", "other", ...) chosen by Intl.PluralRules from params.count.
import { createContext, useContext, useMemo } from 'react';
import en from './en.json' with { type: 'json' };
import zhCN from './zh-CN.json' with { type: 'json' };

const CATALOGS = { en, 'zh-CN': zhCN };

// The [ui] language setting is "system", "en" or "zh-CN". "system" follows the
// OS locale: any Chinese locale maps to zh-CN, every other locale to English.
export function resolveLanguage(setting, systemLocale) {
  if (setting === 'en' || setting === 'zh-CN') return setting;
  return /^zh(?:[-_]|$)/i.test(systemLocale ?? '') ? 'zh-CN' : 'en';
}

export function makeT(language, catalogs = CATALOGS) {
  const catalog = catalogs[language] ?? {};
  // A key missing here falls back to English; missing there too, the key shows.
  const fallback = language === 'en' ? (key) => key : makeT('en', catalogs);
  const plurals = new Intl.PluralRules(language);
  const numbers = new Intl.NumberFormat(language);
  const dates = new Intl.DateTimeFormat(language);

  return function t(key, params = {}) {
    if (!Object.hasOwn(catalog, key)) return fallback(key, params);
    let text = catalog[key];
    if (typeof text === 'object') text = text[plurals.select(params.count)] ?? text.other;
    return text.replace(/\{(\w+)\}/g, (placeholder, name) => {
      const value = params[name];
      if (typeof value === 'number') return numbers.format(value);
      if (value instanceof Date) return dates.format(value);
      return value === undefined ? placeholder : String(value);
    });
  };
}

// The app provides the resolved language; components call useT().
export const LanguageContext = createContext('en');

export function useT() {
  const language = useContext(LanguageContext);
  return useMemo(() => makeT(language), [language]);
}

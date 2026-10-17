// The interface's translation layer: t(key, params) over the JSON catalogs.
// Catalog values are strings with {name} placeholders, or an object of plural
// forms ("one", "other", ...) chosen by Intl.PluralRules from params.count.
import { createContext, useContext, useMemo } from 'react';
import { loader } from '../parts.js';
import en from './en.json' with { type: 'json' };

// English is in the script loaded at start: it is the fallback for a key missing in another
// language. Every other language's catalog is a part of its own (parts.js), loaded at launch when it
// is the window's language (loadLanguage), and only then in CATALOGS.
const CATALOGS = { en };
const PARTS = { 'zh-CN': () => import('./zh-CN.json', { with: { type: 'json' } }) };

// The supported interface languages, each named in its own language (catalog key language.<code>).
export const LANGUAGES = ['en', ...Object.keys(PARTS)];

// Whether a language's catalog is here, so the window can be drawn in it.
export const languageLoaded = (language) => Object.hasOwn(CATALOGS, language);

// Loads a language's catalog, once: settles once it is in CATALOGS, or rejects with NotLoaded.
const loading = {};
export function loadLanguage(language) {
  if (languageLoaded(language)) return Promise.resolve();
  return (loading[language] ??= loader(PARTS[language], 'default')()
    .then((module) => { CATALOGS[language] = module.default; }));
}

// Use English, on the screen of a language that could not load at launch: English is saved (once
// more if that did not take, the settings having changed meanwhile), then the window reloads in it;
// if it could not be saved, English is drawn for this window, which says it was not saved (draw).
export async function chooseEnglish({ save, reload, draw }) {
  if (await save() || await save()) return reload();
  return draw();
}

// What the window says when a language's catalog could not be loaded, in that language: kept here,
// in the script loaded at start, since the catalog that would say it is what could not load.
export const NOT_LOADED = {
  en: {
    message: 'Scholia could not load this interface language. Try again reloads the window; Use English sets the interface language to English.',
    retry: 'Try again',
    english: 'Use English',
  },
  'zh-CN': {
    message: 'Scholia 未能加载中文界面。点“重试”会重新载入窗口；点“使用英文”会把界面语言设为英文。',
    retry: '重试',
    english: '使用英文',
  },
};

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

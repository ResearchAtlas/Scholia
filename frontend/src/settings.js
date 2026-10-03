// The settings files and instruction files as the settings pages edit them (slice-1 spec F12,
// section 4.4). Each change is saved on its own against the file as the page last read or
// saved it: a file changed on disk since then is never overwritten; it is read again and the
// researcher is told, so the change is made again on what the file now holds (ticket 14).
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, get, put } from './api.js';

// Saves updates against `read`, the file as it was last read or saved: the backend refuses
// (409 settings_changed) if the file changed since, and then nothing is written.
export function saveAgainst(read, updates, projectId) {
  return put('/api/settings', { hash: read?.hash, updates, ...(projectId ? { project_id: projectId } : {}) });
}

// One page's saves of one settings file, one after another, each against the file as last
// read or saved. A save the backend refuses because the file changed (settings_changed) writes
// nothing, and the saves queued before that answer are dropped with it: they were made on the
// file as it was. read() reads the file again; write(hash, updates) saves.
export function settingsSaver({ read, write, onFile, onProblem }) {
  let latest = null;
  let queue = Promise.resolve();
  let era = 0;
  const reload = async () => {
    try {
      latest = await read();
      onFile(latest);
    } catch (error) {
      onProblem(error instanceof ApiError ? error.code : 'internal');
    }
  };
  const save = (updates) => {
    const madeIn = era;
    const run = queue.then(async () => {
      if (madeIn !== era) return false;
      onProblem(null);
      try {
        latest = await write(latest?.hash, updates);
        onFile(latest);
        return true;
      } catch (error) {
        const code = error instanceof ApiError ? error.code : 'internal';
        onProblem(code);
        if (code === 'settings_changed') {
          era += 1;
          await reload();
        }
        return false;
      }
    });
    queue = run.catch(() => {});
    return run;
  };
  return { reload, save };
}

// A settings file: the personal one, or a project's when projectId is given.
export function useSettingsFile(projectId) {
  const [file, setFile] = useState(null); // { values, warnings, problems, hash }
  const [problem, setProblem] = useState(null); // an error code
  const saver = useMemo(() => {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return settingsSaver({
      read: () => get(`/api/settings${query}`),
      write: (hash, updates) => saveAgainst({ hash }, updates, projectId),
      onFile: setFile,
      onProblem: setProblem,
    });
  }, [projectId]);

  useEffect(() => {
    saver.reload();
  }, [saver]);

  return { values: file?.values ?? null, problems: file?.problems ?? [], problem, save: saver.save, reload: saver.reload };
}

// An AGENTS.md file: the personal one, or a project's.
export function useInstructions(projectId, withProject) {
  const [file, setFile] = useState(null); // { text, hash, combined_bytes, cap_bytes, warnings }
  const [problem, setProblem] = useState(null);
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}`
    : withProject ? `?with_project=${encodeURIComponent(withProject)}` : '';

  const reload = useCallback(async () => {
    try {
      setFile(await get(`/api/instructions${query}`));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
    }
  }, [query]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Resolves true when saved. On a changed file it resolves false and the file is read again;
  // the caller keeps the draft (onRejected runs before that read lands).
  const save = useCallback(async (text, onRejected) => {
    setProblem(null);
    try {
      await put('/api/instructions', { text, hash: file?.hash, ...(projectId ? { project_id: projectId } : {}) });
      await reload();
      return true;
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
      if (error instanceof ApiError && error.code === 'settings_changed') {
        onRejected?.();
        await reload();
      }
      return false;
    }
  }, [file?.hash, projectId, reload]);

  return { file, problem, save };
}

// The bytes a text takes in UTF-8, as the 32 KiB instructions cap counts them.
export const utf8Bytes = (text) => new TextEncoder().encode(text ?? '').length;

// Each provider's model listing, read once per window and again after a provider changes.
const listings = new Map();

export function loadModels(provider, { refresh = false } = {}) {
  if (refresh || !listings.has(provider)) {
    const query = refresh ? '?refresh=true' : '';
    const listing = get(`/api/providers/${encodeURIComponent(provider)}/models${query}`);
    listings.set(provider, listing);
    listing.catch(() => listings.delete(provider));
  }
  return listings.get(provider);
}

const catalogListeners = new Set();

// Forgets the listings after a provider or its models changed, and tells those who show them;
// quietly when it is the reader itself that wants fresh listings.
export function forgetModels({ quiet = false } = {}) {
  listings.clear();
  if (!quiet) catalogListeners.forEach((listener) => listener());
}

export function onCatalogChange(listener) {
  catalogListeners.add(listener);
  return () => catalogListeners.delete(listener);
}

// The window presets offered as suggestions under a window field (slice-1 spec section 13).
export const WINDOW_PRESETS = [4096, 8192, 16384, 32768, 65536, 131072, 200000, 262144, 1000000];
export const BUDGET_SUGGESTIONS = [5, 10, 20, 30, 40, 50];

// The value at a dotted key ("subagents.at_once") in a settings file's values.
export function valueAt(values, key) {
  return key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), values);
}

// A settings key from its parts, quoting the parts that are not bare TOML keys (model ids
// such as "google/gemini-2.5-flash", whose dots would otherwise split the key).
export function settingKey(...parts) {
  return parts.map((part) => (/^[A-Za-z0-9_-]+$/.test(part) ? part : JSON.stringify(part))).join('.');
}

// A provider's group on the Providers page: Ready, Needs setup (no key yet) or Off.
export function groupOf(provider) {
  if (!provider.enabled) return 'off';
  return provider.has_key ? 'ready' : 'setup';
}

// What a message or a Continue sends for the picker's choice: nothing for no choice (the
// project's or the personal [models] default applies, section 4.4), Auto, or a model with
// its effort.
export function messageRoute(chosen) {
  if (!chosen) return {};
  if (chosen.auto) return { model: 'auto' };
  return { model: chosen.model, provider: chosen.provider, ...(chosen.effort ? { effort: chosen.effort } : {}) };
}

// The picker's choice as the personal settings keep it ([ui] model): the settings keys to
// save for a choice (null clears both), and the choice the settings hold.
export function choiceUpdates(next) {
  if (!next) return { 'ui.model.id': null, 'ui.model.provider': null };
  if (next.auto) return { 'ui.model.id': 'auto', 'ui.model.provider': null };
  return { 'ui.model.id': next.model, 'ui.model.provider': next.provider };
}

export function decodeChoice(value) {
  if (value?.id === 'auto') return { auto: true };
  if (typeof value?.id !== 'string' || typeof value?.provider !== 'string') return null;
  return { provider: value.provider, model: value.id };
}

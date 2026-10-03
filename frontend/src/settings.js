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
  let conflict = false; // a save was refused because the file changed; its message is owed
  // Reads the file again; a read that succeeds clears an earlier error, unless it follows a
  // conflict whose message stays (keep), even after a failed read in between.
  const refresh = async (keep) => {
    try {
      latest = await read();
      onFile(latest);
      if (!keep) conflict = false;
      if (!keep || conflict) onProblem(conflict ? 'settings_changed' : null);
    } catch (error) {
      onProblem(error instanceof ApiError ? error.code : 'internal');
    }
  };
  // Reads wait their turn behind saves and earlier reads, so an older one never lands last; a
  // conflict met after a read was asked for keeps its message.
  const reload = ({ keep = false } = {}) => {
    const asked = era;
    const run = queue.then(() => refresh(keep || asked !== era));
    queue = run.catch(() => {});
    return run;
  };
  const save = (updates) => {
    const madeIn = era;
    const run = queue.then(async () => {
      if (madeIn !== era) return false;
      conflict = false;
      onProblem(null);
      try {
        latest = await write(latest?.hash, updates);
        onFile(latest);
        return true;
      } catch (error) {
        const code = error instanceof ApiError ? error.code : 'internal';
        onProblem(code);
        if (code === 'settings_changed') {
          conflict = true;
          era += 1;
          await refresh(true);
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

  const reload = useCallback(async ({ keep = false } = {}) => {
    try {
      setFile(await get(`/api/instructions${query}`));
      if (!keep) setProblem(null); // a read that succeeds clears an earlier error, not a conflict's
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
        await reload({ keep: true });
      }
      return false;
    }
  }, [file?.hash, projectId, reload]);

  return { file, problem, save, reload };
}

// The bytes a text takes in UTF-8, as the 32 KiB instructions cap counts them.
export const utf8Bytes = (text) => new TextEncoder().encode(text ?? '').length;

// Each provider's model listing, read once per window and again after a provider changes. With
// a project, each model says whether the project allows it (the picker shows only those).
const listings = new Map();

export function loadModels(provider, { refresh = false, projectId = null } = {}) {
  const key = `${provider}\n${projectId ?? ''}`;
  if (refresh || !listings.has(key)) {
    const query = new URLSearchParams({ ...(refresh ? { refresh: 'true' } : {}), ...(projectId ? { project_id: projectId } : {}) });
    const listing = get(`/api/providers/${encodeURIComponent(provider)}/models${query.size ? `?${query}` : ''}`);
    listings.set(key, listing);
    const drop = () => { if (listings.get(key) === listing) listings.delete(key); };
    listing.then((read) => { if (read?.status?.error) drop(); }, drop); // a failed listing is read again next time
  }
  return listings.get(key);
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

// The picker's models after a read: the rows read now, and a provider's earlier rows when only
// its listing failed, but never across a change of the project's protection (section 6.4: only
// the routes the project allows are shown); with no read at all (failed), none.
export function keptModels(current, next, unread) {
  if (!next) return [];
  const kept = current && current.protection === next.protection
    ? current.models.filter((m) => unread.has(m.provider)) : [];
  return [...next.models, ...kept];
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

// Whether a chosen model may still be sent, judged by its provider's listing as read now: a
// listed model must be offered with a usable window (section 8); one missing from a listing
// that read well is gone; with no listing, or one that reports an error, it cannot be judged.
export function stillUsable(listing, model) {
  const row = listing?.models?.find((m) => m.id === model);
  return row ? Boolean(row.offered && row.window?.status === 'ok') : !(listing && !listing.status?.error);
}

// A provider's listing to judge a kept choice by: a provider no longer set up (404) lists
// nothing; a listing that cannot be read is null, which cannot be judged.
export function listingToJudge(provider) {
  return loadModels(provider).catch((error) => (error instanceof ApiError && error.status === 404 ? { models: [] } : null));
}

export function decodeChoice(value) {
  if (value?.id === 'auto') return { auto: true };
  if (typeof value?.id !== 'string' || typeof value?.provider !== 'string') return null;
  return { provider: value.provider, model: value.id };
}

// What a committed field saves for its text: { reject: true } (the field shows the saved value
// again; invalid when it should say so), or { out } (null clears back to the default).
export function commitDecision(text, { type, min, above, step, allowEmpty }) {
  if (!text) return allowEmpty ? { out: null } : { reject: true };
  if (type !== 'number') return { out: text };
  const number = Number(text);
  if (!Number.isFinite(number) || (min !== undefined && number < min) || (above !== undefined && number <= above)
      || (step === 1 && !Number.isInteger(number))) return { reject: true, invalid: true };
  return { out: number };
}

// The settings files and instruction files as the settings pages edit them (slice-1 spec F12,
// section 4.4). Each change is saved on its own (saveSettings in api.js): a file changed on
// disk meanwhile is never overwritten; it is read again and the researcher is told, so the
// change is made again on what the file now holds (ticket 14).
import { useCallback, useEffect, useState } from 'react';
import { ApiError, get, put, saveSettings } from './api.js';

// A settings file: the personal one, or a project's when projectId is given.
export function useSettingsFile(projectId) {
  const [file, setFile] = useState(null); // { values, warnings, hash }
  const [problem, setProblem] = useState(null); // an error code
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';

  const reload = useCallback(async () => {
    try {
      setFile(await get(`/api/settings${query}`));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
    }
  }, [query]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Saves {key: value} (null clears a key back to its default). Resolves true when saved.
  const save = useCallback(async (updates) => {
    setProblem(null);
    try {
      const saved = await saveSettings(updates, projectId);
      if (saved === null) { // changed on disk meanwhile: nothing was written
        setProblem('settings_changed');
        await reload();
        return false;
      }
      setFile(saved);
      return true;
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
      return false;
    }
  }, [projectId, reload]);

  return { values: file?.values ?? null, warnings: file?.warnings ?? [], problem, save, reload };
}

// An AGENTS.md file: the personal one, or a project's.
export function useInstructions(projectId) {
  const [file, setFile] = useState(null); // { text, hash, combined_bytes, cap_bytes, warnings }
  const [problem, setProblem] = useState(null);
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';

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

  const save = useCallback(async (text) => {
    setProblem(null);
    try {
      await put('/api/instructions', { text, hash: file?.hash, ...(projectId ? { project_id: projectId } : {}) });
      await reload();
      return true;
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
      if (error instanceof ApiError && error.code === 'settings_changed') await reload();
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

export function forgetModels() {
  listings.clear();
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

// What a message or a Continue sends for the picker's choice ({ provider, model, effort },
// or null for Auto).
export function messageRoute(chosen) {
  return chosen ? { model: chosen.model, provider: chosen.provider, ...(chosen.effort ? { effort: chosen.effort } : {}) }
    : { model: 'auto' };
}

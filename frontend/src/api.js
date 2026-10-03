// The backend's API: same-origin JSON requests that carry the local client header and this
// launch's session (backend/local_guard.py). Errors are { code, message }; the interface
// shows the code's translation, never the message.
import { EventReader } from './sse.js';
import { takeSession } from './session.js';

let session = null;

export function startSession(win = window) {
  session = takeSession(win.location, win.sessionStorage, win.history);
}

export class ApiError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

export function headers(json) {
  return {
    'X-Scholia-Client': 'local',
    ...(session ? { 'X-Scholia-Session': session } : {}),
    ...(json ? { 'Content-Type': 'application/json' } : {}),
  };
}

export async function api(method, path, body, { signal } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: headers(body !== undefined),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(0, 'unreachable');
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, data?.code ?? 'http_error');
  return data;
}

export const get = (path, options) => api('GET', path, undefined, options);
export const post = (path, body = {}) => api('POST', path, body);
export const put = (path, body) => api('PUT', path, body);
export const patch = (path, body) => api('PATCH', path, body);
export const del = (path) => api('DELETE', path);

// A turn's stream: calls onEvent for each event until the turn ends or signal aborts it.
export async function stream(path, body, onEvent, signal) {
  const response = await fetch(path, { method: 'POST', headers: headers(true), body: JSON.stringify(body), signal })
    .catch((error) => {
      if (error.name === 'AbortError') throw error;
      throw new ApiError(0, 'unreachable');
    });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiError(response.status, data?.code ?? 'http_error');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const events = new EventReader();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const event of events.push(decoder.decode(value, { stream: true }))) onEvent(event);
  }
}

// Saves the keys of one direct action (the layout, the open panel) with the file-hash
// precondition: it reads the file and writes only those keys, so every other key keeps the
// value on disk. If the file changes between the read and the write, nothing is written and
// the file's values stand, since the app never overwrites a change it has not shown
// (ticket 14); the action is simply not remembered. Settings forms reload and ask (S1-10).
export async function saveSettings(updates, projectId) {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
  const { hash } = await get(`/api/settings${query}`);
  try {
    return await put('/api/settings', { hash, updates, ...(projectId ? { project_id: projectId } : {}) });
  } catch (error) {
    if (error instanceof ApiError && error.code === 'settings_changed') return null;
    throw error;
  }
}

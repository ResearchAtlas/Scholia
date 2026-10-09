// Parts of the window loaded when they are first opened, not with the window, so the script loaded
// at start stays within docs/interface-criteria.md's Performance criterion (tools/script_size.mjs
// checks it). A part is a module's named component behind React.lazy, still served by the app
// itself; Boundary shows a fallback while it loads, and what to show instead if it cannot load.
import { Component, Suspense, createElement, lazy } from 'react';

// A part's module could not be loaded: its file missing or refused, or failing as it ran.
export class NotLoaded extends Error {}

// What React.lazy calls: the module's export by name, or NotLoaded when the module failed.
export function loader(load, name) {
  return () => load().then((module) => ({ default: module[name] }),
    (error) => { throw new NotLoaded(String(error?.message ?? error), { cause: error }); });
}

export const part = (load, name) => lazy(loader(load, name));

// Whether an event closes a modal dialog, as the installed Radix dialog decides for a press outside
// it: Escape, or a pointer press other than a right-click or a Control-click (macOS's context-menu
// click), which leave the dialog as it is.
export function closesDialog(event) {
  if (event.type === 'keydown') return event.key === 'Escape';
  return event.type === 'pointerdown' && event.button !== 2 && !(event.button === 0 && event.ctrlKey === true);
}

// A part that can be loaded before it is first drawn: load() loads it once; ready() settles when
// it has loaded or failed, never rejecting (what waits for it goes on, and the part then shows the
// failure); loaded() is its export once loaded, else null, and failed() whether it could not load,
// so either can be drawn at once, where React.lazy would show its fallback for one render first.
export function early(load, name) {
  let module = null;
  let failed = false;
  let loading = null;
  const once = () => (loading ??= load().then((loadedModule) => { module = loadedModule; return loadedModule; },
    (error) => { failed = true; throw error; }));
  return { load: once, ready: () => once().then(() => {}, () => {}), loaded: () => module?.[name] ?? null, failed: () => failed };
}

// The fallback while the part loads, and failed once it could not load. Any other error thrown
// while rendering the part goes on to the window, as it did before parts were loaded apart.
export class Boundary extends Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error instanceof NotLoaded) return this.props.failed;
    if (error) throw error;
    return createElement(Suspense, { fallback: this.props.fallback }, this.props.children);
  }
}

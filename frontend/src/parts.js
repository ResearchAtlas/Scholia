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

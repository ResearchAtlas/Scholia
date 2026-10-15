// Zotero's library in the interface (slice-1 spec F3c, S11; S1-15): the browser's requests, its
// collection tree, the selection's bound, and what an import's report says (backend/zotero.py).
import { get, post } from './api.js';

export const MAX_ITEMS = 100; // per import (backend/zotero.py MAX_ITEMS)

export const zoteroStatus = () => get('/api/zotero/status');
export const zoteroCollections = (library) => get(`/api/zotero/collections?library=${encodeURIComponent(library)}`);
export function zoteroItems(library, collection, start = 0) {
  const query = new URLSearchParams({ library, start: String(start), ...(collection ? { collection } : {}) });
  return get(`/api/zotero/items?${query}`);
}
export function importZotero(projectId, library, items, serverId) {
  return post(`/api/projects/${encodeURIComponent(projectId)}/materials/import-zotero`,
    { library, items, server_id: serverId ?? null });
}

// The collections as the browser lists them, each under its parent: [{ key, name, depth }] in order,
// siblings by name. A collection whose parent is missing, or which is in a cycle of parents, is
// listed at the top, so every collection is shown once.
export function collectionTree(collections, compare = (a, b) => a.localeCompare(b)) {
  const known = new Map(collections.map((c) => [c.key, c]));
  const rooted = new Set();
  const isRoot = (c) => {
    const seen = new Set([c.key]);
    for (let parent = known.get(c.parent); parent; parent = known.get(parent.parent)) {
      if (seen.has(parent.key)) return true; // a cycle: shown at the top
      seen.add(parent.key);
    }
    return !known.has(c.parent);
  };
  const children = new Map();
  for (const c of collections) {
    if (isRoot(c)) rooted.add(c.key);
    else children.set(c.parent, [...(children.get(c.parent) ?? []), c]);
  }
  const sorted = (list) => [...list].sort((a, b) => compare(a.name, b.name));
  const out = [];
  const placed = new Set();
  const walk = (c, depth) => {
    if (placed.has(c.key)) return;
    placed.add(c.key);
    out.push({ key: c.key, name: c.name, depth });
    for (const child of sorted(children.get(c.key) ?? [])) walk(child, depth + 1);
  };
  for (const c of sorted(collections.filter((c) => rooted.has(c.key)))) walk(c, 0);
  for (const c of sorted(collections)) walk(c, 0); // a cycle's members not reached through its root
  return out;
}

// The status's text and whether it says how to turn the API on, as catalog keys.
export const statusKey = (status) => `zotero.status.${status?.state ?? 'checking'}`;

// What an import's report says (its run's summary, read after its commit), as [catalog key, params]. A
// run that did not finish counts nothing (its summary holds its reason only): the items it committed
// before it stopped are in the Library, and it says so.
export function reportParts(summary) {
  if (!summary) return [];
  if (summary.imported === undefined) return [['zotero.report.kept', {}]];
  const notRead = Object.values(summary.pdf ?? {}).reduce((sum, n) => sum + n, 0);
  return [
    ['zotero.report.imported', { count: summary.imported ?? 0 }],
    summary.updated && ['zotero.report.updated', { count: summary.updated }],
    summary.versions && ['zotero.report.versions', { count: summary.versions }],
    summary.unchanged && ['zotero.report.unchanged', { count: summary.unchanged }],
    summary.waiting && ['zotero.report.waiting', { count: summary.waiting }],
    summary.already && ['zotero.report.already', { count: summary.already }],
    notRead && ['zotero.report.pdfNotRead', { count: notRead }],
    summary.not_found && ['zotero.report.notFound', { count: summary.not_found }],
    summary.no_title && ['zotero.report.noTitle', { count: summary.no_title }],
    summary.left_out && ['references.leftOut', { count: summary.left_out }],
  ].filter(Boolean);
}

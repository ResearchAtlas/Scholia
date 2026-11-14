// The world a scenario of tests/library_scenarios.test.mjs runs in, from design review O's appendix A2 (PR 61): the real
// components of one fresh module graph (load.mjs), deferred answers released in a fixed order, no timers. A scenario's
// globals (fetch, FileReader, the timers) are the caller's to put back (scenario() in the test does).
export async function world({ Library, library, mount, jsx }, { strict = false } = {}) {
  const asked = [];
  const papers = [paper('a', 'Paper A'), paper('b', 'Paper B')];
  const hold = { listing: false };
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    const listing = method === 'GET' && /\/materials$/.test(path);
    if (listing && !hold.listing) return Promise.resolve(response(200, { materials: structuredClone(papers), asks: [] }));
    if (method === 'GET' && !listing) return Promise.resolve(response(404, { code: 'not_found' }));
    return new Promise((resolve) => asked.push({ method, path, body: init.body ? JSON.parse(init.body) : null,
      answer: (body, status = 200) => resolve(response(status, body)), fail: (code = 'internal', status = 500) => resolve(response(status, { code })) }));
  };
  globalThis.FileReader = class { readAsDataURL(file) { this.result = `data:;base64,${file.data ?? ''}`; queueMicrotask(() => this.onload()); } };
  // no timer runs while the scenario does (the Library's polls); the test runner's own timers are put back after it
  globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  const ui = mount([jsx(Library, { project: { id: 'p1' } }, 'p1')], { strict }); // keyed by its project, as SidePanel draws it
  await ui.settle();
  // each element with the text inside it
  const elements = () => { const out = []; const stack = [];
    for (const h of ui.hosts()) { if (h.props) { const e = { type: h.type, props: h.props, text: [] }; out.push(e); stack.push(e); }
      else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text); }
    return out.map((e) => ({ ...e, text: e.text.join(' ') })); };
  const one = (test, what) => { const found = elements().filter(test); if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };
  const file = (name, size = 10) => ({ name, size, data: 'eA==' });
  const act = async (fn) => { fn(); await ui.settle(); }; // a handler's own promise is not waited for: its answer may be held
  return { ui, asked, papers, hold, library, file, elements,
    panel: (projectId) => act(() => ui.show(projectId ? [jsx(Library, { project: { id: projectId } }, projectId)] : null)),
    pending: (test) => asked.filter(test),
    says: (role) => elements().filter((e) => e.props.role === role).map((e) => e.text),
    shows: (text) => ui.text().includes(text),
    enabled: (label) => !one((e) => e.type === 'ui:Button' && e.text === label, label).props.disabled,
    click: (label) => act(() => one((e) => e.props.onClick && e.text === label, label).props.onClick({ preventDefault() {}, currentTarget: {} })),
    choose: (testid, ...files) => act(() => one((e) => e.props['data-testid'] === testid, testid).props.onChange({ target: { files, value: '' } })),
    drop: (...files) => act(() => one((e) => e.props.onDrop, 'drop zone').props.onDrop({ preventDefault() {}, stopPropagation() {}, dataTransfer: { files } })),
    type: (field, value) => act(() => one((e) => e.props.onChange && String(e.props.id ?? '').endsWith(`-${field}`), field).props.onChange({ target: { value } })),
    value: (field) => one((e) => e.props.onChange && String(e.props.id ?? '').endsWith(`-${field}`), field).props.value,
    save: () => act(() => one((e) => e.type === 'form' && e.props.onSubmit && e.props['aria-label'], 'details').props.onSubmit({ preventDefault() {} })),
    chip: (id) => act(() => library.openPaperAt('p1', id, null)),
    release: (request, how) => act(() => how(request)),
    changed: (projectId = 'p1') => act(() => library.libraryChanged(projectId)),
  };
}
function paper(id, title) {
  return { id, title, state: 'ready', csl: {}, source: 'upload', version: { id: `v-${id}`, media_type: 'application/pdf', size: 10, seq: 0 },
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', readable: true, retraction: null, extraction: null };
}
export const imports = (r) => /import-references$/.test(r.path);
export const uploads = (r) => r.method === 'POST' && /\/materials$/.test(r.path);
export const saves = (r) => r.method === 'PATCH';
export const rereads = (r) => /\/read$/.test(r.path);
export const report = (n) => (r) => r.answer({ imported: Array(n).fill('x'), waiting: [], already: [], skipped: {} });

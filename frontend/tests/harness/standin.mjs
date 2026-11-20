// A stand-in for React for the Library's scenarios (tests/library_scenarios.test.mjs), from design review O's
// appendix A2 (PR 61): the runner has no DOM and no React renderer.
// A stand-in for React with mount and unmount: instances by position (or key) and type, state kept per instance,
// an update to an unmounted instance ignored (as React ignores it), effects with cleanups, StrictMode's second effect run.
let current = null; let dirty = false; let strict = false;
const ids = new WeakMap(); let nextId = 0;
const idOf = (type) => (typeof type === 'string' ? type : (ids.has(type) ? ids.get(type) : (ids.set(type, `${type.name || 'fn'}#${++nextId}`), ids.get(type))));
export const Fragment = Symbol('Fragment');
export const jsx = (type, props, key) => ({ $el: true, type, key: key ?? null, props: props ?? {} });
export const jsxs = jsx; export const jsxDEV = jsx;
export const createElement = (type, props, ...children) => jsx(type, { ...props, ...(children.length ? { children: children.length === 1 ? children[0] : children } : {}) }, props?.key);
const slot = (make) => { const i = current.index++; if (!(i in current.slots)) current.slots[i] = make(); return current.slots[i]; };
const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
export function useState(initial) {
  const instance = current;
  const s = slot(() => ({ value: typeof initial === 'function' ? initial() : initial }));
  s.set ??= (next) => { if (instance.dead) return; const value = typeof next === 'function' ? next(s.value) : next;
    if (!Object.is(value, s.value)) { s.value = value; dirty = true; } };
  return [s.value, s.set];
}
export const useReducer = (reducer, initial) => { const [v, set] = useState(initial); return [v, (action) => set((now) => reducer(now, action))]; };
export const useRef = (initial) => slot(() => ({ current: initial }));
export const useId = () => slot(() => ({ id: `id${++nextId}` })).id;
export function useMemo(fn, deps) { const i = current.index++; const s = current.slots[i]; if (s && same(s.deps, deps)) return s.value;
  current.slots[i] = { value: fn(), deps }; return current.slots[i].value; }
export const useCallback = (fn, deps) => useMemo(() => fn, deps);
export function useEffect(fn, deps) { const i = current.index++; const s = current.slots[i];
  if (s && deps && same(s.deps, deps)) return; current.effects.push([i, fn, deps]); }
export const useLayoutEffect = useEffect;
export const useContext = (context) => context.value;
export const createContext = (value) => ({ value, Provider: ({ children }) => children });
export function useSyncExternalStore(subscribe, snapshot) { useEffect(() => subscribe(() => { dirty = true; }), [subscribe]); return snapshot(); }
export const lazy = () => () => null; export const Suspense = ({ children }) => children; export class Component {}
export const forwardRef = (fn) => (props) => fn(props, props.ref); export const memo = (fn) => fn;

export function mount(element, options = {}) {
  strict = Boolean(options.strict);
  const instances = new Map();
  let hosts = [];
  function visit(node, path) {
    if (node === null || node === undefined || typeof node === 'boolean') return;
    if (typeof node === 'string' || typeof node === 'number') { hosts.push({ text: String(node) }); return; }
    if (Array.isArray(node)) { node.forEach((child, i) => visit(child, `${path}/${child?.key ?? i}`)); return; }
    const at = `${path}<${idOf(node.type === Fragment ? 'Fragment' : node.type)}>`;
    if (node.type === Fragment) { visit(node.props.children, at); return; }
    if (typeof node.type === 'string') { hosts.push({ type: node.type, props: node.props }); visit(node.props.children, at); hosts.push({ end: node.type }); return; }
    let instance = instances.get(at);
    if (!instance) { instance = { slots: [], dead: false, fresh: true }; instances.set(at, instance); }
    instance.seen = true; instance.index = 0; instance.effects = [];
    const before = current; current = instance;
    let out; try { out = node.type(node.props); } finally { current = before; }
    visit(out, at);
  }
  function once() {
    hosts = [];
    for (const instance of instances.values()) instance.seen = false;
    visit(element, '');
    for (const [at, instance] of instances) if (!instance.seen) {
      instance.dead = true; instances.delete(at);
      for (const s of instance.slots) s?.cleanup?.();
    }
    for (const instance of instances.values()) {
      for (const [i, fn, deps] of instance.effects) {
        instance.slots[i]?.cleanup?.();
        let cleanup = fn();
        if (strict && instance.fresh) { cleanup?.(); cleanup = fn(); } // StrictMode: a new instance's effects run twice
        instance.slots[i] = { deps, cleanup };
      }
      instance.effects = []; instance.fresh = false;
    }
  }
  const ui = {
    draw() { for (let n = 0; n === 0 || dirty; n += 1) { if (n > 50) throw new Error('renders do not settle'); dirty = false; once(); } return ui; },
    // every answer released so far is taken in and drawn: no timer runs, only the microtasks and immediates queued
    async settle() { for (let n = 0; n < 8; n += 1) { await new Promise((resolve) => setImmediate(resolve)); if (dirty) ui.draw(); } return ui; },
    show(next) { element = next; dirty = true; return ui.draw(); },
    hosts: () => hosts,
    find: (test) => hosts.filter((h) => h.props && test(h.props, h.type)),
    text() { return hosts.filter((h) => h.text).map((h) => h.text).join(' '); },
    // the text inside the elements matching test (role="status", role="alert", ...)
    said(test) { const out = []; let depth = 0; let buffer = null;
      for (const h of hosts) { if (h.props && buffer === null && test(h.props, h.type)) { buffer = []; depth = 0; }
        else if (buffer !== null) { if (h.props) depth += 1; else if (h.end) { if (depth === 0) { out.push(buffer.join(' ')); buffer = null; } else depth -= 1; } else buffer.push(h.text); } }
      return out; },
  };
  return ui.draw();
}

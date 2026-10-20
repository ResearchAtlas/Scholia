// The gold sets' review page (evals/gold.py review): one set, one rater. Every text from the set or the
// papers is inserted as text, never as markup; the page's own words come from catalog.json. Each
// decision is posted to the server, which writes it to the labels file before answering; the page says
// saved only on that answer, and keeps a choice that was not saved for another try.
'use strict';

(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  let session = params.get('session');
  let lang = params.get('lang');
  try {  // kept for a reload of this tab; storage may be off, and then this load works alone
    if (session) { sessionStorage.setItem('gold.session', session); sessionStorage.setItem('gold.lang', lang || 'en'); }
    else { session = sessionStorage.getItem('gold.session'); lang = sessionStorage.getItem('gold.lang'); }
  } catch { /* nothing kept */ }
  lang = lang === 'zh-CN' ? 'zh-CN' : 'en';

  let strings = {};
  const t = (key, vars = {}) => (strings[key] ?? key).replace(/\{(\w+)\}/g, (all, name) => (name in vars ? String(vars[name]) : all));
  const $ = (id) => document.getElementById(id);
  const el = (tag, attrs = {}, ...children) => {
    const e = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) {
      if (name === 'text') e.textContent = value;
      else if (name === 'on') for (const [event, fn] of Object.entries(value)) e.addEventListener(event, fn);
      else if (value === true) e.setAttribute(name, '');
      else if (value !== false && value != null) e.setAttribute(name, value);
    }
    e.append(...children.filter((c) => c != null));
    return e;
  };
  const api = async (path, init = {}) => {
    const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Scholia-Client': 'local',
                                                             'X-Scholia-Session': session || '' } });
    let body = null;
    try { body = await response.json(); } catch { /* no body */ }
    return { status: response.status, body };
  };

  let data = null;
  let index = 0;
  let editing = null;  // the edit form's state while a question is being edited
  let pending = null;  // the last save not answered as saved, for Try again
  const passageCache = {};

  const item = () => data.items[index];
  const stateOf = (id) => data.labels[id]?.state ?? 'open';
  const firstOpen = (from = 0) => {
    for (let k = 0; k < data.items.length; k += 1) {
      const i = (from + k) % data.items.length;
      if (stateOf(data.items[i].id) !== 'done') return i;
    }
    return -1;
  };

  function setStatus(text, retry = false) {
    const line = $('statusLine');
    $('status').textContent = text;
    line.querySelector('button')?.remove();
    if (retry) line.append(el('button', { type: 'button', text: t('status.retry'), on: { click: () => send(pending) } }));
  }

  // The question as the rater's label leaves it (as gold.py's labeled does).
  function effective(question) {
    const label = data.labels[question.id];
    if (!label || label.state !== 'done' || label.decision !== 'edit') return question;
    const out = { ...question };
    for (const name of ['text', 'short_answer', 'answerable', 'evidence']) if (name in label) out[name] = label[name];
    if (!out.answerable) { delete out.short_answer; out.evidence = []; }
    return out;
  }

  function where(loc) {
    const paper = data.papers[loc.paper] ?? loc.paper;
    return loc.page == null ? t('passage.wherePageless', { paper }) : t('passage.where', { paper, page: loc.page });
  }

  function passageView(loc, lang) {
    return el('figure', { class: 'passage' },
      el('figcaption', {}, el('span', { text: where(loc) }),
        loc.section_path?.length ? el('span', { class: 'section', text: t('passage.section', { section: loc.section_path.join(' › ') }) }) : null),
      el('blockquote', { lang: lang ?? null, text: loc.quote ?? loc.text }));
  }

  function renderList() {
    const list = $('list');
    list.replaceChildren(...data.items.map((it, i) => {
      const state = stateOf(it.id);
      return el('li', {}, el('button', { type: 'button', class: `item ${state}`, 'aria-current': i === index ? 'true' : null,
                                       'aria-label': t('nav.item', { n: i + 1, state: t(`state.${state}`) }), text: String(i + 1),
                                       on: { click: () => go(i) } }));
    }));
  }

  function questionBody(question) {
    const shown = effective(question);
    const label = data.labels[question.id];
    const body = [el('p', { class: 'meta', text: t('q.asked', { language: t(`lang.${shown.query_language}`) }) }),
      el('h3', { text: t('q.question') }), el('p', { class: 'text', lang: shown.query_language, text: shown.text })];
    if (shown.answerable) {
      body.push(el('h3', { text: t('q.shortAnswer') }), el('p', { lang: shown.query_language, text: shown.short_answer }));
      body.push(el('h3', { text: t('q.evidence') }));
      shown.evidence.forEach((ev, n) => body.push(el('section', { class: 'evidence', 'aria-label': t('q.evidenceItem', { n: n + 1 }) },
        el('h4', { text: t('q.evidenceItem', { n: n + 1 }) }), ...ev.map((loc) => passageView(loc, shown.document_language)))));
    } else {
      body.push(el('p', { class: 'unanswerable', text: t('q.unanswerable') }));
    }
    if (label?.state === 'done') body.push(el('p', { class: 'label', text: t(`q.label.${label.decision}`) }));
    const edited = label?.state === 'done' && label.decision === 'edit';
    body.push(el('div', { class: 'actions' },
      el('button', { type: 'button', 'aria-keyshortcuts': 'K', text: edited ? t('q.keepEdited') : t('q.keep'), on: { click: keepShown } }),
      el('button', { type: 'button', 'aria-keyshortcuts': 'E', text: t('q.edit'), on: { click: startEdit } }),
      el('button', { type: 'button', 'aria-keyshortcuts': 'D', text: t('q.drop'), on: { click: dropShown } }),
      edited ? el('button', { type: 'button', text: t('q.revert'), on: { click: () => decide({ decision: 'keep' }, { revert: true }) } }) : null));
    return body;
  }

  // A passage as the page sends it back: its paper, ordinal and the key the server gave it, so that a
  // passage is never saved under another paper.
  const choice = (loc) => ({ paper: loc.paper, ordinal: loc.ordinal, key: loc.key });

  // Dropping an edited question discards its edit: asked first, so a stray key press loses nothing.
  function dropShown() {
    const label = data.labels[item().id];
    if (label?.state === 'done' && label.decision === 'edit' && !window.confirm(t('q.dropEdited'))) return;
    decide({ decision: 'drop' });
  }

  // Keep keeps what the page shows: after an edit, the edit as saved (going back to the draft is Revert).
  function keepShown() {
    const label = data.labels[item().id];
    if (!(label?.state === 'done' && label.decision === 'edit')) { decide({ decision: 'keep' }); return; }
    const again = { decision: 'edit' };
    for (const name of ['text', 'short_answer', 'answerable']) if (name in label) again[name] = label[name];
    if ('evidence' in label) again.evidence = label.evidence.map((ev) => ev.map(choice));
    decide(again);
  }

  function startEdit() {
    const shown = effective(item());
    editing = { text: shown.text, short_answer: shown.short_answer ?? '', answerable: shown.answerable,
                evidence: shown.evidence.map((ev) => ev.map((loc) => ({ ...loc }))),
                paper: shown.evidence[0]?.[0]?.paper ?? Object.keys(data.papers)[0], filter: '', target: 'new' };
    // What the form started from: a save sends only what the rater changed from it, and the server keeps
    // every other field as saved (another window's edit included), so nothing is overwritten unasked.
    editing.from = { text: editing.text, short_answer: editing.short_answer, answerable: editing.answerable,
                     evidence: evidenceKey(editing.evidence) };
    render();
    $('editText')?.focus();
  }

  async function passagesOf(paper) {
    if (!passageCache[paper]) {
      const answer = await api(`/api/passages?paper=${encodeURIComponent(paper)}`);
      passageCache[paper] = answer.status === 200 ? answer.body.passages : [];
    }
    return passageCache[paper];
  }

  function editBody() {
    const question = item();
    const form = el('form', { class: 'edit', on: { submit: (event) => { event.preventDefault(); saveEdit(); } } });
    if (editing.showSaved) {  // after a conflict: the question as saved elsewhere, above the rater's open edit
      const saved = effective(question);
      form.append(el('section', { class: 'saved-elsewhere', 'aria-label': t('edit.savedElsewhere') },
        el('h3', { text: t('edit.savedElsewhere') }), el('p', { lang: question.query_language, text: saved.text }),
        saved.answerable ? el('p', { lang: question.query_language, text: `${t('q.shortAnswer')}: ${saved.short_answer}` })
          : el('p', { text: t('q.unanswerable') }),
        ...(saved.answerable ? saved.evidence.map((ev, n) => el('div', { class: 'evidence' },
          el('h4', { text: t('q.evidenceItem', { n: n + 1 }) }), ...ev.map((loc) => passageView(loc, question.document_language)))) : [])));
    }
    form.append(el('h3', { text: t('edit.heading') }),
      el('label', { for: 'editText', text: t('edit.text') }),
      el('textarea', { id: 'editText', rows: '3', lang: question.query_language,
                       on: { input: (event) => { editing.text = event.target.value; } } }),
      el('label', { class: 'check' }, el('input', { type: 'checkbox', id: 'editAnswerable', checked: editing.answerable,
                                                     on: { change: (event) => { editing.answerable = event.target.checked; render(); } } }),
        el('span', { text: t('edit.answerable') })));
    form.querySelector('#editText').value = editing.text;
    if (editing.answerable) {
      const answer = el('input', { id: 'editShort', type: 'text', lang: question.query_language,
                                   on: { input: (event) => { editing.short_answer = event.target.value; } } });
      answer.value = editing.short_answer;
      form.append(el('label', { for: 'editShort', text: t('edit.shortAnswer') }), answer, el('h3', { text: t('q.evidence') }));
      editing.evidence.forEach((ev, n) => form.append(el('section', { class: 'evidence' },
        el('h4', { text: t('q.evidenceItem', { n: n + 1 }) }), ...ev.map((loc) => passageView(loc, question.document_language)),
        el('button', { type: 'button', text: t('edit.removeItem', { n: n + 1 }),
                       on: { click: () => { editing.evidence.splice(n, 1); render(); } } }))));
      form.append(picker(question));
    }
    form.append(el('div', { class: 'actions' }, el('button', { type: 'submit', text: t('edit.save') }),
      el('button', { type: 'button', text: t('edit.cancel'), on: { click: () => { editing = null; render(); } } })));
    return [form];
  }

  function picker(question) {
    const box = el('fieldset', { class: 'picker' }, el('legend', { text: t('edit.add') }));
    const paper = el('select', { id: 'pickPaper', on: { change: (event) => { editing.paper = event.target.value; fill(); } } },
      ...Object.entries(data.papers).map(([id, title]) => el('option', { value: id, text: title })));
    paper.value = editing.paper;
    const filter = el('input', { id: 'pickFilter', type: 'search', on: { input: (event) => { editing.filter = event.target.value; fill(); } } });
    filter.value = editing.filter;
    const target = el('select', { id: 'pickTarget', on: { change: (event) => { editing.target = event.target.value; } } },
      el('option', { value: 'new', text: t('edit.newItem') }),
      ...editing.evidence.map((_, n) => el('option', { value: String(n), text: t('q.evidenceItem', { n: n + 1 }) })));
    target.value = editing.evidence[Number(editing.target)] ? editing.target : 'new';
    const results = el('ul', { class: 'results', 'aria-live': 'polite' });
    box.append(el('label', { for: 'pickPaper', text: t('edit.paper') }), paper, el('label', { for: 'pickFilter', text: t('edit.filter') }),
      filter, el('label', { for: 'pickTarget', text: t('edit.addTo') }), target, results);
    async function fill() {
      const from = editing.paper;  // each result is bound to the paper it was loaded for
      const all = await passagesOf(from);
      if (!editing || editing.paper !== from) return;  // another paper was chosen meanwhile: this load is stale
      const wanted = editing.filter.trim().toLowerCase();
      const found = all.filter((p) => !wanted || p.text.toLowerCase().includes(wanted));
      const shown = found.slice(0, 50);
      results.replaceChildren(...(found.length ? [] : [el('li', { text: t('edit.noMatches') })]),
        ...(found.length > shown.length ? [el('li', { class: 'meta', text: t('edit.shownOf', { shown: shown.length, total: found.length }) })] : []),
        ...shown.map((p) => el('li', {}, passageView({ ...p, paper: from }, question.document_language),
          el('button', { type: 'button', text: t('edit.addThis'), on: { click: () => {
            const loc = { paper: from, ordinal: p.ordinal, page: p.page, kind: p.kind, section_path: p.section_path, quote: p.text, key: p.key };
            if (editing.target === 'new' || !editing.evidence[Number(editing.target)]) editing.evidence.push([loc]);
            else editing.evidence[Number(editing.target)].push(loc);
            render();
          } } }))));
    }
    fill();
    return box;
  }

  const evidenceKey = (evidence) => JSON.stringify(evidence.map((ev) => ev.map((loc) => [loc.paper, loc.ordinal])));

  function saveEdit() {
    const from = editing.from;
    const label = { decision: 'edit' };
    if (editing.text !== from.text) label.text = editing.text;
    if (editing.answerable !== from.answerable) label.answerable = editing.answerable;
    if (editing.answerable && editing.short_answer !== from.short_answer) label.short_answer = editing.short_answer;
    if (editing.answerable && evidenceKey(editing.evidence) !== from.evidence) {
      label.evidence = editing.evidence.map((ev) => ev.map(choice));
    }
    if (Object.keys(label).length === 1) { setStatus(t('error.edit_needs_a_change')); return; }
    decide(label);  // an edit builds on the saved one on the server: what it does not name stays as saved
  }

  function pairBody(pair) {
    const label = data.labels[pair.id];
    const choose = (category, key) => el('button', { type: 'button', 'aria-keyshortcuts': key, 'aria-pressed': label?.state === 'done' && label.category === category ? 'true' : 'false',
                                                      text: `${key} ${t(`p.${category}`)}`, on: { click: () => decide({ category }) } });
    return [el('h3', { text: t('p.sentence') }), el('p', { class: 'text', lang: pair.sentence_language, text: pair.sentence }),
      el('h3', { text: t('p.passage') }), passageView(pair.passage, null),
      pair.drafted_category ? el('p', { class: 'meta', text: t('p.drafted', { category: t(`p.${pair.drafted_category}`) }) }) : null,
      label?.state === 'done' ? el('p', { class: 'label', text: t('p.label', { category: t(`p.${label.category}`) }) }) : null,
      el('p', { class: 'ask', text: t('p.ask') }),
      el('div', { class: 'actions' }, choose('supported', '1'), choose('partly', '2'), choose('unsupported', '3'))];
  }

  function render() {
    const it = item();
    $('progress').textContent = t('progress', { done: data.done, total: data.total });
    $('itemHeading').textContent = t('item.heading', { n: index + 1, total: data.items.length });
    $('itemState').textContent = t(`state.${stateOf(it.id)}`);
    $('itemState').className = `state ${stateOf(it.id)}`;
    $('body').replaceChildren(...(data.kind === 'pairs' ? pairBody(it) : editing ? editBody() : questionBody(it)).filter(Boolean));
    $('prev').disabled = index === 0;
    $('next').disabled = index === data.items.length - 1;
    $('nextOpen').disabled = firstOpen() === -1;
    renderList();
  }

  function go(i) {
    if (i < 0 || i >= data.items.length) return;
    if (editing && !window.confirm(t('edit.discard'))) return;  // an open edit is never dropped unasked
    index = i;
    editing = null;
    render();
    $('itemHeading').focus();
  }

  let saving = false;  // one save at a time: a second key press while one is on its way is ignored

  function decide(label, extra = {}) {
    send({ item: item().id, base: data.labels[item().id]?.revision ?? 0, label, ...extra });
  }

  async function send(request) {
    if (saving) return;
    saving = true;
    try { await save(request); } finally { saving = false; }
  }

  async function save(request) {
    pending = request;
    const n = data.items.findIndex((it) => it.id === request.item) + 1;
    setStatus(t('status.saving'));
    let answer;
    try { answer = await api('/api/label', { method: 'POST', body: JSON.stringify(request) }); } catch { answer = { status: 0, body: null }; }
    if (answer.status === 200) {
      pending = null;
      data.labels[request.item] = answer.body.label;
      data.done = answer.body.done;
      editing = null;
      const next = firstOpen(index + 1);
      if (next !== -1 && data.items[index].id === request.item) index = next;
      render();
      $('itemHeading').focus();
      setStatus(t('status.saved', { n }));
    } else if (answer.status === 409) {
      pending = null;
      data.labels[request.item] = answer.body.label;
      if (editing) {
        // Not saved. The open edit stays as typed, with the label as saved now shown above it; saving it again
        // sends only the changes made in it, on top of that label.
        editing.showSaved = true;
        render();
        setStatus(answer.body.code === 'change_not_kept' ? t('status.changeNotKept', { n }) : t('status.conflictEditing', { n }));
      } else {
        render();
        setStatus(answer.body.code === 'change_not_kept' ? t('status.notKept', { n }) : t('status.conflict', { n }));
      }
    } else if (answer.status === 400 || answer.status === 413) {
      pending = null;
      setStatus(t(`error.${answer.body?.code ?? 'label_invalid'}`));
    } else if (answer.status === 401) {
      setStatus(t('error.session_required'));
    } else {
      setStatus(t('status.notSaved', { n }), true);
    }
  }

  document.addEventListener('keydown', (event) => {
    if (!data || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target.closest('input, textarea, select')) {
      if (event.key === 'Escape' && editing) { editing = null; render(); $('itemHeading').focus(); }
      return;
    }
    const key = event.key.toLowerCase();
    const actions = data.kind === 'pairs'
      ? { 1: () => decide({ category: 'supported' }), 2: () => decide({ category: 'partly' }), 3: () => decide({ category: 'unsupported' }) }
      : editing ? { escape: () => { editing = null; render(); } }
        : { k: keepShown, e: startEdit, d: dropShown };
    const move = { arrowleft: () => go(index - 1), arrowright: () => go(index + 1) };
    const action = actions[key] ?? (editing ? null : move[key]);
    if (action) { event.preventDefault(); action(); }
  });

  async function start() {
    try { strings = (await (await fetch('catalog.json')).json())[lang]; } catch { strings = {}; }
    document.documentElement.lang = lang;
    for (const e of document.querySelectorAll('[data-i18n]')) e.textContent = t(e.dataset.i18n);
    for (const e of document.querySelectorAll('[data-i18n-aria]')) e.setAttribute('aria-label', t(e.dataset.i18nAria));
    const answer = await api('/api/data').catch(() => ({ status: 0 }));
    if (answer.status !== 200) { $('title').textContent = t('load.failed'); $('card').hidden = true; return; }
    data = answer.body;
    const title = t(`title.${data.kind}`, { field: data.field });
    $('title').textContent = data.sample ? t('title.sample', { title }) : title;
    document.title = `${$('title').textContent} · ${t('page.title')}`;
    $('keys').textContent = t(`keys.${data.kind}`);
    if (data.notices.length) { $('notice').textContent = data.notices.map((n) => t(`notice.${n}`)).join(' '); $('notice').hidden = false; }
    $('prev').addEventListener('click', () => go(index - 1));
    $('next').addEventListener('click', () => go(index + 1));
    $('nextOpen').addEventListener('click', () => go(firstOpen(index + 1)));
    if (!data.items.length) { $('card').hidden = true; $('progress').textContent = t('progress', { done: 0, total: 0 }); return; }
    index = Math.max(firstOpen(), 0);
    render();
    $('itemHeading').focus();
  }

  start();
})();

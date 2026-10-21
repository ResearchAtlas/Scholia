// An answer's Markdown, imported only with import() by components/Parts.jsx, so it is a chunk of its
// own named for it (markdown-*.js) with react-markdown and remark-gfm in it, not in the start script.
// No raw HTML is drawn (skipHtml); only http(s) and mailto links are kept, and images become links
// (links.js). GitHub Markdown adds tables, strikethrough and autolinks (ticket 71).
//
// S1-19b: an answer's citation markers ([S1.2], as backend/citations.py reads them) become chips. Each
// chip is the citation the answer's record holds at that marker's place in the text; hovering or
// focusing it shows its paper, page and quote, and pressing it opens the paper at its passage. A marker
// whose citation does not exist (not found, not read in the run, its source removed) is shown as such,
// never as a chip that pretends. A sentence no passage supports is underlined quietly; its text is never
// changed. A marker in code stays code.
import { createElement as h, useId } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { imageAsLink, safeHref } from './links.js';

const MARKER = /\[S[^\]\n]{0,12}\]/g; // citations.py: CITATION, and _MALFORMED for one that does not parse
const RUN_GAP = /^[ \t]*$/; // markers side by side ([S1.1][S1.2]) cite one sentence together
const SENTENCE_END = /[.!?](?=\s)|[。！？]/g;

// The citation for a marker at a place in the answer's text: the one recorded at that offset, else the
// first not yet shown with the same marker (a text whose offsets moved, as an escape can make them).
function finder(citations) {
  const used = new Set();
  return (marker, offset) => {
    let index = citations.findIndex((c, i) => !used.has(i) && c.offset === offset && c.marker === marker);
    if (index < 0) index = citations.findIndex((c, i) => !used.has(i) && c.marker === marker);
    if (index >= 0) used.add(index);
    return index;
  };
}

// The text from the start of its sentence, within one text node, to a marker run.
function sentenceStart(before) {
  let start = 0;
  for (const match of before.matchAll(SENTENCE_END)) start = match.index + match[0].length;
  return start;
}

// remark plugin: markers in text become `cite` elements naming their citation's index in the record, and
// the text of an unsupported sentence before them a `span.unsupported`. Text in code, and in a link (a
// chip is a button), is left as it is.
export function citationChips({ citations = [] } = {}) {
  return (tree) => {
    const find = finder(citations);
    const walk = (node, inLink) => {
      if (!node.children) return;
      const out = [];
      for (const child of node.children) {
        if (child.type === 'text' && !inLink) out.push(...split(child, find, citations));
        else {
          walk(child, inLink || child.type === 'link' || child.type === 'linkReference');
          out.push(child);
        }
      }
      node.children = out;
    };
    walk(tree, false);
  };
}

function split(node, find, citations) {
  const value = node.value;
  const base = node.position?.start?.offset;
  const matches = [...value.matchAll(MARKER)];
  if (!matches.length) return [node];
  const runs = []; // consecutive markers, each run with the matches in it
  for (const match of matches) {
    const last = runs.at(-1);
    const previous = last?.at(-1);
    if (previous && RUN_GAP.test(value.slice(previous.index + previous[0].length, match.index))) last.push(match);
    else runs.push([match]);
  }
  const out = [];
  let cursor = 0;
  for (const run of runs) {
    const before = value.slice(cursor, run[0].index);
    const chips = run.map((match) => ({ match, index: find(match[0], base === undefined ? -1 : base + match.index) }));
    const unsupported = chips.some(({ index }) => citations[index]?.support === 'unsupported' && citations[index]?.existence === 'ok');
    const start = unsupported ? sentenceStart(before) : before.length;
    if (before.slice(0, start)) out.push({ type: 'text', value: before.slice(0, start) });
    if (before.slice(start)) {
      out.push({ type: 'unsupported', data: { hName: 'span', hProperties: { className: ['unsupported'] } },
        children: [{ type: 'text', value: before.slice(start) }] });
    }
    let end = run[0].index;
    for (const { match, index } of chips) {
      const gap = value.slice(end, match.index);
      if (gap) out.push({ type: 'text', value: gap });
      out.push(index < 0 ? { type: 'text', value: match[0] } : {
        type: 'citation', data: { hName: 'cite', hProperties: { dataCitation: String(index) } },
        children: [{ type: 'text', value: match[0] }],
      });
      end = match.index + match[0].length;
    }
    cursor = end;
  }
  if (value.slice(cursor)) out.push({ type: 'text', value: value.slice(cursor) });
  return out;
}

const TYPE_NOTE = { abstract: 'cite.abstract', metadata: 'cite.metadata', user_note: 'cite.userNote' };
const SUPPORT = { supported: 'cite.supported', partly: 'cite.partly', unsupported: 'cite.unsupported',
  unchecked: 'cite.unchecked' };
const MISSING = { not_found: 'cite.notFound', not_read_in_run: 'cite.notRead', source_removed: 'cite.removed' };

// The source's number in a marker ([S2.3] is 2), as the chip shows it.
const sourceOf = (marker) => /^\[S([1-9][0-9]*)\./.exec(marker ?? '')?.[1] ?? '?';

// A citation chip: a button with the source's number, its card shown while hovered or focused.
export function Chip({ citation, t, onOpen }) {
  const id = useId();
  if (!citation || citation.existence !== 'ok') {
    const label = t(MISSING[citation?.existence] ?? 'cite.notFound');
    return h('span', { className: 'cite-missing', title: label },
      h('span', { 'aria-hidden': 'true' }, citation?.marker ?? '?'), h('span', { className: 'sr-only' }, label));
  }
  const notes = [citation.retracted && t('cite.retracted'), TYPE_NOTE[citation.evidence_type] && t(TYPE_NOTE[citation.evidence_type]),
    t(SUPPORT[citation.support] ?? 'cite.unchecked')].filter(Boolean);
  const kind = [citation.retracted && 'cite-retracted', TYPE_NOTE[citation.evidence_type] && 'cite-partial',
    citation.support === 'unsupported' && 'cite-unsupported'].filter(Boolean).join(' ');
  return h('span', { className: 'cite group' },
    h('button', { type: 'button', className: `cite-chip ${kind}`, 'aria-describedby': id,
      'aria-label': t('cite.open', { number: sourceOf(citation.marker), title: citation.title ?? t('cite.untitled') }),
      onClick: () => onOpen?.(citation) }, sourceOf(citation.marker), citation.retracted ? '!' : null),
    h('span', { id, role: 'tooltip', className: 'cite-card' },
      h('span', { className: 'cite-title' }, citation.title ?? t('cite.untitled'),
        citation.page ? ` · ${t('cite.page', { page: citation.page })}` : ''),
      citation.quote ? h('span', { className: 'cite-quote' }, `“${citation.quote.slice(0, 300)}${citation.quote.length > 300 ? '…' : ''}”`) : null,
      h('span', { className: 'cite-notes' }, notes.join(' · '))));
}

// An answer: its Markdown, with its citations as chips. t is the interface's translate; onOpen(citation)
// opens a cited paper at its passage. Without citations (an answer still arriving, before its checks are
// recorded), markers stay as they were written.
export default function Answer({ text, citations = null, t, onOpen }) {
  return h(ReactMarkdown, {
    skipHtml: true,
    remarkPlugins: citations ? [remarkGfm, [citationChips, { citations }]] : [remarkGfm],
    components: {
      a: ({ href, children }) => {
        const safe = safeHref(href);
        return safe ? h('a', { href: safe, target: '_blank', rel: 'noreferrer noopener' }, children) : h('span', null, children);
      },
      img: ({ src, alt }) => { // a placeholder with its alt text and host (PR02A), never loaded
        const { href, label, host } = imageAsLink(src, alt);
        const shown = label ? t('turn.image', { label }) : t('turn.imageUnnamed');
        return href ? h('a', { href, target: '_blank', rel: 'noreferrer noopener' }, shown, ' ',
          h('span', { className: 'text-muted-foreground' }, t('turn.imageFrom', { host }))) : h('span', null, shown);
      },
      cite: (props) => {
        const index = Number(props['data-citation']);
        return h(Chip, { citation: citations[index], t, onOpen });
      },
    },
  }, text);
}

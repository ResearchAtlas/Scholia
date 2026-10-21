// An answer's Markdown, imported only with import() by components/Parts.jsx, so it is a chunk of its
// own named for it (markdown-*.js) with react-markdown and remark-gfm in it, not in the start script.
// No raw HTML is drawn (skipHtml); only http(s) and mailto links are kept, and images become links
// (links.js). GitHub Markdown adds tables, strikethrough and autolinks (ticket 71).
//
// S1-19b: an answer's citation markers ([S1.2], as backend/citations.py reads them) become chips. Each
// chip is the citation the answer's record holds at that marker's place in the text; hovering or
// focusing it shows its paper, page and quote, and pressing it opens the paper at its passage. A marker
// whose citation does not exist (not found, not read in the run, its source removed) is shown as such,
// never as a chip that pretends. A sentence no passage supports is underlined quietly (one a cited passage
// supports is not, and the chip of a passage that does not says so of itself); its text is never changed. A
// marker in code stays code.
import { createElement as h, useId } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { imageAsLink, safeHref } from './links.js';

const MARKER = /\[S[^\]\n]{0,12}\]/g; // citations.py: CITATION, and _MALFORMED for one that does not parse
const RUN_GAP = /^[ \t]*$/; // markers side by side ([S1.1][S1.2]) cite one sentence together
const SENTENCE_END = /[.!?](?=\s)|[。！？]|\n/g; // as backend/citations.py: a line's end too

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

const BLOCKS = new Set(['paragraph', 'heading', 'tableRow']); // a sentence never runs past one of these
const MARKERS_AND_BARS = /\[S[^\]\n]{0,12}\]|\|/g;

// The sentences a citation supports (supported or partly), as the backend judges a sentence by each passage
// it cites: one such citation means a passage supports the sentence, whatever another citation's verdict.
const isOk = (c) => c?.existence === 'ok';
export function backedSentences(citations) {
  return new Set(citations.filter((c) => isOk(c) && (c.support === 'supported' || c.support === 'partly') && c.sentence)
    .map((c) => c.sentence));
}

// The consecutive markers in a text ([S1.1][S1.2] cite one sentence together), each with its citation. A run is
// underlined when a citation in it is unsupported and nothing supports its sentence.
function runsOf(node, find, citations, backed) {
  const value = node.value;
  const base = node.position?.start?.offset;
  const runs = [];
  for (const match of value.matchAll(MARKER)) {
    const chip = { start: match.index, end: match.index + match[0].length, marker: match[0],
      index: find(match[0], base === undefined ? -1 : base + match.index) };
    const last = runs.at(-1);
    if (last && RUN_GAP.test(value.slice(last.end, chip.start))) { last.chips.push(chip); last.end = chip.end; } else {
      runs.push({ start: chip.start, end: chip.end, chips: [chip] });
    }
  }
  for (const run of runs) {
    const cited = run.chips.map(({ index }) => citations[index]).filter(isOk);
    run.unsupported = cited.some((c) => c.support === 'unsupported')
      && !cited.some((c) => c.support === 'supported' || c.support === 'partly' || backed.has(c.sentence));
  }
  return runs;
}

// Where sentences end in a leaf's text: after . ! ? before a space (or before the next leaf's space, as
// `**Wages rose.** [S1.1]` ends its sentence), and after 。！？.
function sentenceEnds(value, next) {
  return [...(value + next).matchAll(SENTENCE_END)].map((m) => m.index + m[0].length).filter((at) => at <= value.length);
}

// Mark the sentence a marker run at `to` in leaves[i] cites, from `from` (the previous run's end) back to the
// sentence's start, across the leaves of its block (bold, a link, a table row's cells): each leaf gets its own
// range. A run that follows its sentence's end ("Wages rose. [S1.1]", "| Wages rose. | [S1.1] |") cites the
// sentence before it, as backend/citations.py reads it.
function markBack(leaves, i, from, to) {
  const added = []; // [leaf, [start, end]], from the run back to the sentence's start
  let seen = '';
  for (let j = i; j >= 0 && leaves[j].block === leaves[i].block; j -= 1) {
    const value = leaves[j].node.value;
    const [lower, upper] = j === i ? [from, to] : [0, value.length];
    const next = j + 1 < leaves.length ? leaves[j + 1].node.value.slice(0, 1) : '';
    const cited = seen.replace(MARKERS_AND_BARS, '').trim(); // the cited sentence's text found so far
    // An end at this leaf's end closes an earlier sentence once the cited one has text (`**Old.** New [S1.1]`);
    // before that it is the cited sentence's own end (`**New.** [S1.1]`).
    const ends = sentenceEnds(value.slice(0, upper), j === i ? value.slice(upper, upper + 1) : next)
      .filter((at) => at >= lower && (at < upper || (j < i && cited)));
    const start = ends.reverse().find((at) => (value.slice(at, upper) + seen).replace(MARKERS_AND_BARS, '').trim());
    if (start !== undefined) {
      added.push([leaves[j], [start, upper]]);
      break;
    }
    added.push([leaves[j], [lower, upper]]);
    seen = value.slice(lower, upper) + seen;
    if (j === i && lower > 0) break; // within its text node a previous run ends the sentence's reach
  }
  // The sentence starts at its first character, not the space after the sentence before it.
  for (const [leaf, range] of added.reverse()) {
    const text = leaf.node.value.slice(range[0], range[1]);
    range[0] += text.length - text.trimStart().length;
    if (range[1] > range[0]) {
      break;
    }
  }
  for (const [leaf, range] of added) {
    if (leaf.node.value.slice(range[0], range[1]).trim()) leaf.marks.push(range); // never a mark of space alone
  }
}

// remark plugin: markers in text become `cite` elements naming their citation's index in the record, and
// the text of an unsupported sentence before them `span.unsupported`, across the Markdown nodes that
// sentence spans. Text in code, and in a link (a chip is a button), keeps its markers as they are.
export function citationChips({ citations = [] } = {}) {
  return (tree) => {
    const find = finder(citations);
    const backed = backedSentences(citations);
    const leaves = [];
    const collect = (node, block, inLink) => {
      let current = block;
      for (const child of node.children ?? []) {
        if (child.type === 'break') current = child; // a hard line break ends a sentence, as a line's end does
        else if (child.type === 'text') leaves.push({ node: child, parent: node, block: current, inLink, marks: [], runs: [] });
        else collect(child, BLOCKS.has(child.type) ? child : current, inLink || child.type === 'link' || child.type === 'linkReference');
      }
    };
    collect(tree, tree, false);
    for (const leaf of leaves) if (!leaf.inLink) leaf.runs = runsOf(leaf.node, find, citations, backed);
    leaves.forEach((leaf, i) => {
      let cursor = 0;
      for (const run of leaf.runs) {
        if (run.unsupported) markBack(leaves, i, cursor, run.start);
        cursor = run.end;
      }
    });
    for (const leaf of leaves) {
      if (leaf.runs.length || leaf.marks.some(([a, b]) => b > a)) leaf.node.replacement = rebuild(leaf);
    }
    const replace = (node) => {
      if (!node.children) return;
      node.children = node.children.flatMap((child) => child.replacement ?? [child]);
      for (const child of node.children) replace(child);
    };
    replace(tree);
  };
}

// A leaf's text as nodes: its chips, its marked ranges as `span.unsupported`, the rest as text.
function rebuild(leaf) {
  const value = leaf.node.value;
  const chips = leaf.runs.flatMap((run) => run.chips);
  const cuts = new Set([0, value.length, ...leaf.marks.flat(), ...chips.flatMap((c) => [c.start, c.end])]);
  const points = [...cuts].filter((at) => at >= 0 && at <= value.length).sort((a, b) => a - b);
  const out = [];
  for (let k = 0; k + 1 < points.length; k += 1) {
    const [a, b] = [points[k], points[k + 1]];
    const chip = chips.find((c) => c.start === a);
    if (chip) {
      out.push(chip.index < 0 ? { type: 'text', value: chip.marker } : {
        type: 'citation', data: { hName: 'cite', hProperties: { dataCitation: String(chip.index) } },
        children: [{ type: 'text', value: chip.marker }],
      });
      continue;
    }
    const text = value.slice(a, b);
    const marked = leaf.marks.some(([x, y]) => x <= a && b <= y);
    out.push(marked ? { type: 'unsupported', data: { hName: 'span', hProperties: { className: ['unsupported'] } },
      children: [{ type: 'text', value: text }] } : { type: 'text', value: text });
  }
  return out;
}

const TYPE_NOTE = { abstract: 'cite.abstract', metadata: 'cite.metadata', user_note: 'cite.userNote' };
const SUPPORT = { supported: 'cite.supported', partly: 'cite.partly', unsupported: 'cite.unsupported',
  unchecked: 'cite.unchecked' };
const MISSING = { not_found: 'cite.notFound', not_read_in_run: 'cite.notRead', source_removed: 'cite.removed' };

// The source's number in a marker ([S2.3] is 2), as the chip shows it.
const sourceOf = (marker) => /^\[S([1-9][0-9]*)\./.exec(marker ?? '')?.[1] ?? '?';

// A citation chip: a button with the source's number, its card shown while hovered or focused. backed: another
// citation's passage supports its sentence, so an unsupported verdict is said of this passage alone.
export function Chip({ citation, t, onOpen, backed = false }) {
  const id = useId();
  if (!citation || citation.existence !== 'ok') {
    const label = t(MISSING[citation?.existence] ?? 'cite.notFound');
    return h('span', { className: 'cite-missing', title: label },
      h('span', { 'aria-hidden': 'true' }, citation?.marker ?? '?'), h('span', { className: 'sr-only' }, label));
  }
  const notes = [citation.retracted && t('cite.retracted'), TYPE_NOTE[citation.evidence_type] && t(TYPE_NOTE[citation.evidence_type]),
    t(citation.support === 'unsupported' && backed ? 'cite.unsupportedHere' : SUPPORT[citation.support] ?? 'cite.unchecked')]
    .filter(Boolean);
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
  const backed = citations ? backedSentences(citations) : new Set();
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
        const citation = citations[index];
        return h(Chip, { citation, t, onOpen, backed: Boolean(citation?.sentence && backed.has(citation.sentence)) });
      },
    },
  }, text);
}

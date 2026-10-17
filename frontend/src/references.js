// Reference files (slice-1 spec F3b; S1-14) as the interface sends and reads them: which files the
// chooser offers, the import request, what its report says, and a merge question's two papers
// (backend/references.py, backend/reference_files.py).
import { ApiError, post } from './api.js';
import { readFile } from './library.js';

// The chooser's extensions: RIS, BibTeX, CSL JSON, EndNote tagged, and the .txt and .net exports of
// CNKI and NoteExpress. The backend reads a file's format from its content, never its name.
export const REFERENCE_ACCEPT = '.ris,.bib,.bibtex,.json,.enw,.txt,.net';
export const MAX_REFERENCE_BYTES = 10 * 1024 * 1024; // backend/reference_files.py MAX_BYTES
export const RECORD_LINES = 1000; // backend/reference_files.py RECORD_LINES: the lines read of one record

// Imports one reference file into a project: resolves to the backend's report once it has committed,
// or throws an ApiError (one larger than the backend reads is refused here, unsent).
export async function importReferences(projectId, file) {
  if (file.size > MAX_REFERENCE_BYTES) throw new ApiError(413, 'reference_file_too_large');
  const { name, data } = await readFile(file);
  return post(`/api/projects/${encodeURIComponent(projectId)}/materials/import-references`, { name, data });
}

// What an import's report says, as [catalog key, params] pairs: the papers imported, the duplicates
// waiting for an answer, those already in the project with nothing to add, the records skipped, and
// the values left out of the records kept (invalid_field: a year, DOI, URL or arXiv ID that is not one;
// unsupported_macro: a BibTeX value given by an @string macro or joined with #, which is not read;
// past_record_bound: a field of a RIS, EndNote or NoteExpress record longer than the 1,000 lines read).
export function reportParts(report) {
  const { invalid_field: leftOut = 0, unsupported_macro: macros = 0, past_record_bound: past = 0, ...records } =
    report.skipped ?? {};
  const skipped = Object.values(records).reduce((sum, n) => sum + n, 0);
  return [
    ['references.imported', { count: report.imported.length }],
    report.waiting.length && ['references.waiting', { count: report.waiting.length }],
    report.already.length && ['references.already', { count: report.already.length }],
    skipped && ['references.skipped', { count: skipped }],
    leftOut && ['references.leftOut', { count: leftOut }],
    macros && ['references.unsupportedMacro', { count: macros }],
    past && ['references.pastBound', { count: past, lines: RECORD_LINES }],
  ].filter(Boolean);
}

// A paper as a merge question shows it (backend/references.py _summary): its title, then a line of
// up to three authors (with "et al." past them), its year, venue and DOI, whatever it has of them.
export function paperLine(t, paper) {
  if (!paper) return '';
  const authors = paper.author_count > paper.authors.length
    ? t('library.etAl', { names: paper.authors.join(', ') }) : paper.authors.join(', ');
  return [authors, paper.year, paper.venue, paper.doi && t('references.doi', { doi: paper.doi })].filter(Boolean).join(' · ');
}

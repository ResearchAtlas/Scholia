"""Identifier lookups (slice-1 spec F3a step 3, sections 7.5 and 13; ticket 71).

`resolve(client, scheme, identifier, pace)` sends one identifier alone to an identifier endpoint
and returns what it found: a DOI to OpenAlex, then to Crossref when OpenAlex has no record; an
arXiv ID to export.arxiv.org. Each request is an anonymous single-record GET: no key, no contact
address, no credentials (the outbound gate refuses any). Each source is asked one request at a
time across every lookup, held until its answer, and paced (`SPACING`: arXiv asks for one request
every 3 seconds); a request that meets 429, a server error or a network failure is retried at
most twice, after 1 and 4 seconds (a Retry-After within RETRY_AFTER_MAX instead, in seconds or as
a date), each within TIMEOUT seconds; an answer is read as it streams in, at most MAX_BODY bytes
once decoded. The client is the outbound gate's, made for the project with its dispatch check, so
a refusal (OutboundDenied) is final and is raised.

A DOI resolved through OpenAlex or Crossref records whether the work is retracted (OpenAlex's
`is_retracted`; a retraction, withdrawal or removal in Crossref's `updated-by`); arXiv says
nothing on retraction. Nothing here logs an identifier or a response.
"""

import asyncio
import contextlib
import email.utils
import json
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote
import httpx

from backend.extraction import Unreadable, untrusted_xml
from backend.outbound_gate import OutboundDenied

OPENALEX = "https://api.openalex.org/works/doi:{}"
CROSSREF = "https://api.crossref.org/works/{}"
ARXIV = "https://export.arxiv.org/api/query?id_list={}&max_results=1"
SERVICES = {"doi": ("openalex", "crossref"), "arxiv": ("arxiv",)}
SPACING = {"openalex": 0.2, "crossref": 0.25, "arxiv": 3.0}  # seconds between one source's requests
RETRIES = (1.0, 4.0)
RETRY_AFTER_MAX = 30.0
TIMEOUT = 20.0
MAX_BODY = 4 * 1024 * 1024
_RETRACTED = {"retraction", "withdrawal", "removal"}
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


@dataclass
class Found:
    source: str  # openalex, crossref or arxiv
    csl: dict
    retracted: bool | None  # None: the source says nothing on it
    source_key: str


class Failed(Exception):
    """The identifier could not be resolved: code not_found or unavailable."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Pace:
    """Each source's turns, shared by a harness's lookups: one request at a time per source, held
    from its start to its answer, each starting at least SPACING after the one before."""

    def __init__(self):
        self.next = {}
        self.locks = {}

    @contextlib.asynccontextmanager
    async def turn(self, source):
        async with self.locks.setdefault(source, asyncio.Lock()):
            wait = self.next.get(source, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next[source] = time.monotonic() + SPACING[source]
            yield


async def resolve(client, scheme, identifier, pace) -> Found:
    """What the sources hold for the identifier, or Failed: not_found only when every source asked
    answered that it has no record (OpenAlex's outage with Crossref's not found stays unavailable,
    so the lookup can be tried again)."""
    if scheme == "arxiv":
        return _arxiv(identifier, await _get(client, ARXIV.format(quote(identifier, safe="/.")), "arxiv", pace))
    try:
        return _openalex(identifier, await _get(client, OPENALEX.format(quote(identifier, safe="/")), "openalex", pace))
    except Failed as failed:
        openalex = failed.code
    try:
        return _crossref(identifier, await _get(client, CROSSREF.format(quote(identifier, safe="/")), "crossref", pace))
    except Failed as failed:
        raise Failed("unavailable" if "unavailable" in (openalex, failed.code) else "not_found") from None


async def _get(client, url, source, pace):
    """The body of a 200 answer, retried as the module says. Raises Failed or OutboundDenied."""
    for attempt in range(len(RETRIES) + 1):
        delay = RETRIES[attempt] if attempt < len(RETRIES) else None
        async with pace.turn(source):  # the source's one request in flight, to its whole answer
            try:
                status, after, body = await _fetch(client, url)
            except OutboundDenied:
                raise
            except httpx.HTTPError:
                status = None
        if status == 200:
            return body
        if status is not None:
            if status in (400, 404, 410):
                raise Failed("not_found")
            if status != 429 and status < 500:
                raise Failed("unavailable")
            if after is not None and delay is not None:
                delay = max(delay, after) if after <= RETRY_AFTER_MAX else None
        if delay is None:
            break
        await asyncio.sleep(delay)
    raise Failed("unavailable")


async def _fetch(client, url):
    """(status, Retry-After, body): a 200 answer's body as it streams in, decoded here (gzip or none,
    nothing else), counted as decoded bytes and given up (Failed unavailable) once it would pass
    MAX_BODY, so neither a long body nor a small compressed one that expands is ever held whole. A
    gzip body is one gzip stream, read to its end: anything after its end (another member, a tail) is
    given up too, as it arrives, rather than read on uncounted, and a stream cut before its end (its
    trailer's length and checksum unchecked) is given up as well."""
    async with client.stream("GET", url, headers={"Accept-Encoding": "gzip"}, timeout=TIMEOUT,
                             follow_redirects=False) as response:
        if response.status_code != 200:
            return response.status_code, _retry_after(response), None
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("identity", "gzip"):
            raise Failed("unavailable")
        inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        body = bytearray()
        try:
            async for raw in response.aiter_raw():
                # At most what is left under the limit, and one byte more to show it is passed. Input
                # left over was held back by the limit (unconsumed_tail) or came after the gzip
                # stream's end (unused_data, where every later chunk goes): either is given up.
                body += inflate.decompress(raw, MAX_BODY + 1 - len(body)) if inflate else raw
                if len(body) > MAX_BODY or (inflate and (inflate.unconsumed_tail or inflate.unused_data)):
                    raise Failed("unavailable")
            if inflate:
                body += inflate.flush()
                if not inflate.eof:  # cut before its trailer: its length and checksum were never checked
                    raise Failed("unavailable")
        except zlib.error:
            raise Failed("unavailable") from None
        if len(body) > MAX_BODY:
            raise Failed("unavailable")
        return 200, None, bytes(body)


def _retry_after(response):
    """Retry-After as seconds from now (RFC 9110 section 10.2.3): its delay-seconds, or the time until
    its HTTP-date. None, so the fixed delays apply, when it is absent, malformed or already past;
    _get then bounds it by RETRY_AFTER_MAX."""
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:  # an HTTP-date is in GMT
        when = when.replace(tzinfo=UTC)
    wait = (when - datetime.now(UTC)).total_seconds()
    return wait if wait > 0 else None


def _json(body):
    try:
        data = json.loads(body)
    except ValueError:
        raise Failed("unavailable") from None
    if not isinstance(data, dict):
        raise Failed("unavailable")
    return data


def _text(value, limit=1000):
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else None


def _openalex(doi, body):
    work = _json(body)
    title = _text(work.get("title") or work.get("display_name"))
    if not title:
        raise Failed("not_found")
    csl = {"type": "article-journal" if work.get("type") in ("article", None) else str(work.get("type")),
           "title": title, "DOI": doi}
    authors = [_text((a.get("author") or {}).get("display_name"), 200) for a in work.get("authorships") or []
               if isinstance(a, dict)]
    if authors := [{"literal": name} for name in authors if name]:
        csl["author"] = authors[:100]
    if isinstance(work.get("publication_year"), int):
        csl["issued"] = {"date-parts": [[work["publication_year"]]]}
    source = ((work.get("primary_location") or {}).get("source") or {})
    if venue := _text(source.get("display_name"), 300):
        csl["container-title"] = venue
    biblio = work.get("biblio") or {}
    for key, field in (("volume", "volume"), ("issue", "issue")):
        if value := _text(biblio.get(field), 50):
            csl[key] = value
    if (first := _text(biblio.get("first_page"), 20)) is not None:
        csl["page"] = first + (f"-{last}" if (last := _text(biblio.get("last_page"), 20)) else "")
    retracted = work.get("is_retracted") if isinstance(work.get("is_retracted"), bool) else None
    return Found("openalex", csl, retracted, f"doi:{doi}")


def _crossref(doi, body):
    work = _json(body).get("message")
    if not isinstance(work, dict):
        raise Failed("not_found")
    titles = work.get("title") or []
    title = _text(titles[0] if isinstance(titles, list) and titles else titles)
    if not title:
        raise Failed("not_found")
    csl = {"type": str(work.get("type") or "article-journal"), "title": title, "DOI": doi}
    authors = []
    for author in work.get("author") or []:
        if isinstance(author, dict):
            family, given = _text(author.get("family"), 200), _text(author.get("given"), 200)
            if family:
                authors.append({"family": family, **({"given": given} if given else {})})
            elif name := _text(author.get("name"), 200):
                authors.append({"literal": name})
    if authors:
        csl["author"] = authors[:100]
    parts = ((work.get("issued") or {}).get("date-parts") or [[None]])[0]
    if parts and isinstance(parts[0], int):
        csl["issued"] = {"date-parts": [[parts[0]]]}
    containers = work.get("container-title") or []
    if venue := _text(containers[0] if isinstance(containers, list) and containers else None, 300):
        csl["container-title"] = venue
    for key in ("volume", "issue", "page"):
        if value := _text(work.get(key), 50):
            csl[key] = value
    updates = work.get("updated-by") or []
    retracted = any(isinstance(u, dict) and str(u.get("type", "")).lower() in _RETRACTED for u in updates)
    return Found("crossref", csl, retracted, f"doi:{doi}")


def _arxiv(identifier, body):
    try:
        feed = untrusted_xml(body)  # refused when it declares a document type or an entity
    except Unreadable:
        raise Failed("unavailable") from None
    entry = feed.find(f"{_ATOM}entry")
    if entry is None or "/api/errors" in (entry.findtext(f"{_ATOM}id") or ""):
        raise Failed("not_found")
    title = _text(entry.findtext(f"{_ATOM}title"))
    if not title:
        raise Failed("not_found")
    csl = {"type": "article", "title": title, "publisher": "arXiv", "number": f"arXiv:{identifier}",
           "URL": f"https://arxiv.org/abs/{identifier}"}
    authors = [_text(a.findtext(f"{_ATOM}name"), 200) for a in entry.findall(f"{_ATOM}author")]
    if authors := [{"literal": name} for name in authors if name]:
        csl["author"] = authors[:100]
    published = entry.findtext(f"{_ATOM}published") or ""
    if published[:4].isdigit():
        csl["issued"] = {"date-parts": [[int(published[:4])]]}
    if doi := _text(entry.findtext(f"{_ARXIV_NS}doi"), 200):
        csl["DOI"] = doi.lower()
    return Found("arxiv", csl, None, f"arxiv:{identifier}")

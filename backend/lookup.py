"""Identifier lookups (slice-1 spec F3a step 3, sections 7.5 and 13; ticket 71).

`resolve(client, scheme, identifier, pace)` sends one identifier alone to an identifier endpoint
and returns what it found: a DOI to OpenAlex, then to Crossref when OpenAlex has no record; an
arXiv ID to export.arxiv.org. Each request is an anonymous single-record GET: no key, no contact
address, no credentials (the outbound gate refuses any). Each source is paced (`SPACING`: arXiv
asks for one request every 3 seconds) and a request that meets 429, a server error or a network
failure is retried at most twice, after 1 and 4 seconds (a Retry-After within RETRY_AFTER_MAX
instead), each within TIMEOUT seconds. The client is the outbound gate's, made for the project
with its dispatch check, so a refusal (OutboundDenied) is final and is raised.

A DOI resolved through OpenAlex or Crossref records whether the work is retracted (OpenAlex's
`is_retracted`; a retraction, withdrawal or removal in Crossref's `updated-by`); arXiv says
nothing on retraction. Nothing here logs an identifier or a response.
"""

import asyncio
import json
import time
from dataclasses import dataclass
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
    """When each source may next be asked, shared by a harness's lookups."""

    def __init__(self):
        self.next = {}
        self.locks = {}

    async def turn(self, source):
        lock = self.locks.setdefault(source, asyncio.Lock())
        async with lock:
            wait = self.next.get(source, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next[source] = time.monotonic() + SPACING[source]


async def resolve(client, scheme, identifier, pace) -> Found:
    if scheme == "arxiv":
        return _arxiv(identifier, await _get(client, ARXIV.format(quote(identifier, safe="/.")), "arxiv", pace))
    try:
        return _openalex(identifier, await _get(client, OPENALEX.format(quote(identifier, safe="/")), "openalex", pace))
    except Failed:
        pass
    return _crossref(identifier, await _get(client, CROSSREF.format(quote(identifier, safe="/")), "crossref", pace))


async def _get(client, url, source, pace):
    """The body of a 200 answer, retried as the module says. Raises Failed or OutboundDenied."""
    for attempt in range(len(RETRIES) + 1):
        await pace.turn(source)
        delay = RETRIES[attempt] if attempt < len(RETRIES) else None
        try:
            response = await client.get(url, timeout=TIMEOUT, follow_redirects=False)
        except OutboundDenied:
            raise
        except httpx.HTTPError:
            response = None
        if response is not None:
            if response.status_code == 200:
                if len(response.content) > MAX_BODY:
                    raise Failed("unavailable")
                return response.content
            if response.status_code in (400, 404, 410):
                raise Failed("not_found")
            if response.status_code != 429 and response.status_code < 500:
                raise Failed("unavailable")
            after = _retry_after(response)
            if after is not None and delay is not None:
                delay = max(delay, after) if after <= RETRY_AFTER_MAX else None
        if delay is None:
            break
        await asyncio.sleep(delay)
    raise Failed("unavailable")


def _retry_after(response):
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


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

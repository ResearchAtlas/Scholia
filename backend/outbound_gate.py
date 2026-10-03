"""The outbound gate: one module that every request leaving the app passes through.

Every outgoing request of the app is sent with a client from `OutboundGate.client()`
or `async_client()`: provider catalogs and model calls (backend/openrouter.py and
backend/openrouter_client.py, through backend/runs.py and backend/app.py), and
tests/test_no_bypass.py checks that no other module makes an HTTP client or a
connection, so nothing (telemetry included) leaves another way. The clients are
bound to one project. Their transport classifies each request's
destination (scheme, host and port), allows or refuses it by the project's
sensitivity level, and records the decision in `audit_log` before anything is
sent. Every redirect hop goes through the same check, and a redirect to another
origin is refused. Anything not classified is refused, and so is a request whose
Host header, TLS name or request target (an httpcore extension) differs from its
URL.

Destination kinds: a model provider (configured in settings), a scholarly API, an
open-access link taken from a named candidate of the project (the exact link,
then only the same-origin redirects httpx follows from that fetch; never a model
provider's host), the local helper, a local provider on loopback, and a model
download source. Only model providers, local providers and the helper take a
request body or credentials (Authorization, Proxy-Authorization, Cookie, any
header named like a key or token, or user info in the URL). The others are
public fetches: a GET or HEAD with no body and no credentials, whatever the level,
to a public address: an IP literal that is not globally routable (private,
link-local, shared, unique-local, documentation, reserved or multicast, any
IPv6 address outside 2000::/3, or a NAT64 form of such an IPv4 address) is
refused. A Private model request
must be a POST to exactly /api/v1/chat/completions on OpenRouter. Gated clients keep
no cookies, so no response can make a later request carry one.

Hosts are compared in one canonical spelling (see `_canonical_host`): lowercase
IDNA names without a trailing dot, and IP addresses in their standard form, so
`openrouter.ai.`, `134744072` (8.8.8.8) or `[::ffff:8.8.8.8]` are the hosts they
name. A host on this machine (127.0.0.0/8, ::1, IPv4-compatible forms, 0.0.0.0,
::, localhost names) is loopback: only the helper's and configured providers'
exact origins count there, and nothing else does. Names are never resolved, so
`localhost` and `127.0.0.1` are different origins.

- Normal: every kind.
- Private: model requests only to OpenRouter, on the Private allowlist, carrying
  provider.zdr = true and the entry's other flags, with no plugins or server-side
  features, sent with a key whose data-settings confirmation is current (see
  `_private_problem`); and a local provider only after the researcher declared its
  exact origin, as for Local only, without OpenRouter's flags or key confirmation
  (ticket 64). Scholarly APIs, open-access hosts, the helper and model downloads.
- Local only: the helper; a local provider only after the researcher declared
  its exact origin (`local_declarations`); scholarly APIs and open-access hosts
  only for a client marked as approved by the researcher. Nothing else.

A client may also carry a dispatch check (`admit`), which the harness gives every
model call: it is read in the same decision transaction, so a run revoked by a
deletion or a tightened project, or one in a review-locked project, sends nothing
more, retries and same-origin redirect hops included ("revoked"). The decision and
the hand-off to the transport hold the revocation lock (backend/db/deletion.py,
REVOCATION), which every revoking write holds too, so no revocation commits between
them: an async client hands the request over before letting it go. (A sync client,
which only tests use, lets it go once the request is marked dispatched.)

Audit rows hold the decision, reason, kind, destination origin, method, level
and approval flag; never a path, query, header or body. Every field is from a
fixed set except the destination, which is the canonical origin of a destination
the gate recognizes (a fixed source, a configured provider or the helper, a
declared local server, the candidate's link) and otherwise "unknown", so a
refused host's own name is never kept: a method outside
the standard ones is refused and recorded as OTHER, and a client takes only a
project id in the schema's form. A row records the
decision, not delivery: a crash after the commit leaves an allow row for a
request that was never sent. If the row cannot be written, nothing is sent.

Inputs that come from settings and the Private flows arrive through
`GateInputs`; a missing one refuses, and one that fails is recorded as a refusal
("gate_inputs_unavailable"). The Private route and key confirmation are read
inside the decision transaction, with the level, and only for a project that is
Private then. Code in this process is trusted: the gate
guards against mistakes and model-driven requests, not against code that builds
its own client or reaches into a client's private attributes.

Limits: the gate does no lookups and names are resolved by the system, so a
hosts file that maps "localhost" elsewhere, or a public name that resolves to
this machine or a private network address, is not detected; only an
open-access fetch can reach such a name.
Cross-origin redirects are refused even between allowed hosts, whether or not
the client follows them, so a source that redirects to another host (a download
CDN, say) needs a change here when it is wired in.
"""

import asyncio
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
from collections.abc import Callable, Collection, Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from backend.db.deletion import REVOCATION


class Kind(StrEnum):
    MODEL_PROVIDER = "model_provider"
    SCHOLARLY_API = "scholarly_api"
    OPEN_ACCESS = "open_access"
    LOCAL_HELPER = "local_helper"
    LOCAL_PROVIDER = "local_provider"
    MODEL_DOWNLOAD = "model_download"


OPENROUTER = ("https", "openrouter.ai", 443)
OPENROUTER_CHAT = b"/api/v1/chat/completions"  # the only path a Private request may use, with no query
SCHOLARLY_APIS = frozenset({
    ("https", "api.openalex.org", 443),
    ("https", "api.crossref.org", 443),
    ("https", "export.arxiv.org", 443),
})
# ponytail: the sources' main hosts only; the PR that wires model downloads adds
# the file hosts they redirect to, once checked.
MODEL_SOURCES = frozenset({
    ("https", "huggingface.co", 443),
    ("https", "modelscope.cn", 443),
    ("https", "www.modelscope.cn", 443),
})
# Top-level fields a Private request may carry. Anything else (plugins, web
# search options, presets, newer server-side features) is refused.
PRIVATE_FIELDS = frozenset({
    "model", "models", "messages", "provider", "reasoning", "usage", "stream", "stream_options",
    "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k",
    "frequency_penalty", "presence_penalty", "repetition_penalty", "seed", "stop",
    "logit_bias", "logprobs", "top_logprobs", "response_format",
    "tools", "tool_choice", "parallel_tool_calls",
})
ZDR = {"provider": {"zdr": True}}  # every Private request carries it, whatever the allowlist says
_CLIENT_OPTIONS = frozenset({"base_url", "follow_redirects", "headers", "max_redirects", "timeout"})
# Methods as httpx sends them (upper case); any other is refused and audited as OTHER.
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_PROJECT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# Destinations that may receive a body or credentials, each under its level's rules.
_PRIVATE_PEERS = frozenset({Kind.MODEL_PROVIDER, Kind.LOCAL_PROVIDER, Kind.LOCAL_HELPER})
_CREDENTIAL_HEADER = re.compile(r"auth|cookie|key|token|secret|session|passw|credential", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_THIS_HOST = (ipaddress.IPv4Network("127.0.0.0/8"), ipaddress.IPv4Network("0.0.0.0/8"))
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # a gateway connects to the IPv4 address in its last 32 bits
_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")  # the only IPv6 block assigned for public addresses
_KNOWN = SCHOLARLY_APIS | MODEL_SOURCES | {OPENROUTER}
# Marks a request httpx builds to follow a redirect from an allowed open-access
# fetch; httpx copies a request's extensions into the redirect it builds. The
# value is a one-time token from _Scope.expect, bound to the next URL.
_HOP = object()
MAX_PENDING_HOPS = 16  # per client
# A request extension holding a callable the gate calls once the request has passed its
# check and is handed to the network, so a caller can tell a request that left from one
# that was refused or cancelled before it left.
DISPATCHED = "scholia.dispatched"


class OutboundDenied(Exception):
    """The gate refused a request; it was not sent.

    Not an httpx or OS error, so retry logic cannot mistake it for a network failure.
    """

    def __init__(self, reason: str, destination: str | None):
        super().__init__(f"outbound request to {destination or 'an unknown destination'} refused: {reason}")
        self.reason = reason
        self.destination = destination


@dataclass(frozen=True)
class GateInputs:
    """What the gate needs from settings and the Private flows. A missing input refuses.

    provider_urls: base URLs of the model providers configured in settings.
    helper_url: base URL of the running local helper, on loopback.
    private_route(conn, model): the request flags required by the enabled Private
        allowlist entry covering an OpenRouter model id, or None if none covers it.
    key_attested(conn, key): whether a key has a current data-settings confirmation.
    Both are called inside the decision transaction, with its connection.
    """

    provider_urls: Collection[str] = ()
    helper_url: str | None = None
    private_route: Callable[[object, str], Mapping | None] | None = None
    key_attested: Callable[[object, str], bool] | None = None


@dataclass(frozen=True)
class _Scope:
    project_id: str
    candidate_id: str | None
    approved: bool
    admit: Callable | None = field(default=None, compare=False, repr=False)  # see OutboundGate.client
    # Redirect hops this client may still take: one-time token -> (origin, path and query as sent).
    hops: dict = field(default_factory=dict, compare=False, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    def expect(self, url):
        """A one-time token for the redirect hop to url, an (origin, raw path) pair.

        Tokens live in this client's scope, so no other client can use one. Each
        redirect response gets its own token, even for the same URL, so
        concurrent fetches can each follow theirs. At most MAX_PENDING_HOPS stay
        pending, the oldest dropped first, so redirects that are never followed
        cannot grow memory without bound.
        """
        token = object()
        with self.lock:
            while len(self.hops) >= MAX_PENDING_HOPS:
                del self.hops[next(iter(self.hops))]  # dicts keep insertion order
            self.hops[token] = url
        return token

    def use(self, token, url):
        """Whether token is this client's unused token for url. Using it consumes it."""
        with self.lock:  # sync clients may share threads, and async checks run on worker threads
            try:
                if self.hops.get(token) != url:
                    return False
            except TypeError:  # an unhashable value set by hand
                return False
            del self.hops[token]
            return True


class OutboundGate:
    """Hands out httpx clients whose every request is checked and audited first.

    db is the main Database. inputs returns the current GateInputs and is called
    for each request. transport is where checked requests go; tests pass an
    httpx.MockTransport, and otherwise each client gets httpx's own.

    local_listener(host, port) says whether a process of this account listens there.
    A local provider reached over plain HTTP gets a request only then: another account
    on this machine could otherwise take its port while it is not running and receive
    the key and the content. It defaults to an lsof check when requests reach the real
    network, and to none when a transport is passed (that transport is the destination).
    ponytail: checked just before sending; a listener of this account holds the port, and
    macOS lets no other account bind it meanwhile.
    """

    def __init__(self, db, inputs: Callable[[], GateInputs], *, transport=None, local_listener=None):
        self._db = db
        self._inputs = inputs
        self._transport = transport
        self._local_listener = local_listener or (None if transport is not None else listener_is_ours)

    def client(self, project_id: str, *, candidate_id: str | None = None, approved: bool = False,
               admit: Callable | None = None, **options) -> httpx.Client:
        """A client for one project's requests.

        candidate_id names the candidate whose open-access link this client may
        fetch. approved means the researcher approved these requests, which Local
        only projects need for scholarly APIs and open-access hosts. admit(conn), if
        given, is called in the decision transaction of every request the policy
        allows; anything but True refuses it as "revoked". options are
        limited to base_url, follow_redirects, headers, max_redirects and timeout.
        Proxy and certificate settings in the environment are ignored: TLS uses
        certifi's CA bundle.
        """
        transport = _Transport(self, _Scope(_project_id(project_id), candidate_id, approved, admit),
                               self._transport or httpx.HTTPTransport(trust_env=False))
        return httpx.Client(transport=transport, trust_env=False, cookies=_no_cookies(), **_checked(options))

    def async_client(self, project_id: str, *, candidate_id: str | None = None, approved: bool = False,
                     admit: Callable | None = None, **options) -> httpx.AsyncClient:
        """The async form of client()."""
        transport = _AsyncTransport(self, _Scope(_project_id(project_id), candidate_id, approved, admit),
                                    self._transport or httpx.AsyncHTTPTransport(trust_env=False))
        return httpx.AsyncClient(transport=transport, trust_env=False, cookies=_no_cookies(), **_checked(options))

    def _check(self, request: httpx.Request, scope: _Scope) -> None:
        """Decide one request and record the decision. Raises OutboundDenied."""
        target = _origin(request.url)
        # A Host header or TLS name other than the URL's could reach another site
        # behind the same server or CDN.
        addressed = (request.headers.get("host") == request.url.netloc.decode("ascii")
                     and "sni_hostname" not in request.extensions and "target" not in request.extensions)
        hop = scope.use(request.extensions.get(_HOP), (target, request.url.raw_path))
        # What a scholarly, open-access or download request may not be. User info in
        # the URL is a credential too: httpx turns it into Authorization only for a
        # first request, never for a redirect hop.
        if request.url.userinfo or any(_CREDENTIAL_HEADER.search(name) for name in request.headers.keys()):
            public_problem = "credential_to_non_provider"
        elif request.method not in ("GET", "HEAD") or _body(request) != b"":
            public_problem = "not_a_fetch"
        else:
            public_problem = None
        # The inputs are called outside the transaction, and the request's own Private
        # checks only when the project is Private now. The level is read again in the
        # transaction, where the route and the key's confirmation are checked.
        seen = self._db.read(lambda conn: _level(conn, scope.project_id))
        error, providers, helper, inputs = None, frozenset(), None, None
        try:
            inputs = self._inputs()
            providers, helper = _origins(inputs)
            if target != OPENROUTER:
                private_problem = "not_openrouter"
            elif seen == "private":
                private_problem = _private_request(request, inputs)
            else:
                private_problem = "sensitivity_changed"  # used only if it became Private since
        except Exception as caught:  # recorded as a refusal below, and chained to it
            error = caught
        # Whose process listens at a plain-HTTP local provider, asked outside the transaction
        # (not for the helper, the app's own child).
        owned = None
        if self._local_listener is not None and target is not None and target[0] == "http" \
                and _is_this_host(target[1]) and target in providers and target != helper:
            try:
                owned = bool(self._local_listener(target[1], target[2]))
            except Exception:  # unknown counts as not ours
                owned = False

        def decide(conn):
            # The level is read in the transaction that records the decision, so a
            # change of level is ordered entirely before or after it; so are the Private
            # route, the key's confirmation and the dispatch check, read here too.
            nonlocal error
            level = _level(conn, scope.project_id)
            link = _candidate_link(conn, scope)
            kind = None
            if request.method not in _METHODS:  # an arbitrary method string could carry content
                reason = "unsupported_method"
            elif error is not None:
                reason = "gate_inputs_unavailable"
            else:
                kind = _classify(target, providers, helper, link)
                # An open-access fetch is bound to the candidate's exact link, as sent;
                # other URLs on its origin only as redirects followed from it.
                bound = hop or (link is not None and target == _origin(link) and request.url.raw_path == link.raw_path)
                problem = private_problem
                if isinstance(problem, tuple) and level == "private" and kind is Kind.MODEL_PROVIDER:
                    try:
                        problem = _private_problem(conn, inputs, *problem)
                    except Exception as caught:  # recorded as a refusal, and chained to it
                        error, kind, problem = caught, None, "gate_inputs_unavailable"
                reason = "gate_inputs_unavailable" if error is not None else \
                    _policy(conn, level, kind, target, scope, problem, public_problem, bound)
                if reason is None and not addressed:
                    reason = "host_mismatch"
                if reason is None and kind is Kind.LOCAL_PROVIDER and owned is False:
                    reason = "local_server_not_yours"
                if reason is None and scope.admit is not None:
                    try:
                        admitted = scope.admit(conn)
                    except Exception as caught:  # refused, and chained to it
                        error, admitted = caught, False
                    if admitted is not True:
                        reason = "revoked"
            shown = _shown(conn, target, kind, link, providers, helper)
            _record(conn, request, scope, level, kind, shown, reason)
            return kind, reason, shown

        kind, reason, shown = self._db.write(decide)
        if reason:
            raise OutboundDenied(reason, shown) from error
        return kind

    def _refuse_redirect(self, request: httpx.Request, scope: _Scope, target) -> None:
        try:
            providers, helper = _origins(self._inputs())
        except Exception:  # they only name the destination; the refusal stands either way
            providers, helper = frozenset(), None

        def record(conn):
            shown = _shown(conn, target, None, _candidate_link(conn, scope), providers, helper)
            _record(conn, request, scope, _level(conn, scope.project_id), None, shown, "cross_origin_redirect")
            return shown

        raise OutboundDenied("cross_origin_redirect", self._db.write(record))


class _Transport(httpx.BaseTransport):
    def __init__(self, gate, scope, inner):
        self._gate, self._scope, self._inner = gate, scope, inner

    def handle_request(self, request):
        with REVOCATION:
            kind = self._gate._check(request, self._scope)
            _dispatched(request)
        response = self._inner.handle_request(request)
        leaves, target = _redirect(request, response)
        if leaves:
            response.close()
            self._gate._refuse_redirect(request, self._scope, target)
        if kind is Kind.OPEN_ACCESS and response.has_redirect_location:
            request.extensions[_HOP] = self._scope.expect(_next_url(request, response))  # httpx copies it
        return response

    def close(self):
        self._inner.close()


class _AsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, gate, scope, inner):
        self._gate, self._scope, self._inner = gate, scope, inner

    async def handle_async_request(self, request):
        # The decision and the hand-off are one step against revocations: the lock is taken on
        # the loop without blocking it, and let go once the transport has the request.
        while not REVOCATION.acquire(blocking=False):
            await asyncio.sleep(0.001)
        try:
            # The database blocks, so it is reached off the event loop.
            kind = await asyncio.to_thread(self._gate._check, request, self._scope)
            _dispatched(request)
            sending = asyncio.ensure_future(self._inner.handle_async_request(request))
        finally:
            REVOCATION.release()
        response = await sending  # cancelling this request cancels the send
        leaves, target = _redirect(request, response)
        if leaves:
            await response.aclose()
            await asyncio.to_thread(self._gate._refuse_redirect, request, self._scope, target)
        if kind is Kind.OPEN_ACCESS and response.has_redirect_location:
            request.extensions[_HOP] = self._scope.expect(_next_url(request, response))
        return response

    async def aclose(self):
        await self._inner.aclose()


def listener_is_ours(host, port) -> bool:
    """Whether a process of this account listens at host:port: on that address, or on its
    family's wildcard. A name (localhost) needs both 127.0.0.1 and ::1, since either may be
    reached. lsof run as this account lists only its processes (-u narrows it in any case).
    A wildcard counts only for its own family: lsof cannot tell a dual-stack IPv6 socket
    from an IPv6-only one, so a dual-stack server is reached at its IPv6 address."""
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-u", str(os.getuid()), f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-F", "tn"],
        capture_output=True, timeout=5, check=False)
    if result.returncode != 0:
        return False
    listeners, family = [], None
    for line in result.stdout.decode("ascii", "replace").splitlines():
        if line.startswith("t"):
            family = {"IPv4": 4, "IPv6": 6}.get(line[1:])
        elif line.startswith("n") and family:
            listeners.append((family, line[1:].rsplit(":", 1)[0].strip("[]")))
    try:
        wanted = [ipaddress.ip_address(host)]
    except ValueError:
        wanted = [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]

    def covers(listener, address):
        family, bound = listener
        if bound == "*":
            return family == address.version
        try:
            return ipaddress.ip_address(bound) == address
        except ValueError:
            return False

    return all(any(covers(listener, address) for listener in listeners) for address in wanted)


def local_origin(url) -> str | None:
    """For a URL on this machine, the origin a declaration of its server covers (one per exact
    origin), as the audit log shows it; None for any other URL."""
    origin = _origin_of(url)
    return _show(origin) if origin is not None and _is_this_host(origin[1]) else None


def is_openrouter(url) -> bool:
    """Whether a URL is on OpenRouter's origin, the only one a Private model request may use."""
    return _origin_of(url) == OPENROUTER


def _dispatched(request):
    notify = request.extensions.get(DISPATCHED)
    if callable(notify):
        notify()


def _project_id(project_id):
    """The project id, which is recorded in the audit log, so only in the schema's form."""
    if not (isinstance(project_id, str) and _PROJECT_ID.fullmatch(project_id)):
        raise ValueError("project_id must be a project's id (a lowercase UUID4)")
    return project_id


def _no_cookies():
    """A cookie jar that stores nothing: a gated client never replays a cookie."""
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def _checked(options):
    if extra := options.keys() - _CLIENT_OPTIONS:
        raise TypeError(f"gated clients do not take {sorted(extra)}")
    return options


def _origin(url: httpx.URL, scheme: str | None = None):
    """(scheme, canonical host, port) of an http or https URL, or None.

    Every comparison the gate makes is between these, so two spellings of one
    host are the same destination.
    """
    scheme = scheme or url.scheme
    port = url.port or _DEFAULT_PORTS.get(scheme)
    host = _canonical_host(url.raw_host)
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host, port


def _canonical_host(raw: bytes):
    """The host a connection goes to, in one spelling, or None.

    raw is what httpx connects to: lowercase IDNA for names (ASCII), or an IP
    literal as written. One trailing dot is dropped. An IP literal, including the
    short, integer, hex and octal IPv4 forms that resolvers read as addresses,
    becomes its ipaddress form, with an IPv4-mapped IPv6 address as IPv4.
    """
    try:
        # httpx already lowercases names; lowering again keeps this form independent of it.
        host = raw.decode("ascii").lower().removesuffix(".")
    except UnicodeDecodeError:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            return host or None  # a name
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def _origin_of(text):
    try:
        return _origin(httpx.URL(text)) if isinstance(text, str) else None
    except httpx.InvalidURL:
        return None


def _non_public(host: str) -> bool:
    """Whether a canonical host is an IP literal that is not globally routable.

    Names are not looked up. Public IPv6 is defined positively: inside 2000::/3
    and global by ipaddress (which counts reserved blocks such as 4000::/2 and
    the old site-local fec0::/10 as global). A NAT64 address is judged by the
    IPv4 address a gateway translates it to.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 6:
        if address in _NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address not in _GLOBAL_UNICAST:
            return True
    return not address.is_global or address.is_multicast


def _is_this_host(host: str) -> bool:
    """Whether a canonical host names this machine."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 6:
        if int(address) >> 32 != 0:
            return address.is_loopback
        address = ipaddress.IPv4Address(int(address))  # ::, ::1 and IPv4-compatible forms
    return any(address in network for network in _THIS_HOST)


def _show(origin):
    if origin is None:
        return None
    scheme, host, port = origin
    return f"{scheme}://[{host}]:{port}" if ":" in host else f"{scheme}://{host}:{port}"


def _level(conn, project_id):
    row = conn.execute("SELECT sensitivity FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row[0] if row else None


def _origins(inputs):
    """The configured providers' origins and the helper's origin."""
    return frozenset(_origin_of(url) for url in inputs.provider_urls), _origin_of(inputs.helper_url)


def _candidate_link(conn, scope):
    """The named candidate's open-access link as an httpx.URL, or None."""
    if scope.candidate_id is None:
        return None
    row = conn.execute(
        "SELECT oa_url FROM candidates WHERE id = ? AND project_id = ?", (scope.candidate_id, scope.project_id),
    ).fetchone()
    try:
        return httpx.URL(row[0]) if row and isinstance(row[0], str) else None
    except httpx.InvalidURL:
        return None


def _shown(conn, target, kind, link, providers, helper):
    """How the audit log names a destination: its canonical origin when the gate
    recognizes it, else "unknown", so a refused host's own name is never kept."""
    if target is not None and (
        kind is not None or target in _KNOWN or target in providers or target == helper
        or (link is not None and target == _origin(link))
        or any(_origin_of(url) == target for (url,) in conn.execute("SELECT base_url FROM local_declarations"))
    ):
        return _show(target)
    return "unknown"


def _classify(target, providers, helper, link):
    if target is None:
        return None
    if _is_this_host(target[1]):
        # Loopback is only transport: just the helper and configured providers
        # count, at their exact origins, never a candidate's link.
        if target == helper:
            return Kind.LOCAL_HELPER
        return Kind.LOCAL_PROVIDER if target in providers else None
    if target in providers:
        return Kind.MODEL_PROVIDER
    if target[1] in {origin[1] for origin in providers if origin} | {OPENROUTER[1]}:
        return None  # a model provider's host is never anything else, whatever a candidate says
    if target in SCHOLARLY_APIS:
        return Kind.SCHOLARLY_API
    if target in MODEL_SOURCES:
        return Kind.MODEL_DOWNLOAD
    if link is not None and target == _origin(link):
        return Kind.OPEN_ACCESS
    return None


def _policy(conn, level, kind, target, scope, private_problem, public_problem, bound):
    """None to allow, or the reason for refusing."""
    if level is None:
        return "unknown_project"
    if kind is None:
        return "unknown_destination"
    if kind not in _PRIVATE_PEERS:
        if _non_public(target[1]):
            return "non_public_address"
        if public_problem:
            return public_problem
    if kind is Kind.OPEN_ACCESS and not bound:
        return "not_candidate_url"
    if level == "normal" or kind is Kind.LOCAL_HELPER:
        return None
    if kind is Kind.LOCAL_PROVIDER and level in ("private", "local_only"):  # a declared server only (ticket 64)
        declared = conn.execute("SELECT base_url FROM local_declarations").fetchall()
        return None if any(_origin_of(url) == target for (url,) in declared) else "not_declared"
    if level == "private":
        return private_problem if kind is Kind.MODEL_PROVIDER else None
    if level == "local_only":
        if kind in (Kind.SCHOLARLY_API, Kind.OPEN_ACCESS):
            return None if scope.approved is True else "not_approved"
        return "not_allowed_at_level"
    return "unknown_level"


def _private_request(request: httpx.Request, inputs: GateInputs):
    """What a request to OpenRouter from a Private project carries, for _private_problem to
    check in the decision transaction: (its models, its body, its key); or why it cannot go
    out at all."""
    if inputs.private_route is None or inputs.key_attested is None:
        return "private_inputs_missing"
    if request.method != "POST":
        return "unchecked_request"
    if request.url.raw_path != OPENROUTER_CHAT:  # the path as sent, so no query or other spelling
        return "unsupported_endpoint"
    content = _body(request)
    if content is None:
        return "unchecked_request"
    try:
        body = json.loads(content, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        return "unchecked_request"
    if not isinstance(body, dict):
        return "unchecked_request"
    if not body.keys() <= PRIVATE_FIELDS:
        return "unsupported_feature"
    fallbacks, tools, messages = body.get("models", []), body.get("tools", []), body.get("messages", [])
    if not (isinstance(fallbacks, list) and isinstance(tools, list) and isinstance(messages, list)):
        return "unchecked_request"
    models = ([body["model"]] if "model" in body else []) + fallbacks
    if not models or not all(isinstance(model, str) for model in models):
        return "route_not_allowed"
    if any(model.startswith("@") or ":online" in model for model in models):  # presets, web search
        return "unsupported_feature"
    if not all(isinstance(tool, dict) and tool.get("type") == "function" for tool in tools):
        return "unsupported_feature"  # a server-side tool
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "file" for p in content):
            return "unsupported_feature"  # files go through a parser plugin
    _, _, key = request.headers.get("authorization", "").partition(" ")
    return models, body, request.headers.get("authorization", ""), key.strip()


def _private_problem(conn, inputs: GateInputs, models, body, authorization, key):
    """Why a request to OpenRouter cannot go out from a Private project, or None: each model on
    the allowlist with its entry's flags, provider.zdr = true, and a key whose data-settings
    confirmation is current, read in the decision transaction."""
    for model in models:
        flags = inputs.private_route(conn, model)
        if not isinstance(flags, Mapping):
            return "route_not_allowed"
        if not _carries(body, flags):
            return "missing_flags"
    if not _carries(body, ZDR):
        return "missing_flags"
    scheme = authorization.partition(" ")[0]
    if scheme.lower() != "bearer" or not key or inputs.key_attested(conn, key) is not True:
        return "key_not_confirmed"
    return None


def _body(request: httpx.Request):
    """The request's body if it is in memory, else None (a streamed body).

    A redirect hop reuses its first request's body unread; a body in memory is
    safe to read, and what is read is exactly what is sent.
    """
    return request.read() if isinstance(request.stream, httpx.ByteStream) else None


def _unique_keys(pairs):
    # Parsers disagree on which duplicate wins, so a duplicate could hide a flag.
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate key")
    return dict(pairs)


def _carries(body, flags) -> bool:
    """Whether body holds every flag with the same JSON type and value."""
    for key, want in flags.items():
        if key not in body:
            return False
        have = body[key]
        if isinstance(want, Mapping):
            if not (isinstance(have, dict) and _carries(have, want)):
                return False
        elif type(have) is not type(want) or have != want:  # 1 is not true
            return False
    return True


def _redirect(request: httpx.Request, response: httpx.Response):
    """(True, the origin it leads to or None if unreadable) for a redirect away from
    the request's origin; else (False, None).

    Only a relative path, or an absolute URL with the same origin, stays. Anything
    else, including a Location httpx would read in an unusual way, leaves.
    """
    if not response.has_redirect_location:
        return False, None
    try:
        location = httpx.URL(response.headers["Location"])
    except httpx.InvalidURL:
        return True, None
    if not location.scheme and not location.host:
        return False, None
    target = _origin(location, location.scheme or request.url.scheme) if location.host else None
    if target is not None and target == _origin(request.url):
        return False, None
    return True, target


def _next_url(request: httpx.Request, response: httpx.Response):
    """(origin, raw path) of the request httpx builds to follow a same-origin redirect.

    Computed as httpx computes it; only called once _redirect found it stays.
    """
    location = httpx.URL(response.headers["Location"])
    url = request.url.join(location) if location.is_relative_url else location
    return _origin(url), url.raw_path


def _record(conn, request, scope, level, kind, destination, reason):
    conn.execute(
        "INSERT INTO audit_log (event, project_id, data) VALUES ('outbound', ?, ?)",
        (scope.project_id, json.dumps({
            "decision": "deny" if reason else "allow",
            "reason": reason,
            "kind": kind,
            "destination": destination,
            "method": request.method if request.method in _METHODS else "OTHER",
            "sensitivity": level,
            "approved": scope.approved is True,
        })),
    )

"""The outbound gate: one module that every request leaving the app passes through.

Not wired into the app yet. `OutboundGate.client()` and `async_client()` hand out
httpx clients bound to one project. Their transport classifies each request's
destination (scheme, host and port), allows or refuses it by the project's
sensitivity level, and records the decision in `audit_log` before anything is
sent. Every redirect hop goes through the same check, and a redirect to another
origin is refused. Anything not classified is refused, and so is a request whose
Host header or TLS name differs from its URL's host.

Destination kinds: a model provider (configured in settings), a scholarly API, an
open-access host taken from a named candidate of the project (never a model
provider's host), the local helper, a local provider on loopback, and a model
download source. Only model providers, local providers and the helper take a
request body or credentials (Authorization, Proxy-Authorization, Cookie, any
header named like a key or token, or user info in the URL). The others are
public fetches: a GET or HEAD with no body and no credentials, whatever the level,
to a public address: an IP literal that is not globally routable (private,
link-local, shared, unique-local, documentation, reserved or multicast, or an
IPv6 form embedding such an IPv4 address) is refused. A Private model request
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
  `_private_problem`). Scholarly APIs, open-access hosts, the helper and model
  downloads; no local providers.
- Local only: the helper; a local provider only after the researcher declared
  its exact origin (`local_declarations`); scholarly APIs and open-access hosts
  only for a client marked as approved by the researcher. Nothing else.

Audit rows hold the decision, reason, kind, destination origin, method, level
and approval flag; never a path, query, header or body. A row records the
decision, not delivery: a crash after the commit leaves an allow row for a
request that was never sent. If the row cannot be written, nothing is sent.

Inputs that come from settings and the Private flows arrive through
`GateInputs`; a missing one refuses, and one that fails is recorded as a refusal
("gate_inputs_unavailable"). The Private checks call those inputs only for a
project that is Private. Code in this process is trusted: the gate
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
import re
import socket
from collections.abc import Callable, Collection, Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from dataclasses import dataclass
from enum import StrEnum

import httpx


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
# Destinations that may receive a body or credentials, each under its level's rules.
_PRIVATE_PEERS = frozenset({Kind.MODEL_PROVIDER, Kind.LOCAL_PROVIDER, Kind.LOCAL_HELPER})
_CREDENTIAL_HEADER = re.compile(r"auth|cookie|key|token|secret|session|passw|credential", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_THIS_HOST = (ipaddress.IPv4Network("127.0.0.0/8"), ipaddress.IPv4Network("0.0.0.0/8"))
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # a gateway connects to the IPv4 address in its last 32 bits


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
    private_route: the request flags required by the enabled Private allowlist
        entry covering an OpenRouter model id, or None if no entry covers it.
    key_attested: whether a key has a current data-settings confirmation.
    """

    provider_urls: Collection[str] = ()
    helper_url: str | None = None
    private_route: Callable[[str], Mapping | None] | None = None
    key_attested: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class _Scope:
    project_id: str
    candidate_id: str | None
    approved: bool


class OutboundGate:
    """Hands out httpx clients whose every request is checked and audited first.

    db is the main Database. inputs returns the current GateInputs and is called
    for each request. transport is where checked requests go; tests pass an
    httpx.MockTransport, and otherwise each client gets httpx's own.
    """

    def __init__(self, db, inputs: Callable[[], GateInputs], *, transport=None):
        self._db = db
        self._inputs = inputs
        self._transport = transport

    def client(self, project_id: str, *, candidate_id: str | None = None, approved: bool = False,
               **options) -> httpx.Client:
        """A client for one project's requests.

        candidate_id names the candidate whose open-access link this client may
        fetch. approved means the researcher approved these requests, which Local
        only projects need for scholarly APIs and open-access hosts. options are
        limited to base_url, follow_redirects, headers, max_redirects and timeout.
        Proxy and certificate settings in the environment are ignored: TLS uses
        certifi's CA bundle.
        """
        transport = _Transport(self, _Scope(project_id, candidate_id, approved),
                               self._transport or httpx.HTTPTransport(trust_env=False))
        return httpx.Client(transport=transport, trust_env=False, cookies=_no_cookies(), **_checked(options))

    def async_client(self, project_id: str, *, candidate_id: str | None = None, approved: bool = False,
                     **options) -> httpx.AsyncClient:
        """The async form of client()."""
        transport = _AsyncTransport(self, _Scope(project_id, candidate_id, approved),
                                    self._transport or httpx.AsyncHTTPTransport(trust_env=False))
        return httpx.AsyncClient(transport=transport, trust_env=False, cookies=_no_cookies(), **_checked(options))

    def _check(self, request: httpx.Request, scope: _Scope) -> None:
        """Decide one request and record the decision. Raises OutboundDenied."""
        target = _origin(request.url)
        # A Host header or TLS name other than the URL's could reach another site
        # behind the same server or CDN.
        addressed = (request.headers.get("host") == request.url.netloc.decode("ascii")
                     and "sni_hostname" not in request.extensions)
        # What a scholarly, open-access or download request may not be. User info in
        # the URL is a credential too: httpx turns it into Authorization only for a
        # first request, never for a redirect hop.
        if request.url.userinfo or any(_CREDENTIAL_HEADER.search(name) for name in request.headers.keys()):
            public_problem = "credential_to_non_provider"
        elif request.method not in ("GET", "HEAD") or _body(request) != b"":
            public_problem = "not_a_fetch"
        else:
            public_problem = None
        # The inputs are called outside the transaction, and the Private checks only
        # when the project is Private now. The level is read again in the transaction.
        seen = self._db.read(lambda conn: _level(conn, scope.project_id))
        error = None
        try:
            inputs = self._inputs()
            providers = frozenset(_origin_of(url) for url in inputs.provider_urls)
            helper = _origin_of(inputs.helper_url)
            if target != OPENROUTER:
                private_problem = "not_openrouter"
            elif seen == "private":
                private_problem = _private_problem(request, inputs)
            else:
                private_problem = "sensitivity_changed"  # used only if it became Private since
        except Exception as caught:  # recorded as a refusal below, and chained to it
            error = caught

        def decide(conn):
            # The level is read in the transaction that records the decision, so a
            # change of level is ordered entirely before or after it.
            level = _level(conn, scope.project_id)
            if error is not None:
                kind, reason = None, "gate_inputs_unavailable"
            else:
                kind = _classify(conn, target, providers, helper, scope)
                reason = _policy(conn, level, kind, target, scope, private_problem, public_problem)
                if reason is None and not addressed:
                    reason = "host_mismatch"
            _record(conn, request, scope, level, kind, _show(target), reason)
            return reason

        if reason := self._db.write(decide):
            raise OutboundDenied(reason, _show(target)) from error

    def _refuse_redirect(self, request: httpx.Request, scope: _Scope, destination: str) -> None:
        self._db.write(lambda conn: _record(
            conn, request, scope, _level(conn, scope.project_id), None, destination, "cross_origin_redirect"))
        raise OutboundDenied("cross_origin_redirect", destination)


class _Transport(httpx.BaseTransport):
    def __init__(self, gate, scope, inner):
        self._gate, self._scope, self._inner = gate, scope, inner

    def handle_request(self, request):
        self._gate._check(request, self._scope)
        response = self._inner.handle_request(request)
        if (destination := _foreign_redirect(request, response)) is not None:
            response.close()
            self._gate._refuse_redirect(request, self._scope, destination)
        return response

    def close(self):
        self._inner.close()


class _AsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, gate, scope, inner):
        self._gate, self._scope, self._inner = gate, scope, inner

    async def handle_async_request(self, request):
        # The database blocks, so it is reached off the event loop.
        await asyncio.to_thread(self._gate._check, request, self._scope)
        response = await self._inner.handle_async_request(request)
        if (destination := _foreign_redirect(request, response)) is not None:
            await response.aclose()
            await asyncio.to_thread(self._gate._refuse_redirect, request, self._scope, destination)
        return response

    async def aclose(self):
        await self._inner.aclose()


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

    Names are not looked up. A NAT64 address is judged by the IPv4 address a
    gateway translates it to as well. (ipaddress already counts all of 6to4 and
    Teredo as not global.)
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    addresses = [address]
    if address.version == 6 and address in _NAT64:
        addresses.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return any(not a.is_global or a.is_multicast for a in addresses)


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


def _classify(conn, target, providers, helper, scope):
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
    if scope.candidate_id is not None:
        row = conn.execute(
            "SELECT oa_url FROM candidates WHERE id = ? AND project_id = ?",
            (scope.candidate_id, scope.project_id),
        ).fetchone()
        if row and target == _origin_of(row[0]):
            return Kind.OPEN_ACCESS
    return None


def _policy(conn, level, kind, target, scope, private_problem, public_problem):
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
    if level == "normal" or kind is Kind.LOCAL_HELPER:
        return None
    if level == "private":
        if kind is Kind.MODEL_PROVIDER:
            return private_problem
        return "not_allowed_at_level" if kind is Kind.LOCAL_PROVIDER else None
    if level == "local_only":
        if kind is Kind.LOCAL_PROVIDER:
            declared = conn.execute("SELECT base_url FROM local_declarations").fetchall()
            return None if any(_origin_of(url) == target for (url,) in declared) else "not_declared"
        if kind in (Kind.SCHOLARLY_API, Kind.OPEN_ACCESS):
            return None if scope.approved is True else "not_approved"
        return "not_allowed_at_level"
    return "unknown_level"


def _private_problem(request: httpx.Request, inputs: GateInputs):
    """Why a request to OpenRouter cannot go out from a Private project, or None."""
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
    for model in models:
        flags = inputs.private_route(model)
        if not isinstance(flags, Mapping):
            return "route_not_allowed"
        if not _carries(body, flags):
            return "missing_flags"
    if not _carries(body, ZDR):
        return "missing_flags"
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key.strip() or inputs.key_attested(key.strip()) is not True:
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


def _foreign_redirect(request: httpx.Request, response: httpx.Response):
    """The destination of a redirect away from the request's origin, or None.

    Only a relative path, or an absolute URL with the same origin, stays. Anything
    else, including a Location httpx would read in an unusual way, leaves.
    """
    if not response.has_redirect_location:
        return None
    try:
        location = httpx.URL(response.headers["Location"])
    except httpx.InvalidURL:
        return "unknown"
    if not location.scheme and not location.host:
        return None
    target = _origin(location, location.scheme or request.url.scheme) if location.host else None
    if target is not None and target == _origin(request.url):
        return None
    return _show(target) or "unknown"


def _record(conn, request, scope, level, kind, destination, reason):
    conn.execute(
        "INSERT INTO audit_log (event, project_id, data) VALUES ('outbound', ?, ?)",
        (scope.project_id, json.dumps({
            "decision": "deny" if reason else "allow",
            "reason": reason,
            "kind": kind,
            "destination": destination,
            "method": request.method,
            "sensitivity": level,
            "approved": scope.approved is True,
        })),
    )

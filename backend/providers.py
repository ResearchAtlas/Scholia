"""Model providers and model routes, from the personal settings.

A provider is a `[providers.<name>]` table in the personal config.toml: its kind
(`openrouter` or `openai-compatible`) and base URL. Several can be configured at
once. Keys never live there; they are in the credential store under the
provider's name (backend/credentials.py). A project's config.toml cannot define
providers or keys; it only chooses a model among them.

A model route is a provider plus a model id. Every request to a provider goes
through the outbound gate's client for the project it serves, and the gate
knows the configured providers' origins from `gate_inputs`.
"""

from dataclasses import dataclass

from backend import governance
from backend.outbound_gate import GateInputs, is_openrouter
from backend.settings import load_settings

OPENROUTER = "openrouter"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str  # "openrouter" or "openai-compatible"
    base_url: str

    @property
    def is_openrouter(self) -> bool:
        return self.kind == "openrouter"

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


MIN_WINDOW = 4096  # the smallest supported window (slice-1 spec section 8)


def window(table: dict, model: str, reported) -> dict:
    """A model's window W (slice-1 spec section 8): the model's own setting, else its
    provider's default window; a setting fills a window the provider does not report and
    can lower a reported one, never raise it. status is "needed" with neither, and
    "too_small" under MIN_WINDOW, the smallest window a step can use."""
    reported = reported if type(reported) is int and reported > 0 else None
    setting = ((table.get("windows") or {}).get(model)) or table.get("default_window")
    in_use = min(reported, setting) if reported and setting else reported or setting
    status = "needed" if not in_use else "too_small" if in_use < MIN_WINDOW else "ok"
    return {"reported": reported, "setting": setting, "in_use": in_use, "status": status}


def offered(table: dict, provider: "Provider", model: str, recommended) -> bool:
    """Whether a provider's model is offered, by its `models` setting: Recommended (unset
    on OpenRouter), All (unset elsewhere), or the models picked. A model whose id is "auto"
    is never offered: the id means Auto (budget_router.AUTO)."""
    if model == "auto":
        return False
    choice = table.get("models")
    if choice is None:  # unset; an empty list is a Pick of nothing
        choice = "recommended" if provider.is_openrouter else "all"
    if choice == "all":
        return True
    if choice == "recommended":
        return model in recommended if provider.is_openrouter else True
    return model in choice


@dataclass(frozen=True)
class Route:
    provider: Provider
    model: str

    @property
    def key(self) -> str:
        """The route as it is recorded on a step: provider and model, no URL or key."""
        return f"{self.provider.name}:{self.model}"


def configured(data_root, settings=None, *, include_off=False) -> dict[str, Provider]:
    """The providers in the personal settings that have a kind and a base URL, by name.
    A provider turned off (`enabled = false`) is left out, so nothing calls it and the
    outbound gate does not allow its origin, unless include_off asks for it."""
    values = (settings or load_settings(data_root)).values
    found = {}
    for name, table in (values.get("providers") or {}).items():
        if isinstance(table, dict) and table.get("kind") and table.get("base_url") \
                and (include_off or table.get("enabled") is not False):
            found[name] = Provider(name, table["kind"], table["base_url"])
    return found


def gate_inputs(data_root) -> GateInputs:
    """What the outbound gate needs: the configured providers' base URLs, and for Private
    projects the allowlist entry covering an OpenRouter model, which OpenRouter must also list
    with a zero-retention endpoint, and whether a key's data settings are confirmed (see
    backend/governance.py). The gate calls the last two inside its decision transaction."""
    found = configured(data_root)
    openrouter = [p for p in found.values() if p.is_openrouter and is_openrouter(p.base_url)]

    def private_route(conn, model):
        flags = governance.private_flags(governance.allowlist(conn), model)
        return flags if flags is not None and any(governance.zero_retention(p, model) for p in openrouter) else None

    return GateInputs(provider_urls=tuple(p.base_url for p in found.values()), private_route=private_route,
                      key_attested=lambda conn, key: governance.key_attested(conn, data_root, key))


def resolve_route(data_root, provider_name: str | None, model: str | None) -> Route | None:
    """The route for a provider name and model id, or None when either is unknown.

    With no provider name, the only configured provider is used, or OpenRouter
    when several are configured.
    """
    providers = configured(data_root)
    if provider_name is None:
        if len(providers) == 1:
            provider_name = next(iter(providers))
        elif OPENROUTER in providers:
            provider_name = OPENROUTER
    provider = providers.get(provider_name)
    if provider is None or not model:
        return None
    return Route(provider, model)

from __future__ import annotations
from dataclasses import dataclass
from .catalog import DEFAULT_CATALOG, ProviderCatalog, ProviderSpec
from .resolve import resolve_provider
from .state import RuntimeState
from .types import ProviderCredential

@dataclass
class RegisteredProvider:
    spec: ProviderSpec
    api_key: str
    state: RuntimeState

class ProviderRegistry:
    def __init__(self, catalog: ProviderCatalog | None = None) -> None:
        self.catalog = catalog or DEFAULT_CATALOG
        self._providers: dict[str, RegisteredProvider] = {}

    def add(self, credential: ProviderCredential) -> RegisteredProvider:
        if not credential.api_key.strip():
            raise ValueError("api_key_required")
        spec = resolve_provider(credential.name, self.catalog.all())
        registered = RegisteredProvider(spec=spec, api_key=credential.api_key, state=RuntimeState())
        self._providers[spec.id] = registered
        return registered

    def all(self) -> list[RegisteredProvider]:
        return list(self._providers.values())

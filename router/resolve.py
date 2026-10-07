from __future__ import annotations
from difflib import SequenceMatcher
from collections.abc import Iterable
from .catalog import DEFAULT_CATALOG, ProviderSpec

class ProviderResolutionError(ValueError):
    pass

def _norm(value: str) -> str:
    return " ".join(value.lower().strip().replace("_"," ").split())

def resolve_provider(name: str, providers: Iterable[ProviderSpec] | None = None) -> ProviderSpec:
    catalog = tuple(providers) if providers is not None else DEFAULT_CATALOG.all()
    if not catalog:
        raise ProviderResolutionError("unknown_provider")
    needle = _norm(name)
    exact = [p for p in catalog if needle == _norm(p.id) or needle in {_norm(a) for a in p.aliases}]
    if len(exact) == 1:
        return exact[0]
    scored: list[tuple[float, ProviderSpec]] = []
    for p in catalog:
        candidates = (p.id, *p.aliases)
        score = max(SequenceMatcher(None, needle, _norm(c)).ratio() for c in candidates)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0]
    second = scored[1] if len(scored) > 1 else (0.0, None)
    if best[0] < 0.72:
        raise ProviderResolutionError("unknown_provider")
    if second[1] is not None and best[0] - second[0] < 0.08:
        raise ProviderResolutionError("ambiguous_provider")
    return best[1]

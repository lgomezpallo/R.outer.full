from __future__ import annotations
from difflib import SequenceMatcher
from .catalog import CATALOG, ProviderSpec

class ProviderResolutionError(ValueError):
    pass

def _norm(value: str) -> str:
    return " ".join(value.lower().strip().replace("_"," ").split())

def resolve_provider(name: str) -> ProviderSpec:
    needle = _norm(name)
    exact = [p for p in CATALOG if needle == _norm(p.id) or needle in {_norm(a) for a in p.aliases}]
    if len(exact) == 1:
        return exact[0]
    scored: list[tuple[float, ProviderSpec]] = []
    for p in CATALOG:
        candidates = (p.id, *p.aliases)
        score = max(SequenceMatcher(None, needle, _norm(c)).ratio() for c in candidates)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    best, second = scored[0], scored[1] if len(scored) > 1 else (0.0, None)
    if best[0] < 0.72:
        raise ProviderResolutionError("unknown_provider")
    if second[1] is not None and best[0] - second[0] < 0.08:
        raise ProviderResolutionError("ambiguous_provider")
    return best[1]

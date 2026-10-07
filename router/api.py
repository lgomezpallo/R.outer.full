from __future__ import annotations
import os
import secrets
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from .catalog import ModelSpec, ProviderSpec
from .router import Router
from .types import ProviderCredential, RouteRequest

app = FastAPI(title="Router IA", version="1.0.0")
router = Router(max_retries=int(os.getenv("ROUTER_MAX_RETRIES", "1")))

def require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("ROUTER_SERVICE_TOKEN", "").strip()
    if not expected:
        return
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "unauthorized")

class ProviderInput(BaseModel):
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)

class CatalogModelInput(BaseModel):
    id: str = Field(min_length=1)
    capabilities: list[str] = ["chat"]
    model_class: str = "standard"
    priority: int = 50

class CatalogProviderInput(BaseModel):
    id: str = Field(min_length=1)
    aliases: list[str] = []
    base_url: str = Field(min_length=1)
    protocol: str = "openai-compatible"
    models: list[CatalogModelInput] = []

class RouteInput(BaseModel):
    task: str = Field(min_length=1, max_length=30000)
    context: str = ""
    requirements: list[str] = []
    required_capabilities: list[str] = ["chat"]
    preferred_model_class: str | None = None
    timeout_s: float = 45.0

@app.on_event("startup")
def load_environment_providers() -> None:
    configured = [
        ("Groq", os.getenv("GROQ_API_KEY")),
        ("OpenRouter", os.getenv("OPENROUTER_API_KEY")),
    ]
    known = {p.spec.id for p in router.registry.all()}
    for name, key in configured:
        if key and name.lower() not in known:
            try:
                router.add_provider(name, key)
            except ValueError:
                pass

@app.get("/catalog")
def catalog():
    return {"providers": router.catalog.snapshot()}

@app.post("/catalog/providers", dependencies=[Depends(require_auth)])
def catalog_provider(payload: CatalogProviderInput):
    provider = ProviderSpec(
        id=payload.id.strip().lower(),
        aliases=tuple(alias.strip() for alias in payload.aliases if alias.strip()),
        base_url=payload.base_url.rstrip("/"),
        protocol=payload.protocol,
        models=tuple(
            ModelSpec(
                id=model.id,
                capabilities=frozenset(model.capabilities),
                model_class=model.model_class,
                priority=model.priority,
            )
            for model in payload.models
        ),
    )
    router.catalog.put(provider)
    return {"provider": provider.id, "cataloged": True}

@app.get("/health")
def health():
    providers = []
    for item in router.registry.all():
        providers.append({
            "provider": item.spec.id,
            "available": item.state.available,
            "health_ok": item.state.health_ok,
            "success_rate": item.state.success_rate,
            "latency_ms": item.state.ewma_latency_ms,
            "last_error": item.state.last_error,
        })
    return {"status": "ok", "providers": providers}

@app.post("/providers", dependencies=[Depends(require_auth)])
def add_provider(payload: ProviderInput):
    try:
        registered = router.registry.add(ProviderCredential(payload.name, payload.api_key))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"provider": registered.spec.id, "models": [m.id for m in registered.spec.models]}

@app.post("/route", dependencies=[Depends(require_auth)])
def route(payload: RouteInput):
    req = RouteRequest(
        task=payload.task,
        context=payload.context,
        requirements=tuple(payload.requirements),
        required_capabilities=frozenset(payload.required_capabilities),
        preferred_model_class=payload.preferred_model_class,
        timeout_s=payload.timeout_s,
    )
    result = router.route(req)
    return {
        "ok": result.ok,
        "text": result.text,
        "provider": result.provider,
        "model": result.model,
        "error": result.error,
        "attempts": [a.__dict__ for a in result.attempts],
        "decisions": [d.__dict__ for d in result.decisions],
    }

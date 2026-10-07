from __future__ import annotations
import os
import secrets
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from .catalog import ModelSpec, ProviderCatalog, ProviderSpec
from .router import Router
from .storage import RouterStore
from .types import RouteRequest

app = FastAPI(title="Router IA", version="2.0.0")
catalog = ProviderCatalog(storage_path=os.getenv("ROUTER_CATALOG_FILE") or "router-catalog.json")
store = RouterStore(path=os.getenv("ROUTER_DB_FILE", "router.db"), master_key=os.getenv("ROUTER_MASTER_KEY"))
router = Router(max_retries=int(os.getenv("ROUTER_MAX_RETRIES", "1")), catalog=catalog, store=store)

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
    task: str = Field(min_length=1, max_length=100000)
    context: str = ""
    requirements: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    preferred_model_class: str | None = None
    timeout_s: float = 45.0
    application_name: str = "unknown"
    decompose: bool | None = None
    max_subtasks: int = Field(default=8, ge=1, le=12)

@app.on_event("startup")
def load_environment_providers() -> None:
    router.load_persisted_providers()
    configured = [
        ("Groq", os.getenv("GROQ_API_KEY")),
        ("OpenRouter", os.getenv("OPENROUTER_API_KEY")),
        ("NVIDIA", os.getenv("NVIDIA_API_KEY")),
        ("Mistral", os.getenv("MISTRAL_API_KEY")),
        ("Cerebras", os.getenv("CEREBRAS_API_KEY")),
        ("SambaNova", os.getenv("SAMBANOVA_API_KEY")),
    ]
    known = {p.spec.id for p in router.registry.all()}
    for name, key in configured:
        if key and name.lower() not in known:
            try:
                router.add_provider(name, key, persist=store.can_persist_secrets, discover=True)
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
            "models": {
                model_id: {
                    "success_rate": state.success_rate,
                    "latency_ms": state.ewma_latency_ms,
                    "last_error": state.last_error,
                }
                for model_id, state in item.state.models.items()
            },
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
        application_name=payload.application_name,
        decompose=payload.decompose,
        max_subtasks=payload.max_subtasks,
    )
    result = router.process(req)
    return {
        "ok": result.ok,
        "text": result.text,
        "provider": result.provider,
        "model": result.model,
        "error": result.error,
        "attempts": [a.__dict__ for a in result.attempts],
        "decisions": [d.__dict__ for d in result.decisions],
        "orchestration": (result.raw or {}).get("orchestration"),
    }

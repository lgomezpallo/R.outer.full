from __future__ import annotations
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from .router import Router
from .types import RouteRequest

app = FastAPI(title="Router IA", version="1.0.0")
router = Router(max_retries=int(os.getenv("ROUTER_MAX_RETRIES", "1")))

class ProviderInput(BaseModel):
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)

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

@app.get("/health")
def health():
    return {"status": "ok", "providers": [p.spec.id for p in router.registry.all()]}

@app.post("/providers")
def add_provider(payload: ProviderInput):
    try:
        registered = router.registry.add(__import__("router.types", fromlist=["ProviderCredential"]).ProviderCredential(payload.name, payload.api_key))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"provider": registered.spec.id, "models": [m.id for m in registered.spec.models]}

@app.post("/route")
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

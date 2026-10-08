from __future__ import annotations
import os
import secrets
import time
import threading
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from .catalog import ModelSpec, ProviderCatalog, ProviderSpec
from .router import Router
from .security import validate_provider_base_url
from .storage import RouterStore
from .types import RouteRequest

app = FastAPI(title="Router IA", version="2.0.0")
catalog = ProviderCatalog(storage_path=os.getenv("ROUTER_CATALOG_FILE") or "router-catalog.json")
store = RouterStore(path=os.getenv("ROUTER_DB_FILE", "router.db"), master_key=os.getenv("ROUTER_MASTER_KEY"))
router = Router(max_retries=int(os.getenv("ROUTER_MAX_RETRIES", "1")), catalog=catalog, store=store)

def require_auth(authorization: str | None = Header(default=None)) -> str:
    expected = os.getenv("ROUTER_SERVICE_TOKEN", "").strip()
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if expected and supplied and secrets.compare_digest(supplied, expected):
        return "service"
    if supplied:
        app_name = store.verify_app_token(supplied)
        if app_name:
            return app_name
    if not expected and not store.list_app_tokens():
        return "development"
    raise HTTPException(401, "unauthorized")

def require_admin(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("ROUTER_SERVICE_TOKEN", "").strip()
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "admin_token_required")

class TokenInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)

class ProviderInput(BaseModel):
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)

class ProviderTestInput(BaseModel):
    provider: str

class CatalogModelInput(BaseModel):
    id: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    model_class: str = "standard"
    priority: int = 50
    strategic_cost: int = 50

class CatalogProviderInput(BaseModel):
    id: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    base_url: str = Field(min_length=1)
    protocol: str = "openai-compatible"
    priority: int = 50
    strategic_cost: int = 50
    discover_models: bool = True
    models: list[CatalogModelInput] = Field(default_factory=list)

class ChatMessage(BaseModel):
    role: str
    content: str

class OpenAIChatInput(BaseModel):
    model: str = "router-ia-auto"
    messages: list[ChatMessage]
    stream: bool = False
    task_type: str | None = None

class CapabilityProbeInput(BaseModel):
    provider: str
    model: str
    capability: str

class CapabilityAuditInput(BaseModel):
    provider: str
    inconclusive_only: bool = False

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
    cf_base = os.getenv("CLOUDFLARE_WORKERS_AI_BASE_URL", "").strip()
    cf_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if cf_base and cf_token:
        router.catalog.put(ProviderSpec(
            id="cloudflare",
            aliases=("cloudflare", "workers ai"),
            base_url=cf_base.rstrip("/"),
            protocol="openai-compatible",
            priority=72,
            strategic_cost=12,
            discover_models=True,
            models=(),
        ))
        try:
            router.add_provider("cloudflare", cf_token, persist=store.can_persist_secrets, discover=True)
        except (ValueError, RuntimeError):
            pass

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

    if os.getenv("ROUTER_STARTUP_PROVIDER_TESTS", "").strip() == "1":
        for item in router.registry.all():
            provider_id = item.spec.id
            print(f"ROUTER_PROVIDER_DISCOVERY provider={provider_id} models={len(item.spec.models)}")
            try:
                result = router.test_provider(provider_id)
                print(
                    "ROUTER_PROVIDER_TEST "
                    f"provider={provider_id} "
                    f"status={result.get('status')} "
                    f"model={result.get('model')} "
                    f"latency_ms={result.get('latency_ms')} "
                    f"health={result.get('health_status')}"
                )
            except Exception as exc:
                print(
                    "ROUTER_PROVIDER_TEST "
                    f"provider={provider_id} status=error "
                    f"error={type(exc).__name__}"
                )

    if os.getenv("ROUTER_STARTUP_CAPABILITY_PROFILE", "").strip() == "1":
        threading.Thread(target=_startup_capability_profile, daemon=True).start()

def _startup_capability_profile() -> None:
    if os.getenv("ROUTER_STARTUP_CAPABILITY_PROFILE", "").strip() != "1":
        return
    for item in router.registry.all():
        for capability in ("reasoning", "code", "summarization", "document", "vision", "transcription", "speech", "image_generation"):
            candidates = [m for m in item.spec.models if ("chat" in m.capabilities if capability in {"reasoning","code","summarization","document"} else capability in m.capabilities)]
            verified = None
            checked = 0
            print(f"ROUTER_CAPABILITY_CANDIDATES provider={item.spec.id} capability={capability} candidates={len(candidates)}", flush=True)
            limit = 2 if capability in {"reasoning","code","summarization","document"} else 6
            for model in candidates[:limit]:
                checked += 1
                try:
                    result = router.verify_capability(item.spec.id, model.id, capability)
                except Exception:
                    continue
                print(
                    f"ROUTER_CAPABILITY_DETAIL provider={item.spec.id} capability={capability} "
                    f"model={model.id} status={result.get('status')} "
                    f"http={result.get('http_status', '-')} evidence={result.get('evidence', '-')}",
                    flush=True,
                )
                if result.get("status") == "verified":
                    verified = model.id
                    break
            print(f"ROUTER_CAPABILITY_PROFILE provider={item.spec.id} capability={capability} candidates={len(candidates)} checked={checked} verified={verified or '-'}", flush=True)


@app.get("/catalog")
def catalog():
    return {"providers": router.catalog.snapshot()}

@app.post("/catalog/providers", dependencies=[Depends(require_admin)])
def catalog_provider(payload: CatalogProviderInput):
    provider = ProviderSpec(
        id=payload.id.strip().lower(),
        aliases=tuple(alias.strip() for alias in payload.aliases if alias.strip()),
        base_url=validate_provider_base_url(payload.base_url),
        protocol=payload.protocol,
        priority=payload.priority,
        strategic_cost=payload.strategic_cost,
        discover_models=payload.discover_models,
        models=tuple(
            ModelSpec(
                id=model.id,
                capabilities=frozenset(model.capabilities),
                model_class=model.model_class,
                priority=model.priority,
                strategic_cost=model.strategic_cost,
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
            "health_status": item.state.health_status,
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

@app.post("/tokens", dependencies=[Depends(require_admin)])
def create_token(payload: TokenInput):
    return {"name": payload.name, "token": store.create_app_token(payload.name)}

@app.get("/tokens", dependencies=[Depends(require_admin)])
def list_tokens():
    return {"tokens": store.list_app_tokens()}

@app.post("/providers", dependencies=[Depends(require_admin)])
def add_provider(payload: ProviderInput):
    try:
        registered = router.add_provider(
            payload.name,
            payload.api_key,
            persist=store.can_persist_secrets,
            discover=True,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "provider": registered.spec.id,
        "models": [m.id for m in registered.spec.models],
        "credential_persisted": store.can_persist_secrets,
    }

@app.post("/providers/test", dependencies=[Depends(require_admin)])
def test_provider(payload: ProviderTestInput):
    try:
        return router.test_provider(payload.provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/capabilities/probe", dependencies=[Depends(require_admin)])
def probe_capability(payload: CapabilityProbeInput):
    try:
        return router.verify_capability(payload.provider, payload.model, payload.capability)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/capabilities/audit", dependencies=[Depends(require_admin)])
def audit_capabilities(payload: CapabilityAuditInput):
    try:
        return router.audit_provider_capabilities(
            payload.provider,
            inconclusive_only=payload.inconclusive_only,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/route")
def route(payload: RouteInput, app_identity: str = Depends(require_auth)):
    req = RouteRequest(
        task=payload.task,
        context=payload.context,
        requirements=tuple(payload.requirements),
        required_capabilities=frozenset(payload.required_capabilities),
        preferred_model_class=payload.preferred_model_class,
        timeout_s=payload.timeout_s,
        application_name=(
            payload.application_name
            if app_identity == "service" and payload.application_name != "unknown"
            else app_identity
        ),
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


@app.post("/v1/chat/completions")
def openai_chat(payload: OpenAIChatInput, app_identity: str = Depends(require_auth)):
    if payload.stream:
        raise HTTPException(400, "streaming_not_supported")
    if not payload.messages:
        raise HTTPException(400, "messages_required")
    users = [item.content for item in payload.messages if item.role == "user"]
    if not users:
        raise HTTPException(400, "user_message_required")
    context = "\n".join(
        f"{item.role}: {item.content}"
        for item in payload.messages[:-1]
    )
    capabilities = {"chat"}
    if payload.task_type in {"code", "coding"}:
        capabilities.add("code")
    elif payload.task_type == "reasoning":
        capabilities.add("reasoning")
    elif payload.task_type == "summarization":
        capabilities.add("summarization")
    result = router.process(RouteRequest(
        task=users[-1],
        context=context,
        required_capabilities=frozenset(capabilities),
        application_name=app_identity,
    ))
    if not result.ok or not result.text:
        raise HTTPException(502, result.error or "router_failed")
    return {
        "id": "chatcmpl-router-" + str(int(time.time() * 1000)),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "router-ia-auto",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "router": {"provider": result.provider, "model": result.model},
    }

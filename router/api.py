from __future__ import annotations
import os
import secrets
import time
import threading
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .catalog import ModelSpec, ProviderCatalog, ProviderSpec
from .router import Router
from .security import validate_provider_base_url
from .storage import RouterStore
from .types import RouteRequest
from .temporal import with_temporal_context
from .web_search import needs_current_search, search_context

app = FastAPI(title="Router IA", version="2.0.0")
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/app", StaticFiles(directory=_STATIC_DIR), name="app")

@app.get("/", include_in_schema=False)
def router_ui():
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"name": "Router IA", "status": "ok"}
catalog = ProviderCatalog(storage_path=os.getenv("ROUTER_CATALOG_FILE") or "router-catalog.json")
store = RouterStore(path=os.getenv("ROUTER_DB_FILE", "router.db"), master_key=os.getenv("ROUTER_MASTER_KEY"))
router = Router(max_retries=int(os.getenv("ROUTER_MAX_RETRIES", "1")), catalog=catalog, store=store)

def require_auth(authorization: str | None = Header(default=None)) -> str:
    expected = os.getenv("ROUTER_SERVICE_TOKEN", "").strip()
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if expected and supplied and secrets.compare_digest(supplied, expected):
        return "service"
    iachat_token = os.getenv("ROUTER_IACHAT_TOKEN", "").strip()
    if iachat_token and supplied and secrets.compare_digest(supplied, iachat_token):
        return "iachat"
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

class AuditStepInput(BaseModel):
    provider: str
    max_calls: int = Field(default=1, ge=1, le=3)

# Conservative self-imposed daily budgets, not advertised provider quotas.
AUDIT_DAILY_LIMITS = {"groq": 12, "openrouter": 5, "cloudflare": 4, "nvidia": 1}

class CapabilityAuditInput(BaseModel):
    provider: str
    inconclusive_only: bool = False

class RouteInput(BaseModel):
    task: str = Field(min_length=1, max_length=100000)
    context: str = ""
    conversation: list[ChatMessage] = Field(default_factory=list, max_length=100)
    requirements: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    preferred_model_class: str | None = None
    timeout_s: float = 45.0
    application_name: str = "unknown"
    decompose: bool | None = None
    max_subtasks: int = Field(default=12, ge=1, le=50)

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
    if os.getenv("ROUTER_STARTUP_ROUTING_SMOKE", "").strip() == "1":
        threading.Thread(target=_startup_routing_smoke, daemon=True).start()
    if os.getenv("ROUTER_STARTUP_ORCHESTRATION_SMOKE", "").strip() == "1":
        threading.Thread(target=_startup_orchestration_smoke, daemon=True).start()
    if os.getenv("ROUTER_STARTUP_HETEROGENEOUS_SMOKE", "").strip() == "1":
        threading.Thread(target=_startup_heterogeneous_smoke, daemon=True).start()

def _startup_heterogeneous_smoke() -> None:
    if os.getenv("ROUTER_STARTUP_HETEROGENEOUS_SMOKE", "").strip() != "1":
        return
    long_material = ("alpha beta gamma delta epsilon zeta eta theta " * 1100)
    task = (
        "Construí una única respuesta final usando exactamente tres subtareas independientes. "
        "Subtarea 1: resolver 47*63 y justificar brevemente; capability reasoning. "
        "Subtarea 2: escribir una función Python clamp(x, low, high); capability code. "
        "Subtarea 3: leer el material largo incluido al final y devolver exactamente la marca "
        "ROUTER_HETERO_55119; capability long_context. "
        "Después verificá y componé una respuesta única con los tres resultados. "
        "No mezcles las tres subtareas.\nMATERIAL LARGO:\n" + long_material +
        "\nMARCA: ROUTER_HETERO_55119"
    )
    try:
        result = router.process(RouteRequest(
            task=task,
            required_capabilities=frozenset({"chat","reasoning"}),
            application_name="router-heterogeneous-smoke",
            decompose=True,
            max_subtasks=5,
            timeout_s=45,
        ))
        trace = (result.raw or {}).get("orchestration", {})
        subtasks = trace.get("subtasks", [])
        providers = ",".join(
            f"{item.get('id')}:{item.get('provider') or '-'}:{item.get('model') or '-'}"
            for item in subtasks
        )
        unique_providers = sorted({item.get("provider") for item in subtasks if item.get("provider")})
        print(
            "ROUTER_HETEROGENEOUS_SMOKE "
            f"ok={result.ok} subtasks={len(subtasks)} "
            f"unique_providers={len(unique_providers)} "
            f"providers={providers or '-'} "
            f"verification={'yes' if trace.get('verification') else 'no'} "
            f"error={result.error or '-'}",
            flush=True,
        )
    except Exception as exc:
        print(
            "ROUTER_HETEROGENEOUS_SMOKE "
            f"ok=False error={type(exc).__name__}",
            flush=True,
        )

def _startup_orchestration_smoke() -> None:
    if os.getenv("ROUTER_STARTUP_ORCHESTRATION_SMOKE", "").strip() != "1":
        return
    task = (
        "Analiza este problema en pasos dependientes y devolvé una única respuesta final: "
        "Una cuadrilla tiene que reparar tres sumideros. El primero demora 2 horas, "
        "el segundo 3 horas y el tercero 1 hora. Solo hay dos equipos disponibles. "
        "Proponé un orden de trabajo que minimice el tiempo total, justificá el criterio "
        "y terminá con el tiempo mínimo total estimado."
    )
    try:
        result = router.process(RouteRequest(
            task=task,
            required_capabilities=frozenset({"chat","reasoning"}),
            application_name="router-orchestration-smoke",
            decompose=True,
            max_subtasks=6,
            timeout_s=35,
        ))
        trace = (result.raw or {}).get("orchestration", {})
        subtasks = trace.get("subtasks", [])
        phases = ",".join(a.phase for a in result.attempts)
        providers = ",".join(
            f"{item.get('id')}:{item.get('provider') or '-'}"
            for item in subtasks
        )
        print(
            "ROUTER_ORCHESTRATION_SMOKE "
            f"ok={result.ok} subtasks={len(subtasks)} "
            f"providers={providers or '-'} phases={phases or '-'} "
            f"verification={'yes' if trace.get('verification') else 'no'} "
            f"error={result.error or '-'}",
            flush=True,
        )
    except Exception as exc:
        print(
            "ROUTER_ORCHESTRATION_SMOKE "
            f"ok=False error={type(exc).__name__}",
            flush=True,
        )

def _startup_routing_smoke() -> None:
    if os.getenv("ROUTER_STARTUP_ROUTING_SMOKE", "").strip() != "1":
        return

    cases = [
        ("simple", RouteRequest(
            task="Reply with exactly ROUTER_SMOKE_OK",
            required_capabilities=frozenset({"chat"}),
            application_name="router-smoke",
            decompose=False,
            timeout_s=25,
        )),
        ("reasoning", RouteRequest(
            task="Solve 29*37. Reply with only the number.",
            required_capabilities=frozenset({"reasoning"}),
            application_name="router-smoke",
            decompose=False,
            timeout_s=25,
        )),
        ("code", RouteRequest(
            task="Write a Python function square(n) that returns n*n. Return only code.",
            required_capabilities=frozenset({"code"}),
            application_name="router-smoke",
            decompose=False,
            timeout_s=25,
        )),
        ("long-fallback", RouteRequest(
            task=(("alpha beta gamma delta epsilon zeta eta theta " * 1100)
                  + "\nIMPORTANT MARKER: ROUTER_SMOKE_LONG_93217"
                  + "\nReply with exactly ROUTER_SMOKE_LONG_93217"),
            required_capabilities=frozenset({"chat"}),
            application_name="router-smoke",
            decompose=False,
            timeout_s=45,
        )),
    ]

    for name, request in cases:
        try:
            result = router.route(request)
            attempts = ",".join(
                f"{a.provider}/{a.model}:{'ok' if a.ok else 'fail'}"
                for a in result.attempts
            )
            print(
                "ROUTER_ROUTING_SMOKE "
                f"case={name} ok={result.ok} provider={result.provider or '-'} "
                f"model={result.model or '-'} attempts={attempts or '-'} "
                f"error={result.error or '-'}",
                flush=True,
            )
        except Exception as exc:
            print(
                "ROUTER_ROUTING_SMOKE "
                f"case={name} ok=False error={type(exc).__name__}",
                flush=True,
            )

def _startup_capability_profile() -> None:
    if os.getenv("ROUTER_STARTUP_CAPABILITY_PROFILE", "").strip() != "1":
        return
    for item in router.registry.all():
        for capability in ("json", "reasoning", "code", "summarization", "document", "tools", "long_context", "vision", "transcription", "speech", "image_generation", "image_editing"):
            candidates = [m for m in item.spec.models if ("chat" in m.capabilities if capability in {"json","reasoning","code","summarization","document","tools","long_context"} else capability in m.capabilities)]
            verified = None
            checked = 0
            print(f"ROUTER_CAPABILITY_CANDIDATES provider={item.spec.id} capability={capability} candidates={len(candidates)}", flush=True)
            limit = 2 if capability in {"json","reasoning","code","summarization","document","tools","long_context"} else 6
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

@app.get("/performance")
def performance():
    """Operational measurements only; API success does not prove answer quality."""
    return {"metrics": store.model_performance_report(), "quality_measured": False}

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

@app.get("/audit/progress")
def audit_progress():
    """Read-only progress; inspecting it never invokes any model."""
    counts = {}
    for provider in router.registry.all():
        if provider.spec.id not in AUDIT_DAILY_LIMITS:
            continue
        models = provider.spec.models
        verified = sum(1 for m in models if any(e.status == "verified" and e.evidence != "migrated_from_v1" for e in m.evidence))
        inconclusive = sum(1 for m in models if any(e.status == "inconclusive" for e in m.evidence))
        untouched = sum(1 for m in models if not any(e.evidence != "migrated_from_v1" for e in m.evidence))
        counts[provider.spec.id] = {
            "total_routes": len(models),
            "verified_routes": verified,
            "inconclusive_routes": inconclusive,
            "untested_routes": untouched,
        }
    used = store.audit_budget_used()
    return {
        "platforms": counts,
        "daily_budget": {p: {"limit": limit, "used": used.get(p, 0)}
                         for p, limit in AUDIT_DAILY_LIMITS.items()},
        "note": "Verificación por capacidad, no garantía de calidad. El presupuesto diario se reinicia a las 00:00 UTC.",
    }

@app.get("/audit/budget", dependencies=[Depends(require_admin)])
def audit_budget():
    used = store.audit_budget_used()
    return {"limits": AUDIT_DAILY_LIMITS, "used": used, "note": "Límites internos conservadores, no cuotas oficiales del proveedor."}

@app.post("/audit/step", dependencies=[Depends(require_admin)])
def audit_step(payload: AuditStepInput):
    """Bounded, opt-in probes. Never retry a model already tested for chat."""
    from .catalog import infer_discovered_capabilities
    provider_id = payload.provider.strip().lower()
    limit = AUDIT_DAILY_LIMITS.get(provider_id)
    if limit is None:
        raise HTTPException(400, "provider_not_approved_for_budgeted_audit")
    provider = next((p for p in router.registry.all() if p.spec.id == provider_id), None)
    if provider is None:
        raise HTTPException(400, "provider_not_registered")
    candidates = [
        m for m in provider.spec.models
        if "chat" in m.capabilities
        and "chat" in infer_discovered_capabilities(m.id)
        and not any(e.capability == "chat" for e in m.evidence)
    ]
    candidates.sort(key=lambda m: (len(m.capabilities - {"chat", "json", "fast"}), m.strategic_cost, m.id))
    results = []
    used = store.audit_budget_used().get(provider_id, 0)
    for model in candidates[:payload.max_calls]:
        permitted, used = store.reserve_audit_budget(provider_id, limit)
        if not permitted:
            break
        results.append(router.verify_capability(provider_id, model.id, "chat"))
    return {
        "provider": provider_id, "results": results, "pending_chat": len(candidates)-len(results),
        "daily_limit": limit, "used_today": used,
        "note": "Una reserva corresponde a una prueba de texto; otros tipos de prueba se agregarán por separado.",
    }

@app.post("/capabilities/audit", dependencies=[Depends(require_admin)])
def audit_capabilities(payload: CapabilityAuditInput):
    try:
        return router.audit_provider_capabilities(
            payload.provider,
            inconclusive_only=payload.inconclusive_only,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

def _enriched_context(task: str, context: str) -> str:
    base = with_temporal_context(context)
    if needs_current_search(task):
        base += "\n" + search_context(task)
    return base


@app.post("/route")
def route(payload: RouteInput, app_identity: str = Depends(require_auth)):
    req = RouteRequest(
        task=payload.task,
        context=_enriched_context(payload.task, payload.context),
        conversation=tuple((m.role, m.content) for m in payload.conversation if m.role in ("user", "assistant") and m.content),
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
        context=_enriched_context(users[-1], context),
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

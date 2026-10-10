"""Offline selector smoke test using the persisted Router catalog; no API calls."""
from __future__ import annotations
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router.catalog import ProviderCatalog, infer_discovered_capabilities
import router.selector as selector_module
from router.registry import RegisteredProvider
from router.selector import rank
from router.state import RuntimeState
from router.types import RouteRequest

path = Path(os.environ.get("ROUTER_CATALOG_FILE", "/home/ubuntu/Proyecto/Datos/Router/catalog.json"))
if not path.is_file():
    raise SystemExit(f"ERROR: No existe el catálogo: {path}")

catalog = ProviderCatalog(storage_path=path)
platforms = {"groq", "openrouter", "nvidia", "cloudflare"}
providers = [RegisteredProvider(spec=p, api_key="", state=RuntimeState())
             for p in catalog.all() if p.id in platforms]
print(f"Selector cargado desde: {selector_module.__file__}")
print(f"Clasificación bge-m3: {sorted(infer_discovered_capabilities("@cf/baai/bge-m3"))}")
print(f"Catálogo: {path} | plataformas: {len(providers)} | rutas: {sum(len(p.spec.models) for p in providers)}")
if not providers:
    raise SystemExit("ERROR: no se encontraron plataformas")

for capability, task in [("chat", "Saludá en español"), ("vision", "Describí esta imagen")]:
    ranked = rank(providers, RouteRequest(task=task, required_capabilities=frozenset({capability})))
    print(f"\nTarea: {capability} | candidatos: {len(ranked)}")
    for index, decision in enumerate(ranked[:10], 1):
        shortage = next((x for x in decision.reasons if x.startswith("scarcity_penalty=")), "sin penalización")
        print(f"{index:2}. {decision.provider:11} {decision.model[:54]:54} {shortage:24} puntuación={decision.score:.0f}")

print("\nNOTA: Simulación sin estado operativo ni cuotas en tiempo real. No se realizaron llamadas externas.")

chat_rank=rank(providers, RouteRequest(task="Hola", required_capabilities=frozenset({"chat"})))
blocked=("bge-m3","distilbert","llama-guard","resnet")
assert not any(any(b in d.model.lower() for b in blocked) for d in chat_rank), "ERROR: modelo no conversacional en candidatos de chat"
print("OK: embeddings, clasificadores y filtros de seguridad excluidos del chat.")
llava=[d for d in chat_rank if "llava-" in d.model.lower()]
assert not llava or all(any(r.startswith("scarcity_penalty=") and int(r.split("=")[1]) > 0 for r in d.reasons) for d in llava), "ERROR: LLaVA no está penalizado como especialista en visión"
print("OK: los modelos LLaVA reciben penalización por visión en tareas de texto.")

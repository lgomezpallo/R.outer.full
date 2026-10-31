# R.outer.full

Router de IA común para IAchat, Forja y futuras aplicaciones.

## Qué hace

Recibe una tarea completa, decide si conviene resolverla directamente o descomponerla, ejecuta subtareas con los recursos suficientes de menor costo estratégico, aplica retry/fallback, verifica cuando corresponde y recompone una única respuesta.

Las aplicaciones no conocen Groq, OpenRouter, NVIDIA, Mistral, Cerebras, SambaNova ni ningún proveedor concreto.

## Criterio de costo

**Política vigente:** sólo son elegibles modelos marcados explícitamente `free`, con endpoint permitido. Los modelos `paid` o `unknown` se excluyen en selección y transporte; ante agotamiento de cuota se intenta otro gratuito o se falla sin respuesta. La auditoría de facturación por cuenta no es un prerrequisito de desarrollo, CI o fusión. La marca `free` no garantiza ausencia de cargos en una cuenta. No contratar servicios ni habilitar pagos. El costo estratégico de selección (rareza, reemplazabilidad) es distinto del riesgo de cobro.

Entre candidatos saludables y suficientes, usa primero el menos valioso. Los modelos fuertes/escasos quedan para tareas que realmente los necesiten.

## Arquitectura

- Catálogo proveedor -> múltiples modelos.
- Capacidades declaradas y evidencia verified / unsupported / inconclusive.
- Descubrimiento dinámico de /models cuando el proveedor lo permite.
- Métricas por proveedor/modelo.
- Estado, cooldown, retries y fallback clasificado.
- Persistencia local sin servicios pagos.
- Credenciales opcionalmente cifradas con AES-GCM.
- Orquestación adaptativa inspirada en 5+1 x 2.
- Contrato nativo /route.
- Fachada compatible con OpenAI /v1/chat/completions.

El diseño canónico está en docs/CANONICAL_ROUTER.md.

## Proveedores bootstrap

El catálogo ya reconoce Groq, OpenRouter, NVIDIA NIM, Mistral, Cerebras y SambaNova.

Variables de entorno reconocidas: GROQ_API_KEY, OPENROUTER_API_KEY, NVIDIA_API_KEY, MISTRAL_API_KEY, CEREBRAS_API_KEY y SAMBANOVA_API_KEY.

Fuera del modo offline, cargar una clave puede consultar /models para enriquecer el catálogo; eso no autoriza inferencia ni acredita gratuidad. Para validación sin tráfico externo, configurar `ROUTER_OFFLINE_ONLY=1`, no aportar claves de proveedores y aislar la salida de red.

## Ejecutar

Ejemplo: definir GROQ_API_KEY y ROUTER_SERVICE_TOKEN, opcionalmente ROUTER_MASTER_KEY para persistir claves cifradas, y ejecutar uvicorn router.api:app --host 0.0.0.0 --port 8010.

También puede construirse con Docker mediante docker build -t router-ia .

## API

- GET /health
- GET /catalog
- POST /providers
- POST /capabilities/probe
- POST /route
- POST /v1/chat/completions

POST /route acepta task, context, requirements, capacidades, decompose opcional y max_subtasks. Si decompose no se indica, Router decide.

## Seguridad

- Ninguna API key en Git.
- Catálogo y respuestas nunca exponen claves.
- ROUTER_SERVICE_TOKEN protege endpoints sensibles.
- ROUTER_MASTER_KEY habilita persistencia cifrada de credenciales.
- Una respuesta normal correcta no convierte automáticamente una capacidad en verificada: sólo un probe específico genera evidencia.

## Estado

La candidata de PR #87 requiere CI y staging aislado satisfactorios antes de fusionar o promover. `oracle-promote` permanece deshabilitado; ninguna validación offline acredita un despliegue productivo. La falta de auditoría económica por cuenta ya no bloquea integrar código. No se garantiza ausencia de cargos. Ver [checklist y contrato de release](docs/RELEASE_CANDIDATE_GO_UTE_OFFLINE.md).

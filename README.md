# R.outer.full

Router de IA común para IAchat, Forja y futuras aplicaciones.

## Qué hace

Recibe una tarea completa, decide si conviene resolverla directamente o descomponerla, ejecuta subtareas con los recursos suficientes de menor costo estratégico, aplica retry/fallback, verifica cuando corresponde y recompone una única respuesta.

Las aplicaciones no conocen Groq, OpenRouter, NVIDIA, Mistral, Cerebras, SambaNova ni ningún proveedor concreto.

## Criterio de costo

Para este proyecto los proveedores se consideran de costo monetario cero. El costo que optimiza Router es estratégico: rareza, importancia, reemplazabilidad y conveniencia de reservar un recurso.

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

Al cargar una clave, Router intenta consultar /models y enriquecer el catálogo. Una falla de descubrimiento no impide usar los modelos bootstrap.

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

La v2 busca ser una base usable, no una demo: CI, tests, Docker, catálogo, orquestación, persistencia, métricas, fallback y compatibilidad HTTP están en el repositorio.

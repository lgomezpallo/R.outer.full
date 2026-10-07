# Router IA — diseño canónico

Este archivo fija el contrato actual. Ningún cambio futuro puede degradar estas capacidades sin una decisión explícita.

## Responsabilidad

Router recibe una tarea completa desde cualquier aplicación. Decide si conviene resolverla directamente o descomponerla, distribuye el trabajo entre recursos suficientes de menor costo estratégico, aplica retry/fallback, verifica resultados cuando corresponde, recompone una respuesta única y devuelve una traza auditable.

Las aplicaciones no conocen proveedores ni modelos.

## Ejecución adaptativa 5+1 x 2

El planificador dispone de dos carriles de funciones:

- análisis: understand, inspect, design, evaluate, verify + reserve;
- ejecución: plan, execute, integrate, recover, coordinate + reserve.

No activa todos los roles por obligación. Usa sólo los necesarios para la tarea. Las subtareas simples reciben un techo de costo estratégico; las críticas pueden escalar a recursos fuertes o escasos.

## Costo estratégico

Todos los proveedores se consideran de costo monetario cero para este sistema. "Costo" significa valor del recurso:

- abundancia/reemplazabilidad del proveedor;
- importancia del modelo;
- rareza o cuota;
- conveniencia de reservarlo para tareas difíciles.

Entre candidatos saludables y suficientes, Router prioriza el menor costo estratégico. La prioridad administrativa, éxito y latencia resuelven empates y degradaciones.

## Catálogo

Proveedor -> múltiples modelos. Cada proveedor/modelo conserva:

- endpoint y protocolo;
- aliases;
- capacidades declaradas;
- evidencia de capacidades: verified / unsupported / inconclusive;
- prioridad;
- costo estratégico;
- modelos descubiertos dinámicamente cuando existe /models.

Una llamada normal exitosa NO verifica una capacidad. Las capacidades se verifican con probes específicos y evidencia.

## Proveedores bootstrap

El catálogo incluye presets para Groq, OpenRouter, NVIDIA NIM, Mistral, Cerebras y SambaNova. Las claves no viven en Git. El servicio también intenta cargar esas claves desde variables de entorno y puede descubrir modelos automáticamente.

## Persistencia y seguridad

- catálogo persistente;
- métricas por proveedor/modelo y fase;
- historial de intentos;
- credenciales opcionalmente persistidas cifradas con AES-GCM mediante ROUTER_MASTER_KEY;
- fallback a variables de entorno cuando no se desea persistir claves;
- token de servicio para la API.

## APIs

- POST /route: contrato nativo task + context + requirements; usa orquestación adaptativa.
- POST /v1/chat/completions: fachada compatible con OpenAI para clientes genéricos.
- POST /providers: alta de proveedor.
- GET /catalog: catálogo sin secretos.
- POST /capabilities/probe: verificación explícita de capacidades.
- GET /health: estado operativo y métricas.

## Regla de evolución

El ZIP Router-IA-Privado aporta la base madura de seguridad, persistencia, health, clasificación de errores y adaptadores. El Router actual agrega catálogo multi-modelo, evidencia por capacidad, costo estratégico y descomposición/recomposición. Las mejoras se fusionan; no se vuelve a una versión más pobre.

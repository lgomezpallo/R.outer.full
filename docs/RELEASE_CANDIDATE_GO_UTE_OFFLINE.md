# Router — candidata de entrega offline para Gestión de Obras UTE

Fecha de consolidación: 2026-10-28. **Sin autorización de despliegue, fusión ni tráfico de inferencia.**

## Fuente única y conservación de cambios

- PR de consolidación: **#87**, cuyo head original es `c5a0c67493ab0bdc2dfb23a96570583c48e8dcff`. La rama candidata continúa sobre ese commit.
- La secuencia de PR **#45 a #87** es lineal: el `head_sha` de cada PR coincide con el `base_sha` del siguiente. Se comprobó mediante metadatos GitHub el 28/10. No hay que fusionar PRs apilados por separado.
- `main` de comparación: `0f314bbb0e7154bbf1e322578d116f26b04d8ee4`. El head original de #87 estaba **204 commits por delante y 0 detrás**; el head de #45 es ancestro con **144 commits** de diferencia.
- Referencia histórica de Oracle: `339898a6081061b41ad2557b600e06efbc1f00ef`. GitHub confirma que el head original de #87 está **212 commits por delante, 0 detrás**, con **97 archivos distintos** en la comparación: 18 `router/`, 66 `tests/`, 6 `scripts/`, 5 workflows, 1 documento y `pyproject.toml`. **Esto compara commits de GitHub, NO verifica qué hay actualmente en la VM.**
- Esta candidata sólo incorpora correcciones de bloqueo para validar en modo offline y documentación; no amplía el catálogo ni agrega proveedores.
- Mantener **#45 draft**, sin fusionar. PRs anteriores: conservar como historial y marcar redundantes sólo tras verificar que la candidata preserva los commits y pasa CI. No borrar ramas ni cerrar PRs antes de la verificación.

## Modos y límites reales

- `ROUTER_OFFLINE_ONLY=1` impide que el cliente HTTP emita inferencias/probes y consultas de inventario; suprime descubrimiento al agregar/cargar proveedores y las búsquedas RSS de contexto, y evita iniciar smokes activos al arrancar.
- Además, el bloqueo por cuenta en `router/billing.py` sigue siendo **fail-closed para todas las cuentas**: Groq, NVIDIA, Cloudflare, OpenRouter y proveedores sin aprobación. Una etiqueta de modelo gratuito no acredita que una cuenta no facture.
- La bandera offline es una protección de aplicación, **no** un firewall de sistema ni una prueba formal de ausencia de todo tráfico posible. En una futura validación aislada, aplicar también una política de salida de red denegada. No ejecutar el servidor en Oracle durante esta fase.
- El código permite inspeccionar salud, catálogo y diagnósticos offline. **El chat no puede generar respuestas reales** mientras el bloqueo de facturación permanezca vigente. No presentar esta candidata como un servicio de IA ya utilizable por G.O.
- La consulta `/health` con `status: ok` sólo acredita disponibilidad HTTP, **no** inferencia habilitada ni cuentas verificadas.

## Contrato mínimo de integración con G.O. (sin ejecutar aún)

La aplicación Gestión de Obras UTE es independiente; la integración aquí es **contractual**, no está conectada ni validada extremo a extremo.

- Autenticación: `Authorization: Bearer <token>`. Configurar `ROUTER_SERVICE_TOKEN` con secreto propio y restringir acceso. **Con `ROUTER_OFFLINE_ONLY=1`, la ausencia de credenciales válidas siempre devuelve HTTP 401**, incluso si no existen tokens de aplicación ni token de servicio; el antiguo acceso anónimo de desarrollo sólo subsiste fuera de ese modo. `GET /health` no requiere token y no debe exponerse públicamente sin control de red.
- `GET /health` → JSON `{ "status": "ok", "providers": [...] }`. No garantiza que haya modelos elegibles.
- `POST /route` → JSON de entrada, por ejemplo `{"task":"Resumir el parte diario","application_name":"gestion-obras-ute","required_capabilities":["chat"],"decompose":false}`. Respuesta HTTP 200: `ok`, `text`, `provider`, `model`, `error`, `attempts`, `decisions`, `orchestration`. En modo seguro sin cuentas autorizadas se espera `ok:false`, `error:"no_eligible_provider"`, sin intentos ni texto.
- `POST /v1/chat/completions` acepta `model`, `messages`, `stream:false` y `task_type` opcional. No admite streaming. Sin proveedor elegible responde **HTTP 502**, no una respuesta ficticia.
- `POST /diagnostics/model-ranking` y `GET /diagnostics/inventory` requieren token administrador; son diagnósticos locales, no habilitan uso real.
- `images` en `/route`: sólo data URI PNG/JPEG/WebP, hasta 3, con validaciones de tamaño y firma; no implica que exista un modelo de visión autorizado. Adjuntos PDF/audio de G.O. no están soportados por este contrato de chat y requieren procesamiento propio de G.O. o trabajo posterior autorizado.
- G.O. debe interpretar `ok:false` y HTTP 502 como **indisponibilidad segura**; no reenviar automáticamente a un proveedor externo ni generar respuestas falsas.

## Checklist de release — estado

- [x] Confirmar ancestro #45 y continuidad lineal #45–#87 en GitHub.
- [x] Confirmar CI original #87: run `40660699273`, pytest y Docker build correctos (577 tests antes de esta consolidación).
- [x] Documentar contrato HTTP y error fail-closed para G.O.
- [x] Añadir modo offline explícito y prueba de integración HTTP sin inferencia.
- [x] Validación anterior de la candidata: [CI #40676605797](https://github.com/lgomezpallo/R.outer.full/actions/runs/40676605797), SHA `68c2d7cf40257a49388728d5e3d7ee56fed0b64c`, **581 pytest aprobados, 4 advertencias y Docker build aprobado**. La revisión de aceptación agrega una prueba específica de ausencia total de tokens: **CI del nuevo head pendiente** hasta comprobar la ejecución correspondiente.
- [x] Comparación GitHub del head `68c2d7c` frente a la referencia Oracle `339898a`: **222 commits por delante, 0 detrás**, según `compare_commits` el 28/10; no equivale a inspección de la VM. Se listaron cambios de código, scripts, tests y workflows. No se inspeccionaron datos reales ni se verificó migración sobre una copia de producción.
- [ ] Revisar diff final del **head definitivo** respecto de `main` y Oracle, incluyendo datos y compatibilidad de esquema.
- [ ] Verificar en una futura ventana autorizada el SHA y el estado real de Oracle, los procesos, rutas y volúmenes. No realizado.
- [ ] Preparar y **verificar** backup íntegro de SQLite, catálogo, configuración y secretos en destino privado; no copiar claves a GitHub ni a logs.
- [ ] Aprobar y ensayar rollback de código **y** datos en un entorno aislado. Restaurar un commit anterior no revierte migraciones SQLite.
- [ ] Ejecutar smoke HTTP local en entorno aislado, con `ROUTER_OFFLINE_ONLY=1`, sin claves de proveedores y salida a Internet denegada.
- [ ] Integración real G.O.↔Router y prueba extremo a extremo: **pendiente**.
- [ ] Verificación autorizada de **cada cuenta** Groq/NVIDIA/Cloudflare/OpenRouter: plan, método de pago, sobregiros, límites duros, fecha y evidencia privada. **Bloqueante**.
- [ ] Autorización explícita adicional para fusión, despliegue o tráfico real: **pendiente**.

## Smoke tests propuestos, NO ejecutar en producción

En un entorno local aislado y sin claves de proveedores, instalar dependencias previamente aprobadas, usar archivos de datos temporales y salida de red bloqueada. La prueba HTTP sólo habla con `127.0.0.1`. Los comandos siguientes son una **guía para una ejecución futura**, no evidencia de que se hayan corrido:

```bash
export ROUTER_OFFLINE_ONLY=1
export ROUTER_SERVICE_TOKEN='TOKEN_LOCAL_DE_PRUEBA'
RELEASE_TMP_DIR="$(mktemp -d /tmp/router-release-XXXXXX)"
chmod 700 "$RELEASE_TMP_DIR"
export ROUTER_DB_FILE="$RELEASE_TMP_DIR/router.db"
export ROUTER_CATALOG_FILE="$RELEASE_TMP_DIR/router-catalog.json"
# Borrar la carpeta de pruebas únicamente después de detener el servidor local.
unset GROQ_API_KEY NVIDIA_API_KEY OPENROUTER_API_KEY CLOUDFLARE_API_TOKEN
unset ROUTER_STARTUP_PROVIDER_TESTS ROUTER_STARTUP_CAPABILITY_PROFILE
unset ROUTER_STARTUP_ROUTING_SMOKE ROUTER_STARTUP_ORCHESTRATION_SMOKE ROUTER_STARTUP_HETEROGENEOUS_SMOKE
pytest -q tests/test_go_ute_offline_release_contract.py tests/test_account_billing_fail_closed.py
# Con firewall/sandbox sin salida externa, iniciar uvicorn sólo en 127.0.0.1.
# curl -fsS http://127.0.0.1:8010/health
# curl -sS -H "Authorization: Bearer $ROUTER_SERVICE_TOKEN" -H "Content-Type: application/json" \
#   -d '{"task":"hola","decompose":false,"application_name":"gestion-obras-ute"}' \
#   http://127.0.0.1:8010/route
# Esperado: ok=false, error=no_eligible_provider, attempts=[].
```

**Nunca usar `/providers/test`, `/capabilities/probe`, `/audit`, live-smoke ni workflows Oracle para validar esta candidata.** Los jobs remotos en la rama están deshabilitados, pero eso **no** altera los workflows actuales de `main`.

## Backup y rollback — plan, no ejecución

1. Antes de cualquier cambio autorizado, registrar SHA real de `main`, SHA desplegado y estado de servicio; inventariar rutas/volúmenes efectivos sin exponer secretos.
2. Detener escrituras durante el backup o usar la API de backup consistente de SQLite (incluyendo WAL/SHM cuando corresponda). Guardar `router.db`, `router-catalog.json`, configuración systemd/env y manifiesto de versión en almacenamiento privado, con permisos restrictivos. Comprobar restauración sobre copia aislada.
3. Construir imagen inmutable etiquetada por SHA y preservar la imagen previa. No modificar servicios ni datos reales durante el ensayo.
4. Ante fallo: detener candidato, restaurar imagen/código previo y **restaurar snapshot compatible de base de datos** si hubo migración o cambios de esquema. Verificar salud y registros antes de volver a permitir tráfico.
5. Ningún rollback garantiza deshacer cargos externos: por eso la habilitación de tráfico real requiere primero verificación económica por cuenta y autorización explícita.

## Estados que no deben confundirse

- **Código consolidado en rama:** sí, sujeto al nuevo CI.
- **Pruebas offline originales y nuevas:** 581 pytest y Docker aprobados en CI #40676605797 para `68c2d7c`; la revisión posterior de aceptación requiere su propio CI antes de declararse validada.
- **Integración con G.O.:** contrato documentado y test HTTP sintético; no integración operativa.
- **Oracle en producción:** sin inspección ni cambios.
- **Prueba extremo a extremo con IA real:** no ejecutada y no autorizada.
- **Riesgo económico cero de cuentas externas:** no demostrado; tráfico real prohibido.


## Actualización 28/10 — aceptación aislada y desbloqueo gradual (sin autorización de tráfico)

**Criterio vigente:** no exigir verificar universalmente las cuatro cuentas para cerrar el código, el contrato de G.O. o la integración offline. El primer tráfico real sólo podrá autorizarse por **proveedor + cuenta + modelo concretos**, con evidencia actual del plan sin cobro, límites de cuota/sobregiro y autorización específica. Los demás seguirán bloqueados. Una aprobación de un modelo no autoriza sus hermanos ni el fallback hacia otro proveedor.

**Inventario de candidatos en código, NO inventario vivo ni certificación de gratuidad:**

| Proveedor | Modelos iniciales en `router/catalog.py` | Precio acreditado en el catálogo base | Habilitación real |
| --- | --- | --- | --- |
| Groq | `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `whisper-large-v3`, `whisper-large-v3-turbo` | Desconocido (5/5) | Bloqueada |
| NVIDIA | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`, `google/gemma-4-31b-it`, `moonshotai/kimi-k3` | Desconocido (3/3) | Bloqueada |
| OpenRouter | `openrouter/free` | Desconocido (1/1); el nombre no acredita gratuidad | Bloqueada |
| Cloudflare | Sin modelos predefinidos; se agregan desde inventario local/configuración | Desconocido | Bloqueada |

Los modelos de este cuadro son **candidatos históricos de bootstrap**, no se afirma que sigan existiendo ni que tengan precio cero. El reporte `GET /diagnostics/inventory` puede mostrar más modelos si hay un catálogo local guardado; su marca `reported_free_price` no prueba una cuenta sin sobregiros. `router/capability_evidence.py` distingue declaraciones, inferencias por nombre y registros de evidencia, sin convertirlos en autorización económica.

**Regla de selección:** preservar catálogo multimodelo, ranking por tarea y fallback, pero aplicar el filtro de costo y disponibilidad a **cada intento**. Si el modelo desaparece, su precio es ambiguo, la cuota gratuita se agota o la cuenta no está verificada, debe quedar fuera del ranking; jamás sustituirlo por uno pago o con costo incierto. El bloqueo actual de `account_billing_ineligibility_reason` sigue siendo global y explícito: **no hay todavía un mecanismo productivo de aprobación granular**, y no se lo habilitará basándose sólo en nombres, créditos de prueba o etiquetas free.

**Aceptación CI añadida:** `.github/workflows/ci.yml` ejecuta, después del build, una prueba HTTP de la **imagen real** en `docker run --network none --read-only --cap-drop ALL`, sin claves de proveedores y con `ROUTER_OFFLINE_ONLY=1`. Comprueba autenticación, `/health`, `/route` fail-closed y `/v1/chat/completions` HTTP 502. Esto **no equivale a CI completamente desconectado**: checkout, instalación de dependencias y `docker build` aún pueden requerir Internet. Ninguna de estas pruebas realiza inferencia.

**Secuencia de cierre:** (1) validar la nueva aceptación Docker en CI; (2) comparar configuración y SHA efectivos de Oracle mediante una futura inspección de solo lectura autorizada, sin copiar claves; (3) ensayar backup/restauración y rollback sobre copias aisladas; (4) integrar G.O. en modo seguro sin tráfico externo; (5) para una primera conversación real, verificar únicamente la cuenta y el modelo seleccionados, establecer controles de cuotas fail-closed y pedir autorización explícita de despliegue y prueba. Luego ampliar progresivamente el catálogo gratuito por evidencia, nunca por presunción.

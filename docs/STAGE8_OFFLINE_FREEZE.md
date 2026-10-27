# Router — etapa 8: validación offline y bloqueo de ejecución real (2026-10-26)

## Alcance y límites

Esta rama parte del PR #54 y continúa la cadena de PR draft #45–#54. No se fusiona ni despliega.
Etapas 1–7: implementación y tests **offline**; no equivalen a autorización para inferencia real.
El cierre técnico de etapa 1 **no** verifica el estado económico de las cuentas.

**Bloqueo pendiente:** verificación externa de facturación y límites de Groq, NVIDIA,
Cloudflare y OpenRouter. No afirmar riesgo económico cero ni activar tráfico hasta contar
con evidencia verificable por cuenta y autorización explícita del usuario.

**Referencia productiva congelada:** `339898a` (SHA completo en pruebas existentes).
No se inspeccionó Oracle en esta etapa; no se afirma que la VM esté actualmente en ese commit.

## Hallazgo de la revisión de workflows

En la base del PR #54 había workflows con acceso SSH a Oracle que podían ejecutarse
automáticamente: monitor programado/por push y stage/promote/live-smoke por push a `main`.
El workflow de despliegue también permitía modo manual `deploy`. Una ejecución de
`live-smoke` podría consumir inferencia real, y un `promote` podría reiniciar Router.

Esta rama **elimina los disparadores automáticos** de los workflows con SSH y coloca
`if: ${{ false }}` en sus jobs remotos, incluido el deploy manual. Las pruebas
`tests/test_stage8_workflow_freeze.py` comprueban ambas condiciones. El CI de PR
permanece limitado a `pytest` y `docker build`, sin credenciales de proveedores.

**Importante:** estos controles son código propuesto en un PR draft; NO protegen
todavía los workflows ya existentes en `main`. No se ha tocado `main`, Oracle,
los secretos, las cuentas, ni se han disparado workflows remotos. Para activar
la protección en `main` se necesita autorización separada de fusión. Mientras
tanto, no ejecutar manualmente workflows Oracle ni promover código.

## Evidencia mínima para una futura decisión (no ejecutar ahora)

1. Comprobar con acceso autorizado a cada panel de cuenta: facturación,
   método de pago, excedentes, límites de gasto y si los límites son duros.
   Registrar fecha, cuenta y evidencia sin publicar credenciales.
2. Revisar los PR encadenados, su base y el último commit, con CI aprobado.
   Validar que los tests de facturación estrictos no dependan de los mocks
   que simulan cuentas autorizadas en los tests históricos.
3. Revisar el comportamiento offline del fallback ante 429/503, el ranking
   por modelo/proveedor y la persistencia de métricas/cooldowns.
4. Sólo con autorización explícita posterior, diseñar una ventana de
   despliegue reversible, backup privado y procedimiento de rollback;
   el rollback de código no garantiza reversión de migraciones SQLite.
5. Rehabilitar workflows remotos exclusivamente mediante otro cambio
   revisado. Nunca interpretar CI verde como autorización de uso real.

## Resultado esperado del CI de esta rama

`pytest` debe ejecutar los tests existentes más los nuevos controles
estáticos de workflows; `docker build` debe construir la imagen sin ejecutar
el contenedor ni contactar APIs de inferencia. Un CI aprobado no prueba
el estado de facturación ni la configuración efectiva de Oracle.

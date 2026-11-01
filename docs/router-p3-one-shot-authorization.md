# Router P3 — contrato de autorización para un único ensayo operacional (sin ejecución)

**Estado: PROPUESTA PARA REVISIÓN INDEPENDIENTE.** Este documento no concede acceso a Oracle, no modifica políticas de GitHub, no instala código, no habilita workflows y **no autoriza** un reinicio. Relacionado: [issue #109](https://github.com/lgomezpallo/R.outer.full/issues/109), [PR #107](https://github.com/lgomezpallo/R.outer.full/pull/107).

## Decisión de mecanismo: preferir operador SSH ya autorizado

1. Un responsable de producción distinto del proceso que prepara el cambio debe **identificar un operador con autorización vigente de escritura/reinicio sobre `router.service`**. Tener secrets SSH para inspección de solo lectura no acredita ese permiso. Verificar fuera de los logs públicos qué permisos tiene efectivamente ese operador; **no ejecutar comandos privilegiados para "probar" permisos**.
2. El operador debe aprobar una única ventana de ensayo **offline**, con intervención y recuperación supervisadas. Preferir su sesión SSH administrativa preexistente, con un procedimiento firmado/revisado y privilegios limitados a: crear/quitar el drop-in `zzzzz-router-canonical-execstart.conf` de `router.service`, mantener respaldo privado, `systemctl daemon-reload` y `systemctl restart router.service`. Si se delega con `sudoers`, autorizar **solo un ejecutable root-owned, inmutable y de propósito único** que aplique esas operaciones; **no** dar `sudo bash`, `sudo python`, escritura arbitraria, ni `systemctl` genérico. El ejecutable aún **no existe**: debe prepararse, revisarse y probarse por separado antes de autorizar su instalación.
3. Si no existe operador autorizado, se requiere una **revisión independiente de política** para crear una vía temporal y específica de ejecución. No usar `oracle-deploy.yml` (`if: false`), staging, el gate histórico de `router-release-promote.yml`, ni agregar un workflow que viole el inventario congelado Stage8. La aprobación de un issue/PR o las casillas de `router-production` **no sustituyen** la autorización de Oracle.
4. No introducir credenciales nuevas en el repositorio ni usar las credenciales de lectura para escribir. No publicar rutas privadas, `argv`, `Environment`, contenido de unidades/drop-ins ni secretos.

## Aprobación verificable requerida en #109

Registrar **antes de operar**, con identidad y fecha:
- **Decisión**: APROBADO o RECHAZADO para *un único* ensayo offline de cambio y restauración efectiva de ExecStart.
- **Responsable/operador** y fundamento de su autorización previa de producción.
- **Mecanismo**: sesión administrativa preautorizada o excepción de política revisada; si es un ejecutable delegado, indicar el identificador inmutable y el alcance exacto de sus privilegios.
- **Versión fijada** del adaptador operacional y de la lógica PR #107, con SHA revisado. El archivo `scripts/router_execstart_reconcile.py` de PR #107 solo admite `--rehearse`: **no implementa** el switch/restart real. Su CI no equivale a un ensayo Oracle.
- **Ventana**, exclusividad/lock de ejecución, responsable de recuperación independiente y método de recuperación ante corte de SSH, error de script o fallo de reinicio.
- **Condición de caducidad**: un uso o expiración de la ventana, lo que ocurra primero; retirar cualquier permiso temporal después.

Sin estos campos y sin adaptador revisado: **NO-GO**.

## Contrato técnico del único ensayo (solo después de aprobación)

**Preflight sin escritura**: confirmar `router.service=active`, salud JSON `status=ok` en loopback `127.0.0.1:8010/health`, `ROUTER_OFFLINE_ONLY=1` explícito, exactamente cuatro drop-ins previos, precedencia compatible, `ExecStart` real de 12 tokens y launcher Uvicorn reconocido, checkout/release y SHA esperados, ausencia del drop-in nuevo y ningún ensayo concurrente. Abortar ante cualquier diferencia.

**Respaldo previo obligatorio**: en Oracle, inmediatamente antes del cambio y bajo un directorio privado 0700 con archivos 0600, guardar `systemctl cat`, `ExecStart` efectivo, nombres y bytes de los cuatro drop-ins, estado activo y datos mínimos para restauración. Validar que puede releerse y que existe una ruta de recuperación independiente antes de cambiar el servicio. Nunca imprimir esos bytes en CI o comentarios.

**Switch acotado**: construir el drop-in exclusivamente con el normalizador fail-closed del PR #107, conservando exactamente los argumentos y las tres asignaciones `env`; crear solo ese archivo de forma atómica sin sobrescritura, `daemon-reload` y reiniciar únicamente `router.service`. Comprobar `active`, offline explícito, `ExecStart` efectivo esperado y HTTP loopback saludable. No activar proveedores, no escribir DB ni modificar checkout/release.

**Restauración en el mismo ensayo, aun si falla el switch**: ejecutar rollback por identidad de archivo del PR #107, nunca borrar un archivo ajeno; `daemon-reload` y reiniciar solo `router.service`. Obtener lecturas **nuevas** de `systemctl cat`, los cuatro drop-ins y `ExecStart`; comparar byte a byte con el respaldo y exigir `active`, `ROUTER_OFFLINE_ONLY=1`, HTTP JSON `status=ok`. Si hay conflicto concurrente, fallo de restauración o pérdida de conexión, conservar respaldo y escalar inmediatamente al operador con acceso independiente; no declarar PASS.

**Evidencia publicable**: referencia a la aprobación, SHA del adaptador, identificador de la ejecución y etiquetas `preflight=PASS/FAIL`, `backup=PASS/FAIL`, `switch=PASS/FAIL`, `restore=PASS/FAIL`, `active=PASS/FAIL`, `offline=PASS/FAIL`, `health=PASS/FAIL`. Nunca datos internos. **P3 no se cierra sin `restore=PASS` observado en Oracle.**

## Límites

No fusionar automáticamente PR #107; no activar modo online ni modelos paid/unknown; no alterar Stage8, workflows congelados, IAchat, G.O. ni Prisma. Este PR de documentación **no es** el PR del adaptador operacional ni una aprobación para desplegarlo.

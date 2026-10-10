# Inventario operativo — Todo de Cero
Actualizado: 2026-10-10. Estado registrado durante migración; verificar tras cada cambio.

## Decisiones vigentes
- Organización lógica: `Aplicaciones/`, `Herramientas/`, `Reutilizables/`. No se crea un servicio CoreX ni un coordinador adicional.
- GitHub: fuente de código, versiones, documentación y CI. Render: despliegues existentes que se conservan como alternativa. Oracle: objetivo de ejecución principal, para evitar arranque en frío y límites de uso de Render.
- Migración sin borrar la VM anterior ni los despliegues de Render. No confundir una prueba de salud con una conversación exitosa.
- **No guardar secretos, tokens, claves privadas ni contenidos de bases de datos en Git.**

## Oracle — VM nueva
- Instancia: `instance-20261010-0843`; Ubuntu 24.04; Oracle Always Free E2.1.Micro, 1 GB RAM.
- IP pública observada: `164.152.45.71` (puede cambiar; verificar).
- Usuario: `ubuntu`.
- Carpeta raíz: `/home/ubuntu/Proyecto`.
- `Aplicaciones/IAchat`: clon de `lgomezpallo/I.achat.Full` (privado); entorno `.venv`; servicio `iachat.service`, `127.0.0.1:8000`.
- `Herramientas/Router`: clon de `lgomezpallo/R.outer.full`; entorno `.venv`; servicio `router.service`, `127.0.0.1:8010`.
- `Reutilizables/`: categoría prevista; no implica que Intérprete, WhatsApp, Auditor ni Buscador sean productos independientes ya instalados.
- Datos fuera del código: `/home/ubuntu/Proyecto/Datos/IAchat/iachat.db`, `/home/ubuntu/Proyecto/Datos/Router/router.db`, `/home/ubuntu/Proyecto/Datos/Router/catalog.json`.
- Token **interno** IAchat↔Router: archivo `/etc/iachat-router.env` (permisos 600, solo root), cargado mediante `/etc/systemd/system/{iachat,router}.service.d/credentials.conf`. Variables: `ROUTER_TOKEN` y `ROUTER_IACHAT_TOKEN`. **Nunca registrar su valor.**
- IAchat: `ROUTER_URL=http://127.0.0.1:8010` y `IACHAT_DB` definidos en systemd. Router: `ROUTER_DB_FILE` y `ROUTER_CATALOG_FILE` definidos en systemd.
- Acceso desde Oracle a GitHub privado IAchat: clave SSH de despliegue de solo lectura, privada en `~/.ssh/github_iachat`; pública registrada en GitHub Deploy keys. No publicar la clave privada.
- Confirmado: `systemctl is-active` devuelve `active` para ambos; `GET /health` devuelve 200; `GET IAchat /api/router/status` devolvió `configured:true, reachable:true, routing_verified:false, reason:health_only_not_end_to_end`.
- **Pendiente**: proveedor(es) de IA en Oracle (Router devuelve `providers:[]`), prueba real de `/route` desde IAchat, HTTPS, autenticación de usuario, pruebas de persistencia tras redespliegue/reinicio y copias de seguridad. La red pública no está habilitada para IAchat: ambos servicios escuchan en loopback.

## Render — no modificar durante la migración
- IAchat: `https://iachat-uyx3.onrender.com`, repositorio `I.achat.Full`.
- Router nuevo: `https://router-full-independiente.onrender.com`, repositorio `R.outer.full`.
- Existen servicios antiguos CoreX, Prisma y Router; no forman parte del nuevo despliegue en Oracle.
- No suponer que las credenciales configuradas en Render están automáticamente disponibles en GitHub u Oracle.

## GitHub / credenciales
- `lgomezpallo/R.outer.full`: CI en `.github/workflows/ci.yml`, pruebas y Docker build; **no contiene flujo de despliegue Oracle** a 2026-10-10.
- Se comprobó en pantalla que **Repository secrets** de `R.outer.full` estaba vacío al 2026-10-10. No afirmar que las claves de proveedores están allí.
- Valores de claves de proveedores: ubicación actual **por confirmar**. Pueden estar configurados en Render o conservarse en otro almacén anterior; no copiar valores a la documentación.
- La presencia de GitHub Secrets, cuando se configuren, no implica que se inyecten automáticamente en procesos de Oracle; requiere despliegue explícito y seguro.

## Comprobaciones sin exponer secretos
```bash
systemctl is-active router iachat
curl -sS http://127.0.0.1:8010/health
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/api/router/status
```

## Procedimiento de mantenimiento
Al modificar cualquier componente: registrar motivo, ubicación, servicio, dependencias, secreto por **nombre y ubicación solamente**, prueba efectuada y pendientes. No declarar finalizada una integración hasta probar un caso real de extremo a extremo.

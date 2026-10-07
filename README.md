# R.outer.full

Router de IA desacoplado y reutilizable.

## Objetivo
Recibir una tarea, seleccionar el proveedor/modelo más conveniente según capacidades y estado real, ejecutar con fallback y devolver un resultado uniforme.

## Regla de alta de proveedores
Para el usuario, agregar un proveedor requiere solamente:

- nombre
- API key

El Router debe resolver automáticamente el resto cuando el proveedor pueda identificarse:

- proveedor canónico
- endpoint
- protocolo
- modelos disponibles
- capacidades
- límites relevantes
- health
- latencia
- cooldown
- fallback

El nombre funciona como orientación semántica y admite variantes y errores razonables de escritura (por ejemplo, `Groc` -> Groq).

## Principios
- Ninguna aplicación cliente conoce proveedores concretos.
- Los secretos nunca se guardan en el repositorio.
- Agregar o cambiar proveedor no debe requerir modificar las aplicaciones consumidoras.
- La selección debe ser auditable.
- El Router no divide tareas ni dirige proyectos: sólo selecciona y usa recursos.


## Estado v1
La implementación incluye selección por capacidades y estado, reintentos acotados ante fallas transitorias, cooldown por proveedor, fallback entre proveedores, métricas de éxito/latencia y registro auditable de cada decisión e intento.

## Uso mínimo
```python
from router import Router, RouteRequest

router = Router()
router.add_provider("Groq", api_key="...")
respuesta = router.route(RouteRequest("Explicá este código", required_capabilities=frozenset({"code"})))
print(respuesta.text)
```

Las claves se pasan en tiempo de ejecución; nunca se guardan en el repositorio.

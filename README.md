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

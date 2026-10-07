# Contrato de proveedores v1

## Entrada mínima
```
name: string
api_key: secret
```

## Resolución del nombre
El Router intenta, en este orden:

1. Coincidencia exacta con un proveedor conocido.
2. Alias conocidos.
3. Coincidencia aproximada para errores razonables de escritura.
4. Si existe más de una interpretación plausible, devuelve estado `ambiguous_provider`.
5. Si no puede identificarlo con suficiente confianza, devuelve `unknown_provider`.

Nunca debe adivinar silenciosamente un proveedor cuando la identificación sea incierta.

## Descubrimiento automático
Una vez identificado el proveedor, el Router resuelve mediante su catálogo/adaptador:

- endpoint base
- protocolo
- autenticación
- modelos
- capacidades declaradas
- comprobación de disponibilidad
- métricas operativas

## Estado dinámico
Los datos que cambian con el uso no forman parte del alta manual. El Router mantiene:

- éxito/fallo reciente
- latencia
- cooldown
- último health-check
- errores relevantes
- disponibilidad por modelo

## Condición de aceptación
Un proveedor conocido debe poder quedar operativo a partir de `nombre + API key` sin editar código ni pedir datos técnicos adicionales al usuario.
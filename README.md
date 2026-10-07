# R.outer.full

Router de IA desacoplado y reutilizable.

## Instalación

```bash
pip install -e .
```

## Uso

```python
from router_v1.router import Router

router = Router()
router.register_provider("groq", api_key="<YOUR_GROQ_KEY>")
router.register_provider("openrouter", api_key="<YOUR_OPENROUTER_KEY>")

response = router.route(
    task="translate",
    context={"text": "Hello world"},
    requirements={"model": "llama3-8b", "temperature": 0.7}
)
print(response)
```

## Configuración de proveedores

Solo se necesita el nombre y la API key. El Router resuelve automáticamente el resto.

## Pruebas

```bash
pytest
```

"""Cliente estricto de Router para Gestión de Obras UTE."""
import os
import httpx

class RouterUnavailable(Exception):
    pass

def answer(messages: list[dict]) -> str:
    if os.getenv("GO_ROUTER_ENABLED") != "1":
        raise RouterUnavailable("router_pending_validation")
    url=os.getenv("GO_ROUTER_URL","http://127.0.0.1:8010").rstrip("/")
    token=os.getenv("GO_ROUTER_TOKEN","")
    # Solo loopback: nunca enviar el bearer a destinos externos.
    if not token or url not in ("http://127.0.0.1:8010", "http://localhost:8010"):
        raise RouterUnavailable("router_not_configured")
    try:
        response=httpx.post(url+"/route",headers={"Authorization":"Bearer "+token},json={
            "task":messages[-1]["content"],
            "conversation":messages[:-1][-12:],
            "context":"Sos G.O., asistente de gestión de obras UTE. Respondé en español argentino. Nunca afirmes guardar datos si no están confirmados en PostgreSQL. Distinguí previsto de realizado. No inventes números de presupuesto, permisos ni fechas.",
            "required_capabilities":["chat"],"application_name":"go-ute",
            "decompose":False
        },timeout=45,trust_env=False,follow_redirects=False)
        if response.status_code != 200:
            raise RouterUnavailable("router_http_"+str(response.status_code))
        result=response.json()
        if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("text"), str) or not result["text"].strip():
            raise RouterUnavailable("router_invalid_response")
        return result["text"].strip()
    except (httpx.RequestError,ValueError) as exc:
        raise RouterUnavailable("router_unavailable") from exc


def probe_contract() -> dict[str, object]:
    """Prueba local no facturable: /route recibe dos cuerpos inválidos.

    Nunca se invoca un modelo: falta el campo obligatorio `task`.
    No depende de GO_ROUTER_ENABLED, no cambia el gate y no devuelve secretos.
    """
    url = os.getenv("GO_ROUTER_URL", "http://127.0.0.1:8010").rstrip("/")
    token = os.getenv("GO_ROUTER_TOKEN", "")
    if url not in ("http://127.0.0.1:8010", "http://localhost:8010"):
        return {"status": "unsafe_target"}
    if not token:
        return {"status": "missing_token"}
    try:
        # No seguir redirecciones ni proxies: el bearer solo viaja a loopback.
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            anonymous = client.post(url + "/route", json={})
            authorized = client.post(
                url + "/route",
                json={},
                headers={"Authorization": "Bearer " + token},
            )
        return {
            "status": "verified" if (anonymous.status_code, authorized.status_code) == (401, 422) else "contract_mismatch",
            "anonymous_http": anonymous.status_code,
            "authorized_http": authorized.status_code,
        }
    except httpx.RequestError:
        return {"status": "unreachable"}

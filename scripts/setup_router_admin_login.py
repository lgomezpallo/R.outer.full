#!/usr/bin/env python3
"""One-time setup: use existing Caddy login for Router admin; no token shown to browser."""
from pathlib import Path
import os
import grp
import re
import subprocess
import sys

CONFIG = Path("/etc/caddy/Caddyfile")
ENVFILE = Path("/etc/router-providers.env")
DOMAIN = "r-outer.duckdns.org"

def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Ejecutá con sudo.")
    values = [line.partition("=")[2].strip().strip('"').strip("'")
              for line in ENVFILE.read_text().splitlines()
              if line.startswith("ROUTER_SERVICE_TOKEN=")]
    if not values or not values[-1] or not re.fullmatch(r"[a-zA-Z0-9_~-]{20,200}", values[-1]):
        raise SystemExit("No existe un token administrativo válido. No se modificó Caddy.")
    token = values[-1]
    original = CONFIG.read_text()
    m = re.search(r"(?m)^" + re.escape(DOMAIN) + r"\s*\{", original)
    if not m:
        raise SystemExit("No se encontró el bloque de Router en Caddy.")
    depth = 0
    end = -1
    for i in range(m.end() - 1, len(original)):
        if original[i] == "{":
            depth += 1
        elif original[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        raise SystemExit("Bloque de Router mal cerrado.")
    block = original[m.start():end]
    if not re.search(r"(?m)^\s*basicauth\s*\{", block):
        raise SystemExit("Falta la protección basicauth. No se modificó Caddy.")
    if not re.search(r"(?m)^\s*reverse_proxy\s+127\.0\.0\.1:8010\s*$", block) and "header_up Authorization" not in block:
        raise SystemExit("No se encontró el proxy local esperado.")
    replacement = ("    reverse_proxy 127.0.0.1:8010 {\n"
                   f'        header_up Authorization "Bearer {token}"\n'
                   "    }")
    if "header_up Authorization" in block:
        block = re.sub(
            r"(?ms)^\s*reverse_proxy\s+127\.0\.0\.1:8010\s*\{.*?^\s*\}",
            replacement,
            block,
            count=1,
        )
    else:
        block = re.sub(r"(?m)^\s*reverse_proxy\s+127\.0\.0\.1:8010\s*$", replacement, block, count=1)
    updated = original[:m.start()] + block + original[end:]
    backup = CONFIG.with_name("Caddyfile.before-router-admin")
    backup.write_text(original)
    CONFIG.write_text(updated)
    os.chown(CONFIG, 0, grp.getgrnam('caddy').gr_gid)
    CONFIG.chmod(0o640)
    try:
        subprocess.run(["caddy", "validate", "--config", str(CONFIG)], check=True)
        subprocess.run(["systemctl", "reload", "caddy"], check=True)
    except subprocess.CalledProcessError:
        CONFIG.write_text(original)
        subprocess.run(["systemctl", "reload", "caddy"], check=False)
        raise SystemExit("Falló validación o recarga. Se restauró el archivo anterior.")
    print("OK: ingreso de Router unificado con usuario y contraseña de la PWA.")
    print("No se expuso el token administrativo al navegador ni se modificó IAchat.")

if __name__ == "__main__":
    main()

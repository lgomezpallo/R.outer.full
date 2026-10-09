#!/usr/bin/env bash
# Configure all available Router provider keys without echoing them or writing shell history.
set -euo pipefail
target="/home/ubuntu/.config/router/providers.env"
mkdir -p "$(dirname "$target")"
chmod 700 "$(dirname "$target")"
umask 077
tmp="$(mktemp "$(dirname "$target")/.providers.XXXXXXXX")"
trap 'rm -f "$tmp"' EXIT
python3 - "$tmp" <<'PY'
import getpass, pathlib, sys
providers = ('GROQ', 'NVIDIA', 'OPENROUTER', 'MISTRAL', 'CEREBRAS', 'SAMBANOVA')
out = pathlib.Path(sys.argv[1])
lines = []
for name in providers:
    value = getpass.getpass(f'{name} API key (Enter para omitir): ').strip()
    if not value:
        continue
    if any(c in value for c in '\r\n\0'):
        raise SystemExit(f'Clave inválida para {name}')
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    lines.append(f'{name}_API_KEY="{escaped}"')
if not lines:
    raise SystemExit('No se ingresaron claves; configuración anterior intacta')
out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
print(f'Proveedores preparados: {", ".join(x.split("_API_KEY")[0] for x in lines)}')
PY
chmod 600 "$tmp"
mv -f "$tmp" "$target"
trap - EXIT
sudo mkdir -p /etc/systemd/system/router.service.d
printf '[Service]\nEnvironmentFile=%s\n' "$target" | sudo tee /etc/systemd/system/router.service.d/10-providers.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart router.service
sleep 2
curl -fsS --max-time 10 http://127.0.0.1:8100/health
echo

#!/usr/bin/env bash
# Read-only preflight. Run on Oracle before attempting any Router deployment.
set -euo pipefail
app="${ROUTER_APP_DIR:-/home/ubuntu/Proyecto/Herramientas/Router}"
printf 'service_state: '
systemctl is-active router
printf 'service_unit: '
systemctl show router -p FragmentPath --value
printf 'service_workdir: '
systemctl show router -p WorkingDirectory --value
printf 'service_user: '
systemctl show router -p User --value
printf 'service_exec: '
systemctl show router -p ExecStart --value
printf 'router_health_status: '
curl -fsS --max-time 5 http://127.0.0.1:8010/health | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", "unknown"))'
printf 'application_directory: '
test -d "$app" && echo present
printf 'virtualenv: '
if test -x "$app/.venv/bin/python"; then echo present; else echo missing; fi
printf 'git_checkout: '
if test -d "$app/.git"; then
  echo present
  printf 'git_commit: '
  git -C "$app" rev-parse HEAD
  printf 'git_worktree_clean: '
  if test -z "$(git -C "$app" status --porcelain)"; then echo yes; else echo no; fi
else
  echo missing
fi
printf 'router_db_path_configured: '
if test -n "${ROUTER_DB_FILE:-}"; then echo environment; else echo unknown; fi
echo 'preflight_read_only_complete'

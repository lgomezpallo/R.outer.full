#!/usr/bin/env bash
# Build and test a private integration candidate; never edit the live checkout.
set -euo pipefail
APP="${ROUTER_APP_DIR:-/home/ubuntu/Proyecto/Herramientas/Router}"
UPSTREAM_REF="${ROUTER_UPSTREAM_REF:-origin/main}"
RUN_TESTS="${ROUTER_RECONCILE_TESTS:-1}"
APP="$(git -C "$APP" rev-parse --show-toplevel)"
UPSTREAM="$(git -C "$APP" rev-parse --verify "$UPSTREAM_REF^{commit}")"
# Preserve Oracle-only committed changes as well as uncommitted tracked edits.
# A HEAD-based diff would silently omit committed changes on a diverged branch.
if ! BASE="$(git -C "$APP" merge-base HEAD "$UPSTREAM")"; then
  echo "reconcile_status=no_common_ancestor"
  exit 2
fi
LOCAL_UNIQUE_COMMITS="$(git -C "$APP" rev-list --count "$BASE"..HEAD)"
umask 077
TMP="$(mktemp -d "${TMPDIR:-/tmp}/router-reconcile.XXXXXXXX")"
cleanup() {
  git -C "$APP" worktree remove --force "$TMP/candidate" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
git -C "$APP" diff --binary "$BASE" -- > "$TMP/local.patch"
LOCAL_COUNT="$(git -C "$APP" diff --name-only "$BASE" -- | wc -l | tr -d ' ')"
UNTRACKED_COUNT="$(git -C "$APP" ls-files --others --exclude-standard | wc -l | tr -d ' ')"
echo "local_tracked_files=$LOCAL_COUNT"
echo "local_unique_commits=$LOCAL_UNIQUE_COMMITS"
echo "untracked_files_not_included=$UNTRACKED_COUNT"
echo "oracle_head=$(git -C "$APP" rev-parse --short HEAD)"
echo "upstream_head=$(git -C "$APP" rev-parse --short "$UPSTREAM")"
# Classify untracked paths without printing names or contents to public CI.
# An untracked file may be local-only source, data, or a secret.
git -C "$APP" ls-files --others --exclude-standard -z | python3 -c '
import pathlib, sys
paths = [p for p in sys.stdin.buffer.read().split(b"\0") if p]
source_suffixes = {".py", ".sh", ".js", ".ts", ".tsx", ".json", ".toml", ".yaml", ".yml", ".md", ".html", ".css", ".sql", ".ini", ".cfg", ".service"}
source = sum(pathlib.PurePosixPath(p.decode("utf-8", "surrogateescape")).suffix.lower() in source_suffixes for p in paths)
print(f"untracked_source_like_count={source}")
print(f"untracked_other_count={len(paths) - source}")
'
git -C "$APP" worktree add --detach "$TMP/candidate" "$UPSTREAM" >/dev/null 2>&1
set +e
git -C "$TMP/candidate" apply --3way --index "$TMP/local.patch" > "$TMP/apply.log" 2>&1
APPLY_RC=$?
set -e
# If only Router methods conflict, try a conservative private three-way merge.
# A clean result stays in the disposable worktree; never touches the live tree.
if test "$APPLY_RC" -ne 0; then
  CONFLICTS="$(git -C "$TMP/candidate" ls-files -u | awk '{print $4}' | sort -u)"
  if test "$CONFLICTS" = "router/router.py" &&
     git -C "$APP" cat-file -e "$UPSTREAM:scripts/oracle_method_three_way.py" 2>/dev/null; then
    git -C "$APP" show "$UPSTREAM:scripts/oracle_method_three_way.py" > "$TMP/method_merge.py"
    if python3 "$TMP/method_merge.py" "$APP" "$BASE" "$UPSTREAM" "$TMP/candidate/router/router.py" > "$TMP/method_merge.log" 2>&1; then
      git -C "$TMP/candidate" add -- router/router.py
      if test -z "$(git -C "$TMP/candidate" ls-files -u)"; then
        echo "router_method_merge=clean"
        APPLY_RC=0
      else
        echo "router_method_merge=blocked"
      fi
    else
      echo "router_method_merge=blocked"
    fi
  fi
fi
if test "$APPLY_RC" -ne 0; then
  CONFLICTS="$(git -C "$TMP/candidate" ls-files -u | awk '{print $4}' | sort -u)"
  if test -n "$CONFLICTS"; then
    echo "reconcile_status=conflicts"
    echo "conflicting_paths:"
    printf '%s\n' "$CONFLICTS"
    # Only print method names and counts, not private Oracle code.
    if printf '%s\n' "$CONFLICTS" | grep -Fxq 'router/router.py' &&
       git -C "$APP" cat-file -e "$UPSTREAM:scripts/oracle_reconcile_symbols.py" 2>/dev/null; then
      git -C "$APP" show "$UPSTREAM:scripts/oracle_reconcile_symbols.py" |
        python3 - "$APP" "$BASE" "$UPSTREAM" || echo "router_symbol_diagnostics=unavailable"
    fi
    # Diagnose method compatibility privately in a second isolated worktree.
    # Never print private source or pytest tracebacks in public Actions logs.
    if printf '%s\n' "$CONFLICTS" | grep -Fxq 'router/router.py' &&
       git -C "$APP" cat-file -e "$UPSTREAM:scripts/oracle_private_method_probe.py" 2>/dev/null; then
      git -C "$APP" show "$UPSTREAM:scripts/oracle_private_method_probe.py" > "$TMP/private_probe.py"
      python3 "$TMP/private_probe.py" "$APP" "$UPSTREAM" ||
        echo "router_private_probe=unavailable"
    fi
  else
    echo "reconcile_status=patch_cannot_apply"
    echo "Manual investigation required; private patch and apply log were not published."
  fi
  echo "production_checkout_unchanged=yes"
  # An unresolved candidate must never appear as a successful CI run.
  if test -n "$CONFLICTS"; then exit 3; fi
  exit 4
fi
# Untracked Oracle files are not copied: they still block a completed merge.
# However, test the tracked candidate to learn whether the method merge works.
if test "$UNTRACKED_COUNT" -ne 0; then
  echo "reconcile_status=untracked_files_require_review"
else
  echo "reconcile_status=candidate_applied"
fi
echo "candidate_changed_paths:"
git -C "$TMP/candidate" diff --name-only HEAD
if test "$RUN_TESTS" = 0; then
  echo "candidate_tests=skipped"
  echo "production_checkout_unchanged=yes"
  if test "$UNTRACKED_COUNT" -ne 0; then exit 5; fi
  exit 0
fi
PYTHON="${ROUTER_PYTHON:-$APP/.venv/bin/python}"
if ! test -x "$PYTHON"; then PYTHON="$(command -v python3)"; fi
set +e
(cd "$TMP/candidate" && PYTHONPATH="$TMP/candidate" PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m pytest -q > "$TMP/pytest.log" 2>&1)
TEST_RC=$?
set -e
if test "$TEST_RC" -eq 0; then
  echo "candidate_tests=passed"
else
  echo "candidate_tests=failed"
  # Only report test identifiers, not assertion values, secrets or patch contents.
  grep -E '^(FAILED|ERROR) [A-Za-z0-9_./:\[\]-]+' "$TMP/pytest.log" | sed -E 's/ - .*//' | head -20 || true
fi
echo "production_checkout_unchanged=yes"
# Even passing tracked-code tests cannot authorize deployment while local
# untracked files remain unreviewed.
if test "$UNTRACKED_COUNT" -ne 0; then exit 5; fi
exit "$TEST_RC"

"""Static offline guard for the complete GitHub Actions workflow inventory.

No API calls, credentials, deployment, or provider inference are performed.
This protects only the proposed branch; default-branch workflows stay unchanged
until a separately authorized merge.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Explicit allowlist: new workflows and new jobs require a reviewed policy change.
EXPECTED_JOBS: dict[str, frozenset[str]] = {
    "ci.yml": frozenset({"test"}),
    "oracle-candidate-reconcile.yml": frozenset({"preserve-and-reconcile"}),
    "oracle-ssh-readonly-access.yml": frozenset({"ssh-readonly"}),
    "check-provider-secrets.yml": frozenset({"check"}),
    "oracle-deploy.yml": frozenset({"tests", "deploy"}),
    "router-oracle-monitor.yml": frozenset({"tests", "oracle-check"}),
    "router-release-stage.yml": frozenset({"tests", "oracle-stage"}),
    "router-release-promote.yml": frozenset({"tests", "oracle-promote"}),
    "router-release-live-smoke.yml": frozenset({"tests", "oracle-live-smoke"}),
}
REMOTE_JOBS = {
    "oracle-deploy.yml": "deploy",
    "router-oracle-monitor.yml": "oracle-check",
    "router-release-stage.yml": "oracle-stage",
    "router-release-promote.yml": "oracle-promote",
    "router-release-live-smoke.yml": "oracle-live-smoke",
}
# Pin the complete reviewed workflow contents, not only their names/jobs.
# Any workflow edit must update this map in an explicitly reviewed PR.
EXPECTED_WORKFLOW_BLOBS: dict[str, str] = {
    "ci.yml": "96688b4fa9ed2626fd4a8e00572d5bd5bed23470",
    "oracle-candidate-reconcile.yml": "e24d2e9be6c2a19bb13c88cdab640a573891076c",
    "oracle-ssh-readonly-access.yml": "66a452b307123f6cfcb425366ea1fc939cbf9abc",
    "check-provider-secrets.yml": "30fb296f7d0d87d180d5a1a87b69822382113eb5",
    "oracle-deploy.yml": "5dc10c0fbba1198a14e2ee65321c10d79ecb3545",
    "router-oracle-monitor.yml": "c7fc8ddb2501c33c772488d97bf6dbe216dbbb68",
    "router-release-stage.yml": "36a898fbf82110862a53580901f9e2b8730dafa2",
    "router-release-promote.yml": "163ea9b4aa7508d817e170e54cea187ba23274af",
    "router-release-live-smoke.yml": "e95e4775c95b397d69a00d0290dc56f838178648",
}


EXPECTED_BRIDGE_BLOB = "7bf232c844ec4faa7cd5608f07bed85850d68caf"


def _git_blob_sha(source: str) -> str:
    raw = source.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + bytes([0]) + raw).hexdigest()


TOP_LEVEL = re.compile(r"^[A-Za-z][\w-]*:\s*(?:#.*)?$", re.M)
INDENTED_KEY = re.compile(r"^  ([A-Za-z][\w-]*):\s*(?:#.*)?$", re.M)
REMOTE_ACTION = re.compile(r"\bssh\s+-|\bscp\s+|\brsync\s+|ORACLE_SSH_|router_release_live_smoke", re.I)


def _section(source: str, key: str) -> str | None:
    """Return a top-level YAML mapping's raw body, or None on ambiguity."""
    matches = [m for m in TOP_LEVEL.finditer(source) if m.group().split(":", 1)[0] == key]
    if len(matches) != 1:
        return None
    start = matches[0].end()
    next_section = TOP_LEVEL.search(source, start)
    return source[start:next_section.start() if next_section else len(source)]


def _mapping_sections(source: str) -> dict[str, str] | None:
    matches = list(INDENTED_KEY.finditer(source))
    names = [m.group(1) for m in matches]
    if len(names) != len(set(names)):
        return None
    return {
        m.group(1): source[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(source)]
        for i, m in enumerate(matches)
    }


def audit_workflows(sources: Mapping[str, str]) -> list[str]:
    problems: list[str] = []
    expected = set(EXPECTED_JOBS)
    actual = set(sources)
    for filename in sorted(expected - actual):
        problems.append(f"{filename}: required workflow missing")
    for filename in sorted(actual - expected):
        problems.append(f"{filename}: unreviewed workflow file")

    if "oracle-candidate-reconcile.yml" in actual:
        bridge = Path(__file__).resolve().parents[1] / "scripts" / "oracle_snapshot_bridge.py"
        if not bridge.is_file() or _git_blob_sha(bridge.read_text(encoding="utf-8")) != EXPECTED_BRIDGE_BLOB:
            problems.append("oracle_snapshot_bridge.py: unreviewed remote script content change")

    for filename in sorted(actual & expected):
        source = sources[filename]
        if _git_blob_sha(source) != EXPECTED_WORKFLOW_BLOBS[filename]:
            problems.append(f"{filename}: unreviewed workflow content change")
        triggers = _section(source, "on")
        jobs = _section(source, "jobs")
        if triggers is None or jobs is None:
            problems.append(f"{filename}: missing/duplicate on or jobs mapping")
            continue
        trigger_map = _mapping_sections(triggers)
        job_map = _mapping_sections(jobs)
        if trigger_map is None or job_map is None:
            problems.append(f"{filename}: duplicate trigger or job key")
            continue

        if filename == "ci.yml":
            expected_triggers = {"push", "pull_request"}
        elif filename in {"oracle-candidate-reconcile.yml", "oracle-ssh-readonly-access.yml"}:
            expected_triggers = {"push", "workflow_dispatch"}
        else:
            expected_triggers = {"workflow_dispatch"}
        if set(trigger_map) != expected_triggers:
            problems.append(f"{filename}: unauthorized triggers {sorted(trigger_map)}")
        if set(job_map) != EXPECTED_JOBS[filename]:
            problems.append(f"{filename}: unexpected jobs {sorted(job_map)}")

        if filename == "ci.yml":
            if re.search(r"\$\{\{\s*secrets\.", source, re.I) or REMOTE_ACTION.search(source):
                problems.append("ci.yml: secrets or remote action in automatic CI")
        for job, body in job_map.items():
            if filename in REMOTE_JOBS and job == REMOTE_JOBS[filename]:
                conditions = re.findall(r"^    if:\s*(.*?)\s*$", body, re.M)
                if conditions != ["${{ false }}"]:
                    problems.append(f"{filename}/{job}: remote job is not unconditionally disabled")
            elif (filename == "oracle-candidate-reconcile.yml" and job == "preserve-and-reconcile") or (filename == "oracle-ssh-readonly-access.yml" and job == "ssh-readonly"):
                # Exception: exact reviewed workflow and bridge SHA only. SSH reads
                # Oracle; encrypted snapshot and reconciliation run on a CI runner.
                pass
            elif REMOTE_ACTION.search(body) or re.search(r"\$\{\{\s*secrets\.ORACLE_", body, re.I):
                problems.append(f"{filename}/{job}: remote action outside frozen job")
    return problems


def read_workflows(root: Path = WORKFLOWS) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((*root.glob("*.yml"), *root.glob("*.yaml")))
    }


def main() -> int:
    errors = audit_workflows(read_workflows())
    if errors:
        for error in errors:
            print(f"STAGE8_POLICY_FAIL {error}")
        return 1
    print("STAGE8_POLICY_OK: 9 reviewed workflows; two pinned read-only Oracle jobs; 5 production remote jobs disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

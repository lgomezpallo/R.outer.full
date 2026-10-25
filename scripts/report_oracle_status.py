#!/usr/bin/env python3
"""Publish a sanitized Oracle reconciliation status on the current GitHub commit.

The status contains only a fixed outcome and a link to the Actions run.
Never sends Oracle file names, source, SSH details, diagnostics, or secrets.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request


def build_payload(outcome: str, repository: str, run_id: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    if not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("invalid run id")
    states = {
        "success": ("success", "Oracle reconciliation candidate passed; review untracked files"),
        "failure": ("failure", "Oracle reconciliation blocked; inspect private workflow diagnostics"),
        "skipped": ("error", "Oracle reconciliation skipped"),
        "cancelled": ("error", "Oracle reconciliation cancelled"),
    }
    state, description = states.get(outcome, ("error", "Oracle reconciliation unavailable"))
    return {
        "state": state,
        "context": "router/oracle-reconcile",
        "description": description,
        "target_url": f"https://github.com/{repository}/actions/runs/{run_id}",
    }


def publish() -> None:
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    token = os.environ["GITHUB_TOKEN"]
    run_id = os.environ["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
        raise ValueError("invalid commit SHA")
    payload = build_payload(os.environ.get("RECONCILE_OUTCOME", "unknown"), repository, run_id)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/statuses/{sha}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 201:
            raise RuntimeError("GitHub did not accept commit status")
    print("oracle_commit_status=published")


if __name__ == "__main__":
    publish()

"""Mutation-style offline regression tests for Stage 8 workflow isolation."""
from scripts.check_stage8_offline_workflows import audit_workflows, read_workflows


def _baseline():
    return read_workflows()


def test_complete_current_workflow_inventory_is_frozen():
    assert audit_workflows(_baseline()) == []


def test_new_workflow_cannot_sneak_in_unreviewed():
    sources = _baseline()
    sources["new-live-probe.yml"] = "on:\n  push:\njobs:\n  probe:\n    runs-on: ubuntu-latest\n"
    assert any("unreviewed workflow" in p for p in audit_workflows(sources))


def test_missing_workflow_fails_policy():
    sources = _baseline()
    del sources["router-release-promote.yml"]
    assert any("required workflow missing" in p for p in audit_workflows(sources))


def test_new_job_in_existing_workflow_fails_policy():
    sources = _baseline()
    sources["ci.yml"] += "\n  unsafe:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo unsafe\n"
    assert any("unexpected jobs" in p for p in audit_workflows(sources))


def test_automatic_trigger_in_manual_workflow_fails_policy():
    sources = _baseline()
    sources["router-release-live-smoke.yml"] = sources["router-release-live-smoke.yml"].replace(
        "  workflow_dispatch:\n", "  workflow_dispatch:\n  schedule:\n    - cron: '0 * * * *'\n", 1
    )
    assert any("unauthorized triggers" in p for p in audit_workflows(sources))


def test_reenabled_or_duplicate_remote_condition_fails_policy():
    sources = _baseline()
    source = sources["oracle-deploy.yml"]
    sources["oracle-deploy.yml"] = source.replace("    if: ${{ false }}", "    if: ${{ true }}", 1)
    assert any("not unconditionally disabled" in p for p in audit_workflows(sources))
    sources["oracle-deploy.yml"] = source.replace(
        "    if: ${{ false }}", "    if: ${{ false }}\n    if: ${{ true }}", 1
    )
    assert any("not unconditionally disabled" in p for p in audit_workflows(sources))


def test_ci_cannot_gain_oracle_secrets_or_ssh():
    sources = _baseline()
    sources["ci.yml"] += "\n# ${{ secrets.ORACLE_SSH_PRIVATE_KEY }}\n"
    assert any("secrets or remote action" in p for p in audit_workflows(sources))
    sources = _baseline()
    sources["ci.yml"] += "\n# ssh -i /tmp/key host\n"
    assert any("secrets or remote action" in p for p in audit_workflows(sources))


def test_remote_action_cannot_move_into_unfrozen_tests_job():
    sources = _baseline()
    sources["router-release-stage.yml"] = sources["router-release-stage.yml"].replace(
        "      - run: python3 -m py_compile scripts/router_release_stage.py", "      - run: python3 -m py_compile scripts/router_release_stage.py\n      - run: ssh -i /tmp/key host", 1
    )
    assert any("remote action outside frozen job" in p for p in audit_workflows(sources))


def test_automatic_ci_cannot_add_arbitrary_http_probe():
    sources = _baseline()
    sources["ci.yml"] += "      - run: curl -X POST https://api.groq.com/openai/v1/chat/completions\n"
    assert any("unreviewed workflow content change" in p for p in audit_workflows(sources))


def test_approved_job_cannot_change_existing_command():
    sources = _baseline()
    sources["ci.yml"] = sources["ci.yml"].replace("- run: pytest", "- run: python -c 'print(1)'")
    assert any("unreviewed workflow content change" in p for p in audit_workflows(sources))


def test_manual_tests_job_cannot_gain_hidden_provider_call():
    sources = _baseline()
    sources["router-release-stage.yml"] = sources["router-release-stage.yml"].replace(
        "      - run: python3 -m py_compile scripts/router_release_stage.py",
        "      - run: python3 -m py_compile scripts/router_release_stage.py\n      - run: curl -X POST https://api.nvidia.com/v1/chat/completions",
        1,
    )
    assert any("unreviewed workflow content change" in p for p in audit_workflows(sources))

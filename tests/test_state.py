from time import time
from router.state import RuntimeState

def test_repeated_failures_escalate_cooldown():
    state = RuntimeState()
    state.mark_failure("m", "quota", 60, failure_kind="quota")
    first_remaining = state.cooldown_until - time()
    assert state.cooldown_strikes == 1
    assert first_remaining > 50

    state.mark_failure("m", "quota again", 60, failure_kind="quota")
    second_remaining = state.cooldown_until - time()
    assert state.cooldown_strikes == 2
    assert second_remaining > 110

def test_success_clears_conservation_penalty():
    state = RuntimeState()
    state.mark_failure("m", "timeout", 60, failure_kind="timeout")
    state.mark_success("m", 100)
    assert state.cooldown_strikes == 0
    assert state.last_failure_kind is None
    assert state.available

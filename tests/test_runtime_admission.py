from __future__ import annotations

from threading import Event, Thread

import pytest

from app.runtime.guardrails.admission import InProcessDialogueAdmission
from app.runtime.guardrails.errors import RuntimeBusyError
from app.runtime.guardrails.schemas import DialogueAdmissionRequest


def _admission(*, session_wait: float = 0.2, dialogue_limit: int = 2):
    return InProcessDialogueAdmission(
        session_wait_timeout_seconds=session_wait,
        dialogue_limit=dialogue_limit,
        dialogue_wait_timeout_seconds=0.05,
    )


def test_same_user_and_session_are_serialized_and_registry_is_cleaned():
    admission = _admission()
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    request = DialogueAdmissionRequest(user_id="u1", session_id="s1")

    def first():
        with admission.acquire(request=request):
            first_entered.set()
            release_first.wait(timeout=2)

    def second():
        with admission.acquire(request=request):
            second_entered.set()

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    assert first_entered.wait(timeout=1)
    second_thread.start()
    assert not second_entered.wait(timeout=0.03)
    release_first.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)
    assert second_entered.is_set()
    assert admission.entry_count == 0


def test_same_session_text_for_different_users_does_not_share_lock():
    admission = _admission()
    with admission.acquire(
        request=DialogueAdmissionRequest(user_id="u1", session_id="same")
    ):
        with admission.acquire(
            request=DialogueAdmissionRequest(user_id="u2", session_id="same")
        ):
            assert admission.entry_count == 2
    assert admission.entry_count == 0


def test_session_wait_timeout_is_busy_and_does_not_leak_entry():
    admission = _admission(session_wait=0.01)
    request = DialogueAdmissionRequest(user_id="u1", session_id="s1")
    with admission.acquire(request=request):
        with pytest.raises(RuntimeBusyError) as raised:
            with admission.acquire(request=request):
                pass
        assert raised.value.reason == "session_lock_wait_timeout"
        assert admission.entry_count == 1
    assert admission.entry_count == 0


def test_explicit_handle_can_close_cross_thread_repeatedly() -> None:
    admission = _admission(dialogue_limit=1)
    handle = admission.open_handle(
        request=DialogueAdmissionRequest(user_id="u1", session_id="s1")
    )
    assert admission.entry_count == 1

    threads = [Thread(target=handle.close) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert admission.entry_count == 0
    handle.close()
    with admission.acquire(
        request=DialogueAdmissionRequest(user_id="u1", session_id="s1")
    ):
        pass


def test_explicit_handle_holds_session_and_dialogue_until_close() -> None:
    admission = _admission(session_wait=0.01, dialogue_limit=1)
    handle = admission.open_handle(
        request=DialogueAdmissionRequest(user_id="u1", session_id="s1")
    )
    with pytest.raises(RuntimeBusyError) as session_busy:
        admission.open_handle(
            request=DialogueAdmissionRequest(user_id="u1", session_id="s1")
        )
    assert session_busy.value.reason == "session_lock_wait_timeout"
    with pytest.raises(RuntimeBusyError) as dialogue_busy:
        admission.open_handle(
            request=DialogueAdmissionRequest(user_id="u2", session_id="s2")
        )
    assert dialogue_busy.value.reason == "dialogue_concurrency"
    handle.close()
    assert admission.entry_count == 0

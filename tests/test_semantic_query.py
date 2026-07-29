from __future__ import annotations

from dataclasses import replace
from threading import Barrier, Event, Lock, RLock, Thread

import numpy as np
import pytest

from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import (
    EmbeddingAuthError,
    EmbeddingInvalidInputError,
    EmbeddingInvalidResponseError,
    EmbeddingRateLimitError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingBatchResult, EmbeddingRequest
from app.embeddings.synchronized import SynchronizedEmbeddingProvider
from app.errors import ErrorCode
from app.runtime.guardrails.concurrency import ConcurrencyGate, LimitedEmbeddingProvider
from app.semantic.budget import load_semantic_query_budget
from app.semantic.coordinator import SemanticQueryCoordinator
from app.semantic.errors import (
    SemanticCoordinatorUnhealthyError,
    SemanticProviderSignatureMismatchError,
    SemanticQueryConfigurationError,
)
from app.semantic.query_builder import NeutralTurnQueryBuilderV01
from app.semantic.schemas import SemanticMessage, SemanticTurnInput


def _approved_hash_provider() -> DeterministicHashEmbeddingProvider:
    profile = load_embedding_space_profile("deterministic_hash_v1")
    return DeterministicHashEmbeddingProvider(
        dimension=profile.dimension,
        profile=profile,
    )


def _turn(
    current_input: str = "请回忆我们讨论的项目",
    *,
    recent_messages: tuple[SemanticMessage, ...] = (),
    current_message_id: str = "msg_current",
    turn_id: str = "turn_1",
) -> SemanticTurnInput:
    return SemanticTurnInput(
        turn_id=turn_id,
        user_id="user_1",
        session_id="session_1",
        character_id="aemeath",
        current_user_message_id=current_message_id,
        current_input=current_input,
        recent_messages=recent_messages,
    )


class _CountingProvider:
    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.delegate = _approved_hash_provider()
        self.signature = self.delegate.signature
        self.requests: list[EmbeddingRequest] = []
        self.outcomes = list(outcomes or [])

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        self.requests.append(request)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            assert isinstance(outcome, EmbeddingBatchResult)
            return outcome
        return self.delegate.embed(request)


@pytest.mark.parametrize(
    "field",
    (
        "max_dependency_chars_per_message",
        "max_dependency_utf8_bytes_per_message",
        "max_query_chars",
        "max_query_utf8_bytes",
    ),
)
def test_budget_v1_rejects_same_version_threshold_changes(field: str) -> None:
    canonical = load_semantic_query_budget()

    with pytest.raises(SemanticQueryConfigurationError) as raised:
        replace(canonical, **{field: getattr(canonical, field) + 1})

    assert raised.value.code == ErrorCode.CONFIG_INVALID
    assert raised.value.reason == "semantic_query_budget_definition_mismatch"


def test_budget_loader_rejects_unknown_version() -> None:
    with pytest.raises(SemanticQueryConfigurationError):
        load_semantic_query_budget("semantic-query-budget/unknown")


def test_builder_excludes_current_id_and_uses_latest_complete_pair() -> None:
    builder = NeutralTurnQueryBuilderV01()
    recent = (
        SemanticMessage(id="older", role="user", content="更早的话题"),
        SemanticMessage(id="previous_user", role="user", content="项目使用共享查询"),
        SemanticMessage(id="previous_assistant", role="assistant", content="已经确认边界"),
        SemanticMessage(id="msg_current", role="user", content="继续"),
    )

    built = builder.build(
        turn_input=_turn("继续", recent_messages=recent),
        budget=load_semantic_query_budget(),
    )

    assert built.status == "ready"
    assert built.query_strategy == "last_turn_pair_and_current"
    assert built.dependency_message_ids == ("previous_user", "previous_assistant")
    assert built.canonical_query == (
        "[recent_user]\n项目使用共享查询\n"
        "[recent_assistant]\n已经确认边界\n"
        "[current_user]\n继续"
    )


def test_builder_does_not_deduplicate_equal_message_bodies() -> None:
    recent = (
        SemanticMessage(id="previous_user", role="user", content="继续"),
        SemanticMessage(id="previous_assistant", role="assistant", content="继续"),
    )

    built = NeutralTurnQueryBuilderV01().build(
        turn_input=_turn("继续", recent_messages=recent),
        budget=load_semantic_query_budget(),
    )

    assert built.dependency_message_ids == ("previous_user", "previous_assistant")
    assert built.canonical_query is not None
    assert built.canonical_query.count("继续") == 3


def test_builder_truncates_dependency_and_drops_oldest_for_total_budget() -> None:
    builder = NeutralTurnQueryBuilderV01()
    budget = load_semantic_query_budget()
    truncated = builder.build(
        turn_input=_turn(
            "继续",
            recent_messages=(
                SemanticMessage(id="user", role="user", content="甲" * 1600),
                SemanticMessage(id="assistant", role="assistant", content="乙" * 1600),
            ),
        ),
        budget=budget,
    )

    assert "dependency_truncated_to_semantic_budget" in truncated.budget_reason_codes
    assert truncated.canonical_query is not None
    assert "[semantic_context_truncated]" in truncated.canonical_query
    assert truncated.query_char_count <= budget.max_query_chars
    assert truncated.query_utf8_bytes <= budget.max_query_utf8_bytes

    dropped = builder.build(
        turn_input=_turn(
            "继续" + "今" * 2598,
            recent_messages=(
                SemanticMessage(id="user", role="user", content="甲" * 1024),
                SemanticMessage(id="assistant", role="assistant", content="乙" * 1024),
            ),
        ),
        budget=budget,
    )

    assert dropped.query_strategy == "current_only"
    assert dropped.dependency_message_ids == ()
    assert "dependency_dropped_for_total_semantic_budget" in dropped.budget_reason_codes


@pytest.mark.parametrize("current", ("a" * 4097, "😀" * 2001))
def test_overlong_current_input_skips_provider(current: str) -> None:
    provider = _CountingProvider()
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )

    context = coordinator.prepare_turn_context(turn_input=_turn(current))

    assert context.status == "skipped"
    assert context.provider_called is False
    assert context.resolution_for("memory_recall").reason_code == (
        "current_input_exceeds_semantic_budget"
    )
    assert provider.requests == []


def test_four_roles_share_one_query_result_and_read_only_vector() -> None:
    provider = _CountingProvider()
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=(
            "character_examples",
            "canonical_voice",
            "memory_recall",
            "canonical_lore",
        ),
    )

    context = coordinator.prepare_turn_context(turn_input=_turn())

    assert context.status == "ready"
    assert len(provider.requests) == 1
    assert provider.requests[0].purpose == "query"
    assert len(provider.requests[0].texts) == 1
    embeddings = [resolution.resolved_embedding for resolution in context.resolutions]
    assert all(embedding is embeddings[0] for embedding in embeddings)
    assert embeddings[0] is not None
    assert embeddings[0].vector.flags.writeable is False
    with pytest.raises(ValueError):
        embeddings[0].vector[0] = 0.0


@pytest.mark.parametrize(
    "error",
    (
        EmbeddingTimeoutError(retry_count=1),
        EmbeddingRateLimitError(retry_count=1),
        EmbeddingUnavailableError(),
    ),
)
def test_transient_provider_errors_return_failed_context(error: BaseException) -> None:
    provider = _CountingProvider(outcomes=[error])
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )

    context = coordinator.prepare_turn_context(turn_input=_turn())

    assert context.status == "failed"
    assert context.provider_called is True
    resolution = context.resolution_for("memory_recall")
    assert resolution.reason_code == "embedding_transient_failure"
    assert resolution.error_code in {
        "EMBEDDING_TIMEOUT",
        "EMBEDDING_RATE_LIMITED",
        "EMBEDDING_UNAVAILABLE",
    }


def test_permanent_error_marks_unhealthy_without_retaining_exception() -> None:
    original = EmbeddingAuthError()
    provider = _CountingProvider(outcomes=[original])
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )

    with pytest.raises(EmbeddingAuthError) as first:
        coordinator.prepare_turn_context(turn_input=_turn())
    with pytest.raises(SemanticCoordinatorUnhealthyError) as second:
        coordinator.prepare_turn_context(turn_input=_turn(turn_id="turn_2"))

    assert first.value is original
    assert second.value.code == ErrorCode.SEMANTIC_COORDINATOR_UNHEALTHY
    assert second.value.retryable is False
    assert second.value.metadata == {
        "reason_code": "semantic_coordinator_unhealthy",
        "cause_error_code": "EMBEDDING_AUTH_FAILED",
    }
    assert len(provider.requests) == 1
    unhealthy_state = coordinator._unhealthy_state
    assert unhealthy_state is not None
    assert vars(unhealthy_state) == {
        "reason_code": "semantic_coordinator_unhealthy",
        "cause_error_code": ErrorCode.EMBEDDING_AUTH_FAILED,
    }
    assert all(not isinstance(value, BaseException) for value in vars(unhealthy_state).values())


def test_unhealthy_coordinator_fails_fast_even_when_next_input_would_skip() -> None:
    provider = _CountingProvider(outcomes=[EmbeddingAuthError()])
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )

    with pytest.raises(EmbeddingAuthError):
        coordinator.prepare_turn_context(turn_input=_turn())
    with pytest.raises(SemanticCoordinatorUnhealthyError):
        coordinator.prepare_turn_context(turn_input=_turn("", turn_id="turn_2"))

    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "bad_result",
    (
        lambda provider: EmbeddingBatchResult(
            vectors=np.zeros((0, provider.signature.dimension), dtype=np.float32),
            signature=provider.signature,
        ),
        lambda provider: EmbeddingBatchResult(
            vectors=np.full(
                (1, provider.signature.dimension),
                np.nan,
                dtype=np.float32,
            ),
            signature=provider.signature,
        ),
        lambda provider: EmbeddingBatchResult(
            vectors=np.full(
                (1, provider.signature.dimension),
                0.5,
                dtype=np.float32,
            ),
            signature=provider.signature,
        ),
    ),
)
def test_invalid_provider_result_marks_coordinator_unhealthy(bad_result) -> None:
    provider = _CountingProvider()
    provider.outcomes.append(bad_result(provider))
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )

    with pytest.raises(EmbeddingInvalidResponseError):
        coordinator.prepare_turn_context(turn_input=_turn())
    with pytest.raises(SemanticCoordinatorUnhealthyError) as raised:
        coordinator.prepare_turn_context(turn_input=_turn(turn_id="turn_2"))

    assert raised.value.cause_error_code == ErrorCode.EMBEDDING_INVALID_RESPONSE


def test_signature_mismatch_marks_coordinator_unhealthy() -> None:
    provider = _CountingProvider()
    mismatched_signature = replace(provider.signature, profile_id="other_legal_space")
    provider.outcomes.append(
        EmbeddingBatchResult(
            vectors=np.asarray(
                [[1.0] + [0.0] * (provider.signature.dimension - 1)],
                dtype=np.float32,
            ),
            signature=mismatched_signature,
        )
    )
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )

    with pytest.raises(SemanticProviderSignatureMismatchError) as first:
        coordinator.prepare_turn_context(turn_input=_turn())
    with pytest.raises(SemanticCoordinatorUnhealthyError) as second:
        coordinator.prepare_turn_context(turn_input=_turn(turn_id="turn_2"))

    assert getattr(first.value, "code", None) == ErrorCode.EMBEDDING_SIGNATURE_MISMATCH
    assert second.value.cause_error_code == ErrorCode.EMBEDDING_SIGNATURE_MISMATCH


def test_invalid_input_propagates_without_marking_unhealthy() -> None:
    original = EmbeddingInvalidInputError("query_token_limit_exceeded")
    provider = _CountingProvider(outcomes=[original])
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )

    with pytest.raises(EmbeddingInvalidInputError) as raised:
        coordinator.prepare_turn_context(turn_input=_turn())
    context = coordinator.prepare_turn_context(turn_input=_turn(turn_id="turn_2"))

    assert raised.value is original
    assert context.status == "ready"
    assert len(provider.requests) == 2


def test_coordinator_duration_includes_state_lock_wait() -> None:
    lock_attempted = Event()

    class _SignalingLock:
        def __init__(self) -> None:
            self.inner = RLock()

        def __enter__(self):
            lock_attempted.set()
            self.inner.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            self.inner.release()

        def acquire(self, *, timeout: float) -> bool:
            lock_attempted.set()
            return self.inner.acquire(timeout=timeout)

        def release(self) -> None:
            self.inner.release()

    ticks = iter((10.0, 15.0))
    provider = _CountingProvider()
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
        clock=lambda: next(ticks),
    )
    contexts = []
    signaling_lock = _SignalingLock()
    coordinator._state_lock = signaling_lock
    signaling_lock.inner.acquire()

    worker = Thread(
        target=lambda: contexts.append(
            coordinator.prepare_turn_context(turn_input=_turn())
        )
    )
    worker.start()
    assert lock_attempted.wait(timeout=2.0)
    signaling_lock.inner.release()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert contexts[0].duration_ms == 5000.0


def test_state_lock_capacity_timeout_returns_failed_context_without_provider_call() -> None:
    class _UnavailableLock:
        def acquire(self, *, timeout: float) -> bool:
            assert timeout == 0.01
            return False

        def release(self) -> None:
            raise AssertionError("an unavailable lock must not be released")

    provider = _CountingProvider()
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
        state_wait_timeout_seconds=0.01,
    )
    coordinator._state_lock = _UnavailableLock()

    context = coordinator.prepare_turn_context(turn_input=_turn())

    resolution = context.resolutions[0]
    assert context.status == "failed"
    assert context.provider_called is False
    assert context.unique_query_count == 0
    assert resolution.reason_code == "embedding_local_capacity"
    assert resolution.error_code == ErrorCode.RUNTIME_BUSY.value
    assert provider.requests == []


def test_embedding_gate_capacity_timeout_returns_failed_context_without_delegate_call() -> None:
    provider = _CountingProvider()
    gate = ConcurrencyGate(
        limit=1,
        wait_timeout_seconds=0,
        reason="embedding_concurrency",
    )
    coordinator = SemanticQueryCoordinator(
        provider=LimitedEmbeddingProvider(gate=gate, delegate=provider),
        active_roles=("memory_recall",),
    )

    with gate.acquire():
        context = coordinator.prepare_turn_context(turn_input=_turn())

    resolution = context.resolutions[0]
    assert context.status == "failed"
    assert context.provider_called is False
    assert context.unique_query_count == 0
    assert resolution.reason_code == "embedding_local_capacity"
    assert resolution.error_code == ErrorCode.RUNTIME_BUSY.value
    assert provider.requests == []


def test_two_concurrent_turns_do_not_cross_query_vectors() -> None:
    delegate = _CountingProvider()
    coordinator = SemanticQueryCoordinator(
        provider=SynchronizedEmbeddingProvider(delegate),
        active_roles=("memory_recall",),
    )
    caller_gate = Barrier(3)
    result_lock = Lock()
    contexts = {}

    def run_turn(turn_id: str, query: str) -> None:
        caller_gate.wait(timeout=2.0)
        context = coordinator.prepare_turn_context(
            turn_input=_turn(query, turn_id=turn_id)
        )
        with result_lock:
            contexts[turn_id] = context

    first = Thread(target=run_turn, args=("turn_alpha", "alpha semantic query"))
    second = Thread(target=run_turn, args=("turn_beta", "beta semantic query"))
    first.start()
    second.start()
    caller_gate.wait(timeout=2.0)
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(delegate.requests) == 2
    reference = _approved_hash_provider()
    for turn_id, query in (
        ("turn_alpha", "alpha semantic query"),
        ("turn_beta", "beta semantic query"),
    ):
        resolved = contexts[turn_id].resolved_for("memory_recall")
        assert resolved is not None
        expected = reference.embed(
            EmbeddingRequest(texts=(query,), purpose="query")
        ).vectors[0]
        np.testing.assert_array_equal(resolved.vector, expected)


def test_safe_summary_contains_no_query_or_vector() -> None:
    provider = _CountingProvider()
    context = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    ).prepare_turn_context(turn_input=_turn("SENSITIVE_QUERY_BODY"))

    serialized = repr(context.safe_summary())

    assert "SENSITIVE_QUERY_BODY" not in serialized
    assert "vector" not in serialized.lower()
    assert context.query_strategy == "current_only"
    assert context.dependency_message_count == 0

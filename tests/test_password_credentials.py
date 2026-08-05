from __future__ import annotations

import re

import pytest

from app.auth.csrf import csrf_token_matches, derive_csrf_token
from app.auth.errors import PasswordPolicyError, PasswordWorkBusyError
from app.auth.passwords import (
    APPROVED_PASSWORD_PROFILE,
    PasswordEngine,
    legacy_username_is_browser_eligible,
    validate_browser_username,
    validate_password,
)
from app.auth.rate_limit import InProcessPreAuthRateLimiter
from app.auth.schemas import WebAuthSessionRecord
from app.auth.tokens import new_token, token_digest, validate_token_shape


def test_username_contract_is_lowercase_exact_and_legacy_safe():
    assert validate_browser_username("alice_01") == "alice_01"
    for invalid in ("Alice", " alice", "al", "alice@example", "爱丽丝"):
        with pytest.raises(ValueError):
            validate_browser_username(invalid)
    assert legacy_username_is_browser_eligible("legacy-user")
    assert not legacy_username_is_browser_eligible("LegacyUser")


def test_password_policy_preserves_exact_input_and_boundaries():
    assert validate_password("  exact pass  ") == "  exact pass  "
    assert validate_password("x" * 12) == "x" * 12
    assert validate_password("x" * 128) == "x" * 128
    for invalid in ("x" * 11, "x" * 129, "valid-length\n"):
        with pytest.raises(PasswordPolicyError):
            validate_password(invalid)


def test_argon2id_profile_hash_verify_rehash_and_gate():
    engine = PasswordEngine(wait_timeout_seconds=0)
    encoded = engine.hash_password("correct horse battery staple")

    assert encoded.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert engine.verify_password(encoded, "correct horse battery staple")
    assert not engine.verify_password(encoded, "wrong password material")
    assert engine.check_needs_rehash(encoded) is False
    assert APPROVED_PASSWORD_PROFILE.parallelism == 1

    assert engine._gate.acquire(blocking=False)
    try:
        with pytest.raises(PasswordWorkBusyError):
            engine.hash_password("another valid password")
    finally:
        engine._gate.release()


@pytest.mark.parametrize(
    "kind,prefix",
    [("invitation", "inv_"), ("password_reset", "rst_"), ("web_session", "wsa_")],
)
def test_tokens_are_256_bit_domain_separated_and_strict(kind: str, prefix: str):
    token = new_token(kind)  # type: ignore[arg-type]
    assert token.startswith(prefix)
    assert re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{43}", token)
    assert validate_token_shape(token, kind=kind) == token  # type: ignore[arg-type]
    digest = token_digest(token, kind=kind)  # type: ignore[arg-type]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)

    for other in {"invitation", "password_reset", "web_session"} - {kind}:
        with pytest.raises(ValueError):
            token_digest(token, kind=other)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        token_digest(token + "x", kind=kind)  # type: ignore[arg-type]


def test_csrf_is_stable_session_bound_and_strictly_compared():
    key = b"k" * 32
    verifier = b"v" * 32
    first = derive_csrf_token(key=key, web_session_id="wsa_a", verifier=verifier)
    again = derive_csrf_token(key=key, web_session_id="wsa_a", verifier=verifier)
    other = derive_csrf_token(key=key, web_session_id="wsa_b", verifier=verifier)

    assert first == again
    assert first != other
    assert csrf_token_matches(expected=first, provided=first)
    assert not csrf_token_matches(expected=first, provided=None)
    assert not csrf_token_matches(expected=first, provided=other)


def test_web_session_internal_id_is_repr_hidden():
    record = WebAuthSessionRecord(
        id="wsa_unique_internal_marker",
        account_id="account_1",
        authenticated_credential_version=1,
        created_at="t",
        updated_at="t",
        last_seen_at="t",
        idle_expires_at="t",
        absolute_expires_at="t",
        revoked_at=None,
        revoke_reason=None,
    )
    assert "wsa_unique_internal_marker" not in repr(record)


def test_pre_auth_limiter_uses_longest_wait_and_hard_capacity():
    now = 0.0
    limiter = InProcessPreAuthRateLimiter(
        rate_per_minute=30,
        burst=1,
        global_rate_per_minute=60,
        global_burst=2,
        max_buckets=3,
        clock=lambda: now,
        pseudonym_key=b"p" * 32,
    )
    assert limiter.check(endpoint="login", ip_value="192.0.2.1").allowed
    rejected = limiter.check(endpoint="login", ip_value="192.0.2.1")
    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 2
    assert rejected.reason == "ip"

    assert limiter.check(endpoint="login", ip_value="192.0.2.2").allowed
    capacity = limiter.check(endpoint="login", ip_value="192.0.2.3")
    assert capacity.allowed is False
    assert capacity.reason == "capacity"


def test_pre_auth_identity_bucket_is_independent_from_early_request_bucket():
    limiter = InProcessPreAuthRateLimiter(
        rate_per_minute=1,
        burst=1,
        global_rate_per_minute=100,
        global_burst=10,
        pseudonym_key=b"i" * 32,
    )

    assert limiter.check_request(endpoint="login", ip_value="192.0.2.1").allowed
    assert limiter.check_identity(identity_value="alice").allowed
    assert limiter.check_request(endpoint="login", ip_value="192.0.2.2").allowed
    assert not limiter.check_identity(identity_value="alice").allowed
    assert limiter.check_identity(identity_value="bob").allowed

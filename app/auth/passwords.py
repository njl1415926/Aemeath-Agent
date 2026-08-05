from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore
from typing import Iterator

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.auth.errors import PasswordPolicyError, PasswordWorkBusyError

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")


@dataclass(frozen=True)
class PasswordProfile:
    profile_id: str = "argon2id_owasp_2026_v1"
    memory_cost: int = 19_456
    time_cost: int = 2
    parallelism: int = 1
    hash_len: int = 32
    salt_len: int = 16


APPROVED_PASSWORD_PROFILE = PasswordProfile()


def validate_browser_username(username: str) -> str:
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise ValueError("browser_username_invalid")
    return username


def legacy_username_is_browser_eligible(username: str) -> bool:
    return isinstance(username, str) and USERNAME_RE.fullmatch(username) is not None


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise PasswordPolicyError(reason="type_invalid")
    if not 12 <= len(password) <= 128:
        raise PasswordPolicyError(reason="codepoint_length")
    try:
        encoded = password.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PasswordPolicyError(reason="encoding_invalid") from exc
    if len(encoded) > 1024:
        raise PasswordPolicyError(reason="utf8_length")
    if any(not character.isprintable() for character in password):
        raise PasswordPolicyError(reason="character_invalid")
    return password


class PasswordEngine:
    def __init__(
        self,
        *,
        profile: PasswordProfile = APPROVED_PASSWORD_PROFILE,
        concurrency: int = 1,
        wait_timeout_seconds: float = 1.0,
    ):
        if concurrency != 1:
            raise ValueError("password_work_concurrency_must_be_one")
        if wait_timeout_seconds < 0:
            raise ValueError("password_work_wait_timeout_invalid")
        self.profile = profile
        self.wait_timeout_seconds = wait_timeout_seconds
        self._gate = BoundedSemaphore(concurrency)
        self._hasher = PasswordHasher(
            time_cost=profile.time_cost,
            memory_cost=profile.memory_cost,
            parallelism=profile.parallelism,
            hash_len=profile.hash_len,
            salt_len=profile.salt_len,
            type=Type.ID,
        )
        self._dummy_hash = self._hasher.hash("aemeath-dummy-password-material-v1")

    @contextmanager
    def _work_slot(self) -> Iterator[None]:
        acquired = self._gate.acquire(timeout=self.wait_timeout_seconds)
        if not acquired:
            raise PasswordWorkBusyError()
        try:
            yield
        finally:
            self._gate.release()

    def hash_password(self, password: str) -> str:
        value = validate_password(password)
        with self._work_slot():
            return self._hasher.hash(value)

    def verify_password(self, encoded_hash: str, password: str) -> bool:
        with self._work_slot():
            try:
                return bool(self._hasher.verify(encoded_hash, password))
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                return False

    def verify_dummy(self, password: str) -> None:
        with self._work_slot():
            try:
                self._hasher.verify(self._dummy_hash, password)
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                return None

    def check_needs_rehash(self, encoded_hash: str) -> bool:
        try:
            return bool(self._hasher.check_needs_rehash(encoded_hash))
        except InvalidHashError:
            return False

from __future__ import annotations

import json

from app.errors import AemeathError, ErrorCode
from app.runtime.guardrails.schemas import ChatInputLimitDecision, ChatInputLimitRequest


class ChatInputLimitPolicyV01:
    def __init__(
        self,
        *,
        message_max_chars: int,
        message_max_utf8_bytes: int,
        metadata_max_utf8_bytes: int,
    ):
        if min(message_max_chars, message_max_utf8_bytes, metadata_max_utf8_bytes) <= 0:
            raise ValueError("chat input limits must be positive")
        self.message_max_chars = message_max_chars
        self.message_max_utf8_bytes = message_max_utf8_bytes
        self.metadata_max_utf8_bytes = metadata_max_utf8_bytes

    def validate(self, *, request: ChatInputLimitRequest) -> ChatInputLimitDecision:
        chars = len(request.message)
        if chars > self.message_max_chars:
            self._reject(
                "chat_message_char_limit_exceeded",
                measured_count=chars,
                configured_limit=self.message_max_chars,
                unit="characters",
            )
        try:
            message_bytes = len(request.message.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise self._error(
                "chat_message_utf8_invalid",
                measured_count=None,
                configured_limit=self.message_max_utf8_bytes,
                unit="utf8_bytes",
            ) from exc
        if message_bytes > self.message_max_utf8_bytes:
            self._reject(
                "chat_message_utf8_limit_exceeded",
                measured_count=message_bytes,
                configured_limit=self.message_max_utf8_bytes,
                unit="utf8_bytes",
            )
        try:
            metadata_json = json.dumps(
                request.metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            metadata_bytes = len(metadata_json.encode("utf-8"))
        except (TypeError, ValueError, OverflowError, UnicodeEncodeError) as exc:
            raise self._error(
                "chat_metadata_not_json_serializable",
                measured_count=None,
                configured_limit=self.metadata_max_utf8_bytes,
                unit="utf8_bytes",
            ) from exc
        if metadata_bytes > self.metadata_max_utf8_bytes:
            self._reject(
                "chat_metadata_utf8_limit_exceeded",
                measured_count=metadata_bytes,
                configured_limit=self.metadata_max_utf8_bytes,
                unit="utf8_bytes",
            )
        return ChatInputLimitDecision(
            allowed=True,
            reason_code="input_within_limits",
            message_char_count=chars,
            message_utf8_bytes=message_bytes,
            metadata_utf8_bytes=metadata_bytes,
        )

    @classmethod
    def _reject(
        cls,
        reason: str,
        *,
        measured_count: int | None,
        configured_limit: int,
        unit: str,
    ) -> None:
        raise cls._error(
            reason,
            measured_count=measured_count,
            configured_limit=configured_limit,
            unit=unit,
        )

    @staticmethod
    def _error(
        reason: str,
        *,
        measured_count: int | None,
        configured_limit: int,
        unit: str,
    ) -> AemeathError:
        metadata: dict[str, object] = {
            "limit_reason": reason,
            "configured_limit": configured_limit,
            "unit": unit,
        }
        if measured_count is not None:
            metadata["measured_count"] = measured_count
        return AemeathError(
            code=ErrorCode.INVALID_INPUT,
            public_message="Invalid input.",
            internal_message=reason,
            metadata=metadata,
        )

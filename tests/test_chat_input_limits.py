from __future__ import annotations

import pytest

from app.errors import AemeathError, ErrorCode
from app.runtime.guardrails.input_limits import ChatInputLimitPolicyV01
from app.runtime.guardrails.schemas import ChatInputLimitRequest
from app.services.agent_service import AgentService
from app.services.schemas import ChatRequest, UserContext


def _policy() -> ChatInputLimitPolicyV01:
    return ChatInputLimitPolicyV01(
        message_max_chars=4,
        message_max_utf8_bytes=8,
        metadata_max_utf8_bytes=12,
    )


def test_message_character_and_utf8_boundaries_are_independent():
    assert _policy().validate(
        request=ChatInputLimitRequest(message="abcd", metadata={})
    ).allowed
    with pytest.raises(AemeathError) as chars:
        _policy().validate(
            request=ChatInputLimitRequest(message="abcde", metadata={})
        )
    assert chars.value.metadata["limit_reason"] == "chat_message_char_limit_exceeded"
    with pytest.raises(AemeathError) as encoded:
        _policy().validate(
            request=ChatInputLimitRequest(message="你你你", metadata={})
        )
    assert encoded.value.metadata["limit_reason"] == "chat_message_utf8_limit_exceeded"


def test_metadata_must_be_deterministically_serializable_and_bounded():
    with pytest.raises(AemeathError) as invalid:
        _policy().validate(
            request=ChatInputLimitRequest(message="ok", metadata={"x": object()})
        )
    assert invalid.value.code == ErrorCode.INVALID_INPUT
    assert invalid.value.metadata["limit_reason"] == "chat_metadata_not_json_serializable"
    with pytest.raises(AemeathError) as oversized:
        _policy().validate(
            request=ChatInputLimitRequest(message="ok", metadata={"x": "123456789"})
        )
    assert oversized.value.metadata["limit_reason"] == "chat_metadata_utf8_limit_exceeded"


def test_agent_service_rejects_before_controller_is_called():
    class Controller:
        called = False

        def handle_user_message(self, _turn_input):
            self.called = True
            raise AssertionError("controller must not be called")

    controller = Controller()
    service = AgentService(controller=controller, input_limit_policy=_policy())  # type: ignore[arg-type]
    with pytest.raises(AemeathError) as raised:
        service.handle_chat(
            user_context=UserContext(user_id="user"),
            request=ChatRequest(
                session_id="session",
                character_id="aemeath",
                message="abcde",
            ),
        )
    assert raised.value.metadata["limit_reason"] == "chat_message_char_limit_exceeded"
    assert controller.called is False

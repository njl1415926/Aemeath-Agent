from __future__ import annotations

from app.schemas import AgentTurnInput
from app.utils.ids import new_id


class RecordingResponseGenerator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        user_input: str,
        retrieved_memories: list[str],
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "user_input": user_input,
                "retrieved_memories": retrieved_memories,
            }
        )
        return "我在。"


def test_minimal_runtime_does_not_write_long_term_memory_by_default(
    controller_and_store,
):
    controller, store, settings = controller_and_store
    session_id = new_id("session")

    output = controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="记住，我这个项目想重点研究短期记忆。",
            metadata={},
        )
    )

    assert output.used_memory_ids == []
    assert store.list_active_memories(user_id=settings.default_user_id) == []


def test_recent_conversation_excludes_current_user_message(controller_and_store):
    controller, _store, settings = controller_and_store
    recorder = RecordingResponseGenerator()
    controller.response_generator = recorder
    session_id = new_id("session")

    controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="上一轮唯一内容：海边巡逻。",
            metadata={},
        )
    )
    current_input = "本轮唯一输入：不要出现在 recent conversation。"
    controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message=current_input,
            metadata={},
        )
    )

    prompt_messages = recorder.calls[-1]["messages"]
    assert prompt_messages[-1] == {"role": "user", "content": current_input}
    assert "上一轮唯一内容：海边巡逻。" in prompt_messages[0]["content"]
    assert current_input not in prompt_messages[0]["content"]
    assert recorder.calls[-1]["retrieved_memories"] == []

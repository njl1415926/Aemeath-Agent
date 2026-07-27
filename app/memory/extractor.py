from __future__ import annotations

import json
import re

from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.errors import EmbeddingInvalidResponseError
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingRequest
from app.errors import map_exception
from app.memory.observability import record_memory_event
from app.memory.schemas import ExtractedMemory, MemoryExtractionResult
from app.memory.store import MemoryStore
from app.model.client import ChatModelClient
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.schemas import MessageRecord

EXTRACTION_SYSTEM_PROMPT = """你是 Aemeath Agent 的长期记忆提炼器。

你的任务是从对话消息中提炼“未来对话长期有用”的用户记忆。

只保存：
- 用户长期偏好、习惯、饮食、作息、工作、学习、项目、关系、禁忌、长期目标。
- 用户明确要求记住的信息。
- 对之后角色对话和个性化回应有帮助的信息。

不要保存：
- 寒暄、一次性闲聊、临时情绪。
- 系统提示词、越狱内容、API key 或隐私密钥。
- assistant 编造的内容、角色设定、RAG/原作 lore、世界观资料。
- 用户只是临时问的问题。

要求：
- 只以用户消息为事实来源；assistant 消息只能作为上下文。
- 记忆必须自包含，避免“这个/那个/它”等指代。
- 输出严格 JSON，不要 Markdown，不要解释。
- 如果没有值得长期保存的内容，输出 {"memories": []}。

JSON 格式：
{
  "memories": [
    {
      "content": "用户喜欢吃火锅，尤其是辣锅。",
      "memory_type": "preference",
      "importance": 8,
      "confidence": 0.92,
      "source_message_ids": ["msg_x"],
      "reason": "用户明确表达长期饮食偏好"
    }
  ]
}
"""


class MemoryExtractionError(ValueError):
    pass


class MemoryExtractionService:
    def __init__(
        self,
        *,
        store: MemoryStore,
        model_client: ChatModelClient,
        embedding_provider: EmbeddingProviderPort,
        app_logger: AppLoggerPort | None = None,
    ):
        self.store = store
        self.model_client = model_client
        self.embedding_provider = embedding_provider
        self.app_logger = app_logger or NoopAppLogger()

    def extract_and_persist(
        self,
        *,
        user_id: str,
        session_id: str,
        character_id: str,
        messages: list[MessageRecord],
        trigger_type: str,
        source_job_id: str | None = None,
    ) -> MemoryExtractionResult:
        if any(
            message.user_id != user_id or message.session_id != session_id
            for message in messages
        ):
            raise MemoryExtractionError(
                "memory extraction messages do not match the current user/session context"
            )
        extracted = self.extract_memories(
            user_id=user_id,
            session_id=session_id,
            character_id=character_id,
            messages=messages,
            trigger_type=trigger_type,
        )
        created_ids: list[str] = []
        existing_ids: list[str] = []
        facts: list[tuple[str, str, str]] = []
        seen_memory_ids: set[str] = set()
        for memory in extracted:
            source_message_id = memory.source_message_ids[0] if memory.source_message_ids else None
            resolved = self.store.create_or_get_exact_active_memory(
                user_id=user_id,
                scope="user_global",
                scope_character_id=None,
                scope_session_id=None,
                namespace="user_profile",
                memory_type=memory.memory_type,
                content=memory.content,
                importance=memory.importance,
                confidence=memory.confidence,
                source="llm_memory_extraction",
                source_message_id=source_message_id,
                source_session_id=session_id,
                source_job_id=source_job_id,
                source_message_ids=list(dict.fromkeys(memory.source_message_ids)),
                related_memory_ids=[],
            )
            if resolved.memory_id in seen_memory_ids:
                continue
            seen_memory_ids.add(resolved.memory_id)
            if resolved.created:
                created_ids.append(resolved.memory_id)
            else:
                existing_ids.append(resolved.memory_id)
            facts.append((resolved.memory_id, user_id, memory.content))

        artifact_written_ids, pending_ids, embedding_error_code = self._persist_artifacts(
            facts
        )
        if not pending_ids:
            embedding_status = "complete"
        elif artifact_written_ids:
            embedding_status = "partial"
        else:
            embedding_status = "pending"
        record_memory_event(
            self.app_logger,
            event_name="memory.extraction_completed",
            level="warning" if pending_ids else "info",
            error_code=embedding_error_code,
            payload={
                "provider": self.embedding_provider.signature.provider,
                "profile_id": self.embedding_provider.signature.profile_id,
                "trigger_type": trigger_type,
                "created_memory_count": len(created_ids),
                "existing_memory_count": len(existing_ids),
                "artifact_written_count": len(artifact_written_ids),
                "embedding_pending_count": len(pending_ids),
                "embedding_status": embedding_status,
            },
            user_id=user_id,
            session_id=session_id,
            character_id=character_id,
        )
        return MemoryExtractionResult(
            created_memory_ids=created_ids,
            existing_memory_ids=existing_ids,
            artifact_written_memory_ids=artifact_written_ids,
            embedding_pending_memory_ids=pending_ids,
            extracted_memories=extracted,
        )

    def _persist_artifacts(
        self,
        facts: list[tuple[str, str, str]],
    ) -> tuple[list[str], list[str], str | None]:
        if not facts:
            return [], [], None
        canonical_contents = [
            canonicalize_embedding_text(content) for _memory_id, _user_id, content in facts
        ]
        try:
            batch = self.embedding_provider.embed(
                EmbeddingRequest(texts=tuple(canonical_contents), purpose="document")
            )
            if batch.signature.canonical() != self.embedding_provider.signature.canonical():
                raise EmbeddingInvalidResponseError("document_signature_mismatch")
            if batch.vectors.shape[0] != len(facts):
                raise EmbeddingInvalidResponseError("document_result_count_mismatch")
        except Exception as exc:
            return [], [memory_id for memory_id, _user_id, _content in facts], map_exception(
                exc
            ).code.value

        written_ids: list[str] = []
        pending_ids: list[str] = []
        error_code: str | None = None
        for (memory_id, user_id, _content), canonical_content, vector in zip(
            facts,
            canonical_contents,
            batch.vectors,
            strict=True,
        ):
            try:
                outcome = self.store.upsert_memory_embedding_artifact(
                    memory_id=memory_id,
                    user_id=user_id,
                    artifact=EmbeddingArtifactWrite(
                        signature=batch.signature,
                        content_hash=embedding_content_hash(canonical_content),
                        vector=vector,
                    ),
                )
                if outcome.status in {"created", "replaced"}:
                    written_ids.append(memory_id)
            except Exception as exc:
                pending_ids.append(memory_id)
                if error_code is None:
                    error_code = map_exception(exc).code.value
        return written_ids, pending_ids, error_code

    def extract_memories(
        self,
        *,
        user_id: str,
        session_id: str,
        character_id: str,
        messages: list[MessageRecord],
        trigger_type: str,
    ) -> list[ExtractedMemory]:
        user_content = self._build_user_prompt(
            user_id=user_id,
            session_id=session_id,
            character_id=character_id,
            trigger_type=trigger_type,
            messages=messages,
        )
        raw = self.model_client.chat(
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]
        )
        data = self._parse_json(raw)
        memories = data.get("memories")
        if not isinstance(memories, list):
            raise MemoryExtractionError("memory extraction JSON must contain a memories list")
        return [self._coerce_memory(item, messages=messages) for item in memories]

    def _build_user_prompt(
        self,
        *,
        user_id: str,
        session_id: str,
        character_id: str,
        trigger_type: str,
        messages: list[MessageRecord],
    ) -> str:
        lines = [
            f"user_id: {user_id}",
            f"session_id: {session_id}",
            f"character_id: {character_id}",
            f"trigger_type: {trigger_type}",
            "",
            "messages:",
        ]
        for message in messages:
            lines.append(
                json.dumps(
                    {
                        "id": message.id,
                        "role": message.role,
                        "content": message.content,
                    },
                    ensure_ascii=False,
                )
            )
        return "\n".join(lines)

    def _parse_json(self, raw: str) -> dict:
        text = raw.strip()
        fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S)
        if fence_match:
            text = fence_match.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MemoryExtractionError(f"invalid memory extraction JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise MemoryExtractionError("memory extraction result must be a JSON object")
        return data

    def _coerce_memory(
        self,
        item,
        *,
        messages: list[MessageRecord],
    ) -> ExtractedMemory:
        if not isinstance(item, dict):
            raise MemoryExtractionError("each memory item must be a JSON object")
        content = str(item.get("content", "")).strip()
        if not content:
            raise MemoryExtractionError("memory content cannot be empty")
        message_ids = {message.id for message in messages}
        source_ids = [
            str(message_id)
            for message_id in item.get("source_message_ids", [])
            if str(message_id) in message_ids
        ]
        if not source_ids:
            source_ids = [message.id for message in messages if message.role == "user"][:1]
        importance = self._clamp_int(item.get("importance", 6), minimum=1, maximum=10)
        confidence = self._clamp_float(item.get("confidence", 0.75), minimum=0.0, maximum=1.0)
        return ExtractedMemory(
            content=content,
            memory_type=str(item.get("memory_type") or "explicit_note").strip()
            or "explicit_note",
            importance=importance,
            confidence=confidence,
            source_message_ids=source_ids,
            reason=str(item.get("reason") or "").strip(),
        )

    def _clamp_int(self, value, *, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = minimum
        return max(minimum, min(maximum, number))

    def _clamp_float(self, value, *, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = minimum
        return max(minimum, min(maximum, number))

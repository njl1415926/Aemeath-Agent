from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.retrieval.ports import RetrieverPort
from app.retrieval.schemas import EvidencePack, RetrievalResult
from app.retrieval.world_info import WorldInfoKeywordRetriever
from app.semantic.schemas import TurnSemanticContext

WORLD_INFO_BASE_LORE_UID = 86
WORLD_INFO_BASE_LORE_TRIGGER_UIDS = frozenset(
    {
        *range(1, 19),
        20,
        22,
        *range(37, 44),
        45,
        *range(56, 85),
        *range(97, 113),
        *range(114, 117),
        *range(119, 124),
        *range(125, 128),
        *range(130, 139),
        140,
        141,
        143,
        144,
        *range(152, 156),
    }
)


@dataclass(frozen=True)
class EvidenceRetrievalPlan:
    lore_candidate_limit: int = 0
    lore_final_limit: int = 0
    voice_candidate_limit: int = 0
    voice_final_limit: int = 0
    remove_voice_overlap_from_lore: bool = False
    remove_lore_overlap_from_voice: bool = False
    intent: str = "none"
    lore_score: int = 0
    voice_score: int = 0
    no_retrieval_score: int = 0
    policy_reason: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceNeedDecision:
    intent: str
    lore_score: int
    voice_score: int
    no_retrieval_score: int
    policy_reason: tuple[str, ...]


class EvidenceNeedPolicy:
    def decide(
        self,
        *,
        query: str,
        character_id: str,
        metadata: dict[str, Any],
    ) -> EvidenceNeedDecision:
        forced_intent = metadata.get("retrieval_intent")
        if forced_intent == "none":
            return self._forced("forced_none")
        if forced_intent == "lore":
            return self._forced("forced_lore")
        if forced_intent == "voice":
            return self._forced("forced_voice")
        if forced_intent == "lore_and_voice":
            return self._forced("forced_lore_and_voice")

        lore_score = 0
        voice_score = 0
        no_retrieval_score = 0
        reasons: list[str] = []
        text = query.strip()
        compact_text = _compact_query(text)

        if not compact_text:
            no_retrieval_score += 3
            reasons.append("no_retrieval:empty")
        elif compact_text in DAILY_NO_RETRIEVAL_TEXTS or (
            len(compact_text) <= 2 and not _contains_any(text, NAMED_ENTITIES)
        ):
            no_retrieval_score += 3
            reasons.append("no_retrieval:daily_short")
        elif compact_text.startswith("早") and len(compact_text) <= 3:
            no_retrieval_score += 3
            reasons.append("no_retrieval:greeting")

        if compact_text == "我好累":
            no_retrieval_score += 3
            reasons.append("no_retrieval:tired_statement")

        lore_score += self._score_matches(text, LORE_STRONG_PATTERNS, 3, reasons, "lore_strong")
        lore_score += self._score_matches(text, LORE_QUESTION_PATTERNS, 2, reasons, "lore_question")
        lore_score += self._score_matches(text, LORE_TOPIC_PATTERNS, 2, reasons, "lore_topic")
        lore_score += self._score_matches(text, NAMED_ENTITIES, 1, reasons, "entity")
        lore_score += self._score_matches(text, LORE_RELATION_PATTERNS, 1, reasons, "lore_relation")

        voice_score += self._score_matches(text, VOICE_STRONG_PATTERNS, 3, reasons, "voice_strong")
        voice_score += self._score_matches(
            text,
            VOICE_COMFORT_PATTERNS,
            3,
            reasons,
            "voice_comfort",
        )
        voice_score += self._score_matches(
            text,
            VOICE_COMPANION_PATTERNS,
            2,
            reasons,
            "voice_companion",
        )
        voice_score += self._score_matches(text, VOICE_LINE_PATTERNS, 2, reasons, "voice_line")
        voice_score += self._score_matches(
            text,
            VOICE_EMOTION_PATTERNS,
            1,
            reasons,
            "voice_emotion",
        )

        if "爱弥斯" in text and _contains_any(text, VOICE_INTIMATE_PATTERNS):
            voice_score += 1
            reasons.append("voice_combo:aemeath_intimate")

        if no_retrieval_score >= 3 and lore_score < 3 and voice_score < 3:
            intent = "none"
        elif lore_score >= 3 and voice_score >= 3:
            intent = "query_lore_and_voice"
        elif lore_score >= 3:
            intent = "query_lore"
        elif voice_score >= 3:
            intent = "query_voice"
        else:
            intent = "none"

        if character_id:
            reasons.append(f"character:{character_id}")
        return EvidenceNeedDecision(
            intent=intent,
            lore_score=lore_score,
            voice_score=voice_score,
            no_retrieval_score=no_retrieval_score,
            policy_reason=tuple(reasons),
        )

    def _forced(self, intent: str) -> EvidenceNeedDecision:
        return EvidenceNeedDecision(
            intent=intent,
            lore_score=0,
            voice_score=0,
            no_retrieval_score=0,
            policy_reason=(f"override:{intent}",),
        )

    def _score_matches(
        self,
        query: str,
        patterns: set[str],
        score: int,
        reasons: list[str],
        reason_prefix: str,
    ) -> int:
        total = 0
        for pattern in patterns:
            if pattern in query:
                total += score
                reasons.append(f"{reason_prefix}:{pattern}")
        return total


class EmptyEvidenceProvider:
    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        metadata: dict[str, Any] | None = None,
        response_mode: str | None = None,
        semantic_context: TurnSemanticContext | None = None,
    ) -> EvidencePack:
        return EvidencePack()


class CompositeEvidenceProvider:
    def __init__(
        self,
        *,
        lore_retriever: RetrieverPort | None = None,
        voice_retriever: RetrieverPort | None = None,
    ):
        self.lore_retriever = lore_retriever
        self.voice_retriever = voice_retriever

    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        metadata: dict[str, Any] | None = None,
        response_mode: str | None = None,
        semantic_context: TurnSemanticContext | None = None,
    ) -> EvidencePack:
        lore_results = []
        voice_results = []
        metadata = {
            "character_id": character_id,
            **(metadata or {}),
        }
        if response_mode:
            metadata["legacy_response_mode"] = response_mode
        if self.lore_retriever:
            lore_results = self.lore_retriever.retrieve(
                query=query,
                user_id=user_id,
                namespaces=["lore", "plot"],
                limit=6,
                metadata=metadata,
            )
        if self.voice_retriever:
            voice_results = self.voice_retriever.retrieve(
                query=query,
                user_id=user_id,
                namespaces=["voice", "style_examples"],
                limit=4,
                metadata=metadata,
            )
        return EvidencePack(lore_results=lore_results, voice_results=voice_results)


class WorldInfoEvidenceProvider:
    def __init__(
        self,
        *,
        retriever: RetrieverPort | None = None,
        keyword_limit: int = 5,
        include_constants_by_default: bool = False,
        constant_limit: int | None = None,
    ):
        self.retriever = retriever or WorldInfoKeywordRetriever()
        self.keyword_limit = keyword_limit
        self.include_constants_by_default = include_constants_by_default
        self.constant_limit = constant_limit

    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        metadata: dict[str, Any] | None = None,
        response_mode: str | None = None,
        semantic_context: TurnSemanticContext | None = None,
    ) -> EvidencePack:
        retrieval_metadata = {
            "character_id": character_id,
            "world_info_evidence_provider": "world_info_keyword",
            "include_world_info_constants": self.include_constants_by_default,
            "constant_limit": self.constant_limit,
            **(metadata or {}),
        }
        if response_mode:
            retrieval_metadata["legacy_response_mode"] = response_mode

        results = self.retriever.retrieve(
            query=query,
            user_id=user_id,
            namespaces=["world:constant", "world:keyword"],
            limit=self.keyword_limit,
            metadata=retrieval_metadata,
        )
        results = self._append_policy_constants(
            results=results,
            query=query,
            user_id=user_id,
            metadata=retrieval_metadata,
        )
        return EvidencePack(lore_results=results)

    def _append_policy_constants(
        self,
        *,
        results: list[RetrievalResult],
        query: str,
        user_id: str,
        metadata: dict[str, Any],
    ) -> list[RetrievalResult]:
        matched_keyword_uids = {
            int(item.metadata["uid"])
            for item in results
            if item.namespace == "world:keyword" and "uid" in item.metadata
        }
        policy_constant_uids: list[int] = []
        if matched_keyword_uids & WORLD_INFO_BASE_LORE_TRIGGER_UIDS:
            policy_constant_uids.append(WORLD_INFO_BASE_LORE_UID)

        existing_uids = {
            int(item.metadata["uid"])
            for item in results
            if "uid" in item.metadata
        }
        missing_uids = [uid for uid in policy_constant_uids if uid not in existing_uids]
        if not missing_uids:
            return results

        policy_constants = self.retriever.retrieve(
            query=query,
            user_id=user_id,
            namespaces=["world:constant"],
            limit=0,
            metadata={
                **metadata,
                "include_world_info_constants": True,
                "constant_limit": None,
                "world_info_policy_constant_uids": missing_uids,
            },
        )
        selected_constants = [
            item
            for item in policy_constants
            if int(item.metadata.get("uid", -1)) in set(missing_uids)
        ]
        selected_constants.sort(
            key=lambda item: missing_uids.index(int(item.metadata.get("uid", -1)))
        )
        return [*selected_constants, *results]


DAILY_NO_RETRIEVAL_TEXTS = {
    "早",
    "早呀",
    "早啊",
    "嗯",
    "嗯嗯",
    "好",
    "好的",
    "谢谢",
    "晚安",
    "来了",
    "今天有点困",
    "我去吃饭了",
}

LORE_STRONG_PATTERNS = {
    "原作里",
    "剧情里",
    "文本依据",
    "鸣潮里",
    "设定是什么",
    "世界观",
}

LORE_QUESTION_PATTERNS = {
    "是什么",
    "为什么",
    "怎么回事",
    "有什么关系",
    "发生了什么",
    "背景",
    "详细说说",
    "讲讲",
    "故事",
}

LORE_TOPIC_PATTERNS = {
    "剧情",
    "主线",
    "设定",
    "世界观",
    "经历",
    "过去",
    "真相",
    "任务",
}

NAMED_ENTITIES = {
    "爱弥斯",
    "漂泊者",
    "黎那汐塔",
    "拉海洛",
    "天空海",
    "回音",
    "悲鸣",
    "飞行雪绒",
    "小屋",
    "深空联合",
    "星炬学院",
    "残星会",
}

LORE_RELATION_PATTERNS = {
    "想成为",
    "救世主",
    "认识",
    "记得",
    "承诺",
    "离开",
    "重逢",
    "知道",
}

VOICE_STRONG_PATTERNS = {
    "用爱弥斯的语气",
    "像爱弥斯一样",
    "爱弥斯会怎么说",
    "会怎么回应",
    "会怎么安慰",
    "怎么安慰",
    "爱弥斯风格",
    "模仿爱弥斯",
}

VOICE_COMFORT_PATTERNS = {
    "安慰我",
    "哄我",
    "摸头",
    "欢迎我回家",
    "别离开",
}

VOICE_COMPANION_PATTERNS = {
    "陪我聊",
    "陪我说话",
    "想听你说",
    "跟我说说话",
}

VOICE_LINE_PATTERNS = {
    "写一句",
    "说一句",
    "回复我",
    "对我说",
}

VOICE_EMOTION_PATTERNS = {
    "难过",
    "害怕",
    "担心",
    "孤独",
    "好累",
    "不开心",
    "撑不住",
    "想哭",
    "失眠",
}

VOICE_INTIMATE_PATTERNS = (
    VOICE_COMFORT_PATTERNS
    | VOICE_COMPANION_PATTERNS
    | VOICE_LINE_PATTERNS
    | VOICE_EMOTION_PATTERNS
)


class WuwaRagEvidenceProvider:
    def __init__(
        self,
        *,
        retriever: RetrieverPort,
        evidence_policy: EvidenceNeedPolicy | None = None,
    ):
        self.retriever = retriever
        self.evidence_policy = evidence_policy or EvidenceNeedPolicy()

    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        metadata: dict[str, Any] | None = None,
        response_mode: str | None = None,
        semantic_context: TurnSemanticContext | None = None,
    ) -> EvidencePack:
        plan = self._decide_plan(
            query=query,
            character_id=character_id,
            metadata=metadata or {},
        )
        metadata = {
            **(metadata or {}),
            "character_id": character_id,
            "retrieval_intent": plan.intent,
            "policy_reason": list(plan.policy_reason),
            "lore_score": plan.lore_score,
            "voice_score": plan.voice_score,
            "no_retrieval_score": plan.no_retrieval_score,
        }
        if response_mode:
            metadata["legacy_response_mode"] = response_mode

        lore_results: list[RetrievalResult] = []
        voice_results: list[RetrievalResult] = []
        if not plan.lore_candidate_limit and not plan.voice_candidate_limit:
            return EvidencePack()

        if plan.voice_candidate_limit and plan.remove_lore_overlap_from_voice:
            voice_results = self._retrieve_voice(
                query=query,
                user_id=user_id,
                metadata=metadata,
                candidate_limit=plan.voice_candidate_limit,
                final_limit=plan.voice_final_limit,
            )
            lore_results = self._retrieve_lore(
                query=query,
                user_id=user_id,
                metadata=metadata,
                candidate_limit=plan.lore_candidate_limit,
                final_limit=plan.lore_final_limit,
                overlap_against=voice_results,
            )
            return EvidencePack(lore_results=lore_results, voice_results=voice_results)

        if plan.lore_candidate_limit:
            lore_results = self._retrieve_lore(
                query=query,
                user_id=user_id,
                metadata=metadata,
                candidate_limit=plan.lore_candidate_limit,
                final_limit=plan.lore_final_limit,
            )

        if plan.voice_candidate_limit:
            voice_results = self._retrieve_voice(
                query=query,
                user_id=user_id,
                metadata=metadata,
                candidate_limit=plan.voice_candidate_limit,
                final_limit=plan.voice_final_limit,
                overlap_against=lore_results if plan.remove_voice_overlap_from_lore else None,
            )
        return EvidencePack(lore_results=lore_results, voice_results=voice_results)

    def _decide_plan(
        self,
        *,
        query: str,
        character_id: str,
        metadata: dict[str, Any],
    ) -> EvidenceRetrievalPlan:
        decision = self.evidence_policy.decide(
            query=query,
            character_id=character_id,
            metadata=metadata,
        )
        common = {
            "intent": decision.intent,
            "lore_score": decision.lore_score,
            "voice_score": decision.voice_score,
            "no_retrieval_score": decision.no_retrieval_score,
            "policy_reason": decision.policy_reason,
        }

        if decision.intent == "forced_none" or decision.intent == "none":
            return EvidenceRetrievalPlan(**common)
        if decision.intent == "forced_lore":
            return EvidenceRetrievalPlan(
                lore_candidate_limit=8,
                lore_final_limit=5,
                **common,
            )
        if decision.intent == "forced_voice":
            return EvidenceRetrievalPlan(
                voice_candidate_limit=4,
                voice_final_limit=2,
                **common,
            )
        if decision.intent == "forced_lore_and_voice":
            return EvidenceRetrievalPlan(
                lore_candidate_limit=10,
                lore_final_limit=5,
                voice_candidate_limit=6,
                voice_final_limit=3,
                remove_voice_overlap_from_lore=True,
                **common,
            )
        if decision.intent == "query_lore_and_voice":
            return EvidenceRetrievalPlan(
                lore_candidate_limit=10,
                lore_final_limit=5,
                voice_candidate_limit=6,
                voice_final_limit=3,
                remove_voice_overlap_from_lore=True,
                **common,
            )
        if decision.intent == "query_lore":
            return EvidenceRetrievalPlan(
                lore_candidate_limit=8,
                lore_final_limit=5,
                **common,
            )
        if decision.intent == "query_voice":
            return EvidenceRetrievalPlan(
                voice_candidate_limit=4,
                voice_final_limit=2,
                **common,
            )
        return EvidenceRetrievalPlan(**common)

    def _retrieve_lore(
        self,
        *,
        query: str,
        user_id: str,
        metadata: dict[str, Any],
        candidate_limit: int,
        final_limit: int,
        overlap_against: list[RetrievalResult] | None = None,
    ) -> list[RetrievalResult]:
        candidates = self.retriever.retrieve(
            query=query,
            user_id=user_id,
            namespaces=["canon:profile", "canon:lore", "canon:plot"],
            limit=candidate_limit,
            metadata=metadata,
        )
        return self._without_overlaps(
            candidates,
            overlap_against or [],
            final_limit=final_limit,
        )

    def _retrieve_voice(
        self,
        *,
        query: str,
        user_id: str,
        metadata: dict[str, Any],
        candidate_limit: int,
        final_limit: int,
        overlap_against: list[RetrievalResult] | None = None,
    ) -> list[RetrievalResult]:
        candidates = self.retriever.retrieve(
            query=query,
            user_id=user_id,
            namespaces=["canon:voice"],
            limit=candidate_limit,
            metadata=metadata,
        )
        return self._without_overlaps(
            candidates,
            overlap_against or [],
            final_limit=final_limit,
        )

    def _without_overlaps(
        self,
        candidates: list[RetrievalResult],
        existing: list[RetrievalResult],
        *,
        final_limit: int,
    ) -> list[RetrievalResult]:
        results: list[RetrievalResult] = []
        for candidate in candidates:
            if any(_same_source_or_overlap(candidate, item) for item in existing):
                continue
            if any(_same_source_or_overlap(candidate, item) for item in results):
                continue
            results.append(candidate)
            if len(results) >= final_limit:
                break
        return results


def _same_source_or_overlap(left: RetrievalResult, right: RetrievalResult) -> bool:
    left_meta = left.metadata
    right_meta = right.metadata
    if (
        left_meta.get("original_source_file")
        and left_meta.get("original_source_file") == right_meta.get("original_source_file")
        and left_meta.get("record_id") == right_meta.get("record_id")
    ):
        return True
    if (
        left_meta.get("source_path")
        and left_meta.get("source_path") == right_meta.get("source_path")
        and left_meta.get("state_key") == right_meta.get("state_key")
    ):
        return _range_overlap(
            left_meta.get("source_order_start"),
            left_meta.get("source_order_end"),
            right_meta.get("source_order_start"),
            right_meta.get("source_order_end"),
        )
    return False


def _range_overlap(
    left_start: Any,
    left_end: Any,
    right_start: Any,
    right_end: Any,
) -> bool:
    if None in {left_start, left_end, right_start, right_end}:
        return False
    left_start = int(left_start)
    left_end = int(left_end)
    right_start = int(right_start)
    right_end = int(right_end)
    intersection = max(0, min(left_end, right_end) - max(left_start, right_start) + 1)
    shortest = min(left_end - left_start + 1, right_end - right_start + 1)
    if shortest <= 0:
        return False
    return intersection / shortest >= 0.5


def _compact_query(query: str) -> str:
    return "".join(
        char
        for char in query.strip()
        if char not in {" ", "\t", "\r", "\n", "。", "，", "？", "！", "?", "!"}
    )


def _contains_any(query: str, patterns: set[str]) -> bool:
    return any(pattern in query for pattern in patterns)

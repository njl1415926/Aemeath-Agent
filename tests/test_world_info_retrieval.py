from __future__ import annotations

from app.retrieval.world_info import (
    DEFAULT_WORLD_INFO_PATH,
    WorldInfoKeywordRetriever,
    WorldInfoLoader,
)


def _retriever() -> WorldInfoKeywordRetriever:
    return WorldInfoKeywordRetriever(loader=WorldInfoLoader(DEFAULT_WORLD_INFO_PATH))


def test_world_info_loader_skips_disabled_and_empty_entries():
    entries = WorldInfoLoader(DEFAULT_WORLD_INFO_PATH).load_entries()

    disabled_uids = {35, 36, 46, 85}
    assert entries
    assert disabled_uids.isdisjoint({entry.uid for entry in entries})
    assert all(entry.content.strip() for entry in entries)


def test_world_info_retriever_matches_shorekeeper_generic_and_aemeath_view():
    results = _retriever().retrieve(query="守岸人是谁？")
    comments = {item.metadata["comment"] for item in results}

    assert "守岸人" in comments
    assert "爱弥斯对守岸人的印象" in comments
    assert all(item.namespace == "world:keyword" for item in results)


def test_world_info_retriever_matches_lu_hess_aliases():
    for query in ["陆医生", "陆赫斯", "陆·赫斯"]:
        results = _retriever().retrieve(query=query)
        comments = {item.metadata["comment"] for item in results}

        assert "爱弥斯对陆·赫斯的印象" in comments


def test_world_info_retriever_matches_birthday_keys():
    for query in ["生日", "生日祝福"]:
        results = _retriever().retrieve(query=query)
        comments = {item.metadata["comment"] for item in results}

        assert "爱弥斯对你的生日祝福" in comments


def test_world_info_retriever_matches_home_keywords():
    for query in ["\u5c0f\u5c4b", "\u5bb6"]:
        results = _retriever().retrieve(query=query)
        comments = {item.metadata["comment"] for item in results}

        assert "\u5c0f\u5c4b" in comments


def test_world_info_retriever_excludes_constants_by_default():
    results = _retriever().retrieve(query="守岸人是谁？")

    assert results
    assert all(item.namespace != "world:constant" for item in results)


def test_world_info_retriever_can_include_constants_without_consuming_keyword_limit():
    results = _retriever().retrieve(
        query="守岸人是谁？",
        limit=1,
        metadata={
            "include_world_info_constants": True,
            "constant_limit": 2,
        },
    )

    constant_results = [item for item in results if item.namespace == "world:constant"]
    keyword_results = [item for item in results if item.namespace == "world:keyword"]

    assert len(constant_results) == 2
    assert len(keyword_results) == 1


def test_world_info_retriever_does_not_return_disabled_character_handoff_blocks():
    results = _retriever().retrieve(
        query="漂泊者 爱弥斯外貌细化 爱弥斯数据细化 爱弥斯对话示例",
        limit=20,
        metadata={"include_world_info_constants": True},
    )
    comments = {item.metadata["comment"] for item in results}

    assert "漂泊者（已移交 Character）" not in comments
    assert "爱弥斯外貌细化（已移交 Character）" not in comments
    assert "爱弥斯对话示例（已移交 Character）" not in comments
    assert "爱弥斯数据细化（已拆分）" not in comments


def test_world_info_retriever_metadata_is_trace_friendly():
    result = _retriever().retrieve(query="生日")[0]
    metadata = result.metadata

    assert metadata["retriever"] == "world_info_keyword"
    assert metadata["uid"] == 170
    assert metadata["matched_keys"]
    assert metadata["source_path"].endswith("Aemeath_World_Info.json")
    assert not {"secret", "token", "password", "authorization", "cookie"} & set(metadata)


def test_world_info_retriever_default_keyword_limit_is_five():
    results = _retriever().retrieve(query="你是谁？")
    keyword_results = [item for item in results if item.namespace == "world:keyword"]

    assert len(keyword_results) <= 5

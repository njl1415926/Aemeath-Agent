from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TAG_WITH_TEXT_RE = re.compile(r"<(?:te|ano|color|i|b)(?:\s+[^>]*)?>(.*?)</(?:te|ano|color|i|b)>")
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True)
class ChunkRecord:
    id: str
    namespace: str
    source_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowLine:
    source_order: int
    state_key: str
    row_type: str | None
    normalized_speaker: str
    content: str
    raw: dict[str, Any]


def stable_hash(text: str, *, length: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    previous = None
    while previous != text:
        previous = text
        text = TAG_WITH_TEXT_RE.sub(r"\1", text)
    text = TAG_RE.sub("", text)
    text = text.replace("{PlayerName}", "你")
    text = text.replace("{{user}}", "你")
    text = WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_speaker(speaker_name: str | None, row_type: str | None) -> str:
    if not speaker_name:
        return "你" if row_type == "Option" else "旁白"
    speaker = clean_text(speaker_name)
    if speaker in {"{PlayerName}", "{{user}}"}:
        return "你"
    if speaker == "「爱弥斯」":
        return "爱弥斯"
    return speaker


def _chunk_id(
    *,
    prefix: str,
    namespace: str,
    source_type: str,
    source_key: str,
    content: str,
) -> str:
    return f"{prefix}:{source_key}:{namespace}:{source_type}:{stable_hash(content)}"


def load_wuwa_favor_chunks(path: Path) -> list[ChunkRecord]:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files", {})
    chunks: list[ChunkRecord] = []
    source_path = path.as_posix()

    for record in files.get("favor_role_infos.json", []):
        chunks.extend(_favor_role_info_chunks(record, source_path=source_path))
    for record in files.get("favor_stories.json", []):
        chunks.extend(_paragraph_chunks_for_record(
            record,
            namespace="canon:lore",
            source_type="favor_story",
            title_prefix="角色故事",
            source_path=source_path,
        ))
    for record in files.get("favor_goods.json", []):
        chunks.append(_single_record_chunk(
            record,
            namespace="canon:lore",
            source_type="favor_good",
            title_prefix="偏好物品",
            source_path=source_path,
        ))
    for record in files.get("favor_words.json", []):
        chunks.append(_single_record_chunk(
            record,
            namespace="canon:lore",
            source_type="favor_word_lore",
            title_prefix="爱弥斯心声",
            source_path=source_path,
            derived_view="lore",
        ))
        chunks.append(_single_record_chunk(
            record,
            namespace="canon:voice",
            source_type="favor_word",
            title_prefix="Voice reference",
            source_path=source_path,
            derived_view="voice",
        ))
    return [chunk for chunk in chunks if chunk.content.strip()]


def _base_favor_metadata(
    record: dict[str, Any],
    *,
    source_path: str,
    namespace: str,
    source_type: str,
    chunk_index: int = 0,
    derived_view: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "source_path": source_path,
        "original_source_file": record.get("_source_file"),
        "record_id": record.get("Id"),
        "role_id": record.get("RoleId"),
        "speaker_name": record.get("SpeakerName"),
        "role_nick_name": record.get("RoleNickName"),
        "title": record.get("Title"),
        "namespace": namespace,
        "source_type": source_type,
        "chunk_index": chunk_index,
        "content_hash": "",
        "preprocessor_version": "favor_v1",
    }
    if derived_view:
        metadata["derived_view"] = derived_view
    return metadata


def _single_record_chunk(
    record: dict[str, Any],
    *,
    namespace: str,
    source_type: str,
    title_prefix: str,
    source_path: str,
    derived_view: str | None = None,
) -> ChunkRecord:
    title = clean_text(str(record.get("Title") or record.get("TalentName") or ""))
    content = clean_text(str(record.get("Content") or record.get("Info") or ""))
    full_content = f"[{title_prefix}] {title}\n{content}".strip()
    source_key = f"{record.get('_source_file', 'favor')}:{record.get('Id')}:0"
    metadata = _base_favor_metadata(
        record,
        source_path=source_path,
        namespace=namespace,
        source_type=source_type,
        derived_view=derived_view,
    )
    metadata["content_hash"] = stable_hash(full_content)
    return ChunkRecord(
        id=_chunk_id(
            prefix="favor",
            namespace=namespace,
            source_type=source_type,
            source_key=source_key,
            content=full_content,
        ),
        namespace=namespace,
        source_type=source_type,
        content=full_content,
        metadata=metadata,
    )


def _paragraph_chunks_for_record(
    record: dict[str, Any],
    *,
    namespace: str,
    source_type: str,
    title_prefix: str,
    source_path: str,
    target_chars: int = 800,
) -> list[ChunkRecord]:
    title = clean_text(str(record.get("Title") or ""))
    paragraphs = [clean_text(part) for part in (record.get("Content") or "").split("\n\n")]
    paragraphs = [part for part in paragraphs if part]
    chunks: list[ChunkRecord] = []
    current: list[str] = []
    current_chars = 0
    chunk_index = 0
    for paragraph in paragraphs:
        if current and current_chars + len(paragraph) > target_chars:
            chunks.append(_paragraph_chunk(
                record,
                namespace=namespace,
                source_type=source_type,
                title_prefix=title_prefix,
                title=title,
                parts=current,
                chunk_index=chunk_index,
                source_path=source_path,
            ))
            chunk_index += 1
            current = []
            current_chars = 0
        current.append(paragraph)
        current_chars += len(paragraph)
    if current:
        chunks.append(_paragraph_chunk(
            record,
            namespace=namespace,
            source_type=source_type,
            title_prefix=title_prefix,
            title=title,
            parts=current,
            chunk_index=chunk_index,
            source_path=source_path,
        ))
    return chunks


def _paragraph_chunk(
    record: dict[str, Any],
    *,
    namespace: str,
    source_type: str,
    title_prefix: str,
    title: str,
    parts: list[str],
    chunk_index: int,
    source_path: str,
) -> ChunkRecord:
    full_content = f"[{title_prefix}] {title}\n" + "\n\n".join(parts)
    source_key = f"{record.get('_source_file', 'favor')}:{record.get('Id')}:{chunk_index}"
    metadata = _base_favor_metadata(
        record,
        source_path=source_path,
        namespace=namespace,
        source_type=source_type,
        chunk_index=chunk_index,
    )
    metadata["content_hash"] = stable_hash(full_content)
    return ChunkRecord(
        id=_chunk_id(
            prefix="favor",
            namespace=namespace,
            source_type=source_type,
            source_key=source_key,
            content=full_content,
        ),
        namespace=namespace,
        source_type=source_type,
        content=full_content,
        metadata=metadata,
    )


def _favor_role_info_chunks(record: dict[str, Any], *, source_path: str) -> list[ChunkRecord]:
    chunks: list[ChunkRecord] = []
    fields = [
        ("profile", "Info", "角色档案"),
        ("talent_doc", "TalentDoc", "能力档案"),
        ("talent_certification", "TalentCertification", "共鸣检测"),
    ]
    for chunk_index, (field_key, field_name, title_prefix) in enumerate(fields):
        content = clean_text(str(record.get(field_name) or ""))
        if not content:
            continue
        title = clean_text(str(record.get("SpeakerName") or "爱弥斯"))
        talent_name = clean_text(str(record.get("TalentName") or ""))
        full_content = f"[{title_prefix}] {title}\nTalentName: {talent_name}\n{content}".strip()
        namespace = "canon:profile"
        source_type = "favor_role_info"
        source_key = f"{record.get('_source_file', 'favor')}:{record.get('Id')}:{field_key}"
        metadata = _base_favor_metadata(
            record,
            source_path=source_path,
            namespace=namespace,
            source_type=source_type,
            chunk_index=chunk_index,
        )
        metadata["field_name"] = field_name
        metadata["field_key"] = field_key
        metadata["content_hash"] = stable_hash(full_content)
        chunks.append(ChunkRecord(
            id=_chunk_id(
                prefix="favor",
                namespace=namespace,
                source_type=source_type,
                source_key=source_key,
                content=full_content,
            ),
            namespace=namespace,
            source_type=source_type,
            content=full_content,
            metadata=metadata,
        ))
    return chunks


def load_flow_chunks(path: Path) -> list[ChunkRecord]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    flow_lines = [
        _flow_line_from_row(index, row)
        for index, row in enumerate(rows)
        if clean_text(str(row.get("Content") or ""))
    ]
    scenes: dict[str, list[FlowLine]] = defaultdict(list)
    for line in flow_lines:
        scenes[line.state_key].append(line)

    chunks: list[ChunkRecord] = []
    source_path = path.as_posix()
    for state_key, lines in scenes.items():
        chunks.extend(_plot_chunks_for_scene(
            lines,
            source_path=source_path,
            state_key=state_key,
        ))
        chunks.extend(_voice_chunks_for_scene(
            lines,
            source_path=source_path,
            state_key=state_key,
        ))
    return chunks


def _flow_line_from_row(source_order: int, row: dict[str, Any]) -> FlowLine:
    row_type = row.get("RowType")
    return FlowLine(
        source_order=source_order,
        state_key=str(row.get("StateKey") or f"__missing_state_key__:{source_order}"),
        row_type=row_type,
        normalized_speaker=normalize_speaker(row.get("SpeakerName"), row_type),
        content=clean_text(str(row.get("Content") or "")),
        raw=row,
    )


def _line_text(line: FlowLine) -> str:
    return f"{line.normalized_speaker}: {line.content}"


def _plot_chunks_for_scene(
    lines: list[FlowLine],
    *,
    source_path: str,
    state_key: str,
    target_chars: int = 900,
    max_chars: int = 1200,
    min_chars: int = 180,
    overlap_lines: int = 2,
) -> list[ChunkRecord]:
    windows: list[tuple[int, int]] = []
    start = 0
    while start < len(lines):
        chars = 0
        end = start
        while end < len(lines):
            chars += len(_line_text(lines[end])) + 1
            end += 1
            if chars >= target_chars or chars >= max_chars:
                break
        windows.append((start, end))
        if end >= len(lines):
            break
        start = max(start + 1, end - overlap_lines)

    if len(windows) > 1:
        last_start, last_end = windows[-1]
        last_chars = sum(len(_line_text(line)) + 1 for line in lines[last_start:last_end])
        if last_chars < min_chars:
            prev_start, _prev_end = windows[-2]
            windows[-2] = (prev_start, last_end)
            windows.pop()

    chunks = []
    for chunk_index, (start_index, end_index) in enumerate(windows):
        chunk_lines = lines[start_index:end_index]
        chunks.append(_flow_chunk(
            chunk_lines,
            namespace="canon:plot",
            source_type="flow_scene_window",
            derived_view="plot",
            source_path=source_path,
            state_key=state_key,
            chunk_index=chunk_index,
            line_start=start_index,
            line_end=end_index - 1,
            content_title=f"[Scene: {state_key}]",
        ))
    return chunks


def _voice_chunks_for_scene(
    lines: list[FlowLine],
    *,
    source_path: str,
    state_key: str,
    before: int = 1,
    after: int = 1,
) -> list[ChunkRecord]:
    chunks: list[ChunkRecord] = []
    run_start: int | None = None
    chunk_index = 0

    def flush(run_end: int) -> None:
        nonlocal chunk_index
        if run_start is None:
            return
        voice_lines = lines[run_start:run_end]
        voice_text = "".join(line.content.strip() for line in voice_lines)
        if voice_text in {"……", "...", "嗯", "嗯。", "啊？", "欸？"}:
            return
        start_index = max(0, run_start - before)
        end_index = min(len(lines), run_end + after)
        chunk_lines = lines[start_index:end_index]
        chunks.append(_flow_chunk(
            chunk_lines,
            namespace="canon:voice",
            source_type="flow_voice_context",
            derived_view="voice",
            source_path=source_path,
            state_key=state_key,
            chunk_index=chunk_index,
            line_start=start_index,
            line_end=end_index - 1,
            content_title=f"[Voice context: {state_key}]",
            extra_metadata={
                "voice_order_start": lines[run_start].source_order,
                "voice_order_end": lines[run_end - 1].source_order,
                "voice_line_start": run_start,
                "voice_line_end": run_end - 1,
                "context_before_lines": run_start - start_index,
                "context_after_lines": end_index - run_end,
                "speaker_variants": sorted({line.normalized_speaker for line in voice_lines}),
            },
        ))
        chunk_index += 1

    for index, line in enumerate(lines):
        if is_aemeath_speaker(line.normalized_speaker):
            if run_start is None:
                run_start = index
        else:
            if run_start is not None:
                flush(index)
                run_start = None
    if run_start is not None:
        flush(len(lines))
    return chunks


def is_aemeath_speaker(speaker: str) -> bool:
    return "爱弥斯" in speaker


def _flow_chunk(
    lines: list[FlowLine],
    *,
    namespace: str,
    source_type: str,
    derived_view: str,
    source_path: str,
    state_key: str,
    chunk_index: int,
    line_start: int,
    line_end: int,
    content_title: str,
    extra_metadata: dict[str, Any] | None = None,
) -> ChunkRecord:
    content = "\n".join([content_title, "", *[_line_text(line) for line in lines]]).strip()
    source_start = lines[0].source_order
    source_end = lines[-1].source_order
    speakers = sorted({line.normalized_speaker for line in lines})
    aemeath_line_count = sum(1 for line in lines if is_aemeath_speaker(line.normalized_speaker))
    state_hash = stable_hash(state_key, length=10)
    metadata = {
        "source_path": source_path,
        "namespace": namespace,
        "source_type": source_type,
        "derived_view": derived_view,
        "state_key": state_key,
        "state_hash": state_hash,
        "chunk_index": chunk_index,
        "source_order_start": source_start,
        "source_order_end": source_end,
        "line_start": line_start,
        "line_end": line_end,
        "speakers": speakers,
        "contains_aemeath": aemeath_line_count > 0,
        "aemeath_line_count": aemeath_line_count,
        "content_hash": stable_hash(content),
        "preprocessor_version": "flow_v1",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    source_key = f"{state_hash}:{source_start}-{source_end}:{chunk_index}"
    return ChunkRecord(
        id=_chunk_id(
            prefix="flow",
            namespace=namespace,
            source_type=source_type,
            source_key=source_key,
            content=content,
        ),
        namespace=namespace,
        source_type=source_type,
        content=content,
        metadata=metadata,
    )


def load_wuwa_gamedata_chunks(source_dir: Path) -> list[ChunkRecord]:
    chunks: list[ChunkRecord] = []
    chunks.extend(load_wuwa_favor_chunks(source_dir / "aemeath_favor_extract.json"))
    chunks.extend(load_flow_chunks(source_dir / "flow_texts_tid_sorted.json"))
    return chunks


def count_by_namespace(chunks: Iterable[ChunkRecord]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for chunk in chunks:
        counts[chunk.namespace] += 1
    return dict(counts)

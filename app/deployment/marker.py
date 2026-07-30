from __future__ import annotations

from pathlib import Path
from typing import Iterable

_CHUNK_SIZE = 64 * 1024


def marker_is_absent(*, marker_file: Path, targets: Iterable[Path]) -> bool:
    marker = marker_file.read_bytes()
    if not marker or b"\r" in marker or b"\n" in marker:
        raise ValueError("marker_file must contain one non-empty line without a terminator.")
    marker_path = marker_file.resolve()
    for target in targets:
        for candidate in _iter_files(target):
            if candidate.resolve() == marker_path:
                continue
            if _file_contains(candidate, marker):
                return False
    return True


def _iter_files(target: Path) -> Iterable[Path]:
    if target.is_symlink():
        return
    if target.is_file():
        yield target
        return
    if target.is_dir():
        for candidate in target.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                yield candidate


def _file_contains(path: Path, marker: bytes) -> bool:
    overlap = b""
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            combined = overlap + chunk
            if marker in combined:
                return True
            overlap = combined[-(len(marker) - 1) :] if len(marker) > 1 else b""
    return False

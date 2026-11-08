"""Validate and fingerprint already-encoded media without changing its bytes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_FINGERPRINT_VERSION = "upload-original-v1"
_VTT_HEADER = re.compile(r"^WEBVTT(?:[ \t].*)?$")


class PreparationError(Exception):
    """A media file or caption is missing, unsafe, or invalid."""


@dataclass(frozen=True)
class PreparedVideo:
    video: Path
    subtitles: tuple[Path, ...]
    fingerprint: str


def _validate_file(path: Path, description: str) -> Path:
    try:
        original = Path(path)
        resolved = original.resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size == 0:
            raise PreparationError(
                f"{description} must be a nonempty regular file: {path}"
            )
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PreparationError(f"{description} is missing or invalid: {path}") from exc


def _validate_vtt(path: Path) -> None:
    if path.suffix.lower() != ".vtt":
        raise PreparationError(f"Only WebVTT subtitles are supported: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as subtitle:
            header = subtitle.readline().rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise PreparationError(f"Could not read WebVTT subtitle: {path}") from exc
    if not _VTT_HEADER.fullmatch(header):
        raise PreparationError(f"Invalid WebVTT header: {path}")


def _validated_inputs(
    source: Path, subtitles: list[Path]
) -> tuple[Path, tuple[Path, ...], tuple[Path, ...]]:
    original_video = Path(source)
    original_subtitles = tuple(Path(path) for path in subtitles)
    resolved_video = _validate_file(original_video, "Video")
    resolved_subtitles = tuple(
        _validate_file(path, "Subtitle") for path in original_subtitles
    )
    identities = (resolved_video, *resolved_subtitles)
    if len(set(identities)) != len(identities):
        raise PreparationError("Duplicate source reference")
    for path in original_subtitles:
        _validate_vtt(path)
    return original_video, original_subtitles, identities


def inspect_video(source: Path, subtitles: list[Path]) -> None:
    _validated_inputs(source, subtitles)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise PreparationError(f"Could not read source file: {path}") from exc
    if size == 0:
        raise PreparationError(f"Source file became empty: {path}")
    return size, digest.hexdigest()


def _fingerprint_records(identities: Iterable[Path]) -> list[dict[str, str | int]]:
    records = []
    for identity in identities:
        size, digest = _hash_file(identity)
        records.append({"identity": str(identity), "size": size, "sha256": digest})
    return records


def source_fingerprint(source: Path, subtitles: list[Path]) -> str:
    _, _, identities = _validated_inputs(source, subtitles)
    payload = {
        "version": _FINGERPRINT_VERSION,
        "inputs": _fingerprint_records(identities),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_video(source: Path, subtitles: list[Path]) -> PreparedVideo:
    inspect_video(source, subtitles)
    fingerprint = source_fingerprint(source, subtitles)
    if source_fingerprint(source, subtitles) != fingerprint:
        raise PreparationError("Source inputs changed during preparation")
    return PreparedVideo(
        Path(source), tuple(Path(path) for path in subtitles), fingerprint
    )

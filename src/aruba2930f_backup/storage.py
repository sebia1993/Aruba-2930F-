"""Filesystem helpers for configuration backup runs.

This module deliberately contains no application settings.  Targets and
credentials remain in memory; only completed configuration files and their
run reports are written below the operator-selected output directory.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class StoredConfig:
    """Metadata for one durably written configuration file."""

    path: Path
    sha256: str
    byte_count: int
    line_count: int


def default_output_directory() -> Path:
    """Return the application default without creating or persisting it."""

    return Path.home() / "Documents" / "Aruba2930FConfigBackup" / "backup"


def create_run_directory(
    base_directory: str | os.PathLike[str],
    *,
    now: datetime | None = None,
) -> Path:
    """Create and return a unique ``YYYY-MM-DD/HHmmss`` run directory.

    Runs started in the same second advance to the next available second.  The
    resulting path always keeps the documented shape and creation is atomic,
    including when two application instances start concurrently.
    """

    base_path = Path(base_directory)
    candidate_time = (now or datetime.now()).replace(microsecond=0)
    base_path.mkdir(parents=True, exist_ok=True)

    for offset in range(86_400):
        timestamp = candidate_time + timedelta(seconds=offset)
        candidate = base_path / timestamp.strftime("%Y-%m-%d") / timestamp.strftime("%H%M%S")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate

    raise FileExistsError("하루 범위에서 사용할 수 있는 실행 폴더를 찾지 못했습니다.")


def _safe_component(value: str, *, fallback: str) -> str:
    cleaned = _INVALID_FILENAME_CHARACTERS.sub("_", value.strip())
    cleaned = _WHITESPACE.sub("_", cleaned).strip(" ._")
    if not cleaned:
        cleaned = _INVALID_FILENAME_CHARACTERS.sub("_", fallback.strip()).strip(" ._")
    if not cleaned:
        cleaned = "device"
    if cleaned.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:120].rstrip(" .") or "device"


def device_config_path(
    run_directory: str | os.PathLike[str],
    *,
    hostname: str | None,
    ip_address: str,
    reserved_names: Collection[str] = (),
) -> Path:
    """Choose a non-conflicting ``.txt`` path for a device.

    A detected hostname is preferred.  A repeated hostname gains an IP suffix;
    if that also collides, a numeric suffix is used.  Comparisons are
    case-insensitive to match Windows filesystem behavior.
    """

    directory = Path(run_directory)
    fallback_stem = _safe_component(ip_address, fallback="device")
    hostname_stem = _safe_component(hostname or "", fallback=fallback_stem)
    detected_hostname = bool(hostname and hostname.strip())

    occupied = {name.casefold() for name in reserved_names}
    if directory.exists():
        occupied.update(path.name.casefold() for path in directory.iterdir() if path.is_file())

    preferred = f"{hostname_stem}.txt"
    if preferred.casefold() not in occupied:
        return directory / preferred

    base = f"{hostname_stem}-{fallback_stem}" if detected_hostname else fallback_stem
    candidate = f"{base}.txt"
    counter = 2
    while candidate.casefold() in occupied:
        candidate = f"{base}_{counter}.txt"
        counter += 1
    return directory / candidate


def normalize_config_text(config_text: str) -> str:
    """Normalize all line endings to Windows CRLF without altering content."""

    return config_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def write_config_atomic(
    destination: str | os.PathLike[str],
    config_text: str,
) -> StoredConfig:
    """Write UTF-8 configuration data via a sibling ``.part`` file."""

    final_path = Path(destination)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = final_path.with_name(f"{final_path.name}.part")
    payload = normalize_config_text(config_text).encode("utf-8")

    try:
        with partial_path.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial_path, final_path)
    except BaseException:
        partial_path.unlink(missing_ok=True)
        raise

    return StoredConfig(
        path=final_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        line_count=len(config_text.splitlines()),
    )


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Calculate a file digest without loading the whole file into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup_partial_files(run_directory: str | os.PathLike[str]) -> int:
    """Remove unfinished sibling files from one run directory only."""

    removed = 0
    directory = Path(run_directory)
    if not directory.exists():
        return removed
    for path in directory.glob("*.part"):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed

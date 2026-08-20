from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

import pytest

from aruba2930f_backup.storage import (
    FilenameMode,
    cleanup_partial_files,
    create_run_directory,
    device_config_path,
    sha256_file,
    write_config_atomic,
)


def test_create_run_directory_keeps_shape_and_avoids_same_second_collision(
    tmp_path: Path,
) -> None:
    timestamp = datetime(2026, 8, 20, 9, 8, 7)

    first = create_run_directory(tmp_path, now=timestamp)
    second = create_run_directory(tmp_path, now=timestamp)

    assert first.relative_to(tmp_path).as_posix() == "2026-08-20/090807"
    assert second.relative_to(tmp_path).as_posix() == "2026-08-20/090808"


def test_device_config_path_pairs_hostname_with_ip_and_uses_collision_suffix(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    preferred = device_config_path(
        run_directory,
        hostname="edge/sw:01",
        ip_address="192.0.2.10",
    )
    preferred.write_text("existing", encoding="utf-8")
    collision = device_config_path(
        run_directory,
        hostname="edge/sw:01",
        ip_address="192.0.2.10",
    )
    other_address = device_config_path(
        run_directory,
        hostname="edge/sw:01",
        ip_address="192.0.2.11",
    )
    fallback = device_config_path(
        run_directory,
        hostname=None,
        ip_address="192.0.2.12",
    )

    assert preferred.name == "edge_sw_01(192.0.2.10).txt"
    assert collision.name == "edge_sw_01(192.0.2.10)_2.txt"
    assert other_address.name == "edge_sw_01(192.0.2.11).txt"
    assert fallback.name == "192.0.2.12.txt"


@pytest.mark.parametrize(
    ("filename_mode", "expected"),
    (
        (FilenameMode.HOSTNAME, "edge_sw_01.txt"),
        (FilenameMode.IP, "192.0.2.30.txt"),
        (FilenameMode.HOSTNAME_IP, "edge_sw_01(192.0.2.30).txt"),
    ),
)
def test_device_config_path_supports_each_filename_mode(
    tmp_path: Path,
    filename_mode: FilenameMode,
    expected: str,
) -> None:
    path = device_config_path(
        tmp_path,
        hostname="edge/sw:01",
        ip_address="192.0.2.30",
        filename_mode=filename_mode,
    )

    assert path.name == expected


@pytest.mark.parametrize("filename_mode", tuple(FilenameMode))
def test_device_config_path_uses_ip_when_hostname_is_unavailable(
    tmp_path: Path,
    filename_mode: FilenameMode,
) -> None:
    path = device_config_path(
        tmp_path,
        hostname=None,
        ip_address="192.0.2.31",
        filename_mode=filename_mode,
    )

    assert path.name == "192.0.2.31.txt"


def test_hostname_mode_uses_numeric_suffix_for_duplicate_names(tmp_path: Path) -> None:
    first = device_config_path(
        tmp_path,
        hostname="edge-lab",
        ip_address="192.0.2.40",
        filename_mode=FilenameMode.HOSTNAME,
    )
    first.write_text("existing", encoding="utf-8")
    second = device_config_path(
        tmp_path,
        hostname="edge-lab",
        ip_address="192.0.2.41",
        filename_mode=FilenameMode.HOSTNAME,
    )

    assert first.name == "edge-lab.txt"
    assert second.name == "edge-lab_2.txt"


def test_hostname_mode_sanitizes_windows_reserved_name_and_casefolded_collision(
    tmp_path: Path,
) -> None:
    reserved = device_config_path(
        tmp_path,
        hostname="CON",
        ip_address="192.0.2.42",
        filename_mode=FilenameMode.HOSTNAME,
    )
    collision = device_config_path(
        tmp_path,
        hostname="edge-lab",
        ip_address="192.0.2.43",
        filename_mode=FilenameMode.HOSTNAME,
        reserved_names=("EDGE-LAB.TXT",),
    )

    assert reserved.name == "_CON.txt"
    assert collision.name == "edge-lab_2.txt"


def test_device_config_path_keeps_windows_safe_length_and_invalid_name_fallback(
    tmp_path: Path,
) -> None:
    long_name = device_config_path(
        tmp_path,
        hostname="x" * 200,
        ip_address="192.0.2.20",
    )
    invalid_name = device_config_path(
        tmp_path,
        hostname='<>:"/\\|?*',
        ip_address="192.0.2.21",
    )

    assert long_name.name == f"{'x' * 120}(192.0.2.20).txt"
    assert len(long_name.name) < 255
    assert invalid_name.name == "192.0.2.21.txt"


def test_write_config_atomic_uses_utf8_crlf_and_returns_digest(tmp_path: Path) -> None:
    destination = tmp_path / "switch.txt"

    stored = write_config_atomic(destination, "hostname edge\ninterface 1\r\n exit\r")

    expected = b"hostname edge\r\ninterface 1\r\n exit\r\n"
    assert destination.read_bytes() == expected
    assert stored.sha256 == hashlib.sha256(expected).hexdigest()
    assert stored.byte_count == len(expected)
    assert stored.line_count == 3
    assert not (tmp_path / "switch.txt.part").exists()


def test_cleanup_partial_files_is_limited_to_run_directory(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    child_directory = run_directory / "child"
    child_directory.mkdir(parents=True)
    (run_directory / "one.txt.part").write_text("partial", encoding="utf-8")
    (run_directory / "keep.txt").write_text("complete", encoding="utf-8")
    (child_directory / "nested.txt.part").write_text("nested", encoding="utf-8")

    assert cleanup_partial_files(run_directory) == 1
    assert (run_directory / "keep.txt").exists()
    assert (child_directory / "nested.txt.part").exists()


def test_sha256_file_streams_expected_digest(tmp_path: Path) -> None:
    payload = b"fixture-data" * 200_000
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)

    assert sha256_file(source) == hashlib.sha256(payload).hexdigest()


def test_atomic_write_failure_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "switch.txt"

    def fail_replace(source: object, target: object) -> None:
        del source, target
        raise OSError("fixture replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="fixture replace failure"):
        write_config_atomic(destination, "hostname edge\n")

    assert not destination.exists()
    assert not (tmp_path / "switch.txt.part").exists()


def test_cleanup_missing_directory_is_noop(tmp_path: Path) -> None:
    assert cleanup_partial_files(tmp_path / "missing") == 0

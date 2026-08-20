from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from aruba2930f_backup import __main__ as entrypoint
from aruba2930f_backup import __version__


def _run_module(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [sys.executable, "-m", "aruba2930f_backup", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_version_command(tmp_path: Path) -> None:
    completed = _run_module(tmp_path, "--version")

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"Aruba2930FConfigBackup {__version__}"


def test_smoke_test_is_offline_and_does_not_write_files(tmp_path: Path) -> None:
    before = tuple(tmp_path.rglob("*"))

    completed = _run_module(tmp_path, "--smoke-test")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "application": "Aruba2930FConfigBackup",
        "version": __version__,
        "ok": True,
    }
    assert tuple(tmp_path.rglob("*")) == before


def test_in_process_smoke_path(capsys: object, monkeypatch: object) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")  # type: ignore[attr-defined]

    assert entrypoint.main(["--smoke-test"]) == 0

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out)["ok"] is True


def test_default_entrypoint_delegates_to_gui(monkeypatch: object) -> None:
    import aruba2930f_backup.gui as gui

    monkeypatch.setattr(gui, "run_gui", lambda: 17)  # type: ignore[attr-defined]

    assert entrypoint.main([]) == 17

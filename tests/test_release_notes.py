from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_notes_are_operator_focused_and_korean_first(tmp_path: Path) -> None:
    output = tmp_path / "release-notes.md"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "release_notes.py"),
            "--version",
            "0.1.8",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    notes = output.read_text(encoding="utf-8")

    assert "# Aruba 2930F 설정 백업 v0.1.8" in notes
    assert "## 릴리즈 요약" in notes
    assert "## 운영 영향" in notes
    assert "## 검증 결과" in notes
    assert "## 다운로드 파일" in notes
    assert "## 알려진 제한" in notes
    assert "## v0.1.8 변경 내역" in notes
    assert "Windows x64 portable onedir ZIP" not in notes
    assert "Prerelease" not in notes


def test_release_notes_highlights_do_not_lead_with_security_repetition(tmp_path: Path) -> None:
    output = tmp_path / "release-notes.md"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "release_notes.py"),
            "--version",
            "0.1.8",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    notes = output.read_text(encoding="utf-8")
    summary = notes.split("## 운영 영향", maxsplit=1)[0]

    assert "파일 이름" in summary
    assert "SSH 명령, 진단 코드와 운영 로그의 비민감정보 경계" not in summary

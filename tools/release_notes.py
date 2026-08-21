"""CHANGELOG의 한 버전을 운영자 중심 GitHub Release Notes로 변환합니다."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECTION_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$", re.MULTILINE)
BULLET_RE = re.compile(r"^- (?P<text>.+)$", re.MULTILINE)


def _read_version_body(version: str) -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(?P<body>.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if match is None:
        raise SystemExit(f"CHANGELOG section not found for {version}")
    return match.group("body").strip()


def _split_sections(body: str) -> list[tuple[str, str]]:
    matches = list(SECTION_RE.finditer(body))
    if not matches:
        return [("변경 내용", body)]

    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((match.group("title").strip(), body[start:end].strip()))
    return sections


def _highlights(body: str, limit: int = 5) -> list[str]:
    highlights: list[str] = []
    for title, section_body in _split_sections(body):
        # 기존 보안 경계를 유지했다는 반복 문구보다 실제 사용자/운영 변경을 우선합니다.
        if title == "보안":
            continue
        for match in BULLET_RE.finditer(section_body):
            text = match.group("text").strip()
            if text and text not in highlights:
                highlights.append(text)
            if len(highlights) >= limit:
                return highlights
    return highlights


def _render(version: str, body: str) -> str:
    highlights = _highlights(body)
    highlights_text = "\n".join(f"- {item}" for item in highlights)
    if not highlights_text:
        highlights_text = "- 세부 변경 내용은 아래 버전 변경 내역을 확인하십시오."

    return f"""# Aruba 2930F 설정 백업 v{version}

> **배포 상태: 사전릴리즈**
>
> 이 릴리즈는 자동 테스트, Windows 패키지 검증, SHA-256 및 빌드 출처 검증을
> 통과한 산출물입니다. 실제 Aruba 2930F 운영 환경은 별도 현장 검증 범위로
> 관리합니다.

## 릴리즈 요약

이번 릴리즈는 `CHANGELOG.md`에 확정된 v{version} 변경을 반영한 Windows x64
배포본입니다. 운영자가 먼저 확인해야 할 변경은 다음과 같습니다.

{highlights_text}

## 운영 영향

- 장비 측 기본 동작은 **읽기 전용 설정 수집**입니다.
- 설정 모드에 진입하거나 장비 구성을 변경하는 명령은 실행하지 않습니다.
- 새 버전은 기존 실행 폴더에 덮어쓰기보다 별도 폴더에 압축 해제하는 방식을 권장합니다.
- 입출력 형식이나 동작 변경이 있는 경우 아래 **버전 변경 내역**을 우선 확인하십시오.

## 검증 결과

| 검증 항목 | 결과 |
|---|---|
| 저장소 테스트 및 정적 검증 | 통과 후에만 릴리즈 진행 |
| Windows x64 패키지 빌드 | 통과 |
| 패키지 실행 점검 | 통과 |
| ZIP SHA-256 검증 | 통과 |
| CycloneDX SBOM 생성 | 통과 |
| 태그 / `main` / 빌드 커밋 출처 확인 | 통과 |
| 실제 Aruba 2930F 현장 검증 | 별도 수행 필요 |

## 다운로드 파일

일반 사용자는 아래 ZIP을 사용합니다.

```text
Aruba2930FConfigBackup_v{version}_windows_x64.zip
```

무결성 확인:

```text
Aruba2930FConfigBackup_v{version}_windows_x64.zip.sha256
```

의존성 검토용:

```text
Aruba2930FConfigBackup_v{version}_sbom.cdx.json
```

## 설치 및 업데이트

1. Windows x64 ZIP과 `.sha256`을 받습니다.
2. PowerShell `Get-FileHash`로 ZIP의 SHA-256을 대조합니다.
3. 기존 버전과 다른 새 폴더에 ZIP 전체를 압축 해제합니다.
4. `Aruba2930FConfigBackup.exe`를 실행합니다.
5. 처음 현장에 적용하는 버전은 소수 장비에서 결과를 대조한 뒤 범위를 확대합니다.

현재 사전릴리즈는 Authenticode 서명이 없으므로 Windows SmartScreen 경고가
표시될 수 있습니다.

## 알려진 제한

- 실제 Aruba 2930F 운영 장비에 대한 검증은 자동 테스트와 별개입니다.
- Aruba 2930F 이외 모델은 지원 범위가 아닙니다.
- 설정 복원 및 장비 구성 변경 기능은 제공하지 않습니다.
- 예약 실행, 서비스/에이전트 모드는 현재 범위 밖입니다.

## v{version} 변경 내역

{body}

## 상세 문서

- [README](https://github.com/sebia1993/Aruba-2930F-/blob/main/README.md)
- [프로그램 구조](https://github.com/sebia1993/Aruba-2930F-/blob/main/docs/ARCHITECTURE.md)
- [SSH 수집 및 운영 안전](https://github.com/sebia1993/Aruba-2930F-/blob/main/docs/SSH_AND_SAFETY.md)
- [오류 코드](https://github.com/sebia1993/Aruba-2930F-/blob/main/docs/ERROR_CODES.md)
- [현장 문제 해결](https://github.com/sebia1993/Aruba-2930F-/blob/main/docs/TROUBLESHOOTING.md)
- [릴리즈 운영 원칙](https://github.com/sebia1993/Aruba-2930F-/blob/main/docs/RELEASE_POLICY.md)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    body = _read_version_body(args.version)
    output = _render(args.version, body)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

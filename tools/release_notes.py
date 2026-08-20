"""Extract one version from CHANGELOG.md and add prerelease evidence boundaries."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## \[{re.escape(args.version)}\][^\n]*\n(?P<body>.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if match is None:
        raise SystemExit(f"CHANGELOG section not found for {args.version}")

    body = match.group("body").strip()
    notice = """
> **사전릴리즈 / Prerelease**
>
> 가짜 SSH 서버, 단위 테스트 및 Windows 패키지 smoke로 검증했습니다.
> 실제 Aruba 2930F 장비에서는 아직 검증하지 않았으며 EXE는 Authenticode로
> 서명되지 않았습니다. 현장 사용 전에 해시 확인과 별도 장비 검증이 필요합니다.

## 릴리즈 자산 / Release assets

- Windows x64 portable onedir ZIP (Python 설치 불필요)
- ZIP SHA-256 sidecar
- CycloneDX JSON SBOM
""".strip()
    output = f"# Aruba 2930F Config Backup v{args.version}\n\n{notice}\n\n{body}\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

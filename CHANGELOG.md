# 변경 기록

이 프로젝트는 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을
따르고 [Semantic Versioning](https://semver.org/lang/ko/)을 사용합니다.

## [Unreleased]

## [0.1.0] - 2026-08-20

### 추가

- 여러 Aruba 2930F 장비를 위한 Windows GUI 일괄 백업
- 매 연결에서 모든 `show` 명령보다 먼저 검증하는 `no page` 단계
- SHA-256 SSH 호스트 키 최초 승인과 변경 차단
- 공통 SSH 계정, 선택적 Enable, 제한된 동시 처리와 재시도
- 2930F 모델/SKU 검증 후 한 번의 `show running-config` 수집
- 장비별 UTF-8 TXT, SHA-256 및 `result.xlsx` 실행 보고서
- 취소, 원자적 파일 저장, 출력 한도 및 안정적인 오류 코드
- Python 없는 Windows x64용 PyInstaller onedir portable ZIP
- CI, 릴리즈 패키지 검증, CycloneDX SBOM과 SHA-256 자산

### 보안

- 자격증명과 장비 목록을 세션에만 유지하고 운영 로그에서 민감정보 제거
- 실제 2930F 미검증 및 미서명 바이너리임을 사전릴리즈에 명시

[Unreleased]: https://github.com/sebia1993/Aruba-2930F-/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sebia1993/Aruba-2930F-/releases/tag/v0.1.0

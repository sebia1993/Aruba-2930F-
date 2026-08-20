# 변경 기록

이 프로젝트는 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을
따르고 [Semantic Versioning](https://semver.org/lang/ko/)을 사용합니다.

## [Unreleased]

## [0.1.3] - 2026-08-20

### 추가

- 실패 단계, 고정 오류 ID, 시도 횟수와 세부 분류를 40비트 payload로 담고
  Crockford Base32 및 CRC-5/EPC 검사 문자를 사용하는 15자 오프라인 진단 코드
- 실패 코드를 장비 식별자 없이 집계하는 완료 팝업과 `진단 코드 복사` 버튼
- `result.xlsx`의 `Diagnostic Code` 열, `operation.jsonl`의 코드별 발생 횟수,
  복수 코드와 JSON 출력을 지원하는 유지관리자 진단 CLI
- 앱 초기화, 작업 스레드, 설정/Excel 저장 오류를 위한 실행 단계 및 치명적 오류 코드

### 수정

- ArubaOS-Switch 로그인 배너의 ANSI/백스페이스 문자를 정리하고
  `Press any key to continue`를 제한 시간 안에 해제하도록 SSH 초기화 보완
- 최초 연결과 Enable 전환에서 확인한 EXEC 프롬프트를 캐시하고, 이후 설정 및
  show 명령에서 추가 프롬프트 조회 없이 응답 마지막의 정확한 일치만 검증
- `(config)#` 같은 비-EXEC 모드와 프롬프트 불일치를 계속 거부하면서
  `no page` 이전에는 show 명령을 보내지 않는 순서 보존

### 보안

- 진단 코드와 집계 로그에 IP, 포트, 호스트명, 계정, 경로, 오류 원문 또는 설정
  원문을 포함하지 않도록 고정하고, 코드가 암호화나 전자서명이 아님을 문서화
- 장비 설정 변경 명령 및 실제 장비 테스트 없이 mock과 loopback SSH로 호환 경로 검증

## [0.1.2] - 2026-08-20

### 수정

- 실제 운영에 사용 중인 `wlc_acl` 수집기와 동일한 Paramiko 4 계열로 고정해,
  `ssh-rsa` 또는 `diffie-hellman-group14-sha1`만 제공하는 일부 2930F의 SSH
  호스트 키 사전점검과 인증 연결 호환성 복구
- SSH 알고리즘 불일치를 일반 협상 실패와 구분하고 재시도 불가능한
  `SSH_ALGORITHM_INCOMPATIBLE`로 즉시 보고
- 레거시 알고리즘만 제공하는 loopback Aruba SSH 서버를 통해 호스트 키 승인,
  인증 및 `show running-config` 수집 경로를 회귀 테스트로 고정
- GitHub Actions 태그 체크아웃이 로컬 annotated tag ref를 커밋으로 덮는
  환경에서도 원격 태그 객체를 별도 ref로 검증하도록 릴리즈 게이트 수정

### 보안

- 릴리즈 게시 직전에 원격 태그, 이벤트 커밋, 최신 `main`, ZIP 내부 출처와
  SHA-256을 다시 대조해 빌드와 게시 사이 ref 이동을 차단
- PowerShell 네이티브 명령 실패가 다음 성공 명령에 가려지지 않도록 CI와
  릴리즈 설치·검증 단계를 fail-closed 처리
- 레거시 SSH 호환 연결에서도 기존 SHA-256 호스트 키 사전 검토와 인증 연결의
  지문 고정 검증을 그대로 유지
- Paramiko 4의 의도된 SHA-1 호환성 권고(`PYSEC-2026-2858`,
  `CVE-2026-44405`)만 의존성 감사 예외로 명시하고 제거 조건을 보안 정책에 기록

## [0.1.1] - 2026-08-20

### 추가

- transient 연결 실패를 즉시 반복하지 않고 5초, 15초, 30초 뒤에 다시
  배정하는 총 4라운드 지연 재시도
- 호스트 키 사전점검 시도와 인증 후 백업 시도를 분리한 진행 표시 및 보고서
- 4회 소진 장비를 일반 실패와 구분하는 `retry_exhausted` 상태
- 이전 실행에서 재시도를 소진한 장비만 선택해 새 실행 폴더로 다시 수집하는
  수동 재시도

### 변경

- 일부 장비가 재시도 대기 중이어도 정상 장비의 수집과 결과 저장을 계속 진행
- 완료 요약과 Excel 보고서에서 성공, 재시도 소진, 기타 실패를 별도 집계
- 수동 재시도 때 세션 전용 암호를 다시 입력하도록 하여 자격증명 비저장 원칙 유지

### 보안

- 완전한 Git 작업 트리 상태를 패키지 출처에 기록하고 dirty release build 차단
- 현재 `main` 커밋의 annotated tag만 게시하도록 릴리즈 게이트 강화

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

[Unreleased]: https://github.com/sebia1993/Aruba-2930F-/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/sebia1993/Aruba-2930F-/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/sebia1993/Aruba-2930F-/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/sebia1993/Aruba-2930F-/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/sebia1993/Aruba-2930F-/releases/tag/v0.1.0

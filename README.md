# Aruba 2930F Config Backup

ArubaOS-Switch 기반 **Aruba 2930F** 여러 대의 `running-config`를 SSH로
수집하는 Windows용 GUI 도구입니다. 장비 설정을 변경하지 않으며, 수집 전에
항상 `no page`를 적용해 수동으로 페이지를 넘기지 않고 한 번에 백업합니다.

> **릴리즈 상태:** v0.1.1은 사전릴리즈입니다. 자동 테스트는 가짜 SSH 장비와
> 로컬 파일 시스템을 사용하며, 실제 Aruba 2930F에서의 동작을 증명하지는
> 않습니다. 현장 도입 전 별도 검증이 필요합니다.

## 주요 기능

- IPv4 주소를 한 줄에 하나씩 붙여 넣어 여러 장비를 동시에 백업
- 공통 SSH 계정과 선택적 Enable 암호 사용(메모리에만 유지)
- 최초 접속 시 SSH 호스트 키 SHA-256 지문 일괄 검토
- 승인된 호스트 키 변경 시 연결 차단
- Aruba 2930F 모델/SKU를 확인한 뒤에만 `show running-config` 실행
- 장비별 UTF-8 TXT 파일과 실행 결과 `result.xlsx` 생성
- 5/15/30초 지연을 둔 최대 4라운드 transient 재시도
- 재시도 소진 장비만 새 실행으로 다시 수집하는 수동 재시도
- 즉시 취소, 안정적인 오류 코드와 민감정보 없는 로그
- Python 설치가 필요 없는 Windows x64 portable ZIP 제공

## 다운로드와 실행

1. GitHub Releases에서
   `Aruba2930FConfigBackup_v0.1.1_windows_x64.zip`과 같은 이름의
   `.sha256` 파일을 내려받습니다.
2. PowerShell에서 해시를 확인합니다.

   ```powershell
   Get-FileHash .\Aruba2930FConfigBackup_v0.1.1_windows_x64.zip -Algorithm SHA256
   Get-Content .\Aruba2930FConfigBackup_v0.1.1_windows_x64.zip.sha256
   ```

3. ZIP 전체를 쓰기 가능한 로컬 폴더에 압축 해제합니다. ZIP 안의 EXE만
   따로 꺼내면 실행되지 않습니다.
4. `Aruba2930FConfigBackup\Aruba2930FConfigBackup.exe`를 실행합니다.

이 사전릴리즈는 Authenticode 서명이 없으므로 Windows SmartScreen 경고가
나타날 수 있습니다. 릴리즈 해시와 이 저장소의 GitHub Actions 결과를 먼저
확인하십시오.

## 사용 흐름

1. 장비 IPv4 주소를 한 줄에 하나씩 입력합니다. 공백 줄은 무시하지만 잘못된
   주소나 중복 주소가 하나라도 있으면 전체 실행이 시작되지 않습니다.
2. SSH 포트(기본 22), 공통 사용자 이름/암호, 필요한 경우 Enable 암호를
   입력합니다.
3. 동시 접속 수(기본 10, 1~20)와 결과 폴더를 확인하고 **백업 시작**을
   누릅니다.
4. 새 장비의 SSH 호스트 키 유형과 SHA-256 지문을 실제 장비 또는 관리
   기록과 대조한 다음 승인합니다. 확인할 수 없는 키는 승인하지 마십시오.
5. 진행 표에서 장비별 단계와 결과를 확인합니다. 완료 후 결과 폴더를 열 수
   있습니다.
6. `재시도 소진` 장비가 있으면 암호를 다시 입력한 뒤
   **접속 실패 장비만 다시 시도**를 누를 수 있습니다. 이전 결과는 보존되고
   새 시각의 실행 폴더와 `result.xlsx`가 생성됩니다.

취소하면 새 작업을 배정하지 않고 열린 SSH 세션을 닫습니다. 이미 완성된
백업은 유지하며, 미완성 파일은 결과로 취급하지 않습니다.

## SSH 명령 순서와 `no page`

모든 최초 연결과 재연결에서 다음 순서를 고정합니다.

1. SSH 호스트 키 검증
2. 로그인 및 선택적 Enable
3. `no page` 실행과 CLI 응답/프롬프트 검증
4. 터미널 폭 511 설정
5. `show version`, `show modules`
6. Aruba 2930F 모델/SKU 검증
7. `show running-config` 한 번 실행
8. 최종 프롬프트·잔여 페이저·출력 한도 검증 후 파일 저장

`no page`가 거부되거나 프롬프트로 돌아오지 않으면
`PAGING_SETUP_FAILED`로 중단하며, 이후 `show` 명령은 보내지 않습니다.
재시도 시에도 `no page`를 다시 적용합니다. 비정상적으로 남은 페이지 표시를
제한적으로 처리하지만 시간, 20 MiB 또는 250,000행 한도를 넘으면 실패로
기록합니다.

## 지연 재시도 정책

일시적인 네트워크 또는 명령 시간초과처럼 `retryable`로 분류된 실패만 다음
일정으로 처리합니다.

| 라운드 | 실행 시점 |
|---|---|
| 1/4 | 즉시 |
| 2/4 | 첫 실패 후 5초 |
| 3/4 | 두 번째 실패 후 15초 |
| 4/4 | 세 번째 실패 후 30초 |

- 호스트 키 사전점검과 인증 후 백업의 시도 횟수는 별도로 계산합니다. 화면에는
  `키 N/4 · 백업 N/4`로 표시됩니다.
- 대기 대상은 작업 스레드를 점유하지 않고 지연 큐로 돌아갑니다. 따라서 한
  장비가 `재시도 대기` 상태여도 정상 장비의 연결·백업·저장은 계속됩니다.
- 인증 실패, 호스트 키 변경, 미지원 모델처럼 영구적인 오류는 즉시 일반
  실패로 끝나며 재시도하지 않습니다.
- transient 오류가 4번째에도 발생하면 원래 오류 코드는 보존하면서 상태를
  `retry_exhausted`로 확정합니다. 화면에는 `재시도 소진`으로 표시합니다.
- 실행 종료 후 자동으로 추가 재시도하지 않습니다. **접속 실패 장비만 다시
  시도**는 직전 결과가 `retry_exhausted`인 IP만 대상으로 하며, 세션 전용
  암호를 다시 받아 완전히 새로운 실행 폴더에 결과를 기록합니다.

## 결과 파일

기본 저장 위치는 다음과 같습니다.

```text
%USERPROFILE%\Documents\Aruba2930FConfigBackup\backup\YYYY-MM-DD\HHmmss\
├── <hostname>.txt
├── <hostname>-<ip>.txt       # 호스트명이 중복될 때
├── <ip>.txt                  # 호스트명 탐지 실패 시
├── operation.jsonl           # 민감정보를 제거한 단계/오류 진단 로그
└── result.xlsx
```

설정 파일은 UTF-8과 Windows 줄바꿈으로 저장되고 SHA-256이 계산됩니다.
`result.xlsx`에는 `Summary`, `Devices` 시트가 있으며 장비 주소, 탐지된
호스트명과 모델/SKU, 결과 상태, Host Key Attempts, Backup Attempts,
Total Connection Attempts, 소요시간, 파일 경로/해시 및 민감정보를 제거한
오류 정보가 들어갑니다. Summary는 성공, Retry Exhausted, 기타 실패와 취소를
별도로 집계합니다.

승인한 호스트 키만 다음 사용자별 로컬 파일에 보관됩니다.

```text
%LOCALAPPDATA%\Aruba2930FConfigBackup\known_hosts.json
```

장비 IP 목록, SSH 암호, Enable 암호, 원본 명령 출력은 `operation.jsonl`이나
설정 파일에 저장하지 않습니다. 진단 로그에는 단계, 시도 횟수, 오류 코드와
IP/자격증명을 제거한 짧은 설명만 기록합니다. 결과 TXT에는 장비 설정 자체가
포함되므로 조직의 민감정보 보관 정책에 따라 보호하십시오.

## 오류 코드

| 코드 | 의미 |
|---|---|
| `INPUT_INVALID` | IP, 포트, 동시 작업 수 등 입력이 잘못됨 |
| `HOST_KEY_REJECTED` | 사용자가 새 호스트 키를 승인하지 않음 |
| `HOST_KEY_CHANGED` | 저장된 호스트 키와 현재 키가 다름 |
| `TCP_TIMEOUT` | 장비 TCP 연결 시간 초과 |
| `SSH_NEGOTIATION_FAILED` | SSH 협상 또는 프로토콜 오류 |
| `AUTH_FAILED` | SSH 인증 실패 |
| `ENABLE_FAILED` | Enable 전환 실패 |
| `PAGING_SETUP_FAILED` | `no page` 적용 또는 검증 실패 |
| `MODEL_UNSUPPORTED` | 지원 대상 Aruba 2930F가 아님 |
| `COMMAND_TIMEOUT` | 명령 완료 시간 초과 |
| `COMMAND_REJECTED` | 장비가 읽기 명령을 거부함 |
| `PROMPT_PARSE_FAILED` | CLI 프롬프트를 안전하게 판별하지 못함 |
| `OUTPUT_LIMIT_EXCEEDED` | 수집 출력의 시간/크기/행 제한 초과 |
| `REPORT_WRITE_FAILED` | TXT 또는 Excel 결과 저장 실패 |
| `CANCELLED` | 사용자가 작업을 취소함 |
| `UNEXPECTED_ERROR` | 분류되지 않은 내부 오류 |

`retry_exhausted`는 별도 오류 코드가 아니라 재시도 종료 상태입니다. 마지막
transient 오류 코드가 함께 남으므로 실패 원인을 잃지 않습니다. 인증, 호스트
키 변경, 모델 오류는 재시도하지 않습니다.

## v0.1.1 범위 밖

- 예약 실행 및 서비스/에이전트 모드
- Excel/CSV 장비 목록 가져오기
- 장비별 계정, SSH 개인 키 인증, IPv6 또는 DNS 호스트명 대상
- 설정 비교·변경 탐지·복원
- 장비 설정 변경 명령
- Aruba 2930F 이외 모델 지원

## 소스에서 실행 및 검증

Windows x64와 CPython 3.14가 필요합니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install `
  -r .\requirements-lock.txt -r .\requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m aruba2930f_backup
```

저장소 검증:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\validate.ps1 `
  -PythonPath .\.venv\Scripts\python.exe
```

portable 패키지 빌드:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1 `
  -PythonPath C:\Python314\python.exe -Version 0.1.1
```

빌드는 `artifacts\release` 아래에 ZIP, SHA-256, CycloneDX SBOM을 만들고
패키지 구조, x64 PE 헤더, 버전, EXE 오프스크린 smoke를 검증합니다. 이
검증도 실제 장비 접속 증거는 아닙니다. 기본 빌드는 Git의 추적·미추적 변경이
없는 작업 트리만 허용합니다. 개발 중 패키징 경로만 점검할 때는 명시적으로
`-AllowDirty`를 사용할 수 있지만, 이 산출물은 `dirtyTree: true`로 표시되어
정식 릴리즈에 사용할 수 없습니다.

## English summary

Aruba 2930F Config Backup is a read-only Windows GUI that collects
`show running-config` from multiple ArubaOS-Switch 2930F devices over SSH. It
verifies SSH host-key fingerprints, applies and validates `no page` before any
`show` command on every connection, validates the model, and writes per-device
UTF-8 text backups plus an Excel run report. Credentials and the device list are
session-only. Transient failures use four non-blocking rounds (immediate, then
5/15/30-second delays); exhausted devices can be manually rerun into a new run
folder after credentials are re-entered. v0.1.1 is an unsigned prerelease
validated with mocks and local package checks, not with a live switch.

## 라이선스와 보안 제보

MIT License로 배포됩니다. 취약점은 공개 Issue에 장비 주소, 설정 또는
자격증명을 올리지 말고 [SECURITY.md](SECURITY.md)의 절차로 제보하십시오.

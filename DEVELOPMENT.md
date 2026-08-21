# 개발 및 유지관리 가이드

이 문서는 Aruba 2930F 설정 백업 프로젝트의 개발 환경, 테스트 방법, 운영 안전 경계를 정리합니다.

## 개발 환경

- Windows x64
- Python 3.14
- `src/` 패키지 구조
- PySide6 GUI
- Netmiko / Paramiko 기반 SSH 처리

가상환경 구성:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install `
  -r .\requirements-lock.txt -r .\requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

프로그램 실행:

```powershell
.\.venv\Scripts\python.exe -m aruba2930f_backup
```

## 테스트

전체 테스트:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

저장소 전체 검증:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\validate.ps1 `
  -PythonPath .\.venv\Scripts\python.exe
```

검증 항목에는 다음이 포함됩니다.

- 안전 규칙 검사
- 코드 스타일 검사
- 타입 검사
- 단위/통합 테스트
- 테스트 커버리지
- 의존성 취약점 감사
- 릴리즈 관련 회귀 테스트

## Windows 배포 패키지

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1 `
  -PythonPath C:\Python314\python.exe -Version 0.1.8
```

빌드 결과에는 다음이 포함됩니다.

- Windows x64 portable ZIP
- ZIP SHA-256
- CycloneDX SBOM
- `BUILD_INFO.json`

정식 릴리즈 빌드는 Git 작업 트리에 추적/미추적 변경이 없는 상태에서 수행합니다.

## 변경 시 유지해야 할 운영 안전 경계

### 자격증명

- SSH 암호와 Enable 암호는 실행 세션에서만 사용합니다.
- 자격증명을 로그, 설정 파일, 테스트 fixture에 저장하지 않습니다.
- 실제 장비 주소, hostname, 설정 원문도 테스트 데이터에 포함하지 않습니다.

### SSH 장비 지문

- 최초 연결 시 운영자가 SSH 장비 지문을 확인할 수 있어야 합니다.
- 승인된 장비 지문이 달라지면 연결을 중단합니다.
- 지문 변경을 자동 승인하는 동작을 추가하지 않습니다.

### CLI 명령 순서

매 연결에서 다음 원칙을 유지합니다.

1. SSH 장비 지문 확인
2. EXEC 프롬프트 확인
3. `no page` 적용 및 검증
4. `show version`
5. `show modules`
6. Aruba 2930F 계열 확인
7. `show running-config`
8. 최종 프롬프트와 출력 검증

`no page`가 정상 적용되지 않으면 이후 `show` 명령을 보내지 않습니다.

### 읽기 전용 원칙

- 설정 모드 진입 금지
- 구성 변경 명령 추가 금지
- 실제 장비를 대상으로 한 자동 변경 테스트 금지
- 모델 확인 전에 `show running-config` 실행 금지

## 테스트 설계 원칙

SSH, 파서, 파일 시스템 경계는 실제 운영망 없이 재현할 수 있어야 합니다.

테스트 fixture에는 다음 정보를 사용하지 않습니다.

- 실제 고객/사내 IP 주소
- 실제 hostname
- 실제 계정/암호
- 실제 SSH 장비 지문
- 실제 running-config

문서와 테스트 예시는 RFC 5737 문서용 주소 대역 등 비운영 데이터를 사용합니다.

## 릴리즈 원칙

새 릴리즈는 단순 문서 수정이나 작은 내부 정리만으로 만들지 않습니다.

다음 조건을 기본으로 합니다.

- 의미 있는 운영 기능 또는 버그 수정 단위
- 전체 CI 통과
- Windows 패키지 검증 통과
- 릴리즈 ZIP SHA-256 검증
- SBOM 생성
- 병합된 기본 브랜치의 명시적 버전 태그

자세한 기준은 [docs/RELEASE_POLICY.md](docs/RELEASE_POLICY.md)를 참고하십시오.

## 의존성 업데이트

의존성은 자동 제안만으로 즉시 병합하지 않습니다.

특히 다음 변경은 회귀 검증 후 반영합니다.

- Netmiko / Paramiko: SSH 프롬프트 및 레거시 알고리즘 호환성
- PySide6 / Shiboken6: Windows GUI 및 패키징 호환성
- GitHub Actions: 릴리즈 출처 검증과 artifact 동작

여러 관련 패키지는 가능한 한 하나의 호환성 검증 단위로 묶어 업데이트합니다.

## 문서용 화면 생성

`tools/render_docs_screenshots.py`는 실제 운영망 접속 없이 `MainWindow`에 문서용 가상 데이터를 넣어 README용 화면을 생성합니다.

```powershell
python .\tools\render_docs_screenshots.py --output .\artifacts\docs-screenshots
```

이 화면에는 실제 IP, 계정, 암호, SSH 장비 지문, 설정 정보가 포함되지 않아야 합니다.

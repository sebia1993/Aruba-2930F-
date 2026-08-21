# 오류 코드

오류 코드는 장비별 실패 원인을 빠르게 분류하기 위한 값입니다. 실제 비밀번호나 장비 설정 원문을 코드 안에 넣지 않습니다.

| 코드 | 의미 | 1차 확인 |
|---|---|---|
| `INPUT_INVALID` | 입력값 오류 | IP, 포트, 동시 작업 수 확인 |
| `HOST_KEY_REJECTED` | 새 SSH 장비 지문을 승인하지 않음 | 장비 지문을 운영 기록과 대조 |
| `HOST_KEY_CHANGED` | 기존 승인 지문과 현재 지문이 다름 | 장비 교체/키 재생성/IP 변경 확인 |
| `TCP_TIMEOUT` | TCP 연결 시간 초과 | Ping, 경로, ACL, SSH 서비스 확인 |
| `SSH_ALGORITHM_INCOMPATIBLE` | 공통 SSH 알고리즘 없음 | `show ip ssh`, OS 버전, 지원 알고리즘 확인 |
| `SSH_NEGOTIATION_FAILED` | SSH 협상/프로토콜 실패 | SSH 서비스 상태와 중간 장비 확인 |
| `AUTH_FAILED` | SSH 인증 실패 | 계정/암호/AAA 정책 확인 |
| `ENABLE_FAILED` | Enable 전환 실패 | Enable 암호와 권한 확인 |
| `PAGING_SETUP_FAILED` | `no page` 적용/검증 실패 | CLI 권한, 명령 지원 여부 확인 |
| `MODEL_UNSUPPORTED` | 2930F 확인 실패 또는 모델 증거 충돌 | `show version`, `show modules` 수동 확인 |
| `COMMAND_TIMEOUT` | 명령 완료 시간 초과 | 장비 부하, SSH 지연, 출력량 확인 |
| `COMMAND_REJECTED` | 장비가 명령을 거부 | 계정 권한과 CLI 오류 확인 |
| `PROMPT_PARSE_FAILED` | EXEC 프롬프트 판별 실패 | 로그인 배너/프롬프트 형식 수동 확인 |
| `OUTPUT_LIMIT_EXCEEDED` | 시간/크기/행 제한 초과 | 비정상 출력 또는 설정 크기 확인 |
| `REPORT_WRITE_FAILED` | TXT/Excel 저장 실패 | 폴더 권한, 디스크, 파일 잠금 확인 |
| `CANCELLED` | 사용자가 취소 | 의도된 취소인지 확인 |
| `UNEXPECTED_ERROR` | 분류되지 않은 내부 오류 | 진단 코드와 비민감 로그 확인 |

## 재시도 소진

`retry_exhausted`는 오류 코드가 아니라 **재시도 종료 상태**입니다.

예를 들어 `TCP_TIMEOUT`이 네 번 반복되었다면 최종 상태는 재시도 소진으로 표시하면서 원래 `TCP_TIMEOUT` 원인도 유지합니다.

다음 유형은 기본적으로 재시도 대상이 아닙니다.

- 인증 실패
- SSH 장비 지문 변경
- 지원하지 않는 모델
- 명확한 권한/명령 거부

## 오프라인 진단 코드

일부 실패는 다음과 같은 15자 진단 코드로 요약됩니다.

```text
A3F1-XXXXXXXX-C
```

이 코드에는 앱 버전, 처리 단계, 고정 오류 분류, 시도 횟수와 짧은 세부 분류만 포함합니다.

포함하지 않는 값:

- IP
- 포트
- 호스트명
- 계정
- 암호
- 파일 경로
- 장비 설정 원문
- 원본 오류 메시지

진단 코드는 암호화나 전자서명이 아니라 **비민감 오류 분류용 코드**입니다.

유지관리자용 해석 예:

```powershell
aruba2930f-diagnose A3F1-010EPMRC-3
aruba2930f-diagnose --json A3F1-010EPMRC-3 A3F1-010C8W18-V
```

## 문제 제보 시 권장 정보

가능하면 다음 정보만 전달합니다.

- 프로그램 버전
- 오류 코드
- 오프라인 진단 코드
- 장비 모델/SKU(민감하지 않은 경우)
- ArubaOS-Switch 버전
- 문제가 발생한 단계
- 재현 여부

장비 IP, 계정, 암호, 전체 `running-config`는 공개 Issue에 올리지 마십시오.

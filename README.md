# 지원사업 공고 텔레그램 봇

기업마당, 판판대로, 소상공인24 공고를 모아 회사 적합도를 판단하고 텔레그램으로 알립니다. GitHub Actions가 매일 08:00, 18:00 KST에 실행되며 수동 실행도 지원합니다.

이 버전은 [djfksjd/sole-search](https://github.com/djfksjd/sole-search)의 전수 수집, 수집 범위 공개, 상태 변경 감지, 근거 중심 판정 아이디어를 현재 봇 구조에 맞게 적용했습니다.

## 달라진 점

- 소상공인24 자체 공고와 통합 공고를 마지막 페이지까지 수집합니다.
- 기업마당, 판판대로, 소상공인24 간 동일 공고를 공고 ID 기준으로 합칩니다.
- 실행마다 출처별 `success`, `partial`, `failed`, `disabled` 상태와 수집 건수를 기록합니다.
- 수집 장애가 나면 GitHub Action 자체가 성공하더라도 텔레그램과 Action Summary에 누락 경고를 남깁니다.
- 신규 공고뿐 아니라 제목, 상태, 접수기간, 기관, 내용이 바뀐 공고도 `[변경: ...]`으로 다시 알립니다.
- 회사의 차량방호·출입통제 제품, 제조·R&D, 시험인증, 공공조달·산업재 수출에 대한 구체적인 효익을 먼저 평가합니다. 관련성이 높은 공고부터 변경 여부와 마감 순으로 검토합니다.
- 신청기업 소재지 제한은 본사 안양·공장 화성과 비교하며, 행사 개최지를 자격 제한으로 오인하지 않습니다. 이름으로 성별·연령을 추정하거나 과거 매출을 현재 자격으로 단정하지 않습니다.
- 상세 자료나 판정을 확인하지 못한 항목은 추천에 섞지 않고 재검토 대기열에 보존합니다. 같은 자료를 매회 반복 심사하지 않으며, 자료·회사 정보가 바뀌면 다시 검토합니다.
- 텔레그램 제한을 넘는 결과는 공고 단위로 여러 메시지에 나눠 보내므로 뒤쪽 결과가 사라지지 않습니다.
- LLM 판정은 등급 A/B/C와 별도로 `확인됨`, `조건부`, `확인 필요`, `신청 불가`, `사업전환 후보` 중 하나를 표시합니다.
- 첨부파일 본문은 아직 자동 검증하지 않습니다. 웹 본문만으로 필수 요건이 끝까지 확인되지 않으면 `확인됨` 대신 `확인 필요`로 보수적으로 판정합니다.

## 수집 출처

| 출처 | 범위 | 장애 시 동작 |
|---|---|---|
| 기업마당 지원사업 | 전체 조회 모드, 페이지당 기본 100건 | 전체건수·고유 ID와 실제 종료 사유로 완료 판정 |
| 기업마당 행사 | 전체 조회 모드, 페이지당 기본 100건 | 전체건수·고유 ID와 실제 종료 사유로 완료 판정 |
| 판판대로 | 지원사업 공고, 기본 최대 5페이지 | 0건이면 경고, 비활성화 가능 |
| 소상공인24 | 자체 공고 전체 | 서버 고지 건수와 실제 수집 건수 비교 |
| 소상공인24 통합 | 타 기관 연계 공고 전체 | 서버 고지 건수와 실제 수집 건수 비교 |

## GitHub Secrets

저장소의 `Settings > Secrets and variables > Actions`에 등록합니다.

### 필수

| 이름 | 설명 |
|---|---|
| `BIZINFO_SUPPORT_KEY` | 기업마당 지원사업 API 인증키 |
| `BIZINFO_EVENT_KEY` | 기업마당 행사 API 인증키 |
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급한 봇 토큰 |
| `TELEGRAM_ALLOWED_CHAT_ID` | 알림을 받을 채팅 ID |
| `STATE_ENCRYPTION_KEY` | 영속 상태를 암호화하는 Fernet 키. 기존 상태가 있으면 임의 교체 금지 |

### 선택

| 이름 | 설명 | 기본값 |
|---|---|---|
| `GEMINI_API_KEY` | 상세 적합도 판정. 없으면 키워드 방식으로 대체 | 없음 |
| `BIZINFO_PAGE_UNIT` | 기업마당 페이지당 요청 건수 | `100` |
| `BIZINFO_SEARCH_COUNT` | 이전 환경설정 호환용 페이지 크기 별칭 | `100` |
| `BIZINFO_MAX_PAGES` | 기업마당 수집 안전 상한. 도달하면 부분 수집 표시 | `100` |
| `FANFANDAERO_ENABLED` | 판판대로 사용 여부 | `true` |
| `FANFANDAERO_PAGE_UNIT` | 판판대로 페이지당 요청 건수 | `100` |
| `FANFANDAERO_MAX_PAGES` | 판판대로 최대 페이지 수 | `5` |
| `SBIZ24_ENABLED` | 소상공인24 사용 여부 | `true` |
| `SBIZ24_PAGE_SIZE` | 소상공인24 페이지당 요청 건수, 최대 200 | `100` |
| `SBIZ24_REQUEST_DELAY` | 소상공인24 페이지 사이 대기 시간, 최소 0.5초 | `0.5` |
| `MAX_PROGRAMS_PER_RUN` | 한 실행에서 상세 판정할 신규 공고 수, 변경 공고는 항상 우선 | `40` |
| `PROFILE_REGIONS` | 키워드 대체 판정의 허용 지역 JSON | `["전국"]` |
| `PROFILE_INTERESTS` | 키워드 대체 판정의 관심 분야 JSON | `[]` |
| `PROFILE_KEYWORDS` | 키워드 대체 판정의 포함 키워드 JSON | `[]` |
| `PROFILE_EXCLUDES` | 키워드 대체 판정의 제외 키워드 JSON | `[]` |
| `PROFILE_MIN_SCORE` | 키워드 대체 판정의 최소 점수 | `60` |

LLM 판정용 회사 정보는 현재 `src/company_profile.py`의 `COMPANY_PROFILE`을 사용합니다. 회사 정보가 바뀌면 이 파일도 함께 갱신해야 합니다.

사업 분야와 본사·공장 소재지는 [싸이베리어 공식 소개](https://cybarrier.com/)에 맞춥니다. 현재 매출·고용인원·유효 인증·특수 기업자격 등 증빙이 필요한 조건은 별도 확인 사항입니다.

기업마당은 [지원사업 API 명세](https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi)와 [행사 API 명세](https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoEventApi)에 따라 `searchCnt=0`, `pageUnit`, `pageIndex`를 사용합니다. 한 번에 100건을 받았다는 사실만으로 제한이라고 표시하지 않습니다. 총수 변경·반복 페이지·실제 오류·페이지 또는 시간 안전 상한에서는 미확인 범위를 표시합니다.

## 상태 파일

다음 파일을 암호화해 `bot-state` 브랜치에 보존합니다. 만료될 수 있는 GitHub Actions 캐시를 발송 이력의 원본으로 사용하지 않습니다. 복원 실패·키 불일치·필수 파일 손상 시 빈 상태로 발송을 시작하지 않고 실패로 표시합니다.

- `data/notified_keys.json`: 이미 확인한 공고 키
- `data/program_state.json`: 변경 감지를 위한 직전 공고 스냅샷
- `data/decisions.jsonl`: 필터와 LLM 판정 로그
- `data/bot.db`: 정규화된 공고와 수집 실행 기록
- `data/recovery_state.json`: 복구 기준일과 발송 여부를 확인하지 못한 보류 항목

출처가 `partial` 또는 `failed`이면 해당 출처의 이전 스냅샷을 보존해, 일시 장애 뒤 공고 전체가 신규로 오인되는 것을 막습니다.

### 중단 후 복구와 검증

추천 품질 검수용 `preview` 모드는 최대 8건을 AI로 심사해 미리보기만 저장합니다. 운영 상태의 임시 복사본을 사용하고 메시지는 보내지 않습니다. `probe` 모드는 기업마당 페이지 설정을 두 페이지까지만 비교하며 발송·AI·상태 저장을 수행하지 않습니다.

워크플로 수동 실행의 `mode`는 `run`(일반 알림), `verify`(무발송 검사), `recover`(상태가 완전히 없을 때 한 번만 초기 복구)입니다. `recover`에는 마지막 정상 실행 시각을 `recovery_since`에 ISO 형식으로 입력합니다. 기존 원격 상태가 있으면 초기화 저장을 거절합니다.

초기 복구는 수집만 수행하고 AI 판정이나 Telegram 발송을 하지 않습니다. 기준일 이후 게시됐고 아직 종료되지 않은 신규 공고부터 기존 선별·회차별 처리 규칙을 적용합니다. 이전 공고와 게시일 불명 항목은 미확인 보류로 남기며 발송 성공으로 기록하지 않습니다. 이후 실제 내용이 바뀐 공고는 기존 변경 알림 규칙을 따릅니다. 수집 범위의 `partial` 경고는 복구 후에도 숨기지 않습니다.

복구 후 `verify`로 복원·수집·선별을 확인하고 저장소 변수 `BOT_STATE_READY=true`로 예약 실행을 엽니다. `verify`는 복원 상태를 임시 복사해 사용하며 운영 이력·AI·Telegram에 영향을 주지 않습니다. 예약은 08:00·18:00 KST를 유지합니다. 장기 비활동으로 GitHub가 예약을 중지하면 워크플로를 재활성화해야 하며, 별도 운영 감시기에서는 `disabled_inactivity`만 자동 해제하고 수동 중지는 존중합니다.

## 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m pytest -q
python -m src.run_once
```

`python -m src.run_once`는 GitHub Actions와 같은 1회 실행 경로입니다. 텔레그램 키가 없으면 수집과 coverage 로그까지만 확인하고 메시지는 보내지 않습니다.

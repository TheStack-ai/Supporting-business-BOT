# Supporting-business-BOT v2 — LLM 기반 적격성 판단 재설계

## 배경

현재 봇은 키워드 매칭 기반 스코어링으로 bizinfo.go.kr 지원사업을 필터링한다. 문제:
- 키워드가 넓으면 노이즈(식품/농수산/뷰티 등)가 90% 이상
- 키워드가 좁으면 실제 해당 지원사업을 놓침
- 업종코드/매출규모/종업원수/소재지 등 자격요건을 확인하지 않음
- 모든 공고를 동일 형식으로 나열하여 중요도 구분 불가

## 목표

1. CyBarrier가 실제 지원 가능한 사업을 놓치지 않는다 (높은 재현율)
2. 무관한 공고를 제거한다 (높은 정밀도)
3. 등급별 분류로 의사결정 시간을 줄인다

## 회사 프로필 (LLM 컨텍스트)

```
회사명: (주)싸이베리어 / CyBarrier Co Ltd
대표: 박채영
업종코드: 전기경보·신호장치 제조(28901), 금속구조물 제조(25112),
         기타 전자부품 제조(26422), 기타 산업용 전기장비 제조(28123),
         특수목적용 자동차 제조(30203)
업태: 전기경보·신호장치 제조, 금속구조물 제조, 건물설비설치공사, 패키지SW 개발
제품: 차량방호 볼라드, 대테러 차량차단기(KC), 출입통제시스템(KC),
     ACPCS 차량방호 제어시스템, 교통통제 안전펜스, 디자인 울타리
인증: KC(차량차단기, 출입통제소 서버), UL508A, ISO 9001/14001/45001, IMSA
기업유형: 소기업(소상공인), 벤처기업(혁신성장유형)
소재지: 본사 경기 안양시 동안구, 공장 경기 화성시 양감면
상시종업원: 6명
매출: 18.34억원 (2022)
면허: 금속·창호·지붕·건축물조립공사업
등록: 직접생산확인(볼라드, 출입통제시스템, 차량차단기, 디자인형울타리),
     소프트웨어사업자, 자동차제작자등록(E65, 특수용도 완성차)
```

## 아키텍처

### 파이프라인 흐름

```
bizinfo API (support + event)
    │
    ▼
[수집] BizinfoClient.fetch → normalize → DB upsert
    │  (기존 코드 유지)
    ▼
[중복 제거] notified_keys.json과 비교 → 이미 알린 공고 제외
    │  신규 공고만 다음 단계로
    ▼
[Stage 1] LLM 빠른 분류 (Gemini Flash)
    │  입력: title + summary_raw (정규화된 dict 키)
    │  판단: PASS / REJECT
    │  원칙: 확실히 무관한 것만 REJECT, 애매하면 PASS
    │  배치: 10건씩 묶어 1회 호출
    │  예상 통과율: 20~30%
    ▼
[Stage 2] 상세 판단 (상세페이지 크롤링 + Gemini Flash)
    │  입력: 상세페이지 텍스트(최대 8000자) + 회사 프로필
    │  출력: Assessment(grade, reason, eligibility)
    │  A = 반드시 검토 (자격 충족, 직접 관련)
    │  B = 참고 (일부 관련 또는 자격 미확인)
    │  C = 무관 (자격 미충족 또는 무관)
    ▼
[알림] run_once.py 내 send_graded_notification()
    │  A등급: 즉시 개별 알림
    │  B등급: 다이제스트에 묶어서
    │  C등급: 발송 안 함
    ▼
[기록] notified_keys.json에 알린 program_key 추가
```

### 실행 스케줄

- 하루 2회: 08:00 KST, 18:00 KST
- GitHub Actions cron: `0 23,9 * * *` (UTC)

### 폴백

1. `GEMINI_API_KEY` 미설정 시 → LLM 경로 건너뛰고 즉시 키워드 폴백
2. Gemini API 호출 실패 시 → 기존 키워드 필터(filters.py)로 자동 폴백
3. 알림에 `⚠️ LLM 판단 불가, 키워드 기반 결과` 표시

## 중복 제거 (C3 해결)

### 문제

GitHub Actions는 매 실행마다 인메모리 DB를 새로 생성한다.
bizinfo API는 최근 공고 ~100건을 반환하며, 대부분은 이전 실행에서 이미 처리한 공고다.
중복 제거 없이는 같은 공고를 반복 LLM 판단 + 반복 알림하게 된다.

### 해결: GitHub Actions Cache + notified_keys.json

```python
# 파일: data/notified_keys.json
# 내용: {"keys": ["support:12345", "event:67890", ...], "updated_at": "2026-04-16T08:00:00"}
```

- 매 실행 시작: GitHub Actions cache에서 `data/notified_keys.json` 복원
- 수집 후: `new_items`에서 이미 `notified_keys`에 있는 program_key 제외
- 알림 발송 후: 알린 program_key를 `notified_keys`에 추가하고 저장
- 매 실행 종료: GitHub Actions cache에 저장 (cache key에 날짜 포함, 30일 TTL)
- Stage 1에서 REJECT된 것도 keys에 추가 (재판단 방지)
- 90일 이상 지난 키는 자동 정리 (파일 비대화 방지)

### GitHub Actions workflow 변경

```yaml
- name: Restore notification cache
  uses: actions/cache@v4
  with:
    path: data/notified_keys.json
    key: notified-${{ github.run_id }}
    restore-keys: notified-

- name: Run bot
  run: python -m src.run_once

- name: Save notification cache
  uses: actions/cache/save@v4
  with:
    path: data/notified_keys.json
    key: notified-${{ github.run_id }}
```

## 신규 컴포넌트

### 1. `src/llm_filter.py`

LLM 호출 및 Stage 1/2 판단 로직.

```python
from dataclasses import dataclass

@dataclass
class Assessment:
    grade: str   # "A", "B", "C"
    reason: str  # 한줄 사유
    eligibility: str  # "충족", "미확인", "미충족"

def stage1_quick_filter(programs: list[dict]) -> list[dict]:
    """
    title + summary_raw로 빠른 분류. 10건씩 배치.
    Gemini Flash JSON mode 사용.
    반환: PASS된 프로그램 리스트.
    
    파싱 실패 시: 해당 프로그램은 PASS로 처리 (재현율 우선).
    API 호출 실패 시: 전체 PASS 반환 (상위에서 폴백 판단).
    """

def stage2_assess(program: dict, detail_text: str) -> Assessment:
    """
    상세페이지 텍스트 + 회사 프로필로 적격성 판단.
    Gemini Flash JSON mode 사용 (structured output).
    
    반환: Assessment dataclass.
    
    grade가 A/B/C 외의 값 → B로 처리.
    응답 파싱 실패 → Assessment(grade="B", reason="LLM 판단 불가", eligibility="미확인").
    """
```

**Stage 1: Gemini Flash JSON mode**

```python
# response_mime_type="application/json" 사용
response_schema = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "decision": {"type": "string", "enum": ["PASS", "REJECT"]}
                },
                "required": ["id", "decision"]
            }
        }
    }
}
```

프롬프트:
```
당신은 정부지원사업 사전 분류기입니다.
아래 회사 프로필을 참고하여, 각 공고가 이 회사에 조금이라도 관련될 수 있는지 판단하세요.

[회사 프로필]
{COMPANY_PROFILE}

[판단 기준]
- 회사의 업종(제조업, 보안장비, 건설, SW), 규모(소기업), 소재지(경기)와
  조금이라도 접점이 있으면 PASS
- 식품, 농수산, 뷰티, 섬유, 의료/바이오, 관광 등 명백히 다른 업종만 REJECT
- 일반적 중소기업 지원(수출, 인증, 디지털전환, R&D 등)은 PASS
- 판단이 애매하면 반드시 PASS

[공고 목록]
{번호}. {title} | {summary_raw}
...
```

**Stage 2: Gemini Flash JSON mode**

```python
response_schema = {
    "type": "object",
    "properties": {
        "grade": {"type": "string", "enum": ["A", "B", "C"]},
        "reason": {"type": "string"},
        "eligibility": {"type": "string", "enum": ["충족", "미확인", "미충족"]}
    },
    "required": ["grade", "reason", "eligibility"]
}
```

프롬프트:
```
당신은 정부지원사업 적격성 심사관입니다.
아래 공고의 상세 내용을 읽고, 이 회사가 실제로 지원 가능한지 판단하세요.

[회사 프로필]
{COMPANY_PROFILE}

[공고 상세]
제목: {title}
내용:
{detail_text}

[판단 항목]
1. 업종 요건: 공고가 요구하는 업종과 회사 업종의 일치 여부
2. 규모 요건: 매출, 종업원수, 기업유형 요건 충족 여부
3. 지역 요건: 소재지 제한이 있다면 충족 여부
4. 기타 자격: 특수 인증, 경력, 사전 등록 등

등급 기준:
A = 자격 요건이 충족되고 회사 사업과 직접 관련됨
B = 일부 관련되거나 자격 요건을 공고만으로 확인할 수 없음
C = 자격 미충족이 명확하거나 사업 영역과 무관함

reason은 핵심 정보만 포함 (지원금액, 대상, 마감일 등).
```

### 2. `src/detail_crawler.py`

bizinfo 상세페이지 크롤링 및 텍스트 추출.

```python
def fetch_detail(url: str) -> str:
    """
    bizinfo 공고 상세페이지를 가져와 본문 텍스트를 추출.
    HTML 태그 제거, 불필요한 네비게이션/푸터 제거.
    자격요건/지원대상 섹션을 우선 보존.
    최대 8000자로 잘라서 반환 (자격요건이 하단에 있는 경우 대비).
    실패 시 빈 문자열 반환.
    """
```

- User-Agent: 정상 브라우저 헤더
- 요청 간 1초 딜레이 (rate limiting 방지)
- 타임아웃 10초
- 실패 시 Stage 2 건너뛰고 해당 프로그램을 B등급으로 처리:
  `Assessment(grade="B", reason="상세페이지 접근 불가", eligibility="미확인")`

### 3. `src/notified_cache.py`

중복 알림 방지용 캐시.

```python
CACHE_PATH = "data/notified_keys.json"

def load_notified_keys() -> set[str]:
    """notified_keys.json에서 이미 알린 program_key set 로드."""

def save_notified_keys(keys: set[str]) -> None:
    """program_key set을 notified_keys.json에 저장. 90일 초과 키 정리."""

def filter_new_programs(programs: list[dict], notified: set[str]) -> list[dict]:
    """이미 알린 프로그램 제외하고 신규만 반환."""
```

### 4. `src/run_once.py` 수정

현재 실제 코드 흐름:
```python
# 현재 run_once.py 실제 구조 (의사코드)
init_db()
profile = get_or_create_profile()
client = BizinfoClient(support_key, event_key)

# Phase 1: 지원사업 수집
items = client.fetch_support_programs()
new_items = []
for raw in items:
    normalized = normalize_support(raw)
    upsert_program(normalized)
    new_items.append(normalized)

# Phase 2: 행사 수집 (동일 패턴)
events = client.fetch_events()
for raw in events:
    normalized = normalize_event(raw)
    upsert_program(normalized)
    new_items.append(normalized)

# Phase 3: 필터링 + 알림
recommended = []
for item in new_items:
    ok, score, reasons = is_recommended(item, profile)
    if ok:
        recommended.append((item, score, reasons))
recommended.sort(key=lambda x: -x[1])

# Phase 4: 텔레그램 메시지 직접 구성 및 발송
msg = format_message(recommended[:15])
Bot(token).send_message(chat_id, msg, parse_mode="Markdown")
```

변경 후:
```python
init_db()
profile = get_or_create_profile()
client = BizinfoClient(support_key, event_key)

# Phase 1-2: 수집 (기존과 동일)
new_items = ingest_all(client)  # 수집 로직을 헬퍼로 추출

# Phase 2.5: 중복 제거
notified = load_notified_keys()
new_items = filter_new_programs(new_items, notified)

if not new_items:
    # 신규 없음 → 무알림 (또는 선택적 "신규 없음" 메시지)
    return

# Phase 3: LLM 파이프라인
gemini_key = os.getenv("GEMINI_API_KEY")
if not gemini_key:
    # API 키 없음 → 즉시 키워드 폴백
    _fallback_keyword_filter(new_items, profile, notified)
    return

try:
    # Stage 1: 빠른 분류
    passed = stage1_quick_filter(new_items)

    # Stage 2: 상세 판단
    assessments = []
    for p in passed:
        detail = fetch_detail(p["url"])
        assessment = stage2_assess(p, detail)
        assessments.append((p, assessment))
        log_decision(p, assessment)  # 관찰용 로그

    grade_a = [(p, a) for p, a in assessments if a.grade == "A"]
    grade_b = [(p, a) for p, a in assessments if a.grade == "B"]

    send_graded_notification(grade_a, grade_b)

    # 모든 처리된 프로그램을 캐시에 기록 (A/B/C/REJECT 모두)
    all_processed = {p["program_key"] for p in new_items}
    save_notified_keys(notified | all_processed)

except Exception as e:
    logger.error(f"LLM pipeline failed: {e}")
    _fallback_keyword_filter(new_items, profile, notified)


def _fallback_keyword_filter(items, profile, notified):
    """기존 키워드 필터로 폴백. 알림에 경고 표시."""
    recommended = []
    for item in items:
        ok, score, reasons = is_recommended(item, profile)
        if ok:
            recommended.append((item, score, reasons))
    recommended.sort(key=lambda x: -x[1])
    msg = "⚠️ LLM 판단 불가, 키워드 기반 결과\n\n" + format_legacy_message(recommended[:15])
    Bot(token).send_message(chat_id, msg, parse_mode="Markdown")
    save_notified_keys(notified | {i["program_key"] for i in items})
```

### 5. `src/decision_log.py`

LLM 판단 결과 로깅 (프롬프트 튜닝용).

```python
def log_decision(program: dict, assessment: Assessment) -> None:
    """
    판단 결과를 data/decisions.jsonl에 한 줄씩 기록.
    형식: {"ts": "...", "key": "...", "title": "...", "grade": "...", "reason": "..."}
    GitHub Actions 아티팩트로 보존 가능.
    """
```

### 6. 알림 형식 (`run_once.py` 내 `send_graded_notification()`)

알림 포맷 함수는 `run_once.py` 내에 구현한다 (`telegram_bot.py`는 인터랙티브 모드 전용이므로 분리).

**A등급이 있을 때:**
```
🔴 반드시 검토 (N건)

1. {제목}
   → {사유} 마감 D-{N}
   🔗 {url}

2. ...

🟡 참고 (M건)

1. {제목}
   → {사유}
   🔗 {url}

...
```

**A등급 없이 B등급만 있을 때:**
```
🟡 참고 사항 (M건)

1. {제목}
   → {사유}
   🔗 {url}

...
```

**아무것도 없을 때 (신규 공고는 있었으나 모두 C등급):**
```
✅ 신규 해당 공고 없음 ({총 수집}건 검토, {Stage1 통과}건 상세 판단)
```

## 변경 파일 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `src/llm_filter.py` | 신규 | Gemini Flash 호출, Stage 1/2 로직, Assessment dataclass |
| `src/detail_crawler.py` | 신규 | bizinfo 상세페이지 크롤링, 텍스트 추출 (8000자) |
| `src/company_profile.py` | 신규 | 회사 프로필 상수 + 프롬프트 템플릿 |
| `src/notified_cache.py` | 신규 | notified_keys.json 읽기/쓰기, 중복 제거 |
| `src/decision_log.py` | 신규 | LLM 판단 결과 JSONL 로깅 |
| `src/run_once.py` | 수정 | LLM 파이프라인 호출, 폴백, 등급별 알림 포맷 |
| `.github/workflows/schedule.yml` | 수정 | cron 4회→2회, GEMINI_API_KEY, cache 스텝 추가 |
| `requirements.txt` | 수정 | `google-genai` 추가 |
| `src/filters.py` | 유지 | 폴백용, 수정 없음 |
| `src/bizinfo_client.py` | 유지 | 수정 없음 |
| `src/normalizer.py` | 유지 | 수정 없음 |
| `src/db.py` | 유지 | 수정 없음 |
| `src/telegram_bot.py` | 유지 | 인터랙티브 모드 전용, 이번 수정 범위 밖 |

## GitHub Secrets 추가

| Secret | 용도 |
|--------|------|
| `GEMINI_API_KEY` | Gemini Flash API 인증 |

기존 `PROFILE_*` 시크릿은 폴백용으로 유지.

## 비용 추정

| 항목 | 회당 | 일 2회 | 월 |
|------|------|--------|-----|
| Stage 1 (배치 5회 × ~1K 토큰) | ~5K tokens | ~10K | ~300K |
| Stage 2 (20건 × ~4K 토큰, 8000자 상세) | ~80K tokens | ~160K | ~4.8M |
| **합계** | ~85K | ~170K | ~5.1M |
| **Gemini Flash 비용** | | | **~$0.40/월** |

## 테스트 계획

1. **Stage 1 정확도**: 과거 알림 데이터(~500건)로 PASS/REJECT 정확도 측정. 기대: known-relevant 공고 100% PASS
2. **Stage 2 정확도**: "정보보호 벤처나라", "세종 사이버보안" 등 known-relevant 공고로 A등급 판정 확인
3. **폴백 동작**: GEMINI_API_KEY 미설정 시 키워드 폴백 확인
4. **상세페이지 크롤링**: bizinfo URL 10개 샘플로 텍스트 추출 품질 확인 (자격요건 섹션 포함 여부)
5. **중복 제거**: 동일 프로그램이 연속 2회 실행에서 1번만 알림되는지 확인
6. **알림 형식**: A/B/C 혼합 시나리오로 메시지 포맷 확인
7. **rate limiting**: 연속 크롤링 20건 시 차단 여부 확인
8. **JSON mode 파싱**: Gemini structured output이 스키마 대로 오는지 확인, 이상 응답 시 폴백 동작

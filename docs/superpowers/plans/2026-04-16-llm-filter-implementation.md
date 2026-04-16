# LLM Filter Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace keyword-matching scorer with 2-stage Gemini Flash LLM pipeline that assesses CyBarrier's actual eligibility for government support programs.

**Architecture:** Stage 1 batch-filters programs by title/summary (PASS/REJECT). Stage 2 crawls bizinfo detail pages and does full eligibility assessment (A/B/C grading). Deduplication via notified_keys.json cached across GitHub Actions runs. Falls back to existing keyword filter on Gemini failure.

**Tech Stack:** Python 3.11, google-genai (Gemini Flash), requests, python-telegram-bot, GitHub Actions cache

**Spec:** `docs/superpowers/specs/2026-04-16-llm-filter-redesign.md`

---

## File Structure

| File | Type | Responsibility |
|------|------|---------------|
| `src/company_profile.py` | Create | Company profile constant + prompt templates |
| `src/notified_cache.py` | Create | Load/save notified_keys.json, dedup filter |
| `src/detail_crawler.py` | Create | Fetch+parse bizinfo detail pages to text |
| `src/llm_filter.py` | Create | Gemini Flash calls, Stage 1 batch filter, Stage 2 assessment |
| `src/decision_log.py` | Create | JSONL logging of LLM decisions |
| `src/run_once.py` | Modify | Wire LLM pipeline, graded notifications, fallback |
| `.github/workflows/schedule.yml` | Modify | Cron 2x/day, cache steps, GEMINI_API_KEY |
| `requirements.txt` | Modify | Add google-genai |
| `tests/test_notified_cache.py` | Create | Tests for cache load/save/dedup |
| `tests/test_detail_crawler.py` | Create | Tests for HTML→text extraction |
| `tests/test_llm_filter.py` | Create | Tests for prompt building, response parsing |
| `tests/test_run_once_v2.py` | Create | Integration tests for full pipeline |

---

## Chunk 1: Foundation (company_profile, notified_cache, detail_crawler)

### Task 1: Company Profile + Prompt Templates

**Files:**
- Create: `src/company_profile.py`
- Test: `tests/test_company_profile.py`

- [ ] **Step 1: Write test for profile constant and prompt builders**

```python
# tests/test_company_profile.py
from src.company_profile import COMPANY_PROFILE, build_stage1_prompt, build_stage2_prompt

def test_company_profile_contains_key_info():
    assert "싸이베리어" in COMPANY_PROFILE
    assert "28901" in COMPANY_PROFILE
    assert "안양" in COMPANY_PROFILE
    assert "6명" in COMPANY_PROFILE

def test_build_stage1_prompt():
    programs = [
        {"title": "중소기업 수출 지원", "summary_raw": "수출 바우처"},
        {"title": "수산식품 인증", "summary_raw": "수산물 가공"},
    ]
    prompt = build_stage1_prompt(programs)
    assert "싸이베리어" in prompt
    assert "1. 중소기업 수출 지원 | 수출 바우처" in prompt
    assert "2. 수산식품 인증 | 수산물 가공" in prompt
    assert "PASS" in prompt and "REJECT" in prompt

def test_build_stage2_prompt():
    program = {"title": "정보보호 인증제품 추천"}
    detail_text = "정보보호 인증을 보유한 중소기업 대상..."
    prompt = build_stage2_prompt(program, detail_text)
    assert "싸이베리어" in prompt
    assert "정보보호 인증제품 추천" in prompt
    assert "정보보호 인증을 보유한 중소기업 대상" in prompt

def test_stage1_prompt_handles_none_summary():
    programs = [{"title": "테스트", "summary_raw": None}]
    prompt = build_stage1_prompt(programs)
    assert "1. 테스트 |" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_company_profile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.company_profile'`

- [ ] **Step 3: Implement company_profile.py**

```python
# src/company_profile.py

COMPANY_PROFILE = """회사명: (주)싸이베리어 / CyBarrier Co Ltd
대표: 박채영
업종코드: 전기경보·신호장치 제조(28901), 금속구조물 제조(25112), 기타 전자부품 제조(26422), 기타 산업용 전기장비 제조(28123), 특수목적용 자동차 제조(30203)
업태: 전기경보·신호장치 제조, 금속구조물 제조, 건물설비설치공사, 패키지SW 개발
제품: 차량방호 볼라드, 대테러 차량차단기(KC), 출입통제시스템(KC), ACPCS 차량방호 제어시스템, 교통통제 안전펜스, 디자인 울타리
인증: KC(차량차단기, 출입통제소 서버), UL508A, ISO 9001/14001/45001, IMSA
기업유형: 소기업(소상공인), 벤처기업(혁신성장유형)
소재지: 본사 경기 안양시 동안구, 공장 경기 화성시 양감면
상시종업원: 6명
매출: 18.34억원 (2022)
면허: 금속·창호·지붕·건축물조립공사업
등록: 직접생산확인(볼라드, 출입통제시스템, 차량차단기, 디자인형울타리), 소프트웨어사업자, 자동차제작자등록(E65, 특수용도 완성차)"""

_STAGE1_SYSTEM = """당신은 정부지원사업 사전 분류기입니다.
아래 회사 프로필을 참고하여, 각 공고가 이 회사에 조금이라도 관련될 수 있는지 판단하세요.

[회사 프로필]
{profile}

[판단 기준]
- 회사의 업종(제조업, 보안장비, 건설, SW), 규모(소기업), 소재지(경기)와 조금이라도 접점이 있으면 PASS
- 식품, 농수산, 뷰티, 섬유, 의료/바이오, 관광 등 명백히 다른 업종만 REJECT
- 일반적 중소기업 지원(수출, 인증, 디지털전환, R&D 등)은 PASS
- 판단이 애매하면 반드시 PASS"""

_STAGE2_SYSTEM = """당신은 정부지원사업 적격성 심사관입니다.
아래 공고의 상세 내용을 읽고, 이 회사가 실제로 지원 가능한지 판단하세요.

[회사 프로필]
{profile}

[판단 항목]
1. 업종 요건: 공고가 요구하는 업종과 회사 업종의 일치 여부
2. 규모 요건: 매출, 종업원수, 기업유형 요건 충족 여부
3. 지역 요건: 소재지 제한이 있다면 충족 여부
4. 기타 자격: 특수 인증, 경력, 사전 등록 등

등급 기준:
A = 자격 요건이 충족되고 회사 사업과 직접 관련됨
B = 일부 관련되거나 자격 요건을 공고만으로 확인할 수 없음
C = 자격 미충족이 명확하거나 사업 영역과 무관함

reason은 핵심 정보만 포함 (지원금액, 대상, 마감일 등)."""


def build_stage1_prompt(programs: list[dict]) -> str:
    lines = []
    for i, p in enumerate(programs, 1):
        title = (p.get("title") or "").strip()
        summary = (p.get("summary_raw") or "").strip()
        lines.append(f"{i}. {title} | {summary}")

    system = _STAGE1_SYSTEM.format(profile=COMPANY_PROFILE)
    listing = "\n".join(lines)
    return f"{system}\n\n[공고 목록]\n{listing}"


def build_stage2_prompt(program: dict, detail_text: str) -> str:
    title = (program.get("title") or "").strip()
    system = _STAGE2_SYSTEM.format(profile=COMPANY_PROFILE)
    return f"{system}\n\n[공고 상세]\n제목: {title}\n내용:\n{detail_text}"
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_company_profile.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add src/company_profile.py tests/test_company_profile.py
git commit -m "feat: add company profile and prompt templates"
```

---

### Task 2: Notified Cache (dedup)

**Files:**
- Create: `src/notified_cache.py`
- Test: `tests/test_notified_cache.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_notified_cache.py
import json
import os
import tempfile
from datetime import datetime, timedelta
from src.notified_cache import load_notified_keys, save_notified_keys, filter_new_programs

def test_load_empty_when_no_file():
    keys = load_notified_keys("/tmp/nonexistent_cache_test.json")
    assert keys == set()

def test_save_and_load_roundtrip():
    path = tempfile.mktemp(suffix=".json")
    try:
        save_notified_keys({"support:1", "event:2"}, path)
        loaded = load_notified_keys(path)
        assert loaded == {"support:1", "event:2"}
    finally:
        os.unlink(path)

def test_filter_new_programs():
    programs = [
        {"program_key": "support:1", "title": "A"},
        {"program_key": "support:2", "title": "B"},
        {"program_key": "support:3", "title": "C"},
    ]
    notified = {"support:1", "support:3"}
    result = filter_new_programs(programs, notified)
    assert len(result) == 1
    assert result[0]["program_key"] == "support:2"

def test_save_prunes_old_entries():
    """Keys older than PRUNE_DAYS should be pruned."""
    from src.notified_cache import PRUNE_DAYS
    path = tempfile.mktemp(suffix=".json")
    old_date = (datetime.now() - timedelta(days=PRUNE_DAYS + 10)).isoformat()
    recent_date = datetime.now().isoformat()
    data = {
        "entries": {
            "support:old": old_date,
            "support:new": recent_date,
        }
    }
    with open(path, "w") as f:
        json.dump(data, f)

    # Load, add one, save — old should be pruned
    keys = load_notified_keys(path)
    keys.add("support:added")
    save_notified_keys(keys, path)

    with open(path) as f:
        saved = json.load(f)
    assert "support:old" not in saved["entries"]
    assert "support:new" in saved["entries"]
    assert "support:added" in saved["entries"]
    os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_notified_cache.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement notified_cache.py**

```python
# src/notified_cache.py
import json
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = "data/notified_keys.json"
PRUNE_DAYS = 90


def load_notified_keys(path: str = DEFAULT_CACHE_PATH) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get("entries", {}).keys())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load notified cache: {e}")
        return set()


def save_notified_keys(keys: set[str], path: str = DEFAULT_CACHE_PATH) -> None:
    # Load existing entries to preserve timestamps
    existing_entries = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            existing_entries = data.get("entries", {})
        except (json.JSONDecodeError, OSError):
            pass

    now = datetime.now()
    cutoff = now - timedelta(days=PRUNE_DAYS)

    # Merge: keep existing timestamps, add new keys with current time
    merged = {}
    for key in keys:
        if key in existing_entries:
            ts = existing_entries[key]
            try:
                dt = datetime.fromisoformat(ts)
                if dt >= cutoff:
                    merged[key] = ts
                # else: pruned
            except (ValueError, TypeError):
                merged[key] = now.isoformat()
        else:
            merged[key] = now.isoformat()

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"entries": merged, "updated_at": now.isoformat()}, f, ensure_ascii=False)


def filter_new_programs(programs: list[dict], notified: set[str]) -> list[dict]:
    return [p for p in programs if p.get("program_key") not in notified]
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_notified_cache.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add src/notified_cache.py tests/test_notified_cache.py
git commit -m "feat: add notified cache for cross-run deduplication"
```

---

### Task 3: Detail Crawler

**Files:**
- Create: `src/detail_crawler.py`
- Test: `tests/test_detail_crawler.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_detail_crawler.py
from src.detail_crawler import extract_text_from_html, fetch_detail

def test_extract_text_strips_html():
    html = """
    <html><head><title>Test</title></head>
    <body>
    <div id="header">Navigation stuff</div>
    <div class="content">
        <h2>지원대상</h2>
        <p>중소기업 중 제조업을 영위하는 기업</p>
        <h2>지원내용</h2>
        <p>최대 1억원 지원</p>
    </div>
    <div id="footer">Copyright 2026</div>
    </body></html>
    """
    text = extract_text_from_html(html)
    assert "중소기업 중 제조업을 영위하는 기업" in text
    assert "최대 1억원 지원" in text
    assert "<div" not in text
    assert "<p>" not in text

def test_extract_text_max_length():
    html = "<html><body>" + "가" * 20000 + "</body></html>"
    text = extract_text_from_html(html)
    assert len(text) <= 8000

def test_extract_text_empty_html():
    assert extract_text_from_html("") == ""
    assert extract_text_from_html(None) == ""

def test_fetch_detail_network_error(monkeypatch):
    """Network failure should return empty string, not raise."""
    import requests as req
    def mock_get(*args, **kwargs):
        raise req.ConnectionError("mocked connection failure")
    monkeypatch.setattr(req, "get", mock_get)
    result = fetch_detail("https://www.bizinfo.go.kr/fake")
    assert result == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_detail_crawler.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement detail_crawler.py**

```python
# src/detail_crawler.py
import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 8000
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 10

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# Timestamp for rate limiting across calls
_last_request_time = 0.0


def extract_text_from_html(html: str | None) -> str:
    if not html:
        return ""

    # Remove script/style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Truncate
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]

    return text


def fetch_detail(url: str) -> str:
    global _last_request_time

    if not url or not url.startswith("http"):
        return ""

    # Rate limiting
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        _last_request_time = time.time()
        resp.raise_for_status()
        return extract_text_from_html(resp.text)
    except Exception as e:
        logger.warning(f"Failed to fetch detail page {url}: {e}")
        return ""
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_detail_crawler.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add src/detail_crawler.py tests/test_detail_crawler.py
git commit -m "feat: add detail page crawler with HTML-to-text extraction"
```

---

## Chunk 2: LLM Filter + Decision Log

### Task 4: Decision Log

**Files:**
- Create: `src/decision_log.py`
- Test: `tests/test_decision_log.py`

- [ ] **Step 1: Write test**

```python
# tests/test_decision_log.py
import json
import os
import tempfile
from src.decision_log import log_decision

def test_log_decision_creates_jsonl():
    path = tempfile.mktemp(suffix=".jsonl")
    try:
        log_decision(
            program={"program_key": "support:123", "title": "테스트 공고"},
            grade="A",
            reason="자격 충족",
            stage="stage2",
            log_path=path,
        )
        with open(path) as f:
            line = f.readline()
        entry = json.loads(line)
        assert entry["key"] == "support:123"
        assert entry["grade"] == "A"
        assert entry["reason"] == "자격 충족"
        assert entry["stage"] == "stage2"
        assert "ts" in entry
    finally:
        if os.path.exists(path):
            os.unlink(path)

def test_log_decision_appends():
    path = tempfile.mktemp(suffix=".jsonl")
    try:
        log_decision({"program_key": "s:1", "title": "A"}, "A", "ok", "stage2", path)
        log_decision({"program_key": "s:2", "title": "B"}, "C", "no", "stage2", path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
    finally:
        if os.path.exists(path):
            os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_decision_log.py -v`
Expected: FAIL

- [ ] **Step 3: Implement decision_log.py**

```python
# src/decision_log.py
import json
import os
from datetime import datetime

DEFAULT_LOG_PATH = "data/decisions.jsonl"


def log_decision(
    program: dict,
    grade: str,
    reason: str,
    stage: str,
    log_path: str = DEFAULT_LOG_PATH,
) -> None:
    entry = {
        "ts": datetime.now().isoformat(),
        "key": program.get("program_key", ""),
        "title": program.get("title", ""),
        "grade": grade,
        "reason": reason,
        "stage": stage,
    }
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_decision_log.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add src/decision_log.py tests/test_decision_log.py
git commit -m "feat: add JSONL decision logger for LLM audit trail"
```

---

### Task 5: LLM Filter (Stage 1 + Stage 2)

**Files:**
- Create: `src/llm_filter.py`
- Test: `tests/test_llm_filter.py`

- [ ] **Step 1: Write tests for response parsing (no API calls)**

```python
# tests/test_llm_filter.py
from src.llm_filter import (
    Assessment,
    parse_stage1_response,
    parse_stage2_response,
)

def test_assessment_dataclass():
    a = Assessment(grade="A", reason="자격 충족", eligibility="충족")
    assert a.grade == "A"

def test_parse_stage1_valid():
    raw = {"results": [
        {"id": 1, "decision": "PASS"},
        {"id": 2, "decision": "REJECT"},
        {"id": 3, "decision": "PASS"},
    ]}
    result = parse_stage1_response(raw, total_count=3)
    assert result == {1: "PASS", 2: "REJECT", 3: "PASS"}

def test_parse_stage1_missing_entry_defaults_pass():
    raw = {"results": [
        {"id": 1, "decision": "REJECT"},
        # id 2 missing
    ]}
    result = parse_stage1_response(raw, total_count=2)
    assert result[1] == "REJECT"
    assert result[2] == "PASS"  # Missing defaults to PASS (recall-first)

def test_parse_stage1_garbage_defaults_all_pass():
    result = parse_stage1_response("garbage", total_count=3)
    assert all(v == "PASS" for v in result.values())

def test_parse_stage2_valid():
    raw = {"grade": "A", "reason": "KC 인증 보유 대상", "eligibility": "충족"}
    a = parse_stage2_response(raw)
    assert a.grade == "A"
    assert a.reason == "KC 인증 보유 대상"
    assert a.eligibility == "충족"

def test_parse_stage2_invalid_grade_defaults_b():
    raw = {"grade": "X", "reason": "test", "eligibility": "미확인"}
    a = parse_stage2_response(raw)
    assert a.grade == "B"

def test_parse_stage2_garbage():
    a = parse_stage2_response("not a dict")
    assert a.grade == "B"
    assert a.eligibility == "미확인"

def test_stage1_returns_all_on_api_failure(monkeypatch):
    """When Gemini API fails, all programs should PASS (recall-first)."""
    from src import llm_filter
    from unittest.mock import MagicMock
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("API down")
    monkeypatch.setattr(llm_filter, "_get_gemini_client", lambda: mock_client)

    programs = [
        {"title": "A", "summary_raw": "test", "program_key": "s:1"},
        {"title": "B", "summary_raw": "test", "program_key": "s:2"},
    ]
    from src.llm_filter import stage1_quick_filter
    result = stage1_quick_filter(programs)
    assert len(result) == 2  # All pass on failure

def test_stage2_empty_detail_returns_grade_b():
    """Empty detail text should return B grade without calling API."""
    from src.llm_filter import stage2_assess
    program = {"title": "테스트", "program_key": "s:1"}
    result = stage2_assess(program, "")
    assert result.grade == "B"
    assert "접근 불가" in result.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_llm_filter.py -v`
Expected: FAIL

- [ ] **Step 3: Implement parsing logic in llm_filter.py**

```python
# src/llm_filter.py
import os
import json
import logging
from dataclasses import dataclass

from src.company_profile import build_stage1_prompt, build_stage2_prompt

logger = logging.getLogger(__name__)

BATCH_SIZE = 10

@dataclass
class Assessment:
    grade: str       # "A", "B", "C"
    reason: str      # One-line rationale
    eligibility: str  # "충족", "미확인", "미충족"


def parse_stage1_response(raw, total_count: int) -> dict[int, str]:
    """Parse Stage 1 JSON response. Missing/invalid entries default to PASS."""
    result = {}
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        items = raw.get("results", [])
        for item in items:
            result[item["id"]] = item.get("decision", "PASS")
    except Exception:
        pass

    # Fill missing IDs with PASS
    for i in range(1, total_count + 1):
        if i not in result:
            result[i] = "PASS"

    return result


def parse_stage2_response(raw) -> Assessment:
    """Parse Stage 2 JSON response. Invalid values get safe defaults."""
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        grade = raw.get("grade", "B")
        if grade not in ("A", "B", "C"):
            grade = "B"
        reason = raw.get("reason", "LLM 판단 불가")
        eligibility = raw.get("eligibility", "미확인")
        if eligibility not in ("충족", "미확인", "미충족"):
            eligibility = "미확인"
        return Assessment(grade=grade, reason=reason, eligibility=eligibility)
    except Exception:
        return Assessment(grade="B", reason="LLM 판단 불가", eligibility="미확인")


def _get_gemini_client():
    """Lazy-init Gemini client. Returns None if key not set."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def stage1_quick_filter(programs: list[dict]) -> list[dict]:
    """Batch-filter programs by title+summary. Returns PASS-ed programs."""
    client = _get_gemini_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not configured")

    passed = []
    for batch_start in range(0, len(programs), BATCH_SIZE):
        batch = programs[batch_start : batch_start + BATCH_SIZE]
        prompt = build_stage1_prompt(batch)

        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": {
                        "type": "object",
                        "properties": {
                            "results": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "decision": {"type": "string", "enum": ["PASS", "REJECT"]},
                                    },
                                    "required": ["id", "decision"],
                                },
                            }
                        },
                    },
                },
            )
            decisions = parse_stage1_response(json.loads(response.text), len(batch))
        except Exception as e:
            logger.error(f"Stage 1 Gemini call failed: {e}")
            # All PASS on failure
            decisions = {i: "PASS" for i in range(1, len(batch) + 1)}

        for i, program in enumerate(batch, 1):
            if decisions.get(i) == "PASS":
                passed.append(program)

    return passed


def stage2_assess(program: dict, detail_text: str) -> Assessment:
    """Assess single program with detail page text."""
    client = _get_gemini_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not configured")

    if not detail_text:
        return Assessment(grade="B", reason="상세페이지 접근 불가", eligibility="미확인")

    prompt = build_stage2_prompt(program, detail_text)

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "object",
                    "properties": {
                        "grade": {"type": "string", "enum": ["A", "B", "C"]},
                        "reason": {"type": "string"},
                        "eligibility": {"type": "string", "enum": ["충족", "미확인", "미충족"]},
                    },
                    "required": ["grade", "reason", "eligibility"],
                },
            },
        )
        return parse_stage2_response(json.loads(response.text))
    except Exception as e:
        logger.error(f"Stage 2 Gemini call failed: {e}")
        return Assessment(grade="B", reason="LLM 판단 불가", eligibility="미확인")
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_llm_filter.py -v`
Expected: All PASS (parsing tests don't call API)

- [ ] **Step 5: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add src/llm_filter.py tests/test_llm_filter.py
git commit -m "feat: add LLM filter with Stage 1/2 Gemini Flash integration"
```

---

## Chunk 3: Integration (run_once.py rewrite, workflow, deps)

### Task 6: Update requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add google-genai dependency**

Append `google-genai>=1.0.0` to `requirements.txt`.

- [ ] **Step 2: Install and verify**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && pip install -r requirements.txt`
Expected: Successfully installed google-genai

- [ ] **Step 3: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add requirements.txt
git commit -m "deps: add google-genai for Gemini Flash LLM filter"
```

---

### Task 7: Rewrite run_once.py

**Files:**
- Modify: `src/run_once.py` (full rewrite)
- Test: `tests/test_run_once_v2.py`

- [ ] **Step 1: Write integration test with mocked Gemini**

```python
# tests/test_run_once_v2.py
"""Integration tests for the rewritten run_once pipeline.
All Gemini API calls are mocked — these test wiring, not LLM quality."""
import json
import os
import tempfile
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

from src.llm_filter import Assessment


@pytest.fixture
def mock_env(tmp_path):
    cache_path = str(tmp_path / "notified_keys.json")
    log_path = str(tmp_path / "decisions.jsonl")
    env = {
        "TELEGRAM_BOT_TOKEN": "fake-token",
        "TELEGRAM_ALLOWED_CHAT_ID": "12345",
        "GEMINI_API_KEY": "fake-key",
        "BIZINFO_SUPPORT_KEY": "fake",
        "BIZINFO_EVENT_KEY": "fake",
        "NOTIFIED_CACHE_PATH": cache_path,
        "DECISION_LOG_PATH": log_path,
    }
    with patch.dict(os.environ, env):
        yield {"cache_path": cache_path, "log_path": log_path}


def _make_program(key: str, title: str) -> dict:
    return {
        "program_key": key,
        "kind": "support",
        "source": "bizinfo",
        "seq": key.split(":")[1],
        "title": title,
        "summary_raw": "요약",
        "agency": "기관",
        "category_l1": None,
        "region_raw": "경기",
        "apply_period_raw": None,
        "apply_start_at": None,
        "apply_end_at": "2026-05-01",
        "url": "https://example.com",
        "created_at_source": None,
        "ingested_at": "2026-04-16",
    }


def test_graded_notification_format():
    """Verify the message format for A+B grade results."""
    from src.run_once import format_graded_message

    grade_a = [
        (_make_program("s:1", "보안장비 지원"), Assessment("A", "KC 인증 대상, 1억", "충족")),
    ]
    grade_b = [
        (_make_program("s:2", "디지털전환 컨설팅"), Assessment("B", "SW사업자 가능", "미확인")),
    ]
    msg = format_graded_message(grade_a, grade_b, total_checked=50, stage1_passed=10)
    assert "🔴" in msg
    assert "보안장비 지원" in msg
    assert "KC 인증 대상" in msg
    assert "🟡" in msg
    assert "디지털전환 컨설팅" in msg


def test_graded_notification_no_results():
    from src.run_once import format_graded_message

    msg = format_graded_message([], [], total_checked=50, stage1_passed=5)
    assert "✅" in msg
    assert "50" in msg


def test_fallback_message_has_warning():
    from src.run_once import format_fallback_message

    items = [
        {"item": _make_program("s:1", "테스트"), "score": 45, "reasons": ["관심분야 일치"]},
    ]
    msg = format_fallback_message(items)
    assert "⚠️" in msg
    assert "테스트" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_run_once_v2.py -v`
Expected: FAIL — `cannot import name 'format_graded_message'`

- [ ] **Step 3: Rewrite run_once.py**

Rewrite `src/run_once.py` with the following structure:

```python
# src/run_once.py
import asyncio
import os
import logging
import json
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from src.db import init_db, upsert_program, get_profile, log_ingestion_run
from src.bizinfo_client import BizinfoClient
from src.normalizer import normalize_support, normalize_event
from src.filters import is_recommended
from src.notified_cache import load_notified_keys, save_notified_keys, filter_new_programs
from src.llm_filter import stage1_quick_filter, stage2_assess, Assessment
from src.detail_crawler import fetch_detail
from src.decision_log import log_decision
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _cache_path() -> str:
    return os.getenv("NOTIFIED_CACHE_PATH", "data/notified_keys.json")


def _log_path() -> str:
    return os.getenv("DECISION_LOG_PATH", "data/decisions.jsonl")


def _ingest_all(client: BizinfoClient) -> list[dict]:
    """Fetch and normalize all programs from bizinfo API."""
    new_items = []

    logger.info("Fetching support programs...")
    supports = client.fetch_support_programs()
    if supports:
        logger.debug(f"First support item keys: {supports[0].keys()}")
    for item in supports:
        try:
            norm = normalize_support(item)
            upsert_program(norm)
            new_items.append(norm)
        except Exception as e:
            logger.error(f"Error processing support item: {e}")
    logger.info(f"Support: {len(supports)} fetched, {len(new_items)} normalized")

    events_start = len(new_items)
    logger.info("Fetching events...")
    events = client.fetch_events()
    if events:
        logger.debug(f"First event item keys: {events[0].keys()}")
    for item in events:
        try:
            norm = normalize_event(item)
            upsert_program(norm)
            new_items.append(norm)
        except Exception as e:
            logger.error(f"Error processing event item: {e}")
    logger.info(f"Events: {len(events)} fetched, {len(new_items) - events_start} normalized")

    return new_items


def format_graded_message(
    grade_a: list[tuple[dict, Assessment]],
    grade_b: list[tuple[dict, Assessment]],
    total_checked: int,
    stage1_passed: int,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not grade_a and not grade_b:
        return f"✅ [{today}] 신규 해당 공고 없음 ({total_checked}건 검토, {stage1_passed}건 상세 판단)"

    parts = [f"📢 [{today}] 지원사업 알림\n"]

    if grade_a:
        parts.append(f"🔴 반드시 검토 ({len(grade_a)}건)\n")
        for i, (p, a) in enumerate(grade_a, 1):
            title = (p.get("title") or "제목 없음").strip()
            parts.append(f"{i}. {title}")
            parts.append(f"   → {a.reason}")
            parts.append(f"   🔗 {p.get('url', '#')}\n")

    if grade_b:
        b_heading = "참고 사항" if not grade_a else "참고"
        parts.append(f"🟡 {b_heading} ({len(grade_b)}건)\n")
        for i, (p, a) in enumerate(grade_b, 1):
            title = (p.get("title") or "제목 없음").strip()
            parts.append(f"{i}. {title}")
            parts.append(f"   → {a.reason}")
            parts.append(f"   🔗 {p.get('url', '#')}\n")

    msg = "\n".join(parts)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(생략)..."
    return msg


def format_fallback_message(recommendations: list[dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"⚠️ [{today}] LLM 판단 불가, 키워드 기반 결과 ({len(recommendations)}건)\n"]
    for r in recommendations[:15]:
        item = r["item"]
        title = (item.get("title") or "제목 없음").strip()
        reasons = ", ".join(r["reasons"])
        parts.append(f"[{r['score']}] {title}")
        parts.append(f"💡 {reasons}")
        parts.append(f"🔗 {item.get('url', '#')}\n")

    msg = "\n".join(parts)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(생략)..."
    return msg


def _run_keyword_fallback(items: list[dict], profile: dict) -> list[dict]:
    """Legacy keyword-based filter. Returns sorted recommendations."""
    recommendations = []
    for item in items:
        ok, score, reasons = is_recommended(item, profile)
        if ok:
            recommendations.append({"item": item, "score": score, "reasons": reasons})
    recommendations.sort(key=lambda x: x["score"], reverse=True)
    return recommendations


async def run_once():
    init_db()
    client = BizinfoClient()
    profile = get_profile()

    # 1. Ingest
    all_items = _ingest_all(client)
    logger.info(f"Ingested {len(all_items)} items total")

    # 2. Dedup
    notified = load_notified_keys(_cache_path())
    new_items = filter_new_programs(all_items, notified)
    logger.info(f"After dedup: {len(new_items)} new items")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()

    if not new_items:
        logger.info("No new items to process")
        return

    if not token or not chat_id:
        logger.warning("Telegram token or chat_id missing")
        return

    bot = Bot(token=token)

    # 3. LLM pipeline or fallback
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        logger.info("No GEMINI_API_KEY, using keyword fallback")
        recs = _run_keyword_fallback(new_items, profile)
        if recs:
            msg = format_fallback_message(recs)
            await bot.send_message(chat_id=chat_id, text=msg)
        save_notified_keys(notified | {i["program_key"] for i in new_items}, _cache_path())
        return

    try:
        # Stage 1
        passed = stage1_quick_filter(new_items)
        logger.info(f"Stage 1: {len(passed)}/{len(new_items)} passed")

        for p in new_items:
            if p not in passed:
                log_decision(p, "REJECT", "", "stage1", _log_path())

        # Stage 2
        assessments = []
        for p in passed:
            detail = fetch_detail(p.get("url", ""))
            assessment = stage2_assess(p, detail)
            assessments.append((p, assessment))
            log_decision(p, assessment.grade, assessment.reason, "stage2", _log_path())

        grade_a = [(p, a) for p, a in assessments if a.grade == "A"]
        grade_b = [(p, a) for p, a in assessments if a.grade == "B"]

        msg = format_graded_message(grade_a, grade_b, len(new_items), len(passed))
        await bot.send_message(chat_id=chat_id, text=msg)

        # Record all processed keys
        all_keys = notified | {i["program_key"] for i in new_items}
        save_notified_keys(all_keys, _cache_path())

    except Exception as e:
        logger.error(f"LLM pipeline failed: {e}", exc_info=True)
        recs = _run_keyword_fallback(new_items, profile)
        if recs:
            msg = format_fallback_message(recs)
            await bot.send_message(chat_id=chat_id, text=msg)
        save_notified_keys(notified | {i["program_key"] for i in new_items}, _cache_path())


if __name__ == "__main__":
    asyncio.run(run_once())
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/test_run_once_v2.py -v`
Expected: All PASS

- [ ] **Step 5: Run all existing tests to check for regressions**

Run: `cd /Users/dd/Projects/Supporting-business-BOT && python -m pytest tests/ -v`
Expected: All PASS (existing tests in test_scoring.py, test_due_parser.py should still work since filters.py is unchanged)

- [ ] **Step 6: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add src/run_once.py tests/test_run_once_v2.py
git commit -m "feat: rewrite run_once with LLM pipeline and graded notifications"
```

---

### Task 8: Update GitHub Actions Workflow

**Files:**
- Modify: `.github/workflows/schedule.yml`

- [ ] **Step 1: Update workflow**

Replace the contents of `.github/workflows/schedule.yml` with:

```yaml
name: Bizinfo Bot Schedule

on:
  schedule:
    # Run 2x/day: 08:00 KST (23:00 UTC prev day) and 18:00 KST (09:00 UTC)
    - cron: '0 23,9 * * *'
  workflow_dispatch:

jobs:
  run-bot:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Restore notification cache
        uses: actions/cache/restore@v4
        with:
          path: data/notified_keys.json
          key: notified-${{ github.run_id }}
          restore-keys: notified-

      - name: Run Bot
        env:
          BIZINFO_SUPPORT_KEY: ${{ secrets.BIZINFO_SUPPORT_KEY }}
          BIZINFO_EVENT_KEY: ${{ secrets.BIZINFO_EVENT_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_ALLOWED_CHAT_ID: ${{ secrets.TELEGRAM_ALLOWED_CHAT_ID }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          PROFILE_REGIONS: ${{ secrets.PROFILE_REGIONS }}
          PROFILE_INTERESTS: ${{ secrets.PROFILE_INTERESTS }}
          PROFILE_KEYWORDS: ${{ secrets.PROFILE_KEYWORDS }}
          PROFILE_EXCLUDES: ${{ secrets.PROFILE_EXCLUDES }}
          PROFILE_MIN_SCORE: ${{ secrets.PROFILE_MIN_SCORE }}
        run: python -m src.run_once

      - name: Save notification cache
        if: always()
        uses: actions/cache/save@v4
        with:
          path: data/notified_keys.json
          key: notified-${{ github.run_id }}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git add .github/workflows/schedule.yml
git commit -m "ci: update workflow to 2x/day, add Gemini + cache steps"
```

---

### Task 9: Set GEMINI_API_KEY Secret + Smoke Test

- [ ] **Step 1: Set the GitHub Secret**

```bash
gh secret set GEMINI_API_KEY --repo TheStack-ai/Supporting-business-BOT
# (paste the Gemini API key when prompted)
```

- [ ] **Step 2: Trigger workflow manually and verify**

```bash
gh workflow run schedule.yml --repo TheStack-ai/Supporting-business-BOT
```

- [ ] **Step 3: Watch the run**

```bash
gh run watch --repo TheStack-ai/Supporting-business-BOT
```

Expected: Run completes successfully, Telegram receives graded notification.

- [ ] **Step 4: Verify Telegram output has A/B grading format**

Check the Telegram channel for a message with `🔴 반드시 검토` and/or `🟡 참고` sections (or `✅ 신규 해당 공고 없음` if no new programs).

- [ ] **Step 5: Push all changes**

```bash
cd /Users/dd/Projects/Supporting-business-BOT
git push origin main
```

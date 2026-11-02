from datetime import UTC, datetime, timedelta

import anyio

from src import review_queue
from src import run_once as module
from src.db import init_db
from src.llm_filter import Assessment, EligibilityVerdict
from src.notified_cache import load_notified_keys, save_notified_keys
from src.program_state import load_program_state, save_program_state
from src.recovery_state import load_recovery_state, save_recovery_state
from src.source_ingestion import IngestionOutcome
from src.source_result import CollectionStatus, CoverageEntry


def setup_run(tmp_path, monkeypatch):
    for name, filename in {
        "DB_PATH": "bot.db", "NOTIFIED_CACHE_PATH": "notified_keys.json",
        "PROGRAM_STATE_PATH": "program_state.json", "DECISION_LOG_PATH": "decisions.jsonl",
        "RECOVERY_STATE_PATH": "recovery_state.json",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / filename))
    for name in ("REQUIRE_DURABLE_STATE", "BOT_DRY_RUN", "BOT_PREVIEW_ONLY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "offline")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "offline")
    init_db()
    now = datetime.now(UTC)
    save_recovery_state({"schema_version": 1, "since": (now - timedelta(days=20)).isoformat(), "held": {}})
    original = {
        "program_key": "old", "kind": "support", "source": "bizinfo", "seq": "old",
        "title": "제조기업 KC 제품인증 지원", "summary_raw": "전국 제조기업 시험인증 비용 지원",
        "created_at_source": (now - timedelta(days=30)).isoformat(),
        "apply_end_at": (now + timedelta(days=10)).date().isoformat(),
        "url": "https://example.com/old",
    }
    current = {**original, "apply_end_at": (now + timedelta(days=20)).date().isoformat()}
    save_program_state([original], str(tmp_path / "program_state.json"))
    save_notified_keys({"old"}, str(tmp_path / "notified_keys.json"))
    monkeypatch.setattr(module, "stage1_quick_filter", lambda items: items)
    monkeypatch.setattr(module, "fetch_detail", lambda _url: "전국 제조기업 시험인증 비용 지원")
    messages = []

    class FakeBot:
        async def send_message(self, **kwargs):
            messages.append(kwargs["text"])

    monkeypatch.setattr(module, "Bot", lambda **_kwargs: FakeBot())
    return now, original, current, messages


def successful_assessment():
    return Assessment(
        "B", "현재 매출 확인", "미확인", EligibilityVerdict.NEEDS_CONFIRMATION,
        fit_reason="제조 제품의 시험인증 비용 지원", missing_requirements=("현재 매출 확인",),
        evidence_quotes=("전국 제조기업 시험인증 비용 지원",),
    )


def collection(items):
    return IngestionOutcome(tuple(items), (
        CoverageEntry("bizinfo_support", CollectionStatus.SUCCESS, len(items), len(items), len(items), len(items)),
    ))


def test_pending_old_change_keeps_history_and_retries_after_backoff(tmp_path, monkeypatch):
    now, original, current, messages = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_ingest_all", lambda _client: collection([current]))
    clock = [now.timestamp()]
    monkeypatch.setattr(review_queue.time, "time", lambda: clock[0])
    assessed_kinds = []

    def assess(program, _detail):
        assessed_kinds.append(program["_change_kind"])
        if len(assessed_kinds) == 1:
            return Assessment("REVIEW", "상세페이지 접근 불가", "미확인", review_pending=True)
        return successful_assessment()

    monkeypatch.setattr(module, "stage2_assess", assess)
    anyio.run(module.run_once)
    assert assessed_kinds == ["CHANGED"]
    assert messages == []
    assert load_notified_keys(str(tmp_path / "notified_keys.json")) == {"old"}
    assert load_program_state(str(tmp_path / "program_state.json"))["old"].apply_end_at == original["apply_end_at"]
    assert "old" in review_queue.load_queue()

    # A waiting old change remains CHANGED and in the queue, instead of being
    # downgraded to NEW then suppressed by its pre-recovery publication date.
    anyio.run(module.run_once)
    assert assessed_kinds == ["CHANGED"]
    assert messages == []
    assert "old" in review_queue.load_queue()
    assert "old" not in load_recovery_state()["held"]

    clock[0] += 86401
    anyio.run(module.run_once)
    assert assessed_kinds == ["CHANGED", "CHANGED"]
    assert messages and "변경" in "".join(messages)
    assert review_queue.load_queue() == {}
    assert load_notified_keys(str(tmp_path / "notified_keys.json")) == {"old"}
    assert load_program_state(str(tmp_path / "program_state.json"))["old"].apply_end_at == current["apply_end_at"]


def test_capped_changed_notice_survives_until_next_run(tmp_path, monkeypatch):
    now, original, current, messages = setup_run(tmp_path, monkeypatch)
    stronger = {
        **current, "program_key": "stronger", "seq": "stronger",
        "title": "차량방호 볼라드 실증 지원",
        "created_at_source": (now - timedelta(days=1)).isoformat(),
        "url": "https://example.com/stronger",
    }
    monkeypatch.setenv("MAX_PROGRAMS_PER_RUN", "1")
    monkeypatch.setattr(module, "_ingest_all", lambda _client: collection([current, stronger]))
    assessed = []

    def assess(program, _detail):
        assessed.append((program["program_key"], program["_change_kind"]))
        return successful_assessment()

    monkeypatch.setattr(module, "stage2_assess", assess)
    anyio.run(module.run_once)
    assert assessed == [("stronger", "NEW")]
    assert load_notified_keys(str(tmp_path / "notified_keys.json")) == {"old", "stronger"}
    assert load_program_state(str(tmp_path / "program_state.json"))["old"].apply_end_at == original["apply_end_at"]

    anyio.run(module.run_once)
    assert assessed == [("stronger", "NEW"), ("old", "CHANGED")]
    assert len(messages) == 2
    assert load_program_state(str(tmp_path / "program_state.json"))["old"].apply_end_at == current["apply_end_at"]
    anyio.run(module.run_once)
    assert len(messages) == 2

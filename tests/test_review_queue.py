from datetime import UTC, datetime, timedelta
import json

import anyio

from src.db import init_db
from src.review_queue import load_queue, is_due, record_pending, clear_handled
from src.llm_filter import Assessment, EligibilityVerdict, Stage1Result
from src.source_ingestion import IngestionOutcome
from src.source_result import CollectionStatus, CoverageEntry


def notice(key="fit"):
    return {"program_key": key, "source": "bizinfo", "kind": "support", "seq": key,
            "title": "제조기업 KC 제품인증 지원", "summary_raw": "전국 제조기업 시험인증 비용 지원",
            "created_at_source": datetime.now(UTC).isoformat(),
            "apply_end_at": (datetime.now(UTC)+timedelta(days=30)).date().isoformat(),
            "url": "https://example.com/notice"}


def isolate(tmp_path,monkeypatch):
    for env,name in {"DB_PATH":"bot.db","NOTIFIED_CACHE_PATH":"notified_keys.json",
                     "PROGRAM_STATE_PATH":"program_state.json","DECISION_LOG_PATH":"decisions.jsonl",
                     "RECOVERY_STATE_PATH":"recovery_state.json"}.items():
        monkeypatch.setenv(env,str(tmp_path/name))
    for env in ("REQUIRE_DURABLE_STATE","BOT_DRY_RUN","BOT_PREVIEW_ONLY","GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(env,raising=False)
    init_db()


def test_review_failure_backs_off_but_changed_notice_is_immediately_due(tmp_path,monkeypatch):
    isolate(tmp_path,monkeypatch);item=notice()
    record_pending(item,"상세 확인 실패",now=1000)
    queue=load_queue()
    assert not is_due(item,queue,now=1001)
    assert is_due(item,queue,now=1000+86400)
    changed={**item,"apply_end_at":"2030-01-01"}
    assert is_due(changed,queue,now=1001)
    record_pending(item,"상세 확인 실패",now=1000+86400)
    assert load_queue()["fit"]["attempts"]==2
    clear_handled({"fit"})
    assert load_queue()=={}


def test_pending_assessment_is_not_notified_and_does_not_repeat_next_run(tmp_path,monkeypatch):
    from src import run_once as module
    isolate(tmp_path,monkeypatch);item=notice()
    monkeypatch.setenv("GEMINI_API_KEY","offline")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN","offline")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID","offline")
    coverage=CoverageEntry("bizinfo_support",CollectionStatus.SUCCESS,1,1,1,1)
    monkeypatch.setattr(module,"_ingest_all",lambda _:IngestionOutcome((item,),(coverage,)))
    monkeypatch.setattr(module,"stage1_quick_filter",lambda items:items)
    monkeypatch.setattr(module,"fetch_detail",lambda _:"")
    calls=[]
    def assess(*args):
        calls.append(True)
        return Assessment("REVIEW","상세페이지 접근 불가","미확인",review_pending=True)
    monkeypatch.setattr(module,"stage2_assess",assess)
    monkeypatch.setattr(module,"Bot",lambda **kw: (_ for _ in ()).throw(AssertionError("must not send")))
    anyio.run(module.run_once)
    assert "fit" not in json.loads((tmp_path/"notified_keys.json").read_text())["entries"]
    assert "fit" in load_queue()
    anyio.run(module.run_once)
    assert len(calls)==1


def test_assessment_preview_never_constructs_sender_or_changes_production(tmp_path,monkeypatch):
    from src import run_once as module
    isolate(tmp_path,monkeypatch);item=notice()
    monkeypatch.setenv("GEMINI_API_KEY","offline")
    monkeypatch.setenv("BOT_PREVIEW_ONLY","true")
    preview=tmp_path/"output"/"preview.json"
    monkeypatch.setenv("BOT_PREVIEW_PATH",str(preview))
    coverage=CoverageEntry("bizinfo_support",CollectionStatus.SUCCESS,1,1,1,1)
    monkeypatch.setattr(module,"_ingest_all",lambda _:IngestionOutcome((item,),(coverage,)))
    monkeypatch.setattr(module,"stage1_quick_filter",lambda items:items)
    monkeypatch.setattr(module,"fetch_detail",lambda _:"전국 제조기업 시험인증 비용 지원")
    result=Assessment("B","현재 매출 확인","미확인",EligibilityVerdict.NEEDS_CONFIRMATION,
                      fit_reason="제조 장비의 KC 시험인증 비용 지원",missing_requirements=("현재 매출 확인",),
                      evidence_quotes=("전국 제조기업 시험인증 비용 지원",))
    monkeypatch.setattr(module,"stage2_assess",lambda *args:result)
    monkeypatch.setattr(module,"Bot",lambda **kw: (_ for _ in ()).throw(AssertionError("must not send")))
    before=(tmp_path/"bot.db").read_bytes()
    anyio.run(module.run_once)
    assert (tmp_path/"bot.db").read_bytes()==before
    data=json.loads(preview.read_text())
    assert data["sent"]==0 and not data["production_state_changed"]
    assert not (tmp_path/"notified_keys.json").exists()

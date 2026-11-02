from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from src import bizinfo_repair as repair
from src.db import init_db, upsert_program
from src.normalizer import normalize_support
from src.notified_cache import load_notified_keys, save_notified_keys
from src.program_state import load_program_state, save_program_state
from src.recovery_state import STATE_FILES, save_recovery_state
from src.source_result import CollectionStatus, SourceBatch


def raw_support(key="S1", title="제조기업 시험인증 지원"):
    return {"pblancId": key, "pblancNm": title, "bsnsSumryCn": "전국 제조기업 시험비용 지원",
            "reqstBeginEndDe": "2026-11-01 ~ 2026-12-31", "creatPnttm": "2026-11-01"}


def raw_event(key="E1"):
    return {"eventInfoId": key, "nttNm": "해외규격 인증 실무 설명회", "registDe": "2026-11-01",
            "eventBeginEndDe": "2026-11-10 ~ 2026-11-10"}


class FakeClient:
    def __init__(self, supports=None, events=None, *, partial=None, total_override=None):
        self.supports = [raw_support(), raw_support("S2")] if supports is None else supports
        self.events = [raw_event()] if events is None else events
        self.partial = partial
        self.total_override = total_override
        self.collection_batches = {}
        self.collection_metadata = {}

    def fetch(self, name, items):
        status = CollectionStatus.PARTIAL if self.partial == name else CollectionStatus.SUCCESS
        total = self.total_override if self.total_override is not None else len(items)
        self.collection_batches[name] = SourceBatch(name, tuple(items), status, total, len(items))
        self.collection_metadata[name] = {"pages": 1, "termination": "reported_total_reached"}
        return items

    def fetch_support_programs(self):
        return self.fetch("bizinfo_support", self.supports)

    def fetch_events(self):
        return self.fetch("bizinfo_event", self.events)


@pytest.fixture
def state(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir()
    for name, filename in STATE_FILES.items():
        monkeypatch.setenv(name, str(directory / filename))
    init_db()
    previous = normalize_support(raw_support(title="기존 제목"))
    upsert_program(previous)
    # Seed a secondary source so collection cannot be mistaken for replacement.
    upsert_program({"program_key": "other:1", "source": "fanfandaero", "kind": "support", "seq": "1"})
    save_program_state([previous], str(directory / "program_state.json"))
    save_notified_keys({"support:S1"}, str(directory / "notified_keys.json"))
    save_recovery_state({"schema_version": 1, "since": (datetime.now(UTC)-timedelta(days=20)).isoformat(),
                         "held": {"support:held": {"reason": "published_before_recovery_cutoff"}}})
    (directory / "decisions.jsonl").write_text('{"key":"support:S1","decision":"REVIEW"}\n')
    with sqlite3.connect(directory / "bot.db") as db:
        db.execute("INSERT INTO user_actions VALUES ('support:S1','interest','2026-11-01')")
        db.execute("CREATE TABLE program_review_queue (program_key TEXT PRIMARY KEY, reason TEXT)")
        db.execute("INSERT INTO program_review_queue VALUES ('support:S1','qualification_pending')")
    # Any accidental outbound path is a hard test failure.
    monkeypatch.setattr("requests.sessions.Session.request", lambda *a, **k: pytest.fail("network forbidden"))
    return directory


def bytes_by_name(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def test_read_only_collection_counts_missing_without_state_writes(state):
    before = bytes_by_name(state)
    report = repair.collect(state, client=FakeClient())
    assert report["complete"] and not report["applied"]
    assert not report["database_verified"]
    assert report["sources"]["bizinfo_support"]["missing_before_ids"] == ["support:S2"]
    assert report["sources"]["bizinfo_event"]["missing_before_ids"] == ["event:E1"]
    assert report["sent"] == report["ai_calls"] == 0
    assert bytes_by_name(state) == before


@pytest.mark.parametrize("partial", ["bizinfo_support", "bizinfo_event"])
def test_partial_collection_cannot_write_even_when_apply_requested(state, partial):
    before = bytes_by_name(state)
    report = repair.collect(state, apply=True, client=FakeClient(partial=partial))
    assert report["status"] == "partial" and not report["complete"] and not report["applied"]
    assert bytes_by_name(state) == before


def test_success_status_without_matching_total_cannot_write(state):
    before = bytes_by_name(state)
    report = repair.collect(state, apply=True, client=FakeClient(total_override=100))
    assert not report["complete"]
    assert bytes_by_name(state) == before


def test_apply_recovers_missing_and_updates_values_without_consuming_notifications(state):
    preserved = repair._file_hashes(state)
    with repair._read_db(state) as db:
        protected = repair._table_hashes(db)
    report = repair.collect(state, apply=True, client=FakeClient())
    assert report["complete"] and report["applied"] and report["database_verified"]
    assert report["sent"] == report["ai_calls"] == 0
    assert repair._file_hashes(state) == preserved
    with repair._read_db(state) as db:
        assert repair._table_hashes(db) == protected
        rows = repair._rows_by_key(db)
    assert set(rows) == {"support:S1", "support:S2", "event:E1", "other:1"}
    assert rows["support:S1"]["title"] == "제조기업 시험인증 지원"
    assert report["sources"]["bizinfo_support"]["db_before"] == 1
    assert report["sources"]["bizinfo_support"]["db_after"] == 2
    assert all(item["missing_after"] == item["content_mismatches"] == 0 for item in report["sources"].values())
    assert load_notified_keys(str(state / "notified_keys.json")) == {"support:S1"}
    assert load_program_state(str(state / "program_state.json"))["support:S1"].title == "기존 제목"
    assert "support:S2" not in load_program_state(str(state / "program_state.json"))


def test_restore_verification_uses_receipt_without_api_and_detects_missing_and_changed_rows(state, tmp_path):
    receipt = repair.collect(state, apply=True, client=FakeClient())
    restored = tmp_path / "restored"
    shutil.copytree(state, restored)
    before = bytes_by_name(restored)
    verified = repair.verify_restore(restored, receipt)
    assert verified["complete"] and verified["database_verified"]
    assert verified["protected_files_unchanged"] and verified["protected_tables_unchanged"]
    assert all(item["restored_missing_after"] == 0 for item in verified["sources"].values())
    assert bytes_by_name(restored) == before
    with sqlite3.connect(restored / "bot.db") as db:
        db.execute("DELETE FROM programs WHERE program_key='support:S2'")
        db.execute("UPDATE programs SET title='stale' WHERE program_key='event:E1'")
    verified = repair.verify_restore(restored, receipt)
    assert not verified["complete"]
    assert verified["sources"]["bizinfo_support"]["missing_ids"] == ["support:S2"]
    assert verified["sources"]["bizinfo_event"]["mismatched_ids"] == ["event:E1"]


def test_restore_verification_detects_notification_or_queue_changes(state):
    receipt = repair.collect(state, apply=True, client=FakeClient())
    with sqlite3.connect(state / "bot.db") as db:
        db.execute("DELETE FROM program_review_queue")
    notified = state / "notified_keys.json"
    payload = json.loads(notified.read_text())
    payload["entries"]["support:S2"] = datetime.now(UTC).isoformat()
    notified.write_text(json.dumps(payload))
    result = repair.verify_restore(state, receipt)
    assert not result["complete"]
    assert not result["protected_tables_unchanged"] and not result["protected_files_unchanged"]


@pytest.mark.parametrize("rows", [
    [raw_support("", "제목")],
    [raw_support("same"), raw_support("same", "다른 제목")],
    [raw_support("S2", "")],
])
def test_missing_or_colliding_normalized_identity_fails_before_write(state, rows):
    before = bytes_by_name(state)
    with pytest.raises(repair.RepairError):
        repair.collect(state, apply=True, client=FakeClient(supports=rows))
    assert bytes_by_name(state) == before


def test_transaction_rolls_back_when_insert_cannot_satisfy_schema(state):
    with sqlite3.connect(state / "bot.db") as db:
        db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON programs WHEN NEW.kind='event' "
                   "BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    with repair._read_db(state) as db:
        before = repair._rows_by_key(db)
    with pytest.raises(sqlite3.IntegrityError):
        repair.collect(state, apply=True, client=FakeClient())
    with repair._read_db(state) as db:
        assert repair._rows_by_key(db) == before


def test_cli_failure_receipt_and_summary_do_not_expose_exception_text(state, tmp_path, monkeypatch, capsys):
    output = tmp_path / "audit.json"
    class BrokenClient:
        def fetch_support_programs(self):
            raise RuntimeError("https://example.test/?crtfcKey=private-key")
    monkeypatch.setattr(repair, "BizinfoClient", BrokenClient)
    code = repair.main(["--data-dir", str(state), "--report", str(output), "--apply"])
    assert code == 1
    assert json.loads(output.read_text())["reason"] == "RuntimeError"
    assert "private-key" not in output.read_text() + capsys.readouterr().out


def test_cli_writes_separate_receipt_and_checks_restored_data(state, tmp_path, monkeypatch):
    collected = tmp_path / "collected.json"
    checked = tmp_path / "checked.json"
    monkeypatch.setattr(repair, "BizinfoClient", FakeClient)
    assert repair.main(["--data-dir", str(state), "--report", str(collected), "--apply"]) == 0
    monkeypatch.setattr(repair, "BizinfoClient", lambda: pytest.fail("verify must not create API client"))
    assert repair.main(["--data-dir", str(state), "--report", str(checked), "--verify-report", str(collected)]) == 0
    assert json.loads(checked.read_text())["database_verified"]
    assert collected.stat().st_mode & 0o777 == 0o600


def test_cli_refuses_to_overwrite_state_with_report(state):
    before = bytes_by_name(state)
    with pytest.raises(SystemExit):
        repair.main(["--data-dir", str(state), "--report", str(state / "notified_keys.json"), "--apply"])
    assert bytes_by_name(state) == before


def test_restore_cli_preserves_input_receipt(state, tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(repair.collect(state, apply=True, client=FakeClient())))
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        repair.main(["--data-dir", str(state), "--report", str(path), "--verify-report", str(path)])
    assert path.read_bytes() == before


def test_missing_state_fails_before_client_creation(tmp_path, monkeypatch):
    directory = tmp_path / "empty"
    directory.mkdir()
    monkeypatch.setattr(repair, "BizinfoClient", lambda: pytest.fail("state must validate first"))
    with pytest.raises(Exception):
        repair.collect(directory, apply=True)
    assert list(directory.iterdir()) == []


def test_transport_and_page_receipts_are_whitelisted_in_report_but_summary_only_has_counts(state):
    class DiagnosticClient(FakeClient):
        def fetch(self, name, items):
            result = super().fetch(name, items)
            self.collection_metadata[name].update(
                request_count=2, retry_count=1, failed_attempt_count=1,
                url="https://example.test/?crtfcKey=private-key",
                attempts=[
                    {"page": 1, "format": "json", "attempt": 1, "elapsed_seconds": 10.01,
                     "http_status": None, "error_kind": "read_timeout", "retry_wait_seconds": 1.0,
                     "url": "https://example.test/?crtfcKey=private-key"},
                    {"page": 1, "format": "json", "attempt": 2, "elapsed_seconds": .5,
                     "http_status": 200, "error_kind": None, "retry_stopped": "private-key"}],
                page_receipts=[{"page": 1, "received": len(items), "added_unique": len(items),
                                "cumulative_unique": len(items), "secret": "private-key"}])
            return result
    report = repair.collect(state, client=DiagnosticClient())
    entry = report["sources"]["bizinfo_support"]
    assert entry["request_count"] == 2 and entry["retry_count"] == entry["failed_attempt_count"] == 1
    assert entry["attempts"][0] == {"page": 1, "format": "json", "attempt": 1, "elapsed_seconds": 10.01,
                                    "http_status": None, "error_kind": "read_timeout", "retry_wait_seconds": 1.0}
    assert entry["page_receipts"] == [{"page": 1, "received": 2, "added_unique": 2, "cumulative_unique": 2}]
    assert "private-key" not in json.dumps(report)
    summarized = repair.summary(report)["sources"]["bizinfo_support"]
    assert summarized["retry_count"] == 1
    assert "attempts" not in summarized and "page_receipts" not in summarized

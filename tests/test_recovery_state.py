from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import anyio
import pytest

from src.db import get_db_path, init_db
from src.normalizer import normalize_event
from src.notified_cache import load_notified_keys, save_notified_keys
from src.program_state import save_program_state
from src.recovery_state import (
    KST,
    RecoveryStateError,
    STATE_FILES,
    bootstrap,
    filter_recovery_programs,
    load_recovery_state,
    parse_publication_date,
    save_recovery_state,
    validate_required_state,
)
from src.source_ingestion import IngestionOutcome
from src.source_result import CollectionStatus, CoverageEntry


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    for env, name in STATE_FILES.items():
        monkeypatch.setenv(env, str(tmp_path / name))
    for env in ("REQUIRE_DURABLE_STATE", "BOT_DRY_RUN", "GITHUB_STEP_SUMMARY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    return tmp_path


def program(key, published, **changes):
    return {
        "program_key": key,
        "source": "bizinfo",
        "kind": "support",
        "seq": key,
        "title": "제조기업 지원 " + key,
        "status": "접수중",
        "created_at_source": published,
        "apply_end_at": (datetime.now(UTC) + timedelta(days=10)).date().isoformat(),
        "summary_raw": "제조 및 공공조달 지원",
        "url": "https://example.com/notice",
        **changes,
    }


def policy(since=None):
    return {
        "schema_version": 1,
        "since": since or (datetime.now(UTC) - timedelta(days=20)).isoformat(),
        "initialized_at": datetime.now(UTC).isoformat(),
        "held": {},
    }


def coverage(status=CollectionStatus.SUCCESS, source="bizinfo_support"):
    return CoverageEntry(source, status, 3, 3, 3, 3)


def seed_state(items, recovery=None):
    import os
    init_db()
    save_notified_keys(set(), os.environ["NOTIFIED_CACHE_PATH"])
    save_program_state(items, os.environ["PROGRAM_STATE_PATH"])
    save_recovery_state(recovery or policy())


@pytest.mark.parametrize("value,expected", [
    ("20261010123045", "2026-10-10T12:30:45+09:00"),
    ("2026.10.10", "2026-10-10T00:00:00+09:00"),
    ("20261010", "2026-10-10T00:00:00+09:00"),
    ("2026-10-10T03:30:45Z", "2026-10-10T03:30:45+00:00"),
    ("bad date", None),
    ("", None),
])
def test_publication_date_parser(value, expected):
    result = parse_publication_date(value)
    assert (result.isoformat() if result else None) == expected


def test_recovery_cutoff_holds_old_unknown_and_future_without_marking_sent():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=KST)
    state = policy("2026-10-09T02:48:35Z")
    items = [
        program("old", "2026-10-08", apply_end_at="2026-11-10"),
        program("unknown", None, apply_end_at="2026-11-10"),
        program("future", "2026-12-01", apply_end_at="2026-12-10"),
        program("new", "2026-10-10", apply_end_at="2026-11-10"),
    ]
    eligible, held = filter_recovery_programs(items, state, now=now)
    assert [item["program_key"] for item in eligible] == ["new"]
    assert held == {"old", "unknown", "future"}
    assert all(item["status"] == "unverified" for item in state["held"].values())
    assert state["held"]["unknown"]["reason"] == "publication_date_unknown"


def test_expired_event_is_not_recovered_when_application_end_is_missing():
    now = datetime(2026, 11, 2, tzinfo=KST)
    item = program("event:old", "2026-10-20", kind="event", apply_end_at=None,
                   event_end_at="2026-11-01")
    state = policy("2026-10-09T02:48:35Z")
    eligible, held = filter_recovery_programs([item], state, now=now)
    assert eligible == []
    assert held == {"event:old"}
    assert state["held"]["event:old"]["reason"] == "expired"


def test_observed_change_remains_eligible_and_clears_old_hold():
    item = program("old", "2020-01-01", _change_kind="CHANGED")
    state = policy()
    state["held"]["old"] = {"status": "unverified", "reason": "published_before_recovery_cutoff"}
    eligible, held = filter_recovery_programs([item], state)
    assert eligible == [item]
    assert held == set()
    assert "old" not in state["held"]


def test_event_normalizer_uses_api_regist_de():
    item = normalize_event({"eventInfoId": "E1", "nttNm": "행사", "registDe": "2026-10-20"})
    assert item["created_at_source"] == "2026-10-20"


@pytest.mark.parametrize("filename", ["notified_keys.json", "program_state.json", "bot.db", "recovery_state.json"])
def test_required_state_rejects_missing_files(isolated_paths, filename):
    seed_state([])
    (isolated_paths / filename).unlink()
    with pytest.raises(RecoveryStateError):
        validate_required_state()
    assert not (isolated_paths / filename).exists()


@pytest.mark.parametrize("filename,contents", [
    ("notified_keys.json", '{"entries": ["bad"]}'),
    ("notified_keys.json", '{"entries": {"x": "bad date"}}'),
    ("program_state.json", "{}"),
    ("recovery_state.json", '{"schema_version": 1, "since": "bad", "held": {}}'),
    ("bot.db", "not sqlite"),
])
def test_required_state_rejects_corruption(isolated_paths, filename, contents):
    seed_state([])
    (isolated_paths / filename).write_text(contents)
    with pytest.raises(RecoveryStateError):
        validate_required_state()


def test_strict_run_rejects_missing_state_before_creation_or_network(isolated_paths, monkeypatch):
    from src.run_once import run_once
    monkeypatch.setenv("REQUIRE_DURABLE_STATE", "true")
    monkeypatch.setattr("src.run_once.init_db", lambda: pytest.fail("must not bootstrap implicitly"))
    monkeypatch.setattr("src.run_once._ingest_all", lambda _client: pytest.fail("must not collect"))
    with pytest.raises(RecoveryStateError):
        anyio.run(run_once)
    assert list(isolated_paths.iterdir()) == []


def test_bootstrap_collects_without_ai_or_delivery_and_does_not_mark_old_sent(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    items = (
        program("old", (now - timedelta(days=30)).isoformat()),
        program("new", (now - timedelta(days=5)).isoformat()),
        program("unknown", None),
    )
    monkeypatch.setattr("src.source_ingestion.ingest_all", lambda _client: IngestionOutcome(items, (coverage(),)))
    monkeypatch.setattr("telegram.Bot.send_message", lambda *_a, **_kw: pytest.fail("must not send"))
    monkeypatch.setattr("src.llm_filter.stage2_assess", lambda *_a: pytest.fail("must not assess"))
    report = bootstrap((now - timedelta(days=20)).isoformat(), tmp_path)
    assert report["sent"] == report["ai_calls"] == 0
    assert report["eligible"] == 1
    assert report["held_unverified"] == 2
    assert load_notified_keys(str(tmp_path / "notified_keys.json")) == set()
    assert set(json.loads((tmp_path / "recovery_state.json").read_text())["held"]) == {"old", "unknown"}
    with sqlite3.connect(tmp_path / "bot.db") as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_bootstrap_refuses_existing_history(tmp_path):
    path = tmp_path / "notified_keys.json"
    path.write_text('{"entries":{"old":"2026-10-10"}}')
    before = path.read_bytes()
    with pytest.raises(RecoveryStateError, match="target_contains_state"):
        bootstrap("2026-10-09T02:48:35Z", tmp_path)
    assert path.read_bytes() == before


def test_bootstrap_refuses_no_usable_primary_sources(tmp_path, monkeypatch):
    outcome = IngestionOutcome((), (
        coverage(CollectionStatus.FAILED),
        coverage(CollectionStatus.FAILED, "bizinfo_event"),
    ))
    monkeypatch.setattr("src.source_ingestion.ingest_all", lambda _client: outcome)
    with pytest.raises(RecoveryStateError, match="collection_unavailable"):
        bootstrap("2026-10-09T02:48:35Z", tmp_path)
    assert not (tmp_path / "notified_keys.json").exists()


def test_run_preserves_old_unverified_and_only_handles_eligible_new(isolated_paths, monkeypatch):
    from src.run_once import run_once
    now = datetime.now(UTC)
    items = (
        program("old", (now - timedelta(days=30)).isoformat()),
        program("new", (now - timedelta(days=5)).isoformat()),
        program("unknown", None),
        program("expired-event", (now - timedelta(days=5)).isoformat(), kind="event", apply_end_at=None,
                event_end_at=(now - timedelta(days=1)).date().isoformat()),
    )
    seed_state(list(items))
    messages = []

    class FakeBot:
        async def send_message(self, **kwargs):
            messages.append(kwargs)

    monkeypatch.setenv("REQUIRE_DURABLE_STATE", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "123")
    monkeypatch.setattr("src.run_once._ingest_all", lambda _client: IngestionOutcome(items, (coverage(),)))
    monkeypatch.setattr("src.run_once.Bot", lambda token: FakeBot())
    monkeypatch.setattr("src.run_once._run_keyword_fallback", lambda values, _profile: [
        {"item": item, "score": 100, "reasons": ["test"]} for item in values
    ])
    anyio.run(run_once)
    assert load_notified_keys(str(isolated_paths / "notified_keys.json")) == {"new"}
    assert set(load_recovery_state()["held"]) == {"old", "unknown", "expired-event"}
    assert messages
    assert "지원 new" in "".join(message["text"] for message in messages)
    assert "지원 old" not in "".join(message["text"] for message in messages)


def test_dry_run_uses_scratch_state_and_never_calls_ai_or_telegram(isolated_paths, monkeypatch):
    from src.run_once import run_once
    now = datetime.now(UTC)
    item = program("new", (now - timedelta(days=2)).isoformat())
    seed_state([item])
    before = {path.name: path.read_bytes() for path in isolated_paths.iterdir()}
    monkeypatch.setenv("BOT_DRY_RUN", "true")
    monkeypatch.setenv("REQUIRE_DURABLE_STATE", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    def collect(_client):
        assert Path(get_db_path()).parent != isolated_paths
        with sqlite3.connect(get_db_path()) as db:
            db.execute("UPDATE company_profile SET min_score=1")
        return IngestionOutcome((item,), (coverage(),))

    monkeypatch.setattr("src.run_once._ingest_all", collect)
    monkeypatch.setattr("src.run_once.Bot", lambda **_kwargs: pytest.fail("must not create sender"))
    monkeypatch.setattr("src.run_once._grade_programs", lambda _items: pytest.fail("must not call AI"))
    anyio.run(run_once)
    after = {path.name: path.read_bytes() for path in isolated_paths.iterdir()}
    assert before == after

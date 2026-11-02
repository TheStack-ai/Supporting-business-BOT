from datetime import UTC, datetime, timedelta

import anyio

from src.program_state import snapshot_programs
from src.run_once import IngestionOutcome, run_once
from src.source_result import CollectionStatus, CoverageEntry


class FakeBot:
    def __init__(self, messages):
        self.messages = messages

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


def test_run_once_surfaces_degraded_collection_in_telegram_and_action_summary(
    tmp_path,
    monkeypatch,
):
    program = {
        "program_key": "support:PBLN_1",
        "source": "bizinfo",
        "kind": "support",
        "seq": "PBLN_1",
        "title": "수출 지원",
        "status": "접수중",
        "apply_start_at": "2026-07-01",
        "apply_end_at": "2026-08-01",
        "agency": "중소벤처기업부",
        "summary_raw": "수출 바우처",
    }
    coverage = (
        CoverageEntry(
            source="bizinfo_support",
            status=CollectionStatus.FAILED,
            reported_count=None,
            fetched_count=0,
            unique_count=0,
            normalized_count=0,
            note="0건 반환",
        ),
    )
    messages = []
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "123")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setattr("src.run_once.init_db", lambda: None)
    monkeypatch.setattr("src.run_once.get_profile", dict)
    monkeypatch.setattr(
        "src.run_once._ingest_all",
        lambda _client: IngestionOutcome((program,), coverage),
    )
    monkeypatch.setattr(
        "src.run_once.load_notified_keys",
        lambda _path: {"support:PBLN_1"},
    )
    monkeypatch.setattr(
        "src.run_once.load_program_state",
        lambda _path: snapshot_programs([program]),
    )
    monkeypatch.setattr("src.run_once._persist_program_state", lambda *_args: None)
    monkeypatch.setattr("src.run_once.save_notified_keys", lambda *_args: None)
    monkeypatch.setattr("src.run_once.Bot", lambda token: FakeBot(messages))

    anyio.run(run_once)

    assert messages[0][0] == "123"
    assert "수집 상태 확인" in messages[0][1]
    assert "기업마당 지원사업: 목록을 확인하지 못했습니다" in messages[0][1]
    assert "0개 출처 확인 · 1개 확인 필요" in messages[0][1]
    assert "수집 현황" in summary_path.read_text(encoding="utf-8")


def test_run_once_defers_unhandled_programs_without_marking_them_notified(tmp_path, monkeypatch):
    today = datetime.now(UTC).date()
    programs = tuple(
        {
            "program_key": f"support:PBLN_{index}",
            "source": "bizinfo",
            "kind": "support",
            "seq": f"PBLN_{index}",
            "title": f"지원 공고 {index}",
            "status": "접수중",
            "apply_end_at": end_at,
            "summary_raw": "제조업 지원",
            "url": "https://example.com",
        }
        for index, end_at in enumerate(
            (
                (today + timedelta(days=3)).isoformat(),
                (today + timedelta(days=1)).isoformat(),
                (today + timedelta(days=2)).isoformat(),
            ),
            1,
        )
    )
    coverage = (
        CoverageEntry(
            source="bizinfo_support",
            status=CollectionStatus.SUCCESS,
            reported_count=3,
            fetched_count=3,
            unique_count=3,
            normalized_count=3,
        ),
    )
    messages = []
    saved_keys = []
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "123")
    monkeypatch.setenv("MAX_PROGRAMS_PER_RUN", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr("src.run_once.init_db", lambda: None)
    monkeypatch.setattr("src.run_once.get_profile", dict)
    monkeypatch.setattr(
        "src.run_once._ingest_all",
        lambda _client: IngestionOutcome(programs, coverage),
    )
    monkeypatch.setattr("src.run_once.load_notified_keys", lambda _path: set())
    monkeypatch.setattr(
        "src.run_once.load_program_state",
        lambda _path: snapshot_programs(list(programs)),
    )
    monkeypatch.setattr("src.run_once._persist_program_state", lambda *_args: None)
    monkeypatch.setattr(
        "src.run_once._run_keyword_fallback",
        lambda items, _profile: [
            {"item": item, "score": 100, "reasons": ["테스트"]}
            for item in items
        ],
    )
    monkeypatch.setattr(
        "src.run_once.save_notified_keys",
        lambda keys, _path: saved_keys.append(keys),
    )
    monkeypatch.setattr("src.run_once.Bot", lambda token: FakeBot(messages))

    anyio.run(run_once)

    assert saved_keys == [{"support:PBLN_2"}]
    assert "적합 후보 2건" in "".join(text for _, text in messages)

"""Recover after a lost cache without pretending old notices were delivered.

The cutoff applies to NEW notices. A later observed change retains the ordinary
change-notification behavior; notices whose application/event has ended are not
reintroduced by recovery. Held notices remain separate from notified_keys.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from src.program_state import _StateModel, save_program_state
from src.source_result import CollectionStatus

KST = ZoneInfo("Asia/Seoul")
STATE_FILES = {
    "NOTIFIED_CACHE_PATH": "notified_keys.json",
    "PROGRAM_STATE_PATH": "program_state.json",
    "DB_PATH": "bot.db",
    "RECOVERY_STATE_PATH": "recovery_state.json",
    "DECISION_LOG_PATH": "decisions.jsonl",
}


class RecoveryStateError(RuntimeError):
    pass


def state_path(env: str) -> Path:
    return Path(os.getenv(env) or str(Path("data") / STATE_FILES[env]))


def parse_publication_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    elif re.fullmatch(r"\d{14}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}T{text[8:10]}:{text[10:12]}:{text[12:]}"
    else:
        text = re.sub(r"^(\d{4})[./](\d{2})[./](\d{2})", r"\1-\2-\3", text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RecoveryStateError(f"required_state_unreadable:{path.name}") from error
    if not isinstance(payload, dict):
        raise RecoveryStateError(f"required_state_invalid:{path.name}")
    return payload


def load_recovery_state(*, required: bool = False) -> dict | None:
    path = state_path("RECOVERY_STATE_PATH")
    if not path.exists() and not required:
        return None
    state = _read_json(path)
    if (
        state.get("schema_version") != 1
        or parse_publication_date(state.get("since")) is None
        or not isinstance(state.get("held"), dict)
        or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in state["held"].items())
    ):
        raise RecoveryStateError("required_state_invalid:recovery_state.json")
    return state


def validate_required_state() -> None:
    """Run before init_db, which otherwise silently creates a new history."""
    notified = _read_json(state_path("NOTIFIED_CACHE_PATH"))
    entries = notified.get("entries")
    if not isinstance(entries, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        or parse_publication_date(value) is None
        for key, value in entries.items()
    ):
        raise RecoveryStateError("required_state_invalid:notified_keys.json")
    path = state_path("PROGRAM_STATE_PATH")
    try:
        _StateModel.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        raise RecoveryStateError("required_state_invalid:program_state.json") from error
    db_path = state_path("DB_PATH")
    if not db_path.is_file():
        raise RecoveryStateError("required_state_missing:bot.db")
    try:
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RecoveryStateError("required_state_invalid:bot.db")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"company_profile", "programs", "ingestion_runs"}.issubset(tables):
                raise RecoveryStateError("required_state_invalid:bot.db")
    except sqlite3.DatabaseError as error:
        raise RecoveryStateError("required_state_invalid:bot.db") from error
    load_recovery_state(required=True)


def save_recovery_state(state: dict) -> None:
    path = state_path("RECOVERY_STATE_PATH")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _expired(program: dict, now: datetime) -> bool:
    if str(program.get("status") or "") == "마감":
        return True
    fields = ["apply_end_at"]
    if program.get("kind") == "event":
        fields.append("event_end_at")
    return any(
        parsed is not None and parsed.astimezone(KST).date() < now.astimezone(KST).date()
        for parsed in (parse_publication_date(program.get(field)) for field in fields)
    )


def filter_recovery_programs(
    programs: list[dict], state: dict | None, *, now: datetime | None = None
) -> tuple[list[dict], set[str]]:
    if state is None:
        return programs, set()
    now = now or datetime.now(UTC)
    cutoff = parse_publication_date(state["since"])
    if cutoff is None:
        raise RecoveryStateError("recovery_cutoff_invalid")
    eligible, held_keys = [], set()
    for program in programs:
        key = str(program.get("program_key") or "")
        reason = ""
        if _expired(program, now):
            reason = "expired"
        elif program.get("_change_kind", "NEW") == "NEW":
            published = parse_publication_date(program.get("created_at_source"))
            if published is None:
                reason = "publication_date_unknown"
            elif published <= cutoff:
                reason = "published_before_recovery_cutoff"
            elif published > now:
                reason = "publication_date_in_future"
        if reason:
            held_keys.add(key)
            state["held"][key] = {
                "status": "unverified",
                "reason": reason,
                "title": str(program.get("title") or ""),
                "source": str(program.get("source") or ""),
                "created_at_source": str(program.get("created_at_source") or ""),
                "url": str(program.get("url") or ""),
            }
        else:
            state["held"].pop(key, None)
            eligible.append(program)
    return eligible, held_keys


def recovery_report(state: dict | None, **counts) -> dict:
    return {
        "recovery_since": state.get("since") if state else None,
        "held_unverified": len(state["held"]) if state else 0,
        "held_reasons": dict(Counter(v.get("reason", "unknown") for v in state["held"].values())) if state else {},
        **counts,
    }


@contextmanager
def use_data_directory(directory: Path):
    previous = {env: os.environ.get(env) for env in STATE_FILES}
    for env, name in STATE_FILES.items():
        os.environ[env] = str(directory / name)
    try:
        yield
    finally:
        for env, value in previous.items():
            if value is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = value


@contextmanager
def isolated_state_copy():
    """Dry-run collection may write SQLite; keep those writes in a scratch copy."""
    originals = {env: state_path(env) for env in STATE_FILES}
    with TemporaryDirectory(prefix="support-bot-preview-") as name:
        directory = Path(name)
        for env, path in originals.items():
            if not path.is_file():
                continue
            target = directory / STATE_FILES[env]
            if env == "DB_PATH":
                with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
                    with sqlite3.connect(target) as destination:
                        source.backup(destination)
            else:
                shutil.copy2(path, target)
        with use_data_directory(directory):
            yield


def bootstrap(since: str, directory: Path) -> dict:
    """Collect and establish a recovery boundary; never invoke AI or Telegram."""
    cutoff = parse_publication_date(since)
    if cutoff is None or cutoff > datetime.now(UTC):
        raise RecoveryStateError("recovery_cutoff_invalid")
    if any((directory / name).exists() for name in STATE_FILES.values()):
        raise RecoveryStateError("recovery_target_contains_state")
    from src.bizinfo_client import BizinfoClient
    from src.db import init_db
    from src.notified_cache import save_notified_keys
    from src.source_ingestion import ingest_all

    directory.mkdir(parents=True, exist_ok=True)
    with use_data_directory(directory):
        init_db()
        outcome = ingest_all(BizinfoClient())
        usable = [
            entry for entry in outcome.coverage
            if entry.source in ("bizinfo_support", "bizinfo_event")
            and entry.status in (CollectionStatus.SUCCESS, CollectionStatus.PARTIAL)
            and entry.normalized_count > 0
        ]
        if not usable:
            raise RecoveryStateError("recovery_collection_unavailable")
        items = list(outcome.items)
        state = {
            "schema_version": 1,
            "since": cutoff.astimezone(UTC).isoformat(),
            "initialized_at": datetime.now(UTC).isoformat(),
            "policy": "new_after_cutoff; historical_delivery_unverified",
            "held": {},
            "coverage": [asdict(entry) for entry in outcome.coverage],
        }
        eligible, _held = filter_recovery_programs(items, state)
        save_program_state(items, str(state_path("PROGRAM_STATE_PATH")))
        save_notified_keys(set(), str(state_path("NOTIFIED_CACHE_PATH")))
        save_recovery_state(state)
        validate_required_state()
        return recovery_report(state, collected=len(items), eligible=len(eligible), sent=0, ai_calls=0,
                               coverage=state["coverage"])


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="Last successful run timestamp, ISO 8601")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    report = bootstrap(args.since, args.data_dir)
    print(json.dumps(report, ensure_ascii=False))
    summary = os.getenv("GITHUB_STEP_SUMMARY", "")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("\n### Recovery initialized without delivery\n\n```json\n")
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()

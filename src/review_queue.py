"""Retry unresolved assessments without marking them handled or flooding each run."""
from __future__ import annotations

import hashlib
import json
import time

from src.company_profile import COMPANY_PROFILE
from src.db import get_connection

_FIELDS = ("program_key", "title", "summary_raw", "agency", "status", "region_raw",
           "apply_start_at", "apply_end_at", "event_end_at", "created_at_source")


def fingerprint(program: dict) -> str:
    payload = {field: program.get(field) for field in _FIELDS}
    payload["company_policy"] = COMPANY_PROFILE
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _connection():
    connection = get_connection()
    connection.execute("""CREATE TABLE IF NOT EXISTS program_review_queue (
        program_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, reason TEXT NOT NULL,
        attempts INTEGER NOT NULL, next_review REAL NOT NULL, updated_at REAL NOT NULL
    )""")
    connection.commit()
    return connection


def load_queue() -> dict[str, dict]:
    with _connection() as connection:
        return {row["program_key"]: dict(row) for row in connection.execute("SELECT * FROM program_review_queue")}


def is_due(program: dict, queue: dict, now: float | None = None) -> bool:
    row = queue.get(str(program.get("program_key") or ""))
    return not row or row["fingerprint"] != fingerprint(program) or row["next_review"] <= (time.time() if now is None else now)


def record_pending(program: dict, reason: str, *, core_requirement: bool = False,
                   now: float | None = None) -> None:
    now = time.time() if now is None else now
    key, signature = str(program.get("program_key") or ""), fingerprint(program)
    if not key:
        return
    with _connection() as connection:
        previous = connection.execute("SELECT * FROM program_review_queue WHERE program_key=?", (key,)).fetchone()
        attempts = previous["attempts"] + 1 if previous and previous["fingerprint"] == signature else 1
        # A changed notice/company profile is immediately eligible again. Otherwise
        # transient failures back off, and unknown exclusive qualifications wait.
        days = 7 if core_requirement or attempts >= 3 else attempts
        connection.execute("INSERT OR REPLACE INTO program_review_queue VALUES (?,?,?,?,?,?)",
                           (key, signature, reason[:800], attempts, now + days * 86400, now))


def clear_handled(keys: set[str]) -> None:
    if not keys:
        return
    with _connection() as connection:
        connection.executemany("DELETE FROM program_review_queue WHERE program_key=?", ((key,) for key in keys))

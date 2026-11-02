"""Collect complete Bizinfo lists into an existing DB without AI or notifications.

The collection receipt can be checked against a subsequently restored encrypted
state. Notification history, comparison snapshots and review decisions stay put.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any

from src.bizinfo_client import BizinfoClient
from src.normalizer import normalize_event, normalize_support
from src.recovery_state import STATE_FILES, validate_required_state
from src.source_result import CollectionStatus, SourceBatch

PROTECTED_FILES = ("notified_keys.json", "program_state.json", "recovery_state.json", "decisions.jsonl")
PROTECTED_TABLES = ("company_profile", "user_actions", "program_review_queue")
SOURCES = (("bizinfo_support", "support", "fetch_support_programs", normalize_support),
           ("bizinfo_event", "event", "fetch_events", normalize_event))
ERROR_KINDS = {"tls_error", "connect_timeout", "read_timeout", "timeout", "http_error",
               "connection_error", "request_error"}
TERMINATIONS = {"missing_key", "page_cap", "time_cap", "transport_error", "json_decode", "json_envelope",
                "api_error", "xml_decode", "xml_envelope", "invalid_items", "invalid_total", "total_changed",
                "count_mismatch", "truncated_page", "repeated_page", "xml_total_unknown", "empty_total_unknown",
                "reported_total_reached", "empty_final_page", "short_final_page"}


class RepairError(RuntimeError):
    """Fixed error codes only; never include remote bodies, keys or URLs."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _safe_diagnostics(meta: dict) -> dict:
    def number(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    safe = {key: meta[key] for key in ("pages", "elapsed_seconds", "request_count", "retry_count",
                                     "failed_attempt_count") if number(meta.get(key))}
    if meta.get("format") in ("json", "rss"):
        safe["format"] = meta["format"]
    if meta.get("termination") in TERMINATIONS:
        safe["termination"] = meta["termination"]
    attempts = []
    for record in meta.get("attempts", []) if isinstance(meta.get("attempts"), list) else []:
        if not isinstance(record, dict):
            continue
        item = {key: record[key] for key in ("page", "attempt", "elapsed_seconds", "http_status", "retry_wait_seconds")
                if number(record.get(key))}
        if record.get("http_status") is None:
            item["http_status"] = None
        if record.get("format") in ("json", "rss"):
            item["format"] = record["format"]
        if record.get("error_kind") in ERROR_KINDS or record.get("error_kind") is None:
            item["error_kind"] = record.get("error_kind")
        if record.get("retry_stopped") == "insufficient_time_budget":
            item["retry_stopped"] = record["retry_stopped"]
        attempts.append(item)
    if "attempts" in meta:
        safe["attempts"] = attempts
    receipts = []
    for record in meta.get("page_receipts", []) if isinstance(meta.get("page_receipts"), list) else []:
        if isinstance(record, dict):
            receipts.append({key: record[key] for key in ("page", "received", "added_unique", "cumulative_unique")
                             if number(record.get(key))})
    if "page_receipts" in meta:
        safe["page_receipts"] = receipts
    return safe


@contextmanager
def _state_directory(directory: Path):
    if directory.is_symlink() or not directory.is_dir():
        raise RepairError("existing_state_directory_required")
    if any((directory / name).is_symlink() for name in (*PROTECTED_FILES, "bot.db")):
        raise RepairError("state_symlink_not_allowed")
    previous = {name: os.environ.get(name) for name in STATE_FILES}
    try:
        os.environ.update({name: str(directory / filename) for name, filename in STATE_FILES.items()})
        validate_required_state()
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _file_hashes(directory: Path) -> dict[str, str | None]:
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            if (directory / name).is_file() else None for name in PROTECTED_FILES}


def _table_hashes(db: sqlite3.Connection) -> dict[str, str | None]:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for name in PROTECTED_TABLES:
        if name not in tables:
            result[name] = None
            continue
        columns = [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')]
        rows = [list(row) for row in db.execute(f'SELECT * FROM "{name}"')]
        rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
        result[name] = _digest({"columns": columns, "rows": rows})
    return result


@contextmanager
def _read_db(directory: Path):
    db = sqlite3.connect((directory / "bot.db").resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _rows_by_key(db: sqlite3.Connection) -> dict[str, dict]:
    return {row["program_key"]: dict(row) for row in db.execute("SELECT * FROM programs")}


def _stored_digest(row: dict, fields: list[str]) -> str:
    return _digest({field: row.get(field) for field in fields})


def _checked_program(raw: dict, kind: str, normalizer) -> dict:
    try:
        program = normalizer(raw)
        seq = program.get("seq")
        key = program.get("program_key")
        if (not isinstance(seq, str) or not seq.strip() or not isinstance(key, str)
                or key != f"{kind}:{seq}" or len(key) > 256
                or any(ord(char) < 32 for char in key)
                or not str(program.get("title") or "").strip()):
            raise RepairError("normalization_identity_missing")
        if program.get("source") != "bizinfo" or program.get("kind") != kind:
            raise RepairError("normalization_source_mismatch")
        return program
    except (KeyError, TypeError, ValueError):
        raise RepairError("normalization_failed") from None


def _base_report(mode: str) -> dict:
    return {"schema_version": 1, "mode": mode, "generated_at": datetime.now(UTC).isoformat(),
            "sent": 0, "ai_calls": 0, "complete": False, "applied": False, "sources": {}}


def collect(data_dir: str | Path, *, apply: bool = False, client=None) -> dict:
    directory = Path(data_dir).absolute()
    report = _base_report("apply" if apply else "verify_collection")
    with _state_directory(directory):
        report["preserved_file_hashes"] = _file_hashes(directory)
        with _read_db(directory) as db:
            previous = _rows_by_key(db)
            report["preserved_table_hashes"] = _table_hashes(db)
        client = client or BizinfoClient()
        all_programs: dict[str, dict] = {}
        complete = True
        for source, kind, method, normalizer in SOURCES:
            raw_items = getattr(client, method)()
            batch = getattr(client, "collection_batches", {}).get(source)
            if not isinstance(batch, SourceBatch):
                raise RepairError("collection_evidence_missing")
            source_complete = (batch.status is CollectionStatus.SUCCESS
                               and batch.reported_count is not None
                               and batch.reported_count == batch.unique_count == len(raw_items)
                               and list(batch.items) == raw_items)
            entry = {"status": batch.status.value, "complete": source_complete,
                     "reported_count": batch.reported_count, "fetched_count": batch.fetched_count,
                     "unique_count": batch.unique_count}
            # The client owns transport diagnostics. Only bounded non-secret
            # numeric/status fields are copied into this public receipt.
            meta = getattr(client, "collection_metadata", {}).get(source, {})
            entry.update(_safe_diagnostics(meta))
            report["sources"][source] = entry
            if not source_complete:
                complete = False
                continue
            programs = [_checked_program(item, kind, normalizer) for item in raw_items]
            keys = sorted(program["program_key"] for program in programs)
            if len(keys) != len(set(keys)) or any(key in all_programs for key in keys):
                raise RepairError("normalized_identity_collision")
            all_programs.update((program["program_key"], program) for program in programs)
            missing = sorted(set(keys) - previous.keys())
            entry.update(current_ids=keys, current_ids_sha256=_digest(keys),
                         missing_before=len(missing), missing_before_ids=missing,
                         db_before=sum(key in previous for key in keys),
                         db_source_count_before=sum(row.get("source") == "bizinfo" and row.get("kind") == kind
                                                    for row in previous.values()))
        if not complete:
            report.update(status="partial", reason="both_sources_must_be_complete", database_verified=False)
            if _file_hashes(directory) != report["preserved_file_hashes"]:
                raise RepairError("protected_files_changed")
            with _read_db(directory) as db:
                if _table_hashes(db) != report["preserved_table_hashes"] or _rows_by_key(db) != previous:
                    raise RepairError("state_changed_during_collection")
            report["protected_state_unchanged"] = True
            return report

        fields = sorted({field for item in all_programs.values() for field in item})
        report["record_fields"] = fields
        report["record_digests"] = {key: _stored_digest(program, fields) for key, program in all_programs.items()}
        if apply:
            with sqlite3.connect(directory / "bot.db") as db:
                # No notification history or comparison snapshots are updated.
                # Both lists have passed completeness/normalization before this
                # single transaction begins; any verification failure rolls back.
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                if _rows_by_key(db) != previous or _table_hashes(db) != report["preserved_table_hashes"]:
                    raise RepairError("state_changed_during_collection")
                available = {row[1] for row in db.execute("PRAGMA table_info(programs)")}
                if not set(fields) <= available:
                    raise RepairError("program_schema_mismatch")
                for program in all_programs.values():
                    keys = sorted(program)
                    columns = ",".join(f'"{key}"' for key in keys)
                    placeholders = ",".join("?" for _ in keys)
                    updates = ",".join(f'"{key}"=excluded."{key}"' for key in keys if key != "program_key")
                    db.execute(f"INSERT INTO programs ({columns}) VALUES ({placeholders}) "
                               f"ON CONFLICT(program_key) DO UPDATE SET {updates}", [program[key] for key in keys])
                current = _rows_by_key(db)
                if any(key not in current or _stored_digest(current[key], fields) != expected
                       for key, expected in report["record_digests"].items()):
                    raise RepairError("database_content_verification_failed")
                if _table_hashes(db) != report["preserved_table_hashes"]:
                    raise RepairError("protected_tables_changed")
                if _file_hashes(directory) != report["preserved_file_hashes"]:
                    raise RepairError("protected_files_changed")
            report["applied"] = True
        with _read_db(directory) as db:
            current = _rows_by_key(db)
            if _table_hashes(db) != report["preserved_table_hashes"]:
                raise RepairError("protected_tables_changed")
        if _file_hashes(directory) != report["preserved_file_hashes"]:
            raise RepairError("protected_files_changed")
        if not apply and current != previous:
            raise RepairError("state_changed_during_collection")
        database_verified = True
        for source, *_rest in SOURCES:
            entry = report["sources"][source]
            ids = entry["current_ids"]
            missing_after = sorted(set(ids) - current.keys())
            mismatched = [key for key in ids if key in current
                          and _stored_digest(current[key], fields) != report["record_digests"][key]]
            entry.update(db_after=len(ids) - len(missing_after), missing_after=len(missing_after),
                         missing_after_ids=missing_after, content_mismatches=len(mismatched))
            database_verified = database_verified and not missing_after and not mismatched
        report.update(status="complete", complete=True, database_verified=database_verified,
                      protected_state_unchanged=True)
        return report


def verify_restore(data_dir: str | Path, receipt: dict) -> dict:
    if (receipt.get("schema_version") != 1 or not receipt.get("complete")
            or not receipt.get("applied") or not receipt.get("database_verified")):
        raise RepairError("applied_complete_receipt_required")
    directory = Path(data_dir).absolute()
    result = _base_report("verify_restore")
    with _state_directory(directory):
        files_ok = _file_hashes(directory) == receipt.get("preserved_file_hashes")
        with _read_db(directory) as db:
            current = _rows_by_key(db)
            tables_ok = _table_hashes(db) == receipt.get("preserved_table_hashes")
        fields, digests = receipt.get("record_fields"), receipt.get("record_digests")
        if not isinstance(fields, list) or not isinstance(digests, dict):
            raise RepairError("invalid_verification_receipt")
        complete = files_ok and tables_ok
        for source, *_rest in SOURCES:
            entry = receipt.get("sources", {}).get(source, {})
            ids = entry.get("current_ids")
            if (not isinstance(ids, list) or not all(isinstance(key, str) for key in ids)
                    or len(ids) != len(set(ids)) or entry.get("current_ids_sha256") != _digest(ids)
                    or entry.get("reported_count") != len(ids) or any(key not in digests for key in ids)):
                raise RepairError("invalid_verification_receipt")
            missing = sorted(set(ids) - current.keys())
            mismatched = [key for key in ids if key in current and _stored_digest(current[key], fields) != digests[key]]
            result["sources"][source] = {"expected": len(ids), "db_found": len(ids) - len(missing),
                                          "restored_missing_after": len(missing), "missing_ids": missing,
                                          "content_mismatches": len(mismatched), "mismatched_ids": mismatched}
            complete = complete and not missing and not mismatched
        result.update(complete=complete, status="complete" if complete else "failed",
                      protected_files_unchanged=files_ok, protected_tables_unchanged=tables_ok,
                      database_verified=complete)
    return result


def _write_report(path: Path, report: dict, data_dir: Path) -> None:
    if path.resolve().is_relative_to(data_dir.resolve()):
        raise RepairError("report_must_be_outside_state_directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def summary(report: dict) -> dict:
    fields = ("status", "complete", "applied", "database_verified", "protected_state_unchanged",
              "protected_files_unchanged", "protected_tables_unchanged", "sent", "ai_calls", "reason")
    result = {field: report[field] for field in fields if field in report}
    result["sources"] = {name: {key: value for key, value in item.items()
                               if not key.endswith("ids") and key not in ("current_ids_sha256", "attempts", "page_receipts")}
                         for name, item in report.get("sources", {}).items()}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--report", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-report")
    args = parser.parse_args(argv)
    if args.apply and args.verify_report:
        parser.error("--apply and --verify-report are mutually exclusive")
    directory, output = Path(args.data_dir), Path(args.report)
    if output.resolve().is_relative_to(directory.resolve()):
        parser.error("--report must be outside --data-dir")
    if args.verify_report and output.resolve() == Path(args.verify_report).resolve():
        parser.error("--report must not overwrite --verify-report")
    try:
        if args.verify_report:
            receipt = json.loads(Path(args.verify_report).read_text(encoding="utf-8"))
            report = verify_restore(directory, receipt)
        else:
            report = collect(directory, apply=args.apply)
    except Exception as error:
        # Exception strings from requests, SQLite or paths are deliberately not
        # printed; the diagnostic type/code cannot contain access credentials.
        code = str(error) if isinstance(error, RepairError) else type(error).__name__
        report = {**_base_report("failed"), "status": "failed", "reason": code}
    _write_report(output, report, directory)
    print(json.dumps(summary(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.get("complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())

import logging
import os
import json

import anyio
from dotenv import load_dotenv
from telegram import Bot

from src.bizinfo_client import BizinfoClient
from src.db import get_profile, init_db
from src.decision_log import log_decision
from src.detail_crawler import fetch_detail
from src.llm_filter import Assessment, stage1_quick_filter, stage2_assess
from src.notification_format import (
    format_fallback_message,
    format_graded_message,
    messages_with_coverage,
    source_label as _source_label,
)
from src.notified_cache import load_notified_keys, save_notified_keys
from src.program_selection import (
    apply_hard_filter as _apply_hard_filter,
    persist_program_state as _persist_program_state,
    prioritize_programs,
    programs_to_process as _programs_to_process,
    run_keyword_fallback as _run_keyword_fallback,
)
from src.program_state import ChangeKind, classify_programs, load_program_state
from src.recovery_state import (
    filter_recovery_programs,
    isolated_state_copy,
    load_recovery_state,
    recovery_report,
    save_recovery_state,
    validate_required_state,
)
from src.source_coverage import (
    format_coverage_manifest,
    has_degraded_sources,
    write_github_step_summary,
)
from src.source_ingestion import IngestionOutcome, ingest_all as _ingest_all

__all__ = ["IngestionOutcome", "format_fallback_message", "format_graded_message", "run_once"]

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _cache_path() -> str:
    return os.getenv("NOTIFIED_CACHE_PATH", "data/notified_keys.json")


def _log_path() -> str:
    return os.getenv("DECISION_LOG_PATH", "data/decisions.jsonl")


def _max_programs_per_run() -> int:
    try:
        configured = int(os.getenv("MAX_PROGRAMS_PER_RUN", "40"))
    except ValueError:
        configured = 40
    return max(1, min(configured, 100))


def _append_backlog_note(body: str, deferred_count: int) -> str:
    if not deferred_count:
        return body
    return body + f"\n\n⏳ 마감 순 검토 대기 {deferred_count}건 — 다음 실행에서 계속"


async def _send_messages(bot: Bot, chat_id: str, messages: tuple[str, ...]) -> None:
    for message in messages:
        await bot.send_message(chat_id=chat_id, text=message)


def _log_notification_candidates(
    grade_a: list[tuple[dict, Assessment]],
    grade_b: list[tuple[dict, Assessment]],
) -> None:
    logger.info("Notification candidates: A=%s, B=%s", len(grade_a), len(grade_b))
    for grade, pairs in (("A", grade_a), ("B", grade_b)):
        for program, assessment in pairs:
            logger.info(
                "Notify %s [%s] %s :: %s",
                grade,
                _source_label(program),
                program.get("title", ""),
                assessment.reason,
            )


def _grade_programs(
    items: list[dict],
) -> tuple[list[tuple[dict, Assessment]], list[tuple[dict, Assessment]], int]:
    candidate_items, hard_rejected = _apply_hard_filter(items)
    logger.info("Hard filter: %s/%s rejected", len(hard_rejected), len(items))
    for program, reason in hard_rejected:
        log_decision(program, "REJECT", reason, "hard_filter", _log_path())

    passed = stage1_quick_filter(candidate_items)
    logger.info("Stage 1: %s/%s passed", len(passed), len(candidate_items))
    for program in candidate_items:
        if program not in passed:
            log_decision(program, "REJECT", "", "stage1", _log_path())

    assessments: list[tuple[dict, Assessment]] = []
    for program in passed:
        detail = fetch_detail(program.get("url", ""))
        assessment = stage2_assess(program, detail)
        assessments.append((program, assessment))
        log_decision(program, assessment.grade, assessment.reason, "stage2", _log_path())

    grade_a = [(program, result) for program, result in assessments if result.grade == "A"]
    grade_b = [(program, result) for program, result in assessments if result.grade == "B"]
    grade_c = [(program, result) for program, result in assessments if result.grade == "C"]
    logger.info(
        "Stage 2 grades: A=%s, B=%s, C=%s",
        len(grade_a),
        len(grade_b),
        len(grade_c),
    )
    _log_notification_candidates(grade_a, grade_b)
    return grade_a, grade_b, len(passed)


async def run_once() -> None:
    if os.getenv("REQUIRE_DURABLE_STATE", "").lower() == "true":
        validate_required_state()
    if os.getenv("BOT_DRY_RUN", "").lower() == "true":
        with isolated_state_copy():
            await _run_once(dry_run=True)
    else:
        await _run_once(dry_run=False)


async def _run_once(*, dry_run: bool) -> None:
    init_db()
    profile = get_profile()
    outcome = _ingest_all(BizinfoClient())
    all_items = list(outcome.items)
    coverage_manifest = format_coverage_manifest(outcome.coverage)
    logger.info("Collection coverage\n%s", coverage_manifest)

    github_summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if github_summary_path:
        write_github_step_summary(coverage_manifest, github_summary_path)

    notified = load_notified_keys(_cache_path())
    previous = load_program_state(os.getenv("PROGRAM_STATE_PATH", "data/program_state.json"))
    changes = classify_programs(all_items, previous, notified)
    recovery = load_recovery_state()
    recovery_candidates = [
        dict(change.program, _change_kind=change.kind.value)
        for change in changes if change.kind is not ChangeKind.UNCHANGED
    ]
    _eligible, held_keys = filter_recovery_programs(recovery_candidates, recovery)
    all_actionable_items = [
        program for program in _programs_to_process(changes)
        if str(program.get("program_key") or "") not in held_keys
    ]
    if recovery is not None:
        held_keys |= set(recovery["held"])
        save_recovery_state(recovery)
    actionable_items = prioritize_programs(all_actionable_items, _max_programs_per_run())
    deferred_count = len(all_actionable_items) - len(actionable_items)
    logger.info(
        "Program diff new=%s changed=%s selected=%s deferred=%s total=%s",
        sum(change.kind is ChangeKind.NEW for change in changes),
        sum(change.kind is ChangeKind.CHANGED for change in changes),
        len(actionable_items),
        deferred_count,
        len(all_items),
    )
    report = recovery_report(recovery, selected=len(actionable_items), deferred=deferred_count,
                             collected=len(all_items), dry_run=dry_run)
    logger.info("Recovery selection %s", json.dumps(report, ensure_ascii=False))
    if github_summary_path:
        with open(github_summary_path, "a", encoding="utf-8") as stream:
            stream.write("\n### Recovery selection\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")
    if dry_run:
        logger.info("Dry run complete: no AI calls, no Telegram sends, production state unchanged")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("Telegram token or chat_id missing")
        return

    bot = Bot(token=token)
    actionable_keys = {
        str(item["program_key"])
        for item in all_actionable_items
        if item.get("program_key")
    }
    ignored_keys = {
        str(change.program["program_key"])
        for change in changes
        if change.kind is ChangeKind.NEW
        and change.program.get("program_key")
        and str(change.program["program_key"]) not in actionable_keys
        and str(change.program["program_key"]) not in held_keys
    }
    handled_keys = notified | ignored_keys | {
        str(item["program_key"])
        for item in actionable_items
        if item.get("program_key")
    }
    if not actionable_items:
        if has_degraded_sources(outcome.coverage):
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ 수집 누락이 감지됐습니다.\n\n" + coverage_manifest,
            )
        _persist_program_state(all_items, previous, outcome.coverage)
        save_notified_keys(handled_keys, _cache_path())
        logger.info("No new or changed actionable programs")
        return

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        recommendations = _run_keyword_fallback(actionable_items, profile)
        if recommendations or has_degraded_sources(outcome.coverage):
            body = _append_backlog_note(
                format_fallback_message(recommendations),
                deferred_count,
            )
            messages = messages_with_coverage(
                body,
                coverage_manifest,
            )
            await _send_messages(bot, chat_id, messages)
        _persist_program_state(all_items, previous, outcome.coverage)
        save_notified_keys(handled_keys, _cache_path())
        return

    grade_a, grade_b, stage1_passed = _grade_programs(actionable_items)
    body = _append_backlog_note(
        format_graded_message(
            grade_a,
            grade_b,
            len(actionable_items),
            stage1_passed,
        ),
        deferred_count,
    )
    messages = messages_with_coverage(
        body,
        coverage_manifest,
    )

    await _send_messages(bot, chat_id, messages)
    _persist_program_state(all_items, previous, outcome.coverage)
    save_notified_keys(handled_keys, _cache_path())


if __name__ == "__main__":
    anyio.run(run_once)

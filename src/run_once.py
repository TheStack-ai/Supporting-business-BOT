import logging
import os
import json
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from dotenv import load_dotenv
from telegram import Bot, LinkPreviewOptions

from src.bizinfo_client import BizinfoClient
from src.db import get_profile, init_db
from src.decision_log import log_decision
from src.detail_crawler import fetch_detail
from src.llm_filter import Assessment, stage1_quick_filter, stage2_assess, assessment_notification_status
from src.company_fit import assess_company_fit, company_fit_status
from src.review_queue import load_queue, is_due, record_pending, clear_handled
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
    format_coverage_notice,
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
    return body + f"\n\n그 외 적합 후보 {deferred_count}건은 다음 회차에서 검토합니다."


async def _send_messages(bot: Bot, chat_id: str, messages: tuple[str, ...]) -> None:
    for message in messages:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML",
                               link_preview_options=LinkPreviewOptions(is_disabled=True))


@dataclass
class GradedPrograms:
    grade_a: list = field(default_factory=list)
    grade_b: list = field(default_factory=list)
    stage1_passed: int = 0
    pending: dict[str, str] = field(default_factory=dict)
    rejected: set[str] = field(default_factory=set)


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


def _grade_programs(items: list[dict]) -> GradedPrograms:
    graded = GradedPrograms()
    candidate_items, hard_rejected = _apply_hard_filter(items)
    logger.info("Hard filter: %s/%s rejected", len(hard_rejected), len(items))
    for program, reason in hard_rejected:
        log_decision(program, "REJECT", reason, "hard_filter", _log_path())
        graded.rejected.add(str(program.get("program_key") or ""))

    passed = stage1_quick_filter(candidate_items)
    logger.info("Stage 1: %s/%s passed", len(passed), len(candidate_items))
    for program in candidate_items:
        if program not in passed:
            key = str(program.get("program_key") or "")
            if key in getattr(passed, "pending_keys", set()):
                graded.pending[key] = "사전 검토를 완료하지 못함"
                log_decision(program, "REVIEW", graded.pending[key], "stage1", _log_path())
            else:
                graded.rejected.add(key)
                log_decision(program, "REJECT", "회사 사업 관련성 부족", "stage1", _log_path())

    assessments: list[tuple[dict, Assessment]] = []
    for program in passed:
        detail = fetch_detail(program.get("url", ""))
        assessment = stage2_assess(program, detail)
        disposition = assessment_notification_status(program, assessment)
        key = str(program.get("program_key") or "")
        if disposition == "pending":
            graded.pending[key] = assessment.reason or "신청자격 확인 필요"
        elif disposition == "reject":
            graded.rejected.add(key)
        else:
            assessments.append((program, assessment))
        log_decision(program, "REVIEW" if disposition == "pending" else "REJECT" if disposition == "reject" else assessment.grade,
                     assessment.reason, "stage2", _log_path())

    grade_a = [(program, result) for program, result in assessments if result.grade == "A"]
    grade_b = [(program, result) for program, result in assessments if result.grade == "B"]
    logger.info(
        "Assessment decisions: A=%s, B=%s, rejected=%s, pending=%s",
        len(grade_a), len(grade_b), len(graded.rejected), len(graded.pending),
    )
    _log_notification_candidates(grade_a, grade_b)
    graded.grade_a, graded.grade_b, graded.stage1_passed = grade_a, grade_b, len(passed)
    return graded


async def run_once() -> None:
    if os.getenv("REQUIRE_DURABLE_STATE", "").lower() == "true":
        validate_required_state()
    preview = os.getenv("BOT_PREVIEW_ONLY", "").lower() == "true"
    if os.getenv("BOT_DRY_RUN", "").lower() == "true" or preview:
        with isolated_state_copy():
            await _run_once(dry_run=not preview, preview=preview)
    else:
        await _run_once(dry_run=False)


async def _run_once(*, dry_run: bool, preview: bool = False) -> None:
    init_db()
    profile = get_profile()
    outcome = _ingest_all(BizinfoClient())
    all_items = list(outcome.items)
    coverage_manifest = format_coverage_manifest(outcome.coverage)
    coverage_notice = format_coverage_notice(outcome.coverage)
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
    queue = load_queue()
    fit_candidates, fit_rejected, qualification_pending = [], set(), set()
    waiting_keys = set()
    for program in all_actionable_items:
        key = str(program.get("program_key") or "")
        fit = assess_company_fit(program)
        disposition = company_fit_status(program)
        if disposition == "reject":
            fit_rejected.add(key)
            log_decision(program, "REJECT", fit.reject_reason or fit.reason, "company_fit", _log_path())
        elif disposition == "pending":
            qualification_pending.add(key)
            if is_due(program, queue):
                reason = " · ".join(fit.review_requirements)
                record_pending(program, reason, core_requirement=True)
                log_decision(program, "REVIEW", reason, "company_fit", _log_path())
        elif is_due(program, queue):
            fit_candidates.append(program)
        else:
            waiting_keys.add(key)
    actionable_items = prioritize_programs(fit_candidates, _max_programs_per_run())
    deferred_count = len(fit_candidates) - len(actionable_items)
    logger.info(
        "Program diff new=%s changed=%s selected=%s deferred=%s total=%s",
        sum(change.kind is ChangeKind.NEW for change in changes),
        sum(change.kind is ChangeKind.CHANGED for change in changes),
        len(actionable_items),
        deferred_count,
        len(all_items),
    )
    report = recovery_report(recovery, selected=len(actionable_items), deferred=deferred_count,
                             collected=len(all_items), dry_run=dry_run, preview=preview,
                             company_rejected=len(fit_rejected), qualification_pending=len(qualification_pending),
                             review_waiting=len(waiting_keys))
    logger.info("Recovery selection %s", json.dumps(report, ensure_ascii=False))
    if github_summary_path:
        with open(github_summary_path, "a", encoding="utf-8") as stream:
            stream.write("\n### Recovery selection\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")
    if dry_run:
        logger.info("Dry run complete: no AI calls, no Telegram sends, production state unchanged")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if (not token or not chat_id) and not preview:
        logger.warning("Telegram token or chat_id missing")
        return

    async def publish(messages):
        if preview:
            path = Path(os.getenv("BOT_PREVIEW_PATH", "preview/notifications.json"))
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"sent": 0, "production_state_changed": False, "stats": report,
                       "messages": list(messages), "selected_programs": [
                           {"key": item.get("program_key"), "title": item.get("title"),
                            "fit": assess_company_fit(item).reason, "url": item.get("url")}
                           for item in actionable_items]}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Assessment preview saved: no Telegram sends; production state unchanged")
        elif messages:
            await _send_messages(Bot(token=token), chat_id, tuple(messages))
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
    handled_keys = notified | ignored_keys | fit_rejected | {
        str(item["program_key"])
        for item in actionable_items
        if item.get("program_key")
    }
    handled_keys -= qualification_pending | waiting_keys
    if not actionable_items:
        messages = ()
        if has_degraded_sources(outcome.coverage):
            messages = messages_with_coverage("<b>수집 상태 확인</b>\n일부 출처를 끝까지 확인하지 못했습니다.", coverage_notice)
        await publish(messages)
        _persist_program_state(all_items, previous, outcome.coverage)
        save_notified_keys(handled_keys, _cache_path())
        clear_handled(handled_keys)
        logger.info("No new or changed actionable programs")
        return

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        recommendations = _run_keyword_fallback(actionable_items, profile)
        messages = ()
        if recommendations or has_degraded_sources(outcome.coverage):
            body = _append_backlog_note(
                format_fallback_message(recommendations),
                deferred_count,
            )
            messages = messages_with_coverage(
                body,
                coverage_notice,
            )
        await publish(messages)
        _persist_program_state(all_items, previous, outcome.coverage)
        save_notified_keys(handled_keys, _cache_path())
        clear_handled(handled_keys)
        return

    graded = _grade_programs(actionable_items)
    handled_keys -= graded.pending.keys()
    for program in actionable_items:
        key = str(program.get("program_key") or "")
        if key in graded.pending:
            record_pending(program, graded.pending[key])
    report.update(recommended=len(graded.grade_a)+len(graded.grade_b), assessment_pending=len(graded.pending),
                  assessment_rejected=len(graded.rejected))
    messages = ()
    if graded.grade_a or graded.grade_b or has_degraded_sources(outcome.coverage):
        body = _append_backlog_note(format_graded_message(graded.grade_a, graded.grade_b,
                                   len(actionable_items), graded.stage1_passed), deferred_count)
        messages = messages_with_coverage(body, coverage_notice)
    await publish(messages)
    _persist_program_state(all_items, previous, outcome.coverage)
    save_notified_keys(handled_keys, _cache_path())
    clear_handled(handled_keys)


if __name__ == "__main__":
    anyio.run(run_once)

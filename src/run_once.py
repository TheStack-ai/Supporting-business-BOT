# src/run_once.py
import asyncio
import os
import logging
import json
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from src.db import init_db, upsert_program, get_profile, log_ingestion_run
from src.bizinfo_client import BizinfoClient
from src.normalizer import normalize_support, normalize_event
from src.filters import is_recommended
from src.notified_cache import load_notified_keys, save_notified_keys, filter_new_programs
from src.llm_filter import stage1_quick_filter, stage2_assess, Assessment
from src.detail_crawler import fetch_detail
from src.decision_log import log_decision
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _cache_path() -> str:
    return os.getenv("NOTIFIED_CACHE_PATH", "data/notified_keys.json")


def _log_path() -> str:
    return os.getenv("DECISION_LOG_PATH", "data/decisions.jsonl")


def _ingest_all(client: BizinfoClient) -> list[dict]:
    """Fetch and normalize all programs from bizinfo API."""
    new_items = []

    logger.info("Fetching support programs...")
    supports = client.fetch_support_programs()
    if supports:
        logger.debug(f"First support item keys: {supports[0].keys()}")
    for item in supports:
        try:
            norm = normalize_support(item)
            upsert_program(norm)
            new_items.append(norm)
        except Exception as e:
            logger.error(f"Error processing support item: {e}")
    logger.info(f"Support: {len(supports)} fetched, {len(new_items)} normalized")

    events_start = len(new_items)
    logger.info("Fetching events...")
    events = client.fetch_events()
    if events:
        logger.debug(f"First event item keys: {events[0].keys()}")
    for item in events:
        try:
            norm = normalize_event(item)
            upsert_program(norm)
            new_items.append(norm)
        except Exception as e:
            logger.error(f"Error processing event item: {e}")
    logger.info(f"Events: {len(events)} fetched, {len(new_items) - events_start} normalized")

    return new_items


def format_graded_message(
    grade_a: list[tuple[dict, Assessment]],
    grade_b: list[tuple[dict, Assessment]],
    total_checked: int,
    stage1_passed: int,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not grade_a and not grade_b:
        return f"✅ [{today}] 신규 해당 공고 없음 ({total_checked}건 검토, {stage1_passed}건 상세 판단)"

    parts = [f"📢 [{today}] 지원사업 알림\n"]

    if grade_a:
        parts.append(f"🔴 반드시 검토 ({len(grade_a)}건)\n")
        for i, (p, a) in enumerate(grade_a, 1):
            title = (p.get("title") or "제목 없음").strip()
            parts.append(f"{i}. {title}")
            parts.append(f"   → {a.reason}")
            parts.append(f"   🔗 {p.get('url', '#')}\n")

    if grade_b:
        b_heading = "참고 사항" if not grade_a else "참고"
        parts.append(f"🟡 {b_heading} ({len(grade_b)}건)\n")
        for i, (p, a) in enumerate(grade_b, 1):
            title = (p.get("title") or "제목 없음").strip()
            parts.append(f"{i}. {title}")
            parts.append(f"   → {a.reason}")
            parts.append(f"   🔗 {p.get('url', '#')}\n")

    msg = "\n".join(parts)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(생략)..."
    return msg


def format_fallback_message(recommendations: list[dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"⚠️ [{today}] LLM 판단 불가, 키워드 기반 결과 ({len(recommendations)}건)\n"]
    for r in recommendations[:15]:
        item = r["item"]
        title = (item.get("title") or "제목 없음").strip()
        reasons = ", ".join(r["reasons"])
        parts.append(f"[{r['score']}] {title}")
        parts.append(f"💡 {reasons}")
        parts.append(f"🔗 {item.get('url', '#')}\n")

    msg = "\n".join(parts)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(생략)..."
    return msg


def _run_keyword_fallback(items: list[dict], profile: dict) -> list[dict]:
    """Legacy keyword-based filter. Returns sorted recommendations."""
    recommendations = []
    for item in items:
        ok, score, reasons = is_recommended(item, profile)
        if ok:
            recommendations.append({"item": item, "score": score, "reasons": reasons})
    recommendations.sort(key=lambda x: x["score"], reverse=True)
    return recommendations


async def run_once():
    init_db()
    client = BizinfoClient()
    profile = get_profile()

    # 1. Ingest
    all_items = _ingest_all(client)
    logger.info(f"Ingested {len(all_items)} items total")

    # 2. Dedup
    notified = load_notified_keys(_cache_path())
    new_items = filter_new_programs(all_items, notified)
    logger.info(f"After dedup: {len(new_items)} new items")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()

    if not new_items:
        logger.info("No new items to process")
        return

    if not token or not chat_id:
        logger.warning("Telegram token or chat_id missing")
        return

    bot = Bot(token=token)

    # 3. LLM pipeline or fallback
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        logger.info("No GEMINI_API_KEY, using keyword fallback")
        recs = _run_keyword_fallback(new_items, profile)
        if recs:
            msg = format_fallback_message(recs)
            await bot.send_message(chat_id=chat_id, text=msg)
        save_notified_keys(notified | {i["program_key"] for i in new_items}, _cache_path())
        return

    try:
        # Stage 1
        passed = stage1_quick_filter(new_items)
        logger.info(f"Stage 1: {len(passed)}/{len(new_items)} passed")

        for p in new_items:
            if p not in passed:
                log_decision(p, "REJECT", "", "stage1", _log_path())

        # Stage 2
        assessments = []
        for p in passed:
            detail = fetch_detail(p.get("url", ""))
            assessment = stage2_assess(p, detail)
            assessments.append((p, assessment))
            log_decision(p, assessment.grade, assessment.reason, "stage2", _log_path())

        grade_a = [(p, a) for p, a in assessments if a.grade == "A"]
        grade_b = [(p, a) for p, a in assessments if a.grade == "B"]

        msg = format_graded_message(grade_a, grade_b, len(new_items), len(passed))
        await bot.send_message(chat_id=chat_id, text=msg)

        # Record all processed keys
        all_keys = notified | {i["program_key"] for i in new_items}
        save_notified_keys(all_keys, _cache_path())

    except Exception as e:
        logger.error(f"LLM pipeline failed: {e}", exc_info=True)
        recs = _run_keyword_fallback(new_items, profile)
        if recs:
            msg = format_fallback_message(recs)
            await bot.send_message(chat_id=chat_id, text=msg)
        save_notified_keys(notified | {i["program_key"] for i in new_items}, _cache_path())


if __name__ == "__main__":
    asyncio.run(run_once())

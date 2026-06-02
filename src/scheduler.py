from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone
import logging
import os
from .bizinfo_client import BizinfoClient
from .fanfandaero_client import FanfandaeroClient
from .normalizer import normalize_support, normalize_event, normalize_fanfandaero_support
from .db import upsert_program, log_ingestion_run, get_profile
from datetime import datetime, timedelta
import asyncio

logger = logging.getLogger(__name__)
kst = timezone('Asia/Seoul')

scheduler = AsyncIOScheduler(timezone=kst)
client = BizinfoClient()
fanfandaero_client = FanfandaeroClient()

async def ingest_support():
    logger.info("Starting Support Ingestion")
    items = []
    run_log = {
        "run_at": datetime.now().isoformat(),
        "kind": "support",
        "fetched_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "error": None
    }
    
    try:
        # Retry logic handled in client? No, client just returns list or empty.
        # But we want robust retry if network fails *inside* client.
        # Client handles basic errors. Let's trust client or wrap here.
        # For MVP, assume client does its best.
        
        items = client.fetch_support_programs()
        run_log["fetched_count"] = len(items)
        
        for item in items:
            try:
                normalized = normalize_support(item)
                upsert_program(normalized)
                # We don't track new/updated count precisely in upsert (SQLite UPSERT doesn't return status easily),
                # but we could check distinct. For MVP, just log total fetched.
                run_log["new_count"] += 1 # Rough estimate or just set to same as fetched
            except Exception as e:
                logger.error(f"Error normalizing/upserting item: {e}")
                
    except Exception as e:
        run_log["error"] = str(e)
        logger.error(f"Support ingestion failed: {e}")
        
    log_ingestion_run(run_log)
    logger.info("Finished Support Ingestion")

async def ingest_event():
    logger.info("Starting Event Ingestion")
    items = []
    run_log = {
        "run_at": datetime.now().isoformat(),
        "kind": "event",
        "fetched_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "error": None
    }
    
    try:
        items = client.fetch_events()
        run_log["fetched_count"] = len(items)
        
        for item in items:
            try:
                normalized = normalize_event(item)
                upsert_program(normalized)
                run_log["new_count"] += 1
            except Exception as e:
                logger.error(f"Error normalizing/upserting item: {e}")
                
    except Exception as e:
        run_log["error"] = str(e)
        logger.error(f"Event ingestion failed: {e}")
        
    log_ingestion_run(run_log)
    logger.info("Finished Event Ingestion")

async def ingest_fanfandaero_support():
    logger.info("Starting Fanfandaero Support Ingestion")
    run_log = {
        "run_at": datetime.now().isoformat(),
        "kind": "fanfandaero_support",
        "fetched_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "error": None
    }

    try:
        items = fanfandaero_client.fetch_support_programs()
        run_log["fetched_count"] = len(items)

        for item in items:
            try:
                normalized = normalize_fanfandaero_support(item)
                upsert_program(normalized)
                run_log["new_count"] += 1
            except Exception as e:
                logger.error(f"Error normalizing/upserting Fanfandaero item: {e}")

    except Exception as e:
        run_log["error"] = str(e)
        logger.error(f"Fanfandaero ingestion failed: {e}")

    log_ingestion_run(run_log)
    logger.info("Finished Fanfandaero Support Ingestion")

async def run_digest_job(bot_app):
    """
    Sends digest to the allowed chat ID.
    logic: fetch top N items from last 24h that match profile.
    """
    profile = get_profile()
    if not profile or not profile.get('notify_enabled', 1):
        return
        
    # Import here to avoid circular dependency if any (though bot imports scheduler usually)
    # Actually we just use the bot_app passed in.
    
    # We need to query DB. `db.py` needs a query function.
    # I'll implement a query helper in db or use raw sql here?
    # Better to keep db logic in db.py.
    # For now, I'll access DB directly via get_connection or add a helper in db.py.
    # I'll add `get_recent_recommendations` to db.py later or just use connection here.
    
    # Wait, I should add the logic.
    from .db import get_connection
    conn = get_connection()
    cursor = conn.cursor() # Need row factory if set in db.py
    
    # Get items ingested in last 24h
    yesterday = (datetime.now() - timedelta(hours=24)).isoformat()
    cursor.execute("SELECT * FROM programs WHERE ingested_at >= ?", (yesterday,))
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    from .filters import is_recommended
    
    recommendations = []
    for item in items:
        # Check if already dismissed
        # (Need another query or just check in memory if list small. Better query.)
        # Optimization: Filter out dismissed in SQL?
        # Let's do it in python for MVP.
        recommended, score, reasons = is_recommended(item, profile)
        if recommended:
            recommendations.append({
                "item": item,
                "score": score,
                "reasons": reasons
            })
            
    # Sort by score desc
    recommendations.sort(key=lambda x: x["score"], reverse=True)
    
    # Top 10
    top_10 = recommendations[:10]
    
    if not top_10:
        return # Nothing to send
        
    # Send via bot
    # We need to construct the message.
    # Re-use telegram_bot.format_message?
    # I'll let telegram_bot handle formatting if possible, or do it here.
    # Ideally `telegram_bot.py` has a `send_digest` function.
    # But `bot_app` is generic.
    # I'll assume `bot_app.bot.send_message`.
    
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
    if not chat_id:
        return

    # Importing formatter from telegram_bot might cause circular import if telegram_bot imports scheduler.
    # Creating a `utils.py` or `formatting.py` is better.
    # For now, I'll inline a simple formatter or defer to a method on bot_app if I attach one.
    
    message = f"📢 **일일 추천 ({len(top_10)}건)**\n\n"
    for r in top_10:
        item = r['item']
        reasons = ", ".join(r['reasons'])
        message += f"[{r['score']}] [{item['kind']}] {item['title']}\n"
        message += f"사유: {reasons}\n"
        message += f"/open_{item['program_key'].replace(':','_')}\n\n" 
        # Note: key has ':', telegram commands can't have ':'. Replace with '_' or use callback buttons.
        # PRD says "/open <id>". 
        # I'll match the PRD command format in the text, but for clickable links, maybe generic?
        # "액션: /save ..."
        
    # Send
    # Chunking...
    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=message)
    except Exception as e:
        logger.error(f"Failed to send digest: {e}")

def start_scheduler(bot_app):
    # Ingest jobs
    scheduler.add_job(ingest_support, CronTrigger(hour=8, minute=0, timezone=kst))
    scheduler.add_job(ingest_support, CronTrigger(hour=18, minute=0, timezone=kst))
    
    scheduler.add_job(ingest_event, CronTrigger(hour=8, minute=0, timezone=kst))
    scheduler.add_job(ingest_event, CronTrigger(hour=18, minute=0, timezone=kst))

    scheduler.add_job(ingest_fanfandaero_support, CronTrigger(hour=8, minute=0, timezone=kst))
    scheduler.add_job(ingest_fanfandaero_support, CronTrigger(hour=18, minute=0, timezone=kst))
    
    # Digest job
    # Fetch time from profile?
    # PRD: "Daily digest... 08:30 KST (if notify_enabled=1)".
    # Profile has `notify_time_kst`.
    # We should dynamically set this?
    # For MVP: Load from profile on startup. If profile changes, we might need to reschedule.
    # MVP Simplicity: Just stick to 08:30 or read from profile once.
    
    profile = get_profile()
    notify_time = profile.get('notify_time_kst', "08:30")
    h, m = map(int, notify_time.split(':'))
    
    scheduler.add_job(run_digest_job, CronTrigger(hour=h, minute=m, timezone=kst), args=[bot_app])
    
    scheduler.start()

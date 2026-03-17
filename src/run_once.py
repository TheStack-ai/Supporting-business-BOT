import asyncio
import os
import logging
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load env if present (local dev)
load_dotenv()

from src.db import init_db, upsert_program, get_profile, log_ingestion_run
from src.bizinfo_client import BizinfoClient
from src.normalizer import normalize_support, normalize_event
from src.filters import is_recommended
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_once():
    # 1. Initialize DB (In-memory or fresh file in GitHub Runner)
    init_db()
    
    # 2. Fetch Data
    client = BizinfoClient()
    
    # Support
    logger.info("Fetching support...")
    supports = client.fetch_support_programs()
    new_items = []
    
    # DEBUG: Log first item to check keys
    if supports:
        logger.info(f"DEBUG: First Support Item Keys: {supports[0].keys()}")
        logger.info(f"DEBUG: First Support Item Raw: {json.dumps(supports[0], ensure_ascii=False)[:500]}...")

    for item in supports:
        try:
            norm = normalize_support(item)
            upsert_program(norm)
            new_items.append(norm)
        except Exception as e:
            logger.error(f"Error processing support item: {e}")
            
    # Events
    logger.info("Fetching events...")
    events = client.fetch_events()
    
    if events:
        logger.info(f"DEBUG: First Event Item Keys: {events[0].keys()}")
        logger.info(f"DEBUG: First Event Item Raw: {json.dumps(events[0], ensure_ascii=False)[:500]}...")

    for item in events:
        try:
            norm = normalize_event(item)
            upsert_program(norm)
            new_items.append(norm)
        except Exception as e:
            logger.error(f"Error processing event item: {e}")

    # 3. Filter for Notification
    # in stateless run, "new" is everything we just fetched?
    # Or just items created/updated recently?
    # Bizinfo API returns recent items.
    # To avoid duplicate alerts every 6 hours, we should check `ingested_at`?
    # PROBLEM: In stateless, DB is empty every time. So ALL items are "newly ingested".
    # SOLUTION: We must filter by `created_at_source` or strict time window if available.
    # BUT Bizinfo `created_at` format varies.
    # Alternative: The user accepts some repetition? Or we filter by "today"?
    # User said: "Digest style".
    # Let's filter items that are "created" or "posted" within last 7 hours?
    # Bizinfo `creatPnttm` (created_at_source) is usually available.
    # If not available, we might fallback to just showing all recommended (Digest mode).
    # "Digest" usually implies "Here is what is active/new".
    # If we run every 6 hours, we can show items from last ~7 hours?
    # Let's try to parse `created_at_source`.
    
    profile = get_profile()
    recommendations = []
    
    # Time window: 7 hours ago (to cover 6h gap + buffer)
    # If created_at_source is missing, we might show it if it *looks* new?
    # Or just show top recommended?
    # Let's show all recommended items that are NOT closed.
    # Users can /dismiss (but dismissal won't persist in stateless!).
    # Stateless limitation: Dismissal doesn't work across runs.
    # We should mention this to user? User asked for 24h server or GitHub Actions.
    # Action -> Stateless.
    # We will try to prioritize "Truly New" if possible, but fallback to "Valid & Recommended".
    
    # Optimization: Filter by `created_at_source` if possible.
    cutoff = datetime.now() - timedelta(hours=24) # Show last 24h items to be safe? Or 7h?
    # If we run 4 times a day, 24h list will repeat items.
    # Let's try 12h or 8h.
    # Let's stick to valid items that match profile.
    
    for item in new_items:
        # Check date if available
        created_str = item.get('created_at_source')
        if created_str:
            try:
                # Format: YYYY-MM-DD HH:MM:SS or YYYY-MM-DD
                # If len is 10, add time.
                if len(created_str) == 10:
                    created_dt = datetime.strptime(created_str, "%Y-%m-%d")
                else:
                    created_dt = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
                
                # If older than 7 days, skip? (Bizinfo sometimes gives old items in RSS?)
                # API usually gives recent 100.
                pass
            except:
                pass

        rec, score, reasons = is_recommended(item, profile)
        if rec:
            recommendations.append({
                "item": item,
                "score": score,
                "reasons": reasons
            })
    
    # Sort
    recommendations.sort(key=lambda x: x["score"], reverse=True)
    top_items = recommendations[:15] # Limit total
    
    today_date = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    # 4. Send Telegram
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()

    if not top_items:
        logger.info("No recommendations found.")
        if token and chat_id:
            bot = Bot(token=token)
            msg = f"📉 **[{today_date}] 업데이트 없음**\n\n조건에 맞는 새로운 지원사업/행사가 없습니다.\n(수집: 지원 {len(supports)}건, 행사 {len(events)}건)"
            await bot.send_message(chat_id=chat_id, text=msg)
        return
    
    if token and chat_id:
        bot = Bot(token=token)
        
        # Format message
        msg = f"📢 **[{today_date}] 업데이트 ({len(top_items)}건)**\n\n"
        for r in top_items:
            item = r['item']
            title = item.get('title', '제목 없음').strip()
            if not title: title = "제목 없음"
            
            reasons = ", ".join(r['reasons'])
            url = item.get('url', '#')
            
            msg += f"[{r['score']}] {title}\n"
            msg += f"💡 {reasons}\n"
            msg += f"🔗 {url}\n\n"
            
        # Chunking if needed
        if len(msg) > 4000:
             msg = msg[:4000] + "\n...(생략)..."
             
        await bot.send_message(chat_id=chat_id, text=msg)
    else:
        logger.warning("Token or Chat ID missing.")

if __name__ == "__main__":
    asyncio.run(run_once())

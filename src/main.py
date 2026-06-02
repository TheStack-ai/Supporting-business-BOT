import os
import logging
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder

# Load environment variables first
load_dotenv()

from src.db import init_db
from src.telegram_bot import create_app
from src.scheduler import start_scheduler

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def main():
    # 1. Initialize Database
    logger.info("Initializing database...")
    init_db()
    
    # 2. Key Check
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables.")
        return

    # 3. Create Bot Application
    logger.info("Creating bot application...")
    app = create_app(token)
    
    # 4. Setup Scheduler (via post_init hook or just parallel if async)
    # PTB runs an asyncio loop. AsyncIOScheduler needs to run in that loop.
    # We can use post_init to start scheduler.
    async def post_init(application):
        logger.info("Starting scheduler...")
        start_scheduler(application)
        
    app.post_init = post_init
    
    # 5. Run Polling
    logger.info("Starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()

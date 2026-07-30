"""Copy this file to ~/bots/config/kokabielrc.py and add your private tokens."""

from pathlib import Path

BOT_TOKEN = ""
TMDB_BEARER_TOKEN = ""

KOKABIEL_DIR = Path.home() / "bots" / "Kokabiel"
DATABASE_PATH = KOKABIEL_DIR / "kokabiel.db"
LOG_PATH = KOKABIEL_DIR / "kokabiel.log"

# Empty allows private testing anywhere the bot is added.
# Add the group's long negative chat ID when ready to lock it down.
ALLOWED_CHAT_IDS = []
# Only these Telegram user IDs can use Kokabiel owner commands.
OWNER_USER_IDS = [
    # 123456789,
]

# Legacy ADMIN_USER_IDS is still accepted if OWNER_USER_IDS is absent.

LANGUAGE = "en-US"
REGION = "US"
RESULT_LIMIT = 8
INCLUDE_ADULT = False
LOG_LEVEL = "INFO"

# Your existing local Telegram Bot API server on Arakiel.
# Set both to "" to use Telegram's normal cloud Bot API instead.
LOCAL_BOT_API_URL = "http://127.0.0.1:8081/bot"
LOCAL_BOT_API_FILE_URL = "http://127.0.0.1:8081/file/bot"

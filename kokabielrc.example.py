"""Private runtime configuration for Kokabiel."""

from pathlib import Path

BOT_TOKEN = ""
TMDB_BEARER_TOKEN = ""

BOTS_DIR = Path.home() / "bots"

DATABASE_PATH = BOTS_DIR / "data" / "kokabiel" / "kokabiel.db"
LOG_PATH = BOTS_DIR / "logs" / "kokabiel" / "kokabiel.log"

# Empty allows Kokabiel to operate wherever it is added.
# Add the group's long negative Telegram chat ID to restrict it.
ALLOWED_CHAT_IDS = [
    # -1001234567890,
]

# Legacy compatibility.
ADMIN_USER_IDS = []

# Only these Telegram user IDs can use owner commands.
OWNER_USER_IDS = [
    # 123456789,
]

LANGUAGE = "en-US"
REGION = "US"
RESULT_LIMIT = 8
INCLUDE_ADULT = False
LOG_LEVEL = "INFO"

# Leave both blank to use Telegram's normal cloud Bot API.
LOCAL_BOT_API_URL = ""
LOCAL_BOT_API_FILE_URL = ""

# ── Notes ───────────────────────────────────
# - Do not commit your real BOT_TOKEN.
# - Keep this private configuration outside the repository.
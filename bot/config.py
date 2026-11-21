import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY", "")
# Если не задан, владельцем становится первый, кто отправит /start (запоминается в БД)
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0") or 0)
MODEL = os.getenv("MODEL", "claude-haiku-5-5")
# Недельный разбор рациона — на модели сильнее (решение пользователя): там нужен анализ, а не разбор текста
REVIEW_MODEL = os.getenv("REVIEW_MODEL", "claude-sonnet-5-5")
# Цена за 1M токенов (вход / выход) — для счётчика /cost
PRICE_IN = float(os.getenv("PRICE_IN", "0.10"))
PRICE_OUT = float(os.getenv("PRICE_OUT", "0.50"))
PRICES = {MODEL: (PRICE_IN, PRICE_OUT), "claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0)}
MONTHLY_LLM_BUDGET = float(os.getenv("MONTHLY_LLM_BUDGET", "2.0"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Warsaw"))
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "data" / "bot.db")))
BACKUP_DIR = DB_PATH.parent / "backups"
# Polar AccessLink (импорт тренировок). Клиент создаётся на admin.polaraccesslink.com
POLAR_CLIENT_ID = os.getenv("POLAR_CLIENT_ID", "")
POLAR_CLIENT_SECRET = os.getenv("POLAR_CLIENT_SECRET", "")
# Распознавание голосовых: faster-whisper (Wyoming) на том же Pi
WHISPER_URI = os.getenv("WHISPER_URI", "tcp://127.0.0.1:10300")

import os
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
#  Telegram
# ──────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]

ADMIN_IDS: list[int] = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
]

WELCOME_IMAGE: str = os.getenv("WELCOME_IMAGE", "")

# ──────────────────────────────────────────────────────────────────────────────
#  Anthropic / Claude
# ──────────────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# ──────────────────────────────────────────────────────────────────────────────
#  VedAstro
# ──────────────────────────────────────────────────────────────────────────────
VEDASTRO_API_KEY: str = os.environ["VEDASTRO_API_KEY"]

# ──────────────────────────────────────────────────────────────────────────────
#  Database
# ──────────────────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///bot.db",
)

# ──────────────────────────────────────────────────────────────────────────────
#  Freemium / monetisation
# ──────────────────────────────────────────────────────────────────────────────
PREMIUM_DAYS: int = 30
PREMIUM_MONTHLY_QUESTIONS: int = 3  # bonus questions per month for premium users

PRODUCTS: dict[str, dict] = {
    "premium_month":    {"stars": 500, "kind": "subscription"},
    "q1":               {"stars": 50,  "kind": "questions", "amount": 1},
    "q3":               {"stars": 125, "kind": "questions", "amount": 3},
    "q10":              {"stars": 400, "kind": "questions", "amount": 10},
    "special_synastry": {"stars": 200, "kind": "special"},
    "special_child":    {"stars": 200, "kind": "special"},
    "special_year":     {"stars": 200, "kind": "special"},
}

# ──────────────────────────────────────────────────────────────────────────────
#  Scheduler
# ──────────────────────────────────────────────────────────────────────────────
SCHEDULER_TIMEZONE: str = "UTC"

WEEKLY_FOCUS_DEFAULT_DAY: int = 6    # 0 = Mon … 6 = Sun
WEEKLY_FOCUS_DEFAULT_HOUR: int = 19  # user's local hour

REACTIVATION_AFTER_DAYS: int = 14
PAYWALL_REMINDER_AFTER_DAYS: int = 3
PREMIUM_EXPIRY_REMIND_DAYS: int = 3

# ──────────────────────────────────────────────────────────────────────────────
#  Ekadashi dates  (update annually)
# ──────────────────────────────────────────────────────────────────────────────
EKADASHI_DATES: list[str] = [
    # 2025
    "2025-01-10", "2025-01-25",
    "2025-02-09", "2025-02-24",
    "2025-03-11", "2025-03-25",
    "2025-04-09", "2025-04-24",
    "2025-05-09", "2025-05-23",
    "2025-06-07", "2025-06-22",
    "2025-07-07", "2025-07-21",
    "2025-08-05", "2025-08-20",
    "2025-09-04", "2025-09-19",
    "2025-10-03", "2025-10-18",
    "2025-11-02", "2025-11-17",
    "2025-12-01", "2025-12-16", "2025-12-31",
    # 2026
    "2026-01-15", "2026-01-30",
    "2026-02-14", "2026-02-28",
    "2026-03-15", "2026-03-30",
    "2026-04-14", "2026-04-28",
    "2026-05-14", "2026-05-28",
    "2026-06-12", "2026-06-27",
    "2026-07-11", "2026-07-26",
    "2026-08-10", "2026-08-25",
    "2026-09-08", "2026-09-23",
    "2026-10-08", "2026-10-23",
    "2026-11-06", "2026-11-21",
    "2026-12-06", "2026-12-21",
]

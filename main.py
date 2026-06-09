"""
main.py
=======
Точка входа бота «Раши 2.1».

Порядок регистрации хендлеров (важен — более специфические раньше):
  1. /start, /menu, /help  (CommandHandler)
  2. onboarding ConversationHandler
  3. payments.register      — pay:*, menu:premium, PreCheckout, SUCCESSFUL_PAYMENT group=0
  4. premium.register       — section:weekly, regen:weekly, adv:houses, house:*, section:muhurta + text group=20
  5. questions.register     — menu:question + text group=10
  6. settings.register      — settings:* + text group=40
  7. specials.register      — menu:specials, special:*, SUCCESSFUL_PAYMENT group=1, text group=30
  8. menu.register          — catch-all: menu:main, natal:*, section:*, regen:*, adv:menu

APScheduler стартует через post_init (после инициализации Application),
останавливается через post_shutdown.

БД инициализируется тоже в post_init.

Деплой: `worker: python main.py` (Procfile).
"""
from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import TELEGRAM_TOKEN
from db.models import init_db
from handlers import debug, menu, payments, premium, questions, settings, specials
from handlers.onboarding import build_onboarding_handler, cmd_start
from keyboards.keyboards import main_menu
from scheduler.jobs import setup_scheduler
from texts import TEXTS

# ──────────────────────────────────────────────────────────────────────────────
#  Логирование
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Снижаем уровень шума от httpx / httpcore / apscheduler
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  APScheduler (инициализируется один раз)
# ──────────────────────────────────────────────────────────────────────────────
_scheduler: AsyncIOScheduler | None = None


# ──────────────────────────────────────────────────────────────────────────────
#  Lifecycle hooks
# ──────────────────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Вызывается после Application.initialize() — до первого polling-тика."""
    global _scheduler
    logger.info("Initialising DB …")
    await init_db()
    logger.info("DB ready.")

    _scheduler = setup_scheduler(app.bot)
    _scheduler.start()
    logger.info("Scheduler started.")


async def _post_shutdown(app: Application) -> None:
    """Вызывается после Application.stop()."""
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


# ──────────────────────────────────────────────────────────────────────────────
#  Команды верхнего уровня
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/menu — показать главное меню (или напомнить о регистрации)."""
    from sqlalchemy import select
    from db.models import AsyncSession, User

    async with AsyncSession() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()

    if not user or not user.onboarding_complete:
        await update.message.reply_text(
            TEXTS["errors"]["onboarding_required"],
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        TEXTS["menu"]["title"],
        reply_markup=main_menu(user),
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — краткая справка."""
    await update.message.reply_text(
        TEXTS["common"].get(
            "help_text",
            "Используй /menu для навигации.\n"
            "При проблемах — /start чтобы начать заново.",
        )
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Глобальный обработчик ошибок
# ──────────────────────────────────────────────────────────────────────────────

async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.error(
        "Unhandled exception for update %s",
        update,
        exc_info=context.error,
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(TEXTS["errors"]["generic"])
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
#  Сборка Application
# ──────────────────────────────────────────────────────────────────────────────

def build_app() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # ── Команды ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("diagva", debug.cmd_diagva))  # админская диагностика vedastro

    # ── Онбординг ─────────────────────────────────────────────────────────────
    app.add_handler(build_onboarding_handler())

    # ── Хендлеры в порядке специфичности (от узких к широким) ────────────────
    #
    # payments  → обрабатывает pay:*, menu:premium, все SUCCESSFUL_PAYMENT (group=0)
    # premium   → section:weekly, regen:weekly, adv:houses, house:*, section:muhurta; text group=20
    # questions → menu:question; text group=10
    # settings  → settings:*; text group=40
    # specials  → menu:specials, special:*; SUCCESSFUL_PAYMENT group=1; text group=30
    # menu      → catch-all: menu:main, natal:*, section:*, regen:*, adv:menu
    #
    payments.register(app)
    premium.register(app)
    questions.register(app)
    settings.register(app)
    specials.register(app)
    menu.register(app)   # ← ПОСЛЕДНИЙ: содержит catch-all section: и regen:

    # ── Глобальный error handler ──────────────────────────────────────────────
    app.add_error_handler(error_handler)

    logger.info("Application built. Handlers registered.")
    return app


# ──────────────────────────────────────────────────────────────────────────────
#  Точка входа
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    app = build_app()
    logger.info("Starting polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,   # игнорировать сообщения пока бот был выключен
    )


if __name__ == "__main__":
    main()

"""
handlers/onboarding.py
======================
Поток регистрации нового пользователя.

Шаги:
    /start → [Начать] → Имя → Пол → Дата → Время → Город → Подтверждение
    → asyncio.create_task(_compute_chart) + drama messages + главное меню

Ключевые принципы:
  - Все строки из texts.py, нет хардкода.
  - После подтверждения города сразу запускается фоновый таск.
  - Драматические сообщения покрывают время расчёта (15-30 с).
  - Личность приходит отдельным сообщением когда таск завершится.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime

from sqlalchemy import select
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import WEEKLY_FOCUS_DEFAULT_DAY, WEEKLY_FOCUS_DEFAULT_HOUR
from core.prompts import build_user_prompt, get_system_prompt
from db.models import AsyncSession, User
from services.ai import generate
from services.astrology import build_astro_data, format_natal_code, geocode_city
from services.extractor import FeatureExtractor
from texts import TEXTS

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Состояния ConversationHandler
# ──────────────────────────────────────────────────────────────────────────────
ASK_NAME      = 0
ASK_GENDER    = 1
ASK_DATE      = 2
ASK_TIME      = 3
ASK_CITY      = 4
CONFIRM_CITY  = 5


# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────
def _gender_keyboard() -> InlineKeyboardMarkup:
    t = TEXTS["onboarding"]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t["gender_male"],   callback_data="gender:male"),
        InlineKeyboardButton(t["gender_female"], callback_data="gender:female"),
    ]])


def _city_confirm_keyboard() -> InlineKeyboardMarkup:
    t = TEXTS["common"]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t["yes"], callback_data="city_yes"),
        InlineKeyboardButton(t["no"],  callback_data="city_no"),
    ]])


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура главного меню после онбординга.
    Все разделы разблокированы (бесплатный клик ещё не потрачен).
    При рефакторинге — перенести в keyboards/keyboards.py.
    """
    t = TEXTS["menu"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t["btn_natal"],    callback_data="natal:main")],
        [
            InlineKeyboardButton(t["btn_love"],  callback_data="section:love"),
            InlineKeyboardButton(t["btn_money"], callback_data="section:money"),
        ],
        [
            InlineKeyboardButton(t["btn_karma"],  callback_data="section:karma"),
            InlineKeyboardButton(t["btn_family"], callback_data="section:family"),
        ],
        [InlineKeyboardButton(t["btn_years"],    callback_data="section:years")],
        [InlineKeyboardButton(t["btn_adv"],      callback_data="adv:menu")],
        [InlineKeyboardButton(t["btn_weekly"],   callback_data="section:weekly")],
        [InlineKeyboardButton(t["btn_specials"], callback_data="menu:specials")],
        [InlineKeyboardButton(t["btn_question"], callback_data="menu:question")],
        [InlineKeyboardButton(t["btn_premium"],  callback_data="menu:premium")],
        [InlineKeyboardButton(t["btn_settings"], callback_data="menu:settings")],
    ])


def _validate_date(text: str) -> date | None:
    """Parse ДД.ММ.ГГГГ → date. Returns None on invalid input."""
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text.strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _validate_time(text: str) -> str | None:
    """Validate ЧЧ:ММ. Returns canonical 'HH:MM' or None."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return None


async def _get_user(session, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


# ──────────────────────────────────────────────────────────────────────────────
#  Фоновый расчёт карты
# ──────────────────────────────────────────────────────────────────────────────
async def _compute_chart(
    telegram_id: int,
    birth_date: str,
    birth_time: str,
    lat: float,
    lon: float,
    tz_str: str,
    city: str,
    name: str,
    gender: str,
    bot: Bot,
) -> None:
    """
    Тяжёлый фоновый таск.  Запускается через asyncio.create_task() — не блокирует
    ConversationHandler.

    Порядок:
      1. Параллельный сбор всех метрик VedAstro (asyncio.gather внутри)
      2. Feature Extractor → темы + central_conflict
      3. Форматирование Натального кода
      4. AI-генерация базового разбора личности
      5. Сохранение astro_json в БД + onboarding_complete=True
      6. Отправка разбора пользователю
    """
    try:
        # ── 1. VedAstro ─────────────────────────────────────────────────────
        astro = await build_astro_data(
            birth_date=birth_date,
            birth_time_str=birth_time,
            lat=lat,
            lon=lon,
            tz_str=tz_str,
            city_name=city,
        )

        # ── 2. Feature Extractor ─────────────────────────────────────────────
        chart = {"raw": astro["raw"], "metrics": astro["metrics"]}
        features = FeatureExtractor().extract(chart)
        astro["features"] = features

        # ── 3. Натальный код (без AI) ────────────────────────────────────────
        astro["card_text"] = format_natal_code(astro)

        # ── 4. Разбор личности (AI) ──────────────────────────────────────────
        prompt = build_user_prompt("personality", astro, name, gender)
        system = get_system_prompt(gender)
        personality = await generate(prompt, system=system, max_tokens=1600)
        astro["personality"] = personality

        # ── 5. Сохранение в БД ───────────────────────────────────────────────
        async with AsyncSession() as session:
            user = await _get_user(session, telegram_id)
            if user:
                user.set_astro(astro)
                user.onboarding_complete = True
                user.last_active_at = datetime.utcnow()
                await session.commit()
            else:
                logger.warning(
                    "_compute_chart: user %d not found in DB after onboarding",
                    telegram_id,
                )
                return

        # ── 6. Отправка разбора ─────────────────────────────────────────────
        header = TEXTS["onboarding"]["success_header"].format(name=name)
        await bot.send_message(
            chat_id=telegram_id,
            text=f"{header}\n{personality}",
            parse_mode="HTML",
        )

    except RuntimeError as exc:
        # AI или VedAstro упали
        logger.error("_compute_chart failed for %d: %s", telegram_id, exc)
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=TEXTS["onboarding"]["calc_error"],
            )
        except Exception:
            pass
    except Exception as exc:
        logger.error(
            "_compute_chart unexpected error for %d: %s",
            telegram_id, exc, exc_info=True,
        )
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=TEXTS["onboarding"]["calc_error"],
            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
#  /start — отдельный хендлер (НЕ входит в ConversationHandler)
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает приветствие или главное меню (если уже зарегистрирован).
    Регистрируется в main.py отдельно от onboarding_handler.
    """
    user_tg = update.effective_user

    async with AsyncSession() as session:
        user = await _get_user(session, user_tg.id)

    if user and user.onboarding_complete:
        # Уже зарегистрирован — показываем меню
        name = user.name or user_tg.first_name or "друг"
        await update.message.reply_text(
            TEXTS["onboarding"]["welcome_back"].format(name=name),
            reply_markup=_main_menu_keyboard(),
        )
        return

    # Новый пользователь или не завершил онбординг
    await update.message.reply_text(
        TEXTS["onboarding"]["welcome"],
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                TEXTS["onboarding"]["start_btn"],
                callback_data="onboarding:start",
            )
        ]]),
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Шаги ConversationHandler
# ──────────────────────────────────────────────────────────────────────────────

async def cb_start_onboarding(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Точка входа: пользователь нажал «Начать»."""
    query = update.callback_query
    await query.answer()

    # Инициализируем хранилище данных онбординга
    context.user_data["ob"] = {}

    await query.edit_message_text(
        TEXTS["onboarding"]["ask_name"],
        parse_mode="HTML",
    )
    return ASK_NAME


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip() if update.message.text else ""

    if len(name) < 2:
        await update.message.reply_text(TEXTS["onboarding"]["name_too_short"])
        return ASK_NAME
    if len(name) > 50:
        await update.message.reply_text(TEXTS["onboarding"]["name_too_long"])
        return ASK_NAME

    context.user_data["ob"]["name"] = name

    await update.message.reply_text(
        TEXTS["onboarding"]["ask_gender"].format(name=name),
        reply_markup=_gender_keyboard(),
        parse_mode="HTML",
    )
    return ASK_GENDER


async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = query.data  # "gender:male" | "gender:female"
    gender = data.split(":")[1] if ":" in data else "unknown"

    if gender not in ("male", "female"):
        await query.edit_message_text(TEXTS["onboarding"]["gender_invalid"])
        return ASK_GENDER

    context.user_data["ob"]["gender"] = gender

    await query.edit_message_text(
        TEXTS["onboarding"]["ask_date"],
        parse_mode="HTML",
    )
    return ASK_DATE


async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip() if update.message.text else ""
    parsed = _validate_date(raw)

    if not parsed:
        await update.message.reply_text(
            TEXTS["onboarding"]["date_invalid"],
            parse_mode="HTML",
        )
        return ASK_DATE

    if parsed > date.today():
        await update.message.reply_text(TEXTS["onboarding"]["date_future"])
        return ASK_DATE

    if parsed.year < 1900:
        await update.message.reply_text(TEXTS["onboarding"]["date_too_old"])
        return ASK_DATE

    context.user_data["ob"]["birth_date"] = f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year}"

    await update.message.reply_text(
        TEXTS["onboarding"]["ask_time"],
        parse_mode="HTML",
    )
    return ASK_TIME


async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip() if update.message.text else ""
    parsed = _validate_time(raw)

    if not parsed:
        await update.message.reply_text(
            TEXTS["onboarding"]["time_invalid"],
            parse_mode="HTML",
        )
        return ASK_TIME

    context.user_data["ob"]["birth_time"] = parsed

    await update.message.reply_text(TEXTS["onboarding"]["ask_city"])
    return ASK_CITY


async def handle_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city_input = update.message.text.strip() if update.message.text else ""
    if not city_input:
        await update.message.reply_text(TEXTS["onboarding"]["ask_city"])
        return ASK_CITY

    # Show searching indicator
    searching_msg = await update.message.reply_text(
        TEXTS["onboarding"]["city_searching"].format(city=city_input)
    )

    try:
        lat, lon, short_name, tz_str = await geocode_city(city_input)
    except ValueError:
        await searching_msg.edit_text(TEXTS["onboarding"]["city_not_found"])
        return ASK_CITY
    except RuntimeError:
        await searching_msg.edit_text(TEXTS["errors"]["vedastro_down"])
        return ASK_CITY

    # Save geocode result
    context.user_data["ob"]["lat"]   = lat
    context.user_data["ob"]["lon"]   = lon
    context.user_data["ob"]["city"]  = short_name
    context.user_data["ob"]["tz"]    = tz_str

    await searching_msg.edit_text(
        TEXTS["onboarding"]["city_confirm"].format(city=short_name, tz=tz_str),
        reply_markup=_city_confirm_keyboard(),
        parse_mode="HTML",
    )
    return CONFIRM_CITY


async def confirm_city_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь выбрал «Нет, изменить» — возвращаемся к вводу города."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(TEXTS["onboarding"]["ask_city"])
    return ASK_CITY


async def confirm_city_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Пользователь подтвердил город.

    1. Создаём/обновляем запись User в БД (без astro_json — он придёт из таска).
    2. asyncio.create_task(_compute_chart) — фоновый расчёт.
    3. Отправляем drama_1, спим 3 с, drama_2, спим 4 с.
    4. Отправляем главное меню.
    5. ConversationHandler.END.
    """
    query = update.callback_query
    await query.answer()

    user_tg = update.effective_user
    ob      = context.user_data.get("ob", {})

    name        = ob.get("name", user_tg.first_name or "")
    gender      = ob.get("gender", "unknown")
    birth_date  = ob.get("birth_date", "")
    birth_time  = ob.get("birth_time", "12:00")
    lat         = ob.get("lat", 0.0)
    lon         = ob.get("lon", 0.0)
    city        = ob.get("city", "")
    tz_str      = ob.get("tz", "UTC")

    # ── Создаём/обновляем пользователя в БД ─────────────────────────────────
    async with AsyncSession() as session:
        user = await _get_user(session, user_tg.id)
        if user is None:
            user = User(telegram_id=user_tg.id)
            session.add(user)

        user.username             = user_tg.username
        user.name                 = name
        user.gender               = gender
        user.birth_date           = birth_date
        user.birth_time           = birth_time
        user.birth_city           = city
        user.birth_lat            = lat
        user.birth_lon            = lon
        user.timezone             = tz_str
        user.weekly_focus_day     = WEEKLY_FOCUS_DEFAULT_DAY
        user.weekly_focus_hour    = WEEKLY_FOCUS_DEFAULT_HOUR
        user.onboarding_complete  = False   # таск поставит True когда посчитает
        user.last_active_at       = datetime.utcnow()

        await session.commit()

    # ── Запускаем фоновый расчёт ─────────────────────────────────────────────
    asyncio.create_task(
        _compute_chart(
            telegram_id=user_tg.id,
            birth_date=birth_date,
            birth_time=birth_time,
            lat=lat,
            lon=lon,
            tz_str=tz_str,
            city=city,
            name=name,
            gender=gender,
            bot=context.bot,
        ),
        name=f"chart_{user_tg.id}",
    )

    # ── Drama-сообщения (пока таск считает VedAstro) ─────────────────────────
    await query.edit_message_text(TEXTS["onboarding"]["drama_1"])

    await asyncio.sleep(3)

    await context.bot.send_message(
        chat_id=user_tg.id,
        text=TEXTS["onboarding"]["drama_2"],
    )

    await asyncio.sleep(4)

    await context.bot.send_message(
        chat_id=user_tg.id,
        text=TEXTS["onboarding"]["drama_3"],
    )

    # ── Главное меню ─────────────────────────────────────────────────────────
    await context.bot.send_message(
        chat_id=user_tg.id,
        text=TEXTS["menu"]["title"],
        reply_markup=_main_menu_keyboard(),
    )

    # Очищаем временные данные онбординга
    context.user_data.pop("ob", None)

    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────────
#  Отмена
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("ob", None)
    await update.message.reply_text(TEXTS["onboarding"]["cancelled"])
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────────
#  Сборка ConversationHandler
# ──────────────────────────────────────────────────────────────────────────────
def build_onboarding_handler() -> ConversationHandler:
    """
    Вызывается из main.py.  Возвращает собранный ConversationHandler.

    entry_points: callback «onboarding:start» (кнопка «Начать» в /start).
    Команда /start регистрируется отдельно через cmd_start (не входит сюда).
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_start_onboarding,
                pattern=r"^onboarding:start$",
            ),
        ],
        states={
            ASK_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_name,
                ),
            ],
            ASK_GENDER: [
                CallbackQueryHandler(handle_gender, pattern=r"^gender:"),
            ],
            ASK_DATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_date,
                ),
            ],
            ASK_TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_time,
                ),
            ],
            ASK_CITY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_city,
                ),
            ],
            CONFIRM_CITY: [
                CallbackQueryHandler(confirm_city_yes, pattern=r"^city_yes$"),
                CallbackQueryHandler(confirm_city_no,  pattern=r"^city_no$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start",  cmd_cancel),   # /start во время онбординга = рестарт
        ],
        allow_reentry=True,
        name="onboarding",
    )

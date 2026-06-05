"""
handlers/settings.py
====================
Меню настроек и все вложенные потоки.

Обрабатываемые callbacks:
  menu:settings              → главный экран настроек
  settings:main              → то же (навигация внутри раздела)
  settings:edit_birth        → показать текущие данные + кнопку «Изменить»
  settings:edit_birth_start  → начать поток смены даты рождения
  settings:weekly_time       → выбор дня рассылки
  settings:weekly_day:{n}    → сохранить день, показать выбор часа
  settings:weekly_hour:{n}   → сохранить час, подтвердить
  settings:notifications     → переключить уведомления вкл/выкл
  settings:subscription      → показать статус подписки

MessageHandler (group=40 — тексты для смены даты/времени/города):
  awaiting = "settings_birth_date" → validate → ask time
  awaiting = "settings_birth_time" → validate → ask city
  awaiting = "settings_birth_city" → geocode → confirm

Callback для подтверждения нового города:
  settings:birth_confirm → пересчитать карту (фоновый таск)
  settings:birth_cancel  → отменить

Принцип: бизнес-логика «пересчёт карты» — тот же таск _compute_chart из onboarding.py.
Все тексты — из texts.py["settings"], texts.py["common"].
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime

from sqlalchemy import delete, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db.models import AsyncSession, Forecast, User
from keyboards.keyboards import back_to_menu
from services.astrology import geocode_city
from texts import TEXTS

logger = logging.getLogger(__name__)


# ── Валидаторы ────────────────────────────────────────────────────────────────
def _parse_date(raw: str) -> date | None:
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", raw.strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None

def _parse_time(raw: str) -> str | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if 0 <= h <= 23 and 0 <= mi <= 59 else None

async def _get_user(session, tid: int) -> User | None:
    r = await session.execute(select(User).where(User.telegram_id == tid))
    return r.scalar_one_or_none()


# ── Клавиатуры ────────────────────────────────────────────────────────────────
def _settings_keyboard() -> InlineKeyboardMarkup:
    ts = TEXTS["settings"]
    tc = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ts["btn_edit_birth"],    callback_data="settings:edit_birth")],
        [InlineKeyboardButton(ts["btn_weekly_time"],   callback_data="settings:weekly_time")],
        [InlineKeyboardButton(ts["btn_notifications"], callback_data="settings:notifications")],
        [InlineKeyboardButton(ts["btn_subscription"],  callback_data="settings:subscription")],
        [InlineKeyboardButton(tc["back_to_menu"],      callback_data="menu:main")],
    ])

def _day_picker_keyboard() -> InlineKeyboardMarkup:
    ts = TEXTS["settings"]
    tc = TEXTS["common"]
    days = ts["days"]  # {0: "Понедельник", ..., 6: "Воскресенье"}
    rows, row = [], []
    for i in range(7):
        row.append(InlineKeyboardButton(days[i], callback_data=f"settings:weekly_day:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(tc["back_to_menu"], callback_data="settings:main")])
    return InlineKeyboardMarkup(rows)

def _hour_picker_keyboard(saved_day: int) -> InlineKeyboardMarkup:
    tc = TEXTS["common"]
    # Часы 7-22 в 4 колонки
    hours = list(range(7, 23))
    rows, row = [], []
    for h in hours:
        row.append(InlineKeyboardButton(f"{h}:00", callback_data=f"settings:weekly_hour:{h}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(tc["back_to_menu"], callback_data="settings:weekly_time")])
    return InlineKeyboardMarkup(rows)

def _birth_confirm_keyboard() -> InlineKeyboardMarkup:
    tc = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tc["yes"],    callback_data="settings:birth_confirm")],
        [InlineKeyboardButton(tc["cancel"], callback_data="settings:birth_cancel")],
    ])

def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS["common"]["cancel"], callback_data="settings:main")
    ]])


# ──────────────────────────────────────────────────────────────────────────────
#  Главный экран настроек
# ──────────────────────────────────────────────────────────────────────────────

async def handle_settings_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Callback: menu:settings и settings:main"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting",   None)
    context.user_data.pop("new_birth",  None)

    await query.edit_message_text(
        TEXTS["settings"]["title"],
        reply_markup=_settings_keyboard(),
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Данные рождения
# ──────────────────────────────────────────────────────────────────────────────

async def handle_edit_birth(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показать текущие данные рождения + кнопка «Изменить». Callback: settings:edit_birth"""
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

    ts = TEXTS["settings"]
    tc = TEXTS["common"]
    if not user:
        await query.edit_message_text(TEXTS["errors"]["onboarding_required"])
        return

    body = ts["birth_data_body"].format(
        name=user.name or "—",
        date=user.birth_date or "—",
        time=user.birth_time or "—",
        city=user.birth_city or "—",
        tz=user.timezone or "—",
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(ts["btn_edit_birth"], callback_data="settings:edit_birth_start")],
        [InlineKeyboardButton(tc["back_to_menu"],   callback_data="settings:main")],
    ])
    await query.edit_message_text(
        ts["birth_data_header"] + "\n\n" + body,
        reply_markup=kb, parse_mode=ParseMode.HTML,
    )


async def handle_edit_birth_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Начать поток смены данных рождения. Callback: settings:edit_birth_start"""
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"]  = "settings_birth_date"
    context.user_data["new_birth"] = {}
    await query.edit_message_text(
        TEXTS["settings"]["edit_ask_date"],
        reply_markup=_cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def handle_birth_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Пользователь подтвердил новый город.
    1. Геокодинг (уже в user_data).
    2. Обновить User в БД, обнулить astro_json и Forecast-кэш.
    3. Запустить _compute_chart в фоне.
    4. Сообщить пользователю «пересчитываю».
    Callback: settings:birth_confirm
    """
    from handlers.onboarding import _compute_chart

    query = update.callback_query
    await query.answer()

    nb  = context.user_data.get("new_birth", {})
    context.user_data.pop("awaiting",  None)
    context.user_data.pop("new_birth", None)

    if not all(k in nb for k in ("date", "time", "city", "lat", "lon", "tz")):
        await query.edit_message_text(TEXTS["errors"]["generic"])
        return

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user:
            return

        user.birth_date  = nb["date"]
        user.birth_time  = nb["time"]
        user.birth_city  = nb["city"]
        user.birth_lat   = nb["lat"]
        user.birth_lon   = nb["lon"]
        user.timezone    = nb["tz"]

        # Сброс кэша
        user.astro_json          = None
        user.onboarding_complete = False
        user.free_section_used   = None

        # Удалить устаревшие прогнозы
        await session.execute(
            delete(Forecast).where(Forecast.telegram_id == user.telegram_id)
        )
        await session.commit()

        # Запускаем пересчёт карты в фоне
        asyncio.create_task(
            _compute_chart(
                telegram_id=user.telegram_id,
                birth_date=nb["date"],
                birth_time=nb["time"],
                lat=nb["lat"], lon=nb["lon"],
                tz_str=nb["tz"],
                city=nb["city"],
                name=user.name or "",
                gender=user.gender or "unknown",
                bot=context.bot,
            ),
            name=f"recalc_{user.telegram_id}",
        )

    await query.edit_message_text(
        TEXTS["settings"]["edit_saved"],
        parse_mode=ParseMode.HTML,
    )


async def handle_birth_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Callback: settings:birth_cancel"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting",  None)
    context.user_data.pop("new_birth", None)
    await query.edit_message_text(
        TEXTS["settings"]["title"],
        reply_markup=_settings_keyboard(), parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Время рассылки Фокуса недели
# ──────────────────────────────────────────────────────────────────────────────

async def handle_weekly_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показать пикер дня. Callback: settings:weekly_time"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        TEXTS["settings"]["weekly_day_prompt"],
        reply_markup=_day_picker_keyboard(), parse_mode=ParseMode.HTML,
    )


async def handle_weekly_day(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Сохранить день, показать пикер часа. Callback: settings:weekly_day:{n}"""
    query = update.callback_query
    await query.answer()
    day = int(query.data.split(":")[-1])
    context.user_data["tmp_weekly_day"] = day
    await query.edit_message_text(
        TEXTS["settings"]["weekly_hour_prompt"],
        reply_markup=_hour_picker_keyboard(day), parse_mode=ParseMode.HTML,
    )


async def handle_weekly_hour(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Сохранить день+час в БД. Callback: settings:weekly_hour:{n}"""
    query = update.callback_query
    await query.answer()

    hour = int(query.data.split(":")[-1])
    day  = context.user_data.pop("tmp_weekly_day", 6)

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if user:
            user.weekly_focus_day  = day
            user.weekly_focus_hour = hour
            await session.commit()

    ts = TEXTS["settings"]
    day_name = ts["days"][day]
    await query.edit_message_text(
        ts["weekly_saved"].format(day=day_name, hour=hour),
        reply_markup=_settings_keyboard(), parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Уведомления и подписка
# ──────────────────────────────────────────────────────────────────────────────

async def handle_notifications(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Тумблер уведомлений. Callback: settings:notifications"""
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user:
            return
        user.notifications_enabled = not user.notifications_enabled
        await session.commit()
        enabled = user.notifications_enabled

    ts = TEXTS["settings"]
    msg = ts["notif_toggled_on"] if enabled else ts["notif_toggled_off"]
    await query.edit_message_text(msg, reply_markup=_settings_keyboard(), parse_mode=ParseMode.HTML)


async def handle_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показать статус подписки. Callback: settings:subscription"""
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

    if not user:
        return

    ts = TEXTS["settings"]
    status = ts["sub_active"] if user.is_premium_active else ts["sub_inactive"]
    until  = user.premium_until.strftime("%d.%m.%Y") if user.premium_until else ts["sub_no_date"]
    qs     = user.questions_balance or 0

    body = ts["sub_body"].format(status=status, until=until, questions=qs)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(TEXTS["paywall"]["cta_btn"], callback_data="pay:premium_month")],
        [InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="settings:main")],
    ]) if not user.is_premium_active else InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="settings:main")
    ]])
    await query.edit_message_text(
        ts["sub_header"] + "\n\n" + body + "\n\n" + ts["sub_note"],
        reply_markup=kb, parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Текстовый сборщик (group=40): смена даты рождения
# ──────────────────────────────────────────────────────────────────────────────

async def handle_settings_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текстовые ответы потока смены данных рождения. group=40."""
    state = context.user_data.get("awaiting", "")
    if not state.startswith("settings_birth_"):
        return

    raw = (update.message.text or "").strip()
    nb  = context.user_data.setdefault("new_birth", {})
    ts  = TEXTS["settings"]

    if state == "settings_birth_date":
        parsed = _parse_date(raw)
        if not parsed or parsed > date.today() or parsed.year < 1900:
            await update.message.reply_text(TEXTS["onboarding"]["date_invalid"], parse_mode=ParseMode.HTML)
            return
        nb["date"] = f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year}"
        context.user_data["awaiting"] = "settings_birth_time"
        await update.message.reply_text(ts["edit_ask_time"], reply_markup=_cancel_keyboard(), parse_mode=ParseMode.HTML)

    elif state == "settings_birth_time":
        parsed_t = _parse_time(raw)
        if not parsed_t:
            await update.message.reply_text(TEXTS["onboarding"]["time_invalid"], parse_mode=ParseMode.HTML)
            return
        nb["time"] = parsed_t
        context.user_data["awaiting"] = "settings_birth_city"
        await update.message.reply_text(ts["edit_ask_city"], reply_markup=_cancel_keyboard())

    elif state == "settings_birth_city":
        try:
            lat, lon, city_short, tz_str = await geocode_city(raw)
        except (ValueError, RuntimeError):
            await update.message.reply_text(TEXTS["onboarding"]["city_not_found"])
            return
        nb.update(city=city_short, lat=lat, lon=lon, tz=tz_str)
        context.user_data["awaiting"] = "settings_birth_confirm"

        confirm_text = TEXTS["onboarding"]["city_confirm"].format(city=city_short, tz=tz_str)
        await update.message.reply_text(
            confirm_text, reply_markup=_birth_confirm_keyboard(), parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Регистрация
# ──────────────────────────────────────────────────────────────────────────────

def register(app) -> None:
    for pattern, handler in [
        (r"^menu:settings$",            handle_settings_menu),
        (r"^settings:main$",            handle_settings_menu),
        (r"^settings:edit_birth$",      handle_edit_birth),
        (r"^settings:edit_birth_start$",handle_edit_birth_start),
        (r"^settings:birth_confirm$",   handle_birth_confirm),
        (r"^settings:birth_cancel$",    handle_birth_cancel),
        (r"^settings:weekly_time$",     handle_weekly_time),
        (r"^settings:weekly_day:\d+$",  handle_weekly_day),
        (r"^settings:weekly_hour:\d+$", handle_weekly_hour),
        (r"^settings:notifications$",   handle_notifications),
        (r"^settings:subscription$",    handle_subscription),
    ]:
        app.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    # Текстовые ответы потока смены данных рождения — group=40
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_text),
        group=40,
    )

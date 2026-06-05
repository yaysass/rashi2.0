"""
handlers/premium.py
===================
Логика премиум-функций: Продвинутый Джйотиш, Фокус недели, Разбор 12 домов,
мини-поток Мухурты.

Обрабатываемые callbacks (должны быть зарегистрированы ДО catch-all menu.py):
  section:weekly   → Фокус недели (с кэшем Forecast)
  regen:weekly     → перегенерация Фокуса, обновление кэша
  adv:houses       → подменю «Разбор 12 Домов»
  house:{n}        → разбор конкретного дома (1-12)
  section:muhurta  → начало мини-потока Мухурты

MessageHandler (group=20):
  • Ловит тексты в состояниях awaiting=muhurta_event и awaiting=muhurta_period.
  • Остальные тексты пропускает (группа 20 — выше приоритетом, чем 10 у questions).

Кэш Фокуса недели:
  Ключ: YYYY-W{nn} (ISO-неделя, например «2025-W03»).
  Таблица: Forecast(telegram_id, period_key, content).
  При первом открытии на неделе генерирует и сохраняет.
  При regen:weekly — удаляет старое, генерирует заново.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import delete, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.access import can_open_premium
from core.prompts import build_user_prompt, get_system_prompt
from db.models import AsyncSession, Forecast, User
from handlers.menu import run_reading
from keyboards.keyboards import back_to_menu, premium_only_keyboard
from services.ai import generate
from texts import TEXTS

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Утилиты
# ──────────────────────────────────────────────────────────────────────────────

def _current_week_key() -> str:
    """ISO-неделя в формате «YYYY-W{nn}», например '2025-W03'."""
    d = date.today()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


async def _get_user(session, telegram_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


def _reading_done_keyboard(reading_key: str, show_regen: bool = True) -> InlineKeyboardMarkup:
    tc = TEXTS["common"]
    rows = []
    if show_regen:
        rows.append([InlineKeyboardButton(tc["regenerate"], callback_data=f"regen:{reading_key}")])
    rows.append([InlineKeyboardButton(tc["back_to_menu"], callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Фокус недели
# ──────────────────────────────────────────────────────────────────────────────

async def _get_or_generate_weekly(
    session,
    user: User,
    period_key: str,
    force: bool = False,
) -> str | None:
    """
    Возвращает текст Фокуса недели.
    Если force=False — пробует кэш, при промахе генерирует и кэширует.
    Если force=True — удаляет старый кэш, генерирует заново.
    """
    astro = user.get_astro()
    if not astro:
        return None

    # ── Попытка кэша ────────────────────────────────────────────────────────
    if not force:
        row = (await session.execute(
            select(Forecast).where(
                Forecast.telegram_id == user.telegram_id,
                Forecast.period_key  == period_key,
            )
        )).scalar_one_or_none()
        if row:
            logger.debug("Weekly focus cache hit: user=%s week=%s", user.telegram_id, period_key)
            return row.content

    # ── Генерация ────────────────────────────────────────────────────────────
    try:
        prompt = build_user_prompt(
            section_key="weekly",
            astro_json=astro,
            user_name=user.name or "пользователь",
            gender=user.gender or "unknown",
            week=period_key,
        )
        system = get_system_prompt(user.gender or "unknown")
        content = await generate(prompt=prompt, system=system, max_tokens=900)
    except RuntimeError as exc:
        logger.error("Weekly focus generation failed: %s", exc)
        return None

    # ── Сохранение в кэш ─────────────────────────────────────────────────────
    if force:
        await session.execute(
            delete(Forecast).where(
                Forecast.telegram_id == user.telegram_id,
                Forecast.period_key  == period_key,
            )
        )
    session.add(Forecast(
        telegram_id=user.telegram_id,
        period_key=period_key,
        content=content,
        sent=False,
    ))
    await session.commit()
    logger.info(
        "Weekly focus %s: generated for user %s (force=%s)",
        period_key, user.telegram_id, force,
    )
    return content


async def handle_weekly(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Показать (или сгенерировать) Фокус недели.
    Callback: section:weekly
    """
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

        if not user or not can_open_premium(user):
            await query.edit_message_text(
                TEXTS["paywall"]["premium_only_body"],
                reply_markup=premium_only_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        await query.edit_message_text(
            TEXTS["premium"]["weekly_loading"],
            parse_mode=ParseMode.HTML,
        )

        period_key = _current_week_key()
        content = await _get_or_generate_weekly(session, user, period_key, force=False)

    if not content:
        await update.effective_message.edit_text(
            TEXTS["errors"]["generation_failed"],
            reply_markup=_reading_done_keyboard("weekly", show_regen=True),
            parse_mode=ParseMode.HTML,
        )
        return

    header = TEXTS["premium"]["weekly_title"] + "\n\n"
    await update.effective_message.edit_text(
        header + content,
        reply_markup=_reading_done_keyboard("weekly", show_regen=True),
        parse_mode=ParseMode.HTML,
    )


async def handle_regen_weekly(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Перегенерация Фокуса недели (force=True → удаляет старый кэш).
    Callback: regen:weekly
    """
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

        if not user or not can_open_premium(user):
            await query.edit_message_text(
                TEXTS["paywall"]["premium_only_body"],
                reply_markup=premium_only_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        await query.edit_message_text(
            TEXTS["premium"]["weekly_loading"],
            parse_mode=ParseMode.HTML,
        )

        period_key = _current_week_key()
        content = await _get_or_generate_weekly(session, user, period_key, force=True)

    header = TEXTS["premium"]["weekly_title"] + "\n\n"
    text = (header + content) if content else TEXTS["errors"]["generation_failed"]
    await update.effective_message.edit_text(
        text,
        reply_markup=_reading_done_keyboard("weekly", show_regen=True),
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Разбор 12 Домов
# ──────────────────────────────────────────────────────────────────────────────

def _houses_keyboard() -> InlineKeyboardMarkup:
    tp = TEXTS["premium"]
    tc = TEXTS["common"]
    # 12 домов по 3 в ряд
    rows = []
    for start in range(1, 13, 3):
        row = [
            InlineKeyboardButton(
                tp["btn_house"].format(n=n),
                callback_data=f"house:{n}",
            )
            for n in range(start, min(start + 3, 13))
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton(tc["back_to_menu"], callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def _build_house_prompt(astro: dict, user_name: str, gender: str, house_n: int) -> str:
    """
    Строит промпт для разбора конкретного дома.

    Использует build_user_prompt("personality") чтобы получить ПОЛНЫЙ набор
    chart_anchors (все планеты + лагна), затем заменяет стандартную инструкцию
    на house-specific.
    """
    full = build_user_prompt(
        section_key="personality",
        astro_json=astro,
        user_name=user_name,
        gender=gender,
    )
    # Отделяем данные карты от инструкции по разделителю "—" × 40
    sep = "—" * 40
    data_section = full.split(sep)[0] if sep in full else full

    house_instruction = (
        f"Пользователь открыл «{house_n}-й Дом».\n\n"
        f"Расскажи про {house_n}-й дом в этой карте:\n"
        f"- какой знак стоит на {house_n}-м доме и что он говорит про тему дома\n"
        f"- планеты в {house_n}-м доме (если есть): как они влияют на эту сферу\n"
        f"- управитель {house_n}-го дома: где он стоит и что это означает на практике\n"
        f"- как тема {house_n}-го дома проявляется в конкретных паттернах жизни\n\n"
        f"Раскрой через узнавание: сначала как это ощущается, потом астрологическая опора."
    )
    return data_section + sep + "\n\n" + house_instruction


async def handle_houses_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Показать подменю «Разбор 12 Домов». Callback: adv:houses"""
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

    if not user or not can_open_premium(user):
        await query.edit_message_text(
            TEXTS["paywall"]["premium_only_body"],
            reply_markup=premium_only_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text(
        TEXTS["premium"]["houses_title"],
        reply_markup=_houses_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def handle_house_reading(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Генерирует разбор конкретного дома.
    Callback: house:{n}
    """
    query = update.callback_query
    await query.answer()

    house_n = int(query.data.split(":", 1)[1])

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

        if not user or not can_open_premium(user):
            await query.edit_message_text(
                TEXTS["paywall"]["premium_only_body"],
                reply_markup=premium_only_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        astro = user.get_astro()
        if not astro:
            await query.edit_message_text(
                TEXTS["errors"]["calculation_pending"],
                reply_markup=back_to_menu(),
                parse_mode=ParseMode.HTML,
            )
            return

        await query.edit_message_text(
            TEXTS["common"]["loading"],
            parse_mode=ParseMode.HTML,
        )

        prompt = _build_house_prompt(
            astro=astro,
            user_name=user.name or "пользователь",
            gender=user.gender or "unknown",
            house_n=house_n,
        )
        system = get_system_prompt(user.gender or "unknown")

    try:
        text = await generate(prompt=prompt, system=system, max_tokens=1400)
    except RuntimeError as exc:
        logger.error("House %d reading failed: %s", house_n, exc)
        text = TEXTS["errors"]["generation_failed"]

    tp = TEXTS["premium"]
    header = f"Дом {house_n}\n\n"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(TEXTS["common"]["regenerate"], callback_data=f"house:{house_n}")],
        [InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="adv:houses")],
    ])
    try:
        await update.effective_message.edit_text(
            header + text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await update.effective_message.reply_text(
            header + text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Мухурта — мини-поток: событие → период → генерация
# ──────────────────────────────────────────────────────────────────────────────

async def handle_muhurta_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Начало мини-потока Мухурты: спрашивает, для какого события.
    Callback: section:muhurta
    """
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

    if not user or not can_open_premium(user):
        await query.edit_message_text(
            TEXTS["paywall"]["premium_only_body"],
            reply_markup=premium_only_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data["awaiting"] = "muhurta_event"
    context.user_data.pop("muhurta_event",  None)
    context.user_data.pop("muhurta_period", None)

    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS["common"]["cancel"], callback_data="menu:main")
    ]])
    await query.edit_message_text(
        TEXTS["premium"]["muhurta_ask_event"],
        reply_markup=cancel_kb,
        parse_mode=ParseMode.HTML,
    )


async def handle_muhurta_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Обрабатывает текстовые ответы в потоке Мухурты.
    Зарегистрирован в group=20 — проверяет user_data["awaiting"] перед обработкой.
    MessageHandler(TEXT & ~COMMAND, group=20).
    """
    awaiting = context.user_data.get("awaiting", "")
    if awaiting not in ("muhurta_event", "muhurta_period"):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # ── Шаг 1: сохранить событие, спросить период ────────────────────────────
    if awaiting == "muhurta_event":
        context.user_data["muhurta_event"] = text
        context.user_data["awaiting"] = "muhurta_period"

        cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(TEXTS["common"]["cancel"], callback_data="menu:main")
        ]])
        await update.message.reply_text(
            TEXTS["premium"]["muhurta_ask_period"],
            reply_markup=cancel_kb,
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Шаг 2: генерация ─────────────────────────────────────────────────────
    event  = context.user_data.pop("muhurta_event", "")
    period = text
    context.user_data.pop("awaiting", None)

    await update.message.reply_text(
        TEXTS["common"]["loading"],
        parse_mode=ParseMode.HTML,
    )

    # Делегируем в run_reading с нужными параметрами
    await run_reading(
        update=update,
        context=context,
        key="muhurta",
        event=event,
        period=period,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Регистрация
# ──────────────────────────────────────────────────────────────────────────────

def register(app) -> None:
    """
    Регистрировать ДО handlers/menu.register(app).

    section:weekly и regen:weekly перехватываются здесь (точные паттерны)
    до того, как menu.py зарегистрирует catch-all section: и regen:.
    """
    # Фокус недели
    app.add_handler(CallbackQueryHandler(
        handle_weekly,       pattern=r"^section:weekly$"
    ))
    app.add_handler(CallbackQueryHandler(
        handle_regen_weekly, pattern=r"^regen:weekly$"
    ))

    # Дома
    app.add_handler(CallbackQueryHandler(
        handle_houses_menu,   pattern=r"^adv:houses$"
    ))
    app.add_handler(CallbackQueryHandler(
        handle_house_reading, pattern=r"^house:\d+$"
    ))

    # Мухурта
    app.add_handler(CallbackQueryHandler(
        handle_muhurta_start, pattern=r"^section:muhurta$"
    ))

    # Текстовые ответы в потоке Мухурты — group=20
    # Все группы обрабатывают каждый апдейт независимо; хендлер проверяет
    # user_data["awaiting"] и быстро выходит если не в своём состоянии.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_muhurta_text),
        group=20,
    )

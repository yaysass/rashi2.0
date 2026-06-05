"""
handlers/specials.py
====================
Особые разборы: Синастрия / Детская карта / Вектор года.

Жизненный цикл:
  1. Пользователь нажимает кнопку в меню → payments.py отправляет инвойс.
  2. После successful_payment:
       payments.py  (group=0) → логирует оплату, ставит user_data["special_pending"].
       specials.py  (group=1) → видит тот же апдейт → запускает start_special_flow().
  3. Сбор дополнительных данных (синастрия/ребёнок) через user_data["awaiting"].
  4. Подтверждение → геокодинг → build_astro_data → generate.

Вектор года: доп. данных не нужно, генерируется сразу.

Callback-паттерны (регистрировать ДО menu.py):
  menu:specials         → показать каталог особых разборов
  special:confirm       → подтвердить данные и запустить генерацию
  special:cancel        → отменить сбор данных

MessageHandler (group=1 для SUCCESSFUL_PAYMENT, group=30 для текстовых ответов):
  group=1  → перехватывает special_* платежи после payments.py (group=0)
  group=30 → сбор имени / даты / времени / города

Вся стоимость — в config.PRODUCTS, описания — в texts.py["specials"].
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.prompts import _extract_anchors, build_user_prompt, get_system_prompt
from db.models import AsyncSession, User
from keyboards.keyboards import back_to_menu
from services.ai import generate
from services.astrology import build_astro_data, geocode_city
from texts import TEXTS

logger = logging.getLogger(__name__)

# ── Валидаторы (те же что в onboarding) ──────────────────────────────────────
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

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS["common"]["cancel"], callback_data="special:cancel")
    ]])

def _confirm_kb(kind: str) -> InlineKeyboardMarkup:
    tc = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tc["yes"],    callback_data=f"special:confirm:{kind}")],
        [InlineKeyboardButton(tc["cancel"], callback_data="special:cancel")],
    ])

def _format_anchors(astro: dict, section: str) -> str:
    anchors = _extract_anchors(section, astro)
    return ", ".join(f"{k}: {v}" for k, v in anchors.items() if v) or "—"

# ──────────────────────────────────────────────────────────────────────────────
#  Меню Особых разборов
# ──────────────────────────────────────────────────────────────────────────────

async def handle_specials_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показать каталог особых разборов. Callback: menu:specials"""
    query = update.callback_query
    await query.answer()
    ts = TEXTS["specials"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(ts["btn_synastry"], callback_data="pay:special_synastry")],
        [InlineKeyboardButton(ts["btn_child"],    callback_data="pay:special_child")],
        [InlineKeyboardButton(ts["btn_year"],     callback_data="pay:special_year")],
        [InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="menu:main")],
    ])
    await query.edit_message_text(
        ts["title"] + "\n\n" + ts["body"],
        reply_markup=kb, parse_mode=ParseMode.HTML,
    )

# ──────────────────────────────────────────────────────────────────────────────
#  Запуск потока после оплаты  (вызывается из group=1 SUCCESSFUL_PAYMENT)
# ──────────────────────────────────────────────────────────────────────────────

async def start_special_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, product_key: str
) -> None:
    """
    Точка входа после оплаты особого разбора.
    Для transit_year — генерирует сразу.
    Для synastry/child — начинает сбор данных.
    """
    ts = TEXTS["specials"]
    context.user_data["sp_data"] = {}

    if product_key == "special_synastry":
        context.user_data["awaiting"] = "synastry_name"
        await update.message.reply_text(ts["synastry_ask_name"], reply_markup=_cancel_kb())

    elif product_key == "special_child":
        context.user_data["awaiting"] = "child_name"
        await update.message.reply_text(ts["child_ask_name"], reply_markup=_cancel_kb())

    elif product_key == "special_year":
        # Вектор года — доп. данные не нужны
        context.user_data.pop("awaiting", None)
        await _run_transit_year(update, context)


async def _handle_special_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    SUCCESSFUL_PAYMENT group=1. Фильтрует special_* и запускает поток.
    payments.py (group=0) уже залоговал оплату.
    """
    payload = update.message.successful_payment.invoice_payload
    if not payload.startswith("special_"):
        return
    await start_special_flow(update, context, payload)

# ──────────────────────────────────────────────────────────────────────────────
#  Генераторы
# ──────────────────────────────────────────────────────────────────────────────

async def _run_transit_year(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Вектор года — использует chart пользователя, доп. данных нет."""
    msg = update.message or update.effective_message
    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user or not user.get_astro():
            await msg.reply_text(TEXTS["errors"]["calculation_pending"])
            return
        astro = user.get_astro()

    loading = await msg.reply_text(TEXTS["specials"]["generating"], parse_mode=ParseMode.HTML)
    try:
        prompt = build_user_prompt("transit_year", astro, user.name or "", user.gender or "unknown")
        text = await generate(prompt=prompt, system=get_system_prompt(user.gender or "unknown"), max_tokens=1400)
    except RuntimeError:
        text = TEXTS["errors"]["generation_failed"]

    await loading.edit_text(text, reply_markup=back_to_menu(), parse_mode=ParseMode.HTML)


async def _run_synastry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Финальная генерация синастрии после подтверждения данных партнёра."""
    sp   = context.user_data.get("sp_data", {})
    msg  = update.message or update.effective_message

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user or not user.get_astro():
            await msg.reply_text(TEXTS["errors"]["calculation_pending"])
            return
        user_astro = user.get_astro()

    loading = await msg.reply_text(TEXTS["specials"]["generating"], parse_mode=ParseMode.HTML)
    try:
        lat, lon, city_short, tz_str = await geocode_city(sp["city"])
        partner_astro = await build_astro_data(
            birth_date=sp["date"], birth_time_str=sp["time"],
            lat=lat, lon=lon, tz_str=tz_str, city_name=city_short,
        )
        anchors1 = _format_anchors(user_astro,    "synastry")
        anchors2 = _format_anchors(partner_astro, "synastry")
        prompt = build_user_prompt(
            "synastry", user_astro, user.name or "", user.gender or "unknown",
            name1=user.name or "", name2=sp["name"],
            anchors1=anchors1, anchors2=anchors2,
        )
        text = await generate(prompt=prompt, system=get_system_prompt(user.gender or "unknown"), max_tokens=1400)
    except Exception as exc:
        logger.error("Synastry generation failed: %s", exc)
        text = TEXTS["errors"]["generation_failed"]

    await loading.edit_text(text, reply_markup=back_to_menu(), parse_mode=ParseMode.HTML)


async def _run_child(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Финальная генерация детской карты после подтверждения данных ребёнка."""
    sp  = context.user_data.get("sp_data", {})
    msg = update.message or update.effective_message

    loading = await msg.reply_text(TEXTS["specials"]["generating"], parse_mode=ParseMode.HTML)
    try:
        lat, lon, city_short, tz_str = await geocode_city(sp["city"])
        child_astro = await build_astro_data(
            birth_date=sp["date"], birth_time_str=sp["time"],
            lat=lat, lon=lon, tz_str=tz_str, city_name=city_short,
        )
        # Разбор строится вокруг карты ребёнка; user_name = имя ребёнка
        prompt = build_user_prompt(
            "child", child_astro, sp["name"], "unknown",
            child_name=sp["name"],
        )
        async with AsyncSession() as session:
            user = await _get_user(session, update.effective_user.id)
            system = get_system_prompt(user.gender if user else "unknown")
        text = await generate(prompt=prompt, system=system, max_tokens=1400)
    except Exception as exc:
        logger.error("Child reading generation failed: %s", exc)
        text = TEXTS["errors"]["generation_failed"]

    await loading.edit_text(text, reply_markup=back_to_menu(), parse_mode=ParseMode.HTML)

# ──────────────────────────────────────────────────────────────────────────────
#  Сборщик данных — текстовые ответы (group=30)
# ──────────────────────────────────────────────────────────────────────────────

# Машина состояний: ключ awaiting → (следующее состояние, ключ сохранения, текст следующего вопроса)
_SYNASTRY_STEPS = {
    "synastry_name": ("synastry_date",  "name", "synastry_ask_date"),
    "synastry_date": ("synastry_time",  "date", "synastry_ask_time"),
    "synastry_time": ("synastry_city",  "time", "synastry_ask_city"),
}
_CHILD_STEPS = {
    "child_name": ("child_date", "name", "child_ask_date"),
    "child_date": ("child_time", "date", "child_ask_time"),
    "child_time": ("child_city", "time", "child_ask_city"),
}

async def handle_specials_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Обрабатывает текстовые ответы в потоках синастрии и детской карты."""
    state = context.user_data.get("awaiting", "")
    if not (state.startswith("synastry_") or state.startswith("child_")):
        return

    raw  = (update.message.text or "").strip()
    ts   = TEXTS["specials"]
    sp   = context.user_data.setdefault("sp_data", {})
    kind = "synastry" if state.startswith("synastry_") else "child"
    steps = _SYNASTRY_STEPS if kind == "synastry" else _CHILD_STEPS

    # ── Валидация полей дата/время ────────────────────────────────────────────
    if state in ("synastry_date", "child_date"):
        if not _parse_date(raw):
            await update.message.reply_text(TEXTS["onboarding"]["date_invalid"], parse_mode=ParseMode.HTML)
            return
    if state in ("synastry_time", "child_time"):
        parsed_t = _parse_time(raw)
        if not parsed_t:
            await update.message.reply_text(TEXTS["onboarding"]["time_invalid"], parse_mode=ParseMode.HTML)
            return
        raw = parsed_t

    if state in steps:
        next_state, save_key, next_question_key = steps[state]
        sp[save_key] = raw
        context.user_data["awaiting"] = next_state
        await update.message.reply_text(ts[next_question_key], reply_markup=_cancel_kb())

    # Последний шаг — город: показать подтверждение
    elif state in ("synastry_city", "child_city"):
        sp["city"] = raw
        context.user_data["awaiting"] = f"{kind}_confirm"
        confirm_key = "synastry_confirm" if kind == "synastry" else "synastry_confirm"
        text = ts[confirm_key].format(
            name=sp.get("name",""), date=sp.get("date",""),
            time=sp.get("time",""), city=sp.get("city",""),
        ) if kind == "synastry" else (
            f"Данные {sp.get('name','')}:\n"
            f"<b>{sp.get('date','')}</b>, {sp.get('time','')}, {sp.get('city','')}\n\n"
            "Запустить разбор?"
        )
        await update.message.reply_text(text, reply_markup=_confirm_kb(kind), parse_mode=ParseMode.HTML)

# ──────────────────────────────────────────────────────────────────────────────
#  Confirm / Cancel callbacks
# ──────────────────────────────────────────────────────────────────────────────

async def handle_special_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Callback: special:confirm:{kind}"""
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":")[-1]
    context.user_data.pop("awaiting", None)

    if kind == "synastry":
        await _run_synastry(update, context)
    elif kind == "child":
        await _run_child(update, context)


async def handle_special_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Callback: special:cancel — сброс состояния."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting", None)
    context.user_data.pop("sp_data",  None)
    await query.edit_message_text(
        TEXTS["menu"]["title"],
        reply_markup=back_to_menu(), parse_mode=ParseMode.HTML,
    )

# ──────────────────────────────────────────────────────────────────────────────
#  Регистрация
# ──────────────────────────────────────────────────────────────────────────────

def register(app) -> None:
    """
    Регистрировать ДО menu.register(app).
    SUCCESSFUL_PAYMENT в group=1 — после payments.py (group=0).
    """
    app.add_handler(CallbackQueryHandler(handle_specials_menu,   pattern=r"^menu:specials$"))
    app.add_handler(CallbackQueryHandler(handle_special_confirm, pattern=r"^special:confirm:"))
    app.add_handler(CallbackQueryHandler(handle_special_cancel,  pattern=r"^special:cancel$"))

    # Перехват SUCCESSFUL_PAYMENT для special_* — group=1
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, _handle_special_payment),
        group=1,
    )
    # Сбор данных (name / date / time / city) — group=30
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_specials_text),
        group=30,
    )

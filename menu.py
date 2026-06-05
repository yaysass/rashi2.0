"""
handlers/menu.py
================
Главное меню + тематические разборы.

Архитектурные ограничения (из ТЗ):
  - НЕТ длинных if/elif. Маршрутизация — через словарь _HANDLERS.
  - Доступ проверяется ТОЛЬКО через core/access.py.
  - Разборы читаются из core/catalog.py (READINGS).
  - Все строки — из texts.TEXTS.

Точка входа для main.py:
    from handlers.menu import setup_menu_handlers
    setup_menu_handlers(application)
"""
from __future__ import annotations

import logging
from typing import Callable, Coroutine

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from core.access import (
    AccessResult,
    check_and_grant,
    check_regen_access,
    is_section_locked,
)
from core.catalog import READINGS
from core.prompts import build_user_prompt, get_system_prompt
from db.crud import get_user
from db.models import AsyncSession
from keyboards.keyboards import (
    adv_jyotish_kb,
    back_to_menu_kb,
    houses_kb,
    main_menu_kb,
    natal_card_kb,
    natal_code_kb,
    paywall_kb,
    questions_shop_kb,
    reading_actions_kb,
    settings_kb,
    specials_kb,
)
from services.ai import generate
from texts import TEXTS

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Мелкие хелперы
# ──────────────────────────────────────────────────────────────────────────────

async def _safe_edit(
    query: CallbackQuery,
    text: str,
    **kwargs,
) -> None:
    """Edit message in-place; gracefully handles Telegram errors."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        # "Message is not modified" — нормально при повторном нажатии
        if "not modified" not in str(e).lower():
            logger.warning("edit_message_text failed: %s", e)
    except Exception as e:
        logger.error("Unexpected error editing message: %s", e)


async def _edit_by_id(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    text: str,
    **kwargs,
) -> None:
    """Edit after async gap (query might no longer be fresh)."""
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            **kwargs,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            # Если редактировать не удалось — отправляем новым сообщением
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    **{k: v for k, v in kwargs.items()
                       if k in ("parse_mode", "reply_markup")},
                )
            except Exception as send_err:
                logger.error("send_message fallback failed: %s", send_err)
    except Exception as e:
        logger.error("_edit_by_id failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
#  Ядро: run_reading
# ──────────────────────────────────────────────────────────────────────────────

async def run_reading(
    section_key: str,
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    is_regen: bool = False,
    extra: dict | None = None,
) -> None:
    """
    Единственная точка генерации разборов.

    Алгоритм:
      1. Читаем User из БД.
      2. Проверяем ready-статус карты.
      3. Для natalty (personality) без regen — возвращаем кэш.
      4. Проверяем доступ через access.py (только при !is_regen).
      5. Показываем loading-заглушку.
      6. Генерируем через AI.
      7. Обновляем кэш personality если нужно.
      8. Отправляем результат с кнопками.
    """
    telegram_id = query.from_user.id
    chat_id     = query.message.chat_id
    message_id  = query.message.message_id

    # ── 1. Читаем пользователя ───────────────────────────────────────────────
    async with AsyncSession() as session:
        user = await get_user(session, telegram_id)

        if not user or not user.onboarding_complete:
            await _safe_edit(query, TEXTS["errors"]["onboarding_required"])
            return

        astro = user.get_astro()
        if not astro or not astro.get("features"):
            await _safe_edit(query, TEXTS["errors"]["calculation_pending"])
            return

        # ── 2. Кэш personality (только при первом открытии) ──────────────────
        if section_key == "personality" and not is_regen:
            cached_text = astro.get("personality")
            if cached_text:
                await _safe_edit(
                    query,
                    cached_text,
                    parse_mode="HTML",
                    reply_markup=natal_card_kb(),
                )
                return

        # ── 3. Проверка доступа ──────────────────────────────────────────────
        if is_regen:
            if not check_regen_access(user, section_key):
                await _safe_edit(
                    query,
                    TEXTS["paywall"]["locked_body"],
                    reply_markup=paywall_kb(),
                )
                return
        else:
            result = await check_and_grant(session, user, section_key)

            if result == AccessResult.NOT_READY:
                await _safe_edit(query, TEXTS["errors"]["calculation_pending"])
                return
            if result == AccessResult.PAYWALL_FREEMIUM:
                # Записать время для брошенного-пейволл планировщика
                from datetime import datetime
                if not user.paywall_shown_at:
                    user.paywall_shown_at = datetime.utcnow()
                    await session.commit()
                await _safe_edit(
                    query,
                    TEXTS["paywall"]["locked_body"],
                    reply_markup=paywall_kb(),
                )
                return
            if result == AccessResult.PAYWALL_PREMIUM:
                await _safe_edit(
                    query,
                    TEXTS["paywall"]["premium_only_body"],
                    reply_markup=paywall_kb(premium_only=True),
                )
                return

        # Конфигурация раздела
        config = READINGS.get(section_key)
        if not config:
            await _safe_edit(query, TEXTS["errors"]["reading_not_found"])
            return

        # Сохраняем всё нужное до закрытия сессии
        user_name    = user.name or "друг"
        user_gender  = user.gender or "unknown"
        show_regen   = is_regen or check_regen_access(user, section_key)

    # ── 4. Loading-заглушка ───────────────────────────────────────────────────
    await _safe_edit(query, TEXTS["common"]["loading"])

    # ── 5. Генерация (может занять 5–15 с, сессия уже закрыта) ───────────────
    try:
        prompt = build_user_prompt(
            section_key, astro, user_name, user_gender,
            **(extra or {}),
        )
        system = get_system_prompt(user_gender)
        text = await generate(prompt, system=system, max_tokens=config.max_tokens)
    except RuntimeError as exc:
        logger.error("run_reading generate failed [%s]: %s", section_key, exc)
        await _edit_by_id(
            context, chat_id, message_id,
            TEXTS["errors"]["generation_failed"],
            reply_markup=back_to_menu_kb(),
        )
        return

    # ── 6. Кэшировать personality ─────────────────────────────────────────────
    if section_key == "personality":
        async with AsyncSession() as session:
            user_upd = await get_user(session, telegram_id)
            if user_upd:
                astro_upd = user_upd.get_astro() or {}
                astro_upd["personality"] = text
                user_upd.set_astro(astro_upd)
                await session.commit()

    # ── 7. Отправить результат ────────────────────────────────────────────────
    kb = (
        natal_card_kb()
        if section_key == "personality"
        else reading_actions_kb(section_key, show_regen=show_regen)
    )

    await _edit_by_id(
        context, chat_id, message_id,
        text,
        parse_mode="HTML",
        reply_markup=kb,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Атомарные обработчики (один callback-prefix = один обработчик)
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_section(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Все разборы: section:love, section:navamsha, ..."""
    section_key = query.data.split(":")[1]
    await run_reading(section_key, query, context, is_regen=False)


async def _handle_regen(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перегенерация: regen:love, regen:personality, ..."""
    section_key = query.data.split(":")[1]
    await run_reading(section_key, query, context, is_regen=True)


async def _handle_natal(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Натальная карта: natal:main (разбор личности) | natal:code (сырые данные)."""
    sub = query.data.split(":")[1] if ":" in query.data else ""

    if sub == "main":
        await run_reading("personality", query, context, is_regen=False)
        return

    # natal:code — мгновенный вывод, без AI
    telegram_id = query.from_user.id
    async with AsyncSession() as session:
        user = await get_user(session, telegram_id)

    if not user:
        await _safe_edit(query, TEXTS["errors"]["onboarding_required"])
        return

    astro = user.get_astro()
    card_text = astro.get("card_text", "") if astro else ""

    if not card_text:
        await _safe_edit(query, TEXTS["errors"]["calculation_pending"])
        return

    header = TEXTS["natal"]["code_header"]
    await _safe_edit(
        query,
        f"{header}\n\n{card_text}",
        reply_markup=natal_code_kb(),
    )


async def _handle_house(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Разбор конкретного дома: house:1 .. house:12"""
    try:
        n = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await _safe_edit(query, TEXTS["errors"]["reading_not_found"])
        return
    await run_reading(f"house_{n}", query, context, is_regen=False, extra={"house_n": n})


# ── Навигация по подменю ───────────────────────────────────────────────────────

async def _show_main_menu(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = query.from_user.id
    async with AsyncSession() as session:
        user = await get_user(session, telegram_id)

    if not user or not user.onboarding_complete:
        await _safe_edit(query, TEXTS["errors"]["onboarding_required"])
        return

    await _safe_edit(
        query,
        TEXTS["menu"]["title"],
        reply_markup=main_menu_kb(user),
    )


async def _show_premium(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    pt = TEXTS["premium"]
    await _safe_edit(
        query,
        pt["body"],
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(pt["btn_buy"], callback_data="buy:premium_month")],
            [InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="menu:main")],
        ]),
    )


async def _show_specials(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_edit(
        query,
        TEXTS["specials"]["body"],
        parse_mode="HTML",
        reply_markup=specials_kb(),
    )


async def _show_question(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вопрос: показать баланс или магазин.
    Сам поток ответа — в handlers/questions.py (Фаза 6).
    """
    from texts import pluralize
    telegram_id = query.from_user.id

    async with AsyncSession() as session:
        from db.crud import get_or_replenish_questions
        user = await get_user(session, telegram_id)
        if not user:
            await _safe_edit(query, TEXTS["errors"]["onboarding_required"])
            return
        balance = await get_or_replenish_questions(session, user)

    if balance > 0:
        word = pluralize(balance, TEXTS["shop"]["words_q"])
        await _safe_edit(
            query,
            TEXTS["shop"]["balance_info"].format(n=balance, word=word),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    TEXTS["shop"]["ask_question"].split("\n")[0],
                    callback_data="question:ask",
                )],
                [InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="menu:main")],
            ]),
        )
    else:
        await _safe_edit(
            query,
            TEXTS["shop"]["no_balance_body"],
            reply_markup=questions_shop_kb(),
        )


async def _show_settings(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Настройки. Полная реализация — Фаза 8 (handlers/settings.py)."""
    telegram_id = query.from_user.id
    async with AsyncSession() as session:
        user = await get_user(session, telegram_id)

    if not user:
        await _safe_edit(query, TEXTS["errors"]["onboarding_required"])
        return

    # Собираем краткий статус для отображения
    from datetime import datetime
    st = TEXTS["settings"]
    if user.is_premium_active and user.premium_until:
        status = st["sub_active"]
        until  = user.premium_until.strftime("%d.%m.%Y")
    else:
        status = st["sub_inactive"]
        until  = st["sub_no_date"]

    sub_info = st["sub_body"].format(
        status    = status,
        until     = until,
        questions = user.questions_balance or 0,
    )

    await _safe_edit(
        query,
        f"{st['title']}\n\n{sub_info}",
        parse_mode="HTML",
        reply_markup=settings_kb(),
    )


# Реестр навигационных sub-команд (menu:*)
_NAV_HANDLERS: dict[str, Callable[..., Coroutine]] = {
    "main":     _show_main_menu,
    "premium":  _show_premium,
    "specials": _show_specials,
    "question": _show_question,
    "settings": _show_settings,
}


async def _handle_nav(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Навигация: menu:main, menu:premium, menu:specials, ..."""
    sub = query.data.split(":")[1] if ":" in query.data else ""
    handler = _NAV_HANDLERS.get(sub)
    if handler:
        await handler(query, context)
    else:
        logger.warning("Unknown nav sub: %s", query.data)
        await _safe_edit(query, TEXTS["errors"]["generic"])


async def _handle_adv(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Продвинутый Джйотиш: adv:menu | adv:houses"""
    sub = query.data.split(":")[1] if ":" in query.data else ""
    telegram_id = query.from_user.id

    async with AsyncSession() as session:
        user = await get_user(session, telegram_id)

    if not user:
        await _safe_edit(query, TEXTS["errors"]["onboarding_required"])
        return

    if not user.is_premium_active:
        await _safe_edit(
            query,
            TEXTS["paywall"]["premium_only_body"],
            reply_markup=paywall_kb(premium_only=True),
        )
        return

    if sub == "menu":
        await _safe_edit(
            query,
            TEXTS["premium"]["adv_title"],
            reply_markup=adv_jyotish_kb(),
        )
    elif sub == "houses":
        await _safe_edit(
            query,
            TEXTS["premium"]["houses_title"],
            reply_markup=houses_kb(),
        )
    else:
        await _safe_edit(query, TEXTS["errors"]["generic"])


# ──────────────────────────────────────────────────────────────────────────────
#  Главный диспетчер callback-ов (БЕЗ if/elif)
# ──────────────────────────────────────────────────────────────────────────────

#: Маршрутная таблица: prefix → handler
#: Это и есть замена «гигантскому if/elif» из v1.
_HANDLERS: dict[str, Callable[..., Coroutine]] = {
    "section": _handle_section,
    "natal":   _handle_natal,
    "regen":   _handle_regen,
    "menu":    _handle_nav,
    "adv":     _handle_adv,
    "house":   _handle_house,
}


async def dispatch_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Единственный обработчик для всех menu-related callbacks.
    Маршрутизирует по первому сегменту callback_data до «:».

    Регистрируется в main.py паттерном:
        ^(section|natal|regen|menu|adv|house):
    """
    query = update.callback_query
    await query.answer()

    prefix = query.data.split(":")[0]
    handler = _HANDLERS.get(prefix)

    if handler:
        await handler(query, context)
    else:
        logger.warning("dispatch_menu_callback: no handler for %r", query.data)
        await _safe_edit(query, TEXTS["errors"]["generic"])


# ──────────────────────────────────────────────────────────────────────────────
#  Команда /menu
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/menu — показать главное меню."""
    telegram_id = update.effective_user.id

    async with AsyncSession() as session:
        user = await get_user(session, telegram_id)

    if not user or not user.onboarding_complete:
        await update.message.reply_text(
            TEXTS["errors"]["onboarding_required"],
        )
        return

    await update.message.reply_text(
        TEXTS["menu"]["title"],
        reply_markup=main_menu_kb(user),
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Регистрация хендлеров (вызывается из main.py)
# ──────────────────────────────────────────────────────────────────────────────

def setup_menu_handlers(app) -> None:
    """
    Регистрирует все menu-хендлеры в Application.
    Вызов: setup_menu_handlers(application) из main.py.
    """
    # Одна точка для ВСЕХ menu-related callbacks
    app.add_handler(
        CallbackQueryHandler(
            dispatch_menu_callback,
            pattern=r"^(section|natal|regen|menu|adv|house):",
        )
    )
    # /menu команда
    app.add_handler(CommandHandler("menu", cmd_menu))

"""
handlers/payments.py
====================
Полный платёжный поток на Telegram Stars.

Обязанности этого модуля:
  • Лендинг «Премиум-доступ» (callback menu:premium)
  • Выставление инвойсов (callback pay:{product_key})
  • PreCheckoutQueryHandler — всегда отвечает ok=True
  • MessageHandler(SUCCESSFUL_PAYMENT):
      subscription → продлить premium_until, начислить бонусные вопросы
      questions    → пополнить questions_balance
      special      → залоговать, передать в specials.py через user_data
  • Запись аналитики в таблицу payments (Payment)

Логика доступа/баланса — только в core/access.py; здесь мы лишь обновляем поля.

Порядок регистрации (в main.py):
  register(app) вызывается ДО handlers/menu.register(app), чтобы menu:premium
  и pay:* перехватывались раньше catch-all.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from config import PREMIUM_DAYS, PREMIUM_MONTHLY_QUESTIONS, PRODUCTS
from db.models import AsyncSession, Forecast, Payment, User
from keyboards.keyboards import back_to_menu, main_menu
from texts import TEXTS, pluralize

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

async def _get_user(session, telegram_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


def _premium_landing_keyboard() -> InlineKeyboardMarkup:
    tp = TEXTS["premium"]
    tc = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tp["btn_buy"], callback_data="pay:premium_month")],
        [InlineKeyboardButton(tc["back_to_menu"],  callback_data="menu:main")],
    ])


def _shop_keyboard() -> InlineKeyboardMarkup:
    ts = TEXTS["shop"]
    tc = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ts["btn_q1"],  callback_data="pay:q1")],
        [InlineKeyboardButton(ts["btn_q3"],  callback_data="pay:q3")],
        [InlineKeyboardButton(ts["btn_q10"], callback_data="pay:q10")],
        [InlineKeyboardButton(tc["back_to_menu"], callback_data="menu:main")],
    ])


async def _log_payment(session, telegram_id: int, product: str, stars: int) -> None:
    session.add(Payment(
        telegram_id=telegram_id,
        product=product,
        stars=stars,
    ))
    await session.commit()


# ──────────────────────────────────────────────────────────────────────────────
#  Лендинг «Премиум-доступ»
# ──────────────────────────────────────────────────────────────────────────────

async def handle_premium_landing(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Показывает описание Премиума и кнопку оформления.
    Если подписка уже активна — сообщает об этом.
    Callback: menu:premium
    """
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

    tp = TEXTS["premium"]
    tpw = TEXTS["paywall"]

    if user and user.is_premium_active:
        await query.edit_message_text(
            tpw["already_premium"],
            reply_markup=back_to_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text(
        tp["title"] + "\n\n" + tp["body"],
        reply_markup=_premium_landing_keyboard(),
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Выставление инвойсов
# ──────────────────────────────────────────────────────────────────────────────

async def handle_send_invoice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Отправляет Telegram Stars инвойс для любого ключа из PRODUCTS.
    Callback: pay:{product_key}
    """
    query = update.callback_query
    await query.answer()

    product_key = query.data.split(":", 1)[1]
    product = PRODUCTS.get(product_key)

    if not product:
        logger.warning("Unknown product key: %s", product_key)
        return

    stars = product["stars"]
    kind  = product["kind"]

    # Заголовок и описание инвойса из texts.py
    tp = TEXTS["premium"]
    ts = TEXTS["shop"]
    tsp = TEXTS["specials"]

    if kind == "subscription":
        title       = tp["purchase_title"]
        description = tp["purchase_desc"]
    elif kind == "questions":
        amount = product["amount"]
        word   = pluralize(amount, ts["words_q"])
        title       = f"{amount} {word}"
        description = f"Пополнение баланса вопросов в боте Раши"
    else:  # special
        title       = tsp["purchase_title"]
        description = tsp["purchase_desc"]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=product_key,
        provider_token="",    # пустой для Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )


# ──────────────────────────────────────────────────────────────────────────────
#  PreCheckout — всегда OK
# ──────────────────────────────────────────────────────────────────────────────

async def handle_pre_checkout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    PreCheckoutQueryHandler.
    Telegram требует ответа в течение 10 секунд.
    Мы всегда отвечаем ok=True (Telegram Stars не требует проверки наличия товара).
    """
    await update.pre_checkout_query.answer(ok=True)


# ──────────────────────────────────────────────────────────────────────────────
#  Успешная оплата
# ──────────────────────────────────────────────────────────────────────────────

async def handle_successful_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Обрабатывает событие successful_payment.

    Диспетчер по PRODUCTS[payload]["kind"]:
      subscription → продлить premium_until на PREMIUM_DAYS дней;
                     при первой активации начислить PREMIUM_MONTHLY_QUESTIONS вопросов.
      questions    → добавить amount в questions_balance.
      special      → залоговать, передать в specials-поток через user_data.

    Всегда записывает строку в таблицу payments (аналитика).
    """
    sp          = update.message.successful_payment
    product_key = sp.invoice_payload
    product     = PRODUCTS.get(product_key)

    if not product:
        logger.error("successful_payment with unknown payload: %s", product_key)
        return

    kind  = product["kind"]
    stars = product["stars"]

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user:
            logger.error("successful_payment: user not found for %s", update.effective_user.id)
            return

        # ── subscription ───────────────────────────────────────────────────
        if kind == "subscription":
            now = datetime.utcnow()

            # Продляем от текущей даты истечения (если активна) или от now
            if user.is_premium_active and user.premium_until:
                new_until = user.premium_until + timedelta(days=PREMIUM_DAYS)
            else:
                new_until = now + timedelta(days=PREMIUM_DAYS)

            was_premium = user.is_premium_active  # до обновления

            user.is_premium    = True
            user.premium_until = new_until

            # Первая активация или возобновление — начисляем бонусные вопросы
            if not was_premium:
                user.questions_balance = (user.questions_balance or 0) + PREMIUM_MONTHLY_QUESTIONS
                user.questions_month   = now.strftime("%Y-%m")

            await _log_payment(session, user.telegram_id, product_key, stars)
            await session.commit()

            await update.message.reply_text(
                TEXTS["premium"]["activated"],
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(user),
            )
            logger.info(
                "User %s: premium activated until %s",
                user.telegram_id, new_until.date(),
            )

        # ── questions ──────────────────────────────────────────────────────
        elif kind == "questions":
            amount = product["amount"]
            user.questions_balance = (user.questions_balance or 0) + amount

            await _log_payment(session, user.telegram_id, product_key, stars)
            await session.commit()

            word = pluralize(amount, TEXTS["shop"]["words_q"])
            await update.message.reply_text(
                TEXTS["shop"]["bought"].format(amount=amount, word=word),
                parse_mode=ParseMode.HTML,
                reply_markup=back_to_menu(),
            )
            logger.info(
                "User %s: +%d questions (balance=%d)",
                user.telegram_id, amount, user.questions_balance,
            )

        # ── special ────────────────────────────────────────────────────────
        elif kind == "special":
            await _log_payment(session, user.telegram_id, product_key, stars)
            await session.commit()

            # Передаём управление в specials.py через user_data
            context.user_data["special_pending"] = product_key
            logger.info(
                "User %s: special payment '%s' logged",
                user.telegram_id, product_key,
            )

            # specials.py зарегистрирован с MessageHandler(SUCCESSFUL_PAYMENT)
            # с более высоким приоритетом или обрабатывает через user_data.
            # Здесь только подтверждение — specials.py продолжает поток.
            await update.message.reply_text(
                TEXTS["specials"]["generating"],
                parse_mode=ParseMode.HTML,
            )

        else:
            logger.warning("Unknown product kind: %s", kind)


# ──────────────────────────────────────────────────────────────────────────────
#  Регистрация
# ──────────────────────────────────────────────────────────────────────────────

def register(app) -> None:
    """
    Зарегистрировать платёжные хендлеры.

    Вызывать ДО handlers/menu.register(app) — иначе pay:* и menu:premium
    будут перехвачены catch-all хендлерами меню.
    """
    # Лендинг
    app.add_handler(CallbackQueryHandler(
        handle_premium_landing, pattern=r"^menu:premium$"
    ))

    # Инвойсы: pay:{product_key}
    app.add_handler(CallbackQueryHandler(
        handle_send_invoice, pattern=r"^pay:"
    ))

    # PreCheckout — системный хендлер Telegram
    app.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))

    # Успешная оплата
    app.add_handler(MessageHandler(
        filters.SUCCESSFUL_PAYMENT,
        handle_successful_payment,
    ))

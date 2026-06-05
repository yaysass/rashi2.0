"""
handlers/questions.py
=====================
Поток «Задать свой вопрос»: баланс, магазин пополнения, AI-ответ.

Обрабатываемые callbacks:
  menu:question → проверить баланс, начислить бонус (премиум), показать
                  экран «задай вопрос» или магазин пополнения.

MessageHandler (group=10):
  Ловит тексты когда user_data["awaiting"] == "question".
  Валидирует → генерирует ответ → списывает 1 вопрос → показывает остаток.

Логика пополнения (get_or_replenish_questions):
  Если пользователь — активный Премиум И текущий месяц (YYYY-MM)
  ≠ user.questions_month → начисляем PREMIUM_MONTHLY_QUESTIONS, обновляем месяц.
  Это происходит при каждом открытии раздела «Задать вопрос».

Магазин вопросов:
  pay:q1 / pay:q3 / pay:q10 — обрабатываются в handlers/payments.py,
  здесь только ПОКАЗЫВАЕМ кнопки.

Максимальная длина вопроса — 500 символов.
Минимальная — 10 символов (меньше бессмысленно для ИИ).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import PREMIUM_MONTHLY_QUESTIONS
from core.access import deduct_question, has_questions
from core.prompts import build_user_prompt, get_system_prompt
from db.models import AsyncSession, User
from keyboards.keyboards import back_to_menu
from services.ai import generate
from texts import TEXTS, pluralize

logger = logging.getLogger(__name__)

# ── Лимиты вопроса ────────────────────────────────────────────────────────────
_MIN_Q_LEN = 10
_MAX_Q_LEN = 500


# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

async def _get_user(session, telegram_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def _replenish_if_needed(session, user: User) -> bool:
    """
    Начисляет бонусные вопросы премиум-пользователю в новом месяце.
    Возвращает True, если начисление произошло.
    """
    if not user.is_premium_active:
        return False
    current_month = datetime.utcnow().strftime("%Y-%m")
    if user.questions_month == current_month:
        return False
    user.questions_balance = (user.questions_balance or 0) + PREMIUM_MONTHLY_QUESTIONS
    user.questions_month   = current_month
    await session.commit()
    logger.info(
        "User %s: monthly replenishment +%d (now %d)",
        user.telegram_id, PREMIUM_MONTHLY_QUESTIONS, user.questions_balance,
    )
    return True


def _shop_keyboard() -> InlineKeyboardMarkup:
    """Кнопки магазина вопросов (pay:* → payments.py)."""
    ts = TEXTS["shop"]
    tc = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ts["btn_q1"],  callback_data="pay:q1")],
        [InlineKeyboardButton(ts["btn_q3"],  callback_data="pay:q3")],
        [InlineKeyboardButton(ts["btn_q10"], callback_data="pay:q10")],
        [InlineKeyboardButton(tc["back_to_menu"], callback_data="menu:main")],
    ])


def _ask_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Отмена» пока ждём текст вопроса."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS["common"]["cancel"], callback_data="question:cancel")
    ]])


# ──────────────────────────────────────────────────────────────────────────────
#  Открытие раздела
# ──────────────────────────────────────────────────────────────────────────────

async def handle_question_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Точка входа: клик «✒️ Задать свой вопрос».

    Алгоритм:
    1. Начислить бонусные вопросы премиуму (если новый месяц).
    2. Если баланс == 0 → показать магазин.
    3. Если баланс > 0 → показать «Задай вопрос», установить awaiting="question".
    Callback: menu:question
    """
    query = update.callback_query
    await query.answer()

    ts = TEXTS["shop"]

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

        if not user or not user.onboarding_complete:
            await query.edit_message_text(
                TEXTS["errors"]["onboarding_required"],
                reply_markup=back_to_menu(),
                parse_mode=ParseMode.HTML,
            )
            return

        await _replenish_if_needed(session, user)
        balance = user.questions_balance or 0

    if balance == 0:
        await query.edit_message_text(
            ts["no_balance_title"] + "\n\n" + ts["no_balance_body"],
            reply_markup=_shop_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    # Есть вопросы — ждём текст
    context.user_data["awaiting"] = "question"
    word = pluralize(balance, ts["words_q"])

    await query.edit_message_text(
        ts["balance_info"].format(n=balance, word=word) + "\n\n" + ts["ask_question"],
        reply_markup=_ask_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def handle_question_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Отмена ввода вопроса.
    Callback: question:cancel
    """
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting", None)

    await query.edit_message_text(
        TEXTS["menu"]["title"],
        parse_mode=ParseMode.HTML,
    )
    # Перенаправляем в главное меню — импортируем здесь чтобы избежать кругового импорта
    from keyboards.keyboards import main_menu
    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if user:
            await query.edit_message_text(
                TEXTS["menu"]["title"],
                reply_markup=main_menu(user),
                parse_mode=ParseMode.HTML,
            )


# ──────────────────────────────────────────────────────────────────────────────
#  Обработка текста вопроса
# ──────────────────────────────────────────────────────────────────────────────

async def handle_question_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Принимает текст вопроса, генерирует ответ, списывает 1 вопрос.
    Зарегистрирован в group=10 — проверяет awaiting перед обработкой.

    Алгоритм:
    1. Проверить awaiting == "question" (иначе выйти).
    2. Валидировать длину: слишком короткий / длинный.
    3. Повторно проверить баланс (мог обнулиться между кликом и вводом).
    4. Отправить «loading», сгенерировать ответ.
    5. Списать 1 вопрос, показать ответ + остаток.
    """
    if context.user_data.get("awaiting") != "question":
        return

    raw = (update.message.text or "").strip()
    ts = TEXTS["shop"]

    # ── Валидация ─────────────────────────────────────────────────────────────
    if len(raw) < _MIN_Q_LEN:
        await update.message.reply_text(
            ts["question_too_short"],
            parse_mode=ParseMode.HTML,
        )
        return

    if len(raw) > _MAX_Q_LEN:
        await update.message.reply_text(
            ts["question_too_long"],
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Загружаем данные ──────────────────────────────────────────────────────
    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)

        if not user:
            context.user_data.pop("awaiting", None)
            return

        astro = user.get_astro()
        if not astro:
            await update.message.reply_text(
                TEXTS["errors"]["calculation_pending"],
                reply_markup=back_to_menu(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Перепроверяем баланс (мог измениться)
        if not has_questions(user):
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                ts["no_balance_title"] + "\n\n" + ts["no_balance_body"],
                reply_markup=_shop_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Снимаем состояние ДО длительного generate()
        context.user_data.pop("awaiting", None)

        # ── Loading ───────────────────────────────────────────────────────────
        loading_msg = await update.message.reply_text(
            ts["generating"],
            parse_mode=ParseMode.HTML,
        )

        # ── Промпт ───────────────────────────────────────────────────────────
        prompt = build_user_prompt(
            section_key="question",
            astro_json=astro,
            user_name=user.name or "пользователь",
            gender=user.gender or "unknown",
            question=raw,
        )
        system = get_system_prompt(user.gender or "unknown")

        # ── Генерация ─────────────────────────────────────────────────────────
        answer: str | None = None
        try:
            answer = await generate(
                prompt=prompt,
                system=system,
                max_tokens=800,     # вопросы короче разборов
            )
        except RuntimeError as exc:
            logger.error("Question generation failed: %s", exc)

        # ── Списание ─────────────────────────────────────────────────────────
        if answer:
            remaining = await deduct_question(session, user)
            remaining_word = pluralize(remaining, ts["words_q"])
            footer = "\n\n" + ts["remaining"].format(n=remaining, word=remaining_word)
        else:
            remaining = user.questions_balance or 0
            footer = ""
            answer = TEXTS["errors"]["generation_failed"]

    # ── Отправляем ответ ──────────────────────────────────────────────────────
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="menu:main")
    ]])
    try:
        await loading_msg.edit_text(
            answer + footer,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await update.message.reply_text(
            answer + footer,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Регистрация
# ──────────────────────────────────────────────────────────────────────────────

def register(app) -> None:
    """
    Регистрировать ДО handlers/menu.register(app).

    group=10 для текстового хендлера:
      • Все группы PTB обрабатывают каждый апдейт независимо.
      • Хендлер проверяет awaiting == "question" и выходит сразу, если не в том состоянии.
      • group=20 (premium.py muhurta) работает параллельно по той же схеме.
    """
    app.add_handler(CallbackQueryHandler(
        handle_question_start, pattern=r"^menu:question$"
    ))
    app.add_handler(CallbackQueryHandler(
        handle_question_cancel, pattern=r"^question:cancel$"
    ))

    # Текстовый ввод вопроса — group=10
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question_text),
        group=10,
    )

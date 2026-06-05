"""
handlers/menu.py
================
Обработчики главного меню и тематических разборов.

Обрабатываемые callbacks:
  menu:main       → перерисовать главное меню
  natal:main      → разбор личности (из кэша, или на лету)
  natal:code      → Натальный код (card_text без AI, мгновенно)
  section:{key}   → проверка доступа → run_reading(key)
  regen:{key}     → повторная генерация того же раздела
  adv:menu        → подменю Продвинутого Джйотиша

Центральная функция run_reading() — единый диспетчер разборов.
Никакого if/elif по ключам: логика доступа в core/access.py,
метаданные в core/catalog.py, промпты в core/prompts.py.

Порядок регистрации (важно!):
  Хендлеры menu.py регистрируются ПОСЛЕДНИМИ. Специфические хендлеры
  (premium.py → section:muhurta, section:weekly; questions.py → menu:question)
  должны быть зарегистрированы раньше, чтобы перехватывать свои callbacks
  до того, как до них доберётся catch-all «section:».
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes

from core.access import (
    can_open_premium,
    can_open_theme,
    charge_free_section,
    record_paywall_shown,
)
from core.catalog import READINGS
from core.prompts import build_user_prompt, get_system_prompt
from db.models import AsyncSession, User
from keyboards.keyboards import (
    advanced_menu,
    back_to_menu,
    main_menu,
    natal_code_back,
    paywall_keyboard,
    premium_only_keyboard,
    reading_actions,
)
from services.ai import generate
from texts import TEXTS

logger = logging.getLogger(__name__)

# Эти ключи требуют multi-step flow и обрабатываются отдельными хендлерами.
# section:{key} с этими ключами НЕ должны попадать в handle_section этого модуля.
_DELEGATED_KEYS: frozenset[str] = frozenset({"muhurta", "weekly"})


# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

async def _get_user(session, telegram_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def _safe_edit(update: Update, text: str, **kwargs) -> None:
    """
    Редактирует текущее сообщение (callback) или шлёт новое (текстовый хендлер).
    """
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, **kwargs)
            return
        except Exception:
            pass
    if update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)


# ──────────────────────────────────────────────────────────────────────────────
#  Центральный диспетчер разборов
# ──────────────────────────────────────────────────────────────────────────────

async def run_reading(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
    **extra,
) -> None:
    """
    Единая точка генерации разбора для любого reading_key из READINGS.

    Алгоритм:
    ┌── 1. Проверить entry в каталоге.
    ├── 2. Загрузить пользователя и astro_json.
    ├── 3. Проверить доступ (access.can_*).
    │     └── Отказ → paywall / premium_only и выход.
    ├── 4. Списать бесплатный клик (если нужно).
    ├── 5. Показать loading-сообщение.
    ├── 6. Собрать промпт через build_user_prompt.
    ├── 7. Вызвать generate().
    └── 8. Отредактировать loading → разбор + кнопки.

    Параметры:
      key    — ключ из READINGS (совпадает с prompt_key в _SECTION_INSTRUCTIONS)
      **extra — параметры параметризованных промптов:
                  weekly:  week="YYYY-Www"
                  muhurta: event=str, period=str
                  question: question=str
    """
    query = update.callback_query
    if query:
        await query.answer()

    # ── 1. Каталог ────────────────────────────────────────────────────────
    entry = READINGS.get(key)
    if entry is None:
        await _safe_edit(
            update,
            TEXTS["errors"]["reading_not_found"],
            parse_mode=ParseMode.HTML,
        )
        return

    async with AsyncSession() as session:
        # ── 2. Пользователь и кэш ────────────────────────────────────────
        user = await _get_user(session, update.effective_user.id)

        if not user or not user.onboarding_complete:
            await _safe_edit(
                update,
                TEXTS["errors"]["onboarding_required"],
                parse_mode=ParseMode.HTML,
            )
            return

        astro = user.get_astro()
        if not astro:
            await _safe_edit(
                update,
                TEXTS["errors"]["calculation_pending"],
                parse_mode=ParseMode.HTML,
            )
            return

        # ── 3. Проверка доступа ───────────────────────────────────────────
        if entry.access == "freemium":
            if not can_open_theme(user, key):
                # Бесплатный клик уже потрачен на другую тему — пейволл
                await record_paywall_shown(session, user)
                await _safe_edit(
                    update,
                    TEXTS["paywall"]["locked_body"],
                    reply_markup=paywall_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return

        elif entry.access == "premium":
            if not can_open_premium(user):
                await _safe_edit(
                    update,
                    TEXTS["paywall"]["premium_only_body"],
                    reply_markup=premium_only_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return

        # entry.access == "free" — пропускаем без проверок

        # ── 4. Списать бесплатный клик (только для freemium, только первый раз) ──
        if entry.access == "freemium":
            charged = await charge_free_section(session, user, key)
            if charged:
                logger.info("Free click charged: user=%s key=%s", user.telegram_id, key)

        # ── 5. Loading-сообщение ─────────────────────────────────────────
        loading_msg = None
        try:
            if query:
                loading_msg = await query.edit_message_text(
                    TEXTS["common"]["loading"],
                    parse_mode=ParseMode.HTML,
                )
            elif update.effective_message:
                loading_msg = await update.effective_message.reply_text(
                    TEXTS["common"]["loading"],
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            logger.debug("Could not send loading message: %s", e)

        # ── 6. Сборка промпта ────────────────────────────────────────────
        prompt = build_user_prompt(
            section_key=entry.prompt_key,   # совпадает с key во всех стандартных случаях
            astro_json=astro,
            user_name=user.name or "пользователь",
            gender=user.gender or "unknown",
            **extra,
        )
        system = get_system_prompt(user.gender or "unknown")

        # ── 7. Генерация ─────────────────────────────────────────────────
        result_text: str | None = None
        try:
            result_text = await generate(
                prompt=prompt,
                system=system,
                max_tokens=entry.max_tokens,
            )
        except RuntimeError as exc:
            logger.error("Generation failed for key=%s: %s", key, exc)

        # ── 8. Отправка результата ────────────────────────────────────────
        # Перечитываем пользователя — charge_free_section мог изменить free_section_used.
        # Сессия ещё открыта, объект user уже содержит актуальное состояние.
        kb = reading_actions(key, user)

        if not result_text:
            display_text = TEXTS["errors"]["generation_failed"]
        else:
            display_text = result_text

        if loading_msg:
            try:
                await loading_msg.edit_text(
                    display_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                # Если edit не удался — шлём новым сообщением
                if update.effective_message:
                    await update.effective_message.reply_text(
                        display_text,
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                    )
        else:
            if update.effective_message:
                await update.effective_message.reply_text(
                    display_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )


# ──────────────────────────────────────────────────────────────────────────────
#  Callback-хендлеры
# ──────────────────────────────────────────────────────────────────────────────

async def handle_menu_main(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Перерисовать главное меню.
    Callback: menu:main
    """
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user:
            await query.edit_message_text(
                TEXTS["errors"]["onboarding_required"],
                parse_mode=ParseMode.HTML,
            )
            return

        await query.edit_message_text(
            TEXTS["menu"]["title"],
            reply_markup=main_menu(user),
            parse_mode=ParseMode.HTML,
        )


async def handle_natal_main(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Показать разбор личности (personality) из astro_json-кэша.
    Если кэш пустой — сгенерировать на лету и сохранить.
    Callback: natal:main
    """
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user:
            return

        astro = user.get_astro()
        if not astro:
            await query.edit_message_text(
                TEXTS["natal"]["no_data"],
                reply_markup=back_to_menu(),
                parse_mode=ParseMode.HTML,
            )
            return

        personality = astro.get("personality")

        if not personality:
            # Кэш пустой — генерируем на лету (старый пользователь без кэша)
            await query.edit_message_text(
                TEXTS["common"]["loading"],
                parse_mode=ParseMode.HTML,
            )
            try:
                prompt = build_user_prompt(
                    section_key="personality",
                    astro_json=astro,
                    user_name=user.name or "пользователь",
                    gender=user.gender or "unknown",
                )
                system = get_system_prompt(user.gender or "unknown")
                personality = await generate(
                    prompt=prompt,
                    system=system,
                    max_tokens=1600,
                )
                # Кэшируем
                astro["personality"] = personality
                user.set_astro(astro)
                await session.commit()
            except RuntimeError:
                personality = TEXTS["errors"]["generation_failed"]

        full_text = TEXTS["natal"]["header"] + "\n\n" + personality
        kb = reading_actions("personality", user)

        try:
            await query.edit_message_text(
                full_text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await update.effective_message.reply_text(
                full_text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )


async def handle_natal_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Показать Натальный код — сырые астрологические данные (card_text).
    Мгновенно, без AI-генерации. Заголовки обычным регистром, пустые поля пропущены.
    Callback: natal:code
    """
    query = update.callback_query
    await query.answer()

    async with AsyncSession() as session:
        user = await _get_user(session, update.effective_user.id)
        if not user:
            return

        astro = user.get_astro()
        card_text = astro.get("card_text") if astro else None

        if not card_text:
            await query.edit_message_text(
                TEXTS["natal"]["no_data"],
                reply_markup=back_to_menu(),
                parse_mode=ParseMode.HTML,
            )
            return

        full_text = TEXTS["natal"]["code_header"] + "\n\n" + card_text
        try:
            await query.edit_message_text(
                full_text,
                reply_markup=natal_code_back(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            # Натальный код может быть длиннее лимита edit — шлём новым сообщением
            await update.effective_message.reply_text(
                full_text,
                reply_markup=natal_code_back(),
                parse_mode=ParseMode.HTML,
            )


async def handle_section(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Универсальный диспетчер для section:{key} callbacks.

    Примечание: multi-step разборы (muhurta, weekly) регистрируются
    в своих хендлерах (premium.py) РАНЬШЕ этого catch-all.
    Если они всё равно попали сюда — логируем и игнорируем.
    Callback: section:*
    """
    query = update.callback_query
    key = query.data.split(":", 1)[1]

    if key in _DELEGATED_KEYS:
        logger.warning(
            "section:%s reached menu catch-all handler (should be handled elsewhere)",
            key,
        )
        await query.answer()
        return

    await run_reading(update, context, key)


async def handle_regen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Перегенерация того же раздела.

    Не тратит повторный бесплатный клик:
      • charge_free_section() ничего не делает если free_section_used уже == key.
      • can_open_theme() возвращает True для той же темы.

    Callback: regen:{key}
    """
    query = update.callback_query
    key = query.data.split(":", 1)[1]
    await run_reading(update, context, key)


async def handle_adv_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Показать подменю Продвинутого Джйотиша.
    Требует активного премиума; иначе — premium_only paywall.
    Callback: adv:menu
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

    # Заголовок подменю — используем label кнопки из главного меню
    title = TEXTS["menu"]["btn_adv"]
    await query.edit_message_text(
        title,
        reply_markup=advanced_menu(),
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Регистрация хендлеров
# ──────────────────────────────────────────────────────────────────────────────

def register(app) -> None:
    """
    Зарегистрировать все хендлеры этого модуля в Application.

    ВАЖНО: вызывать ПОСЛЕ регистрации специфических хендлеров из:
      - handlers/premium.py  (section:muhurta, section:weekly, adv:*)
      - handlers/questions.py (menu:question)
      - handlers/payments.py  (menu:premium)
      - handlers/settings.py  (menu:settings)
      - handlers/specials.py  (menu:specials)

    Паттерн «section:» является catch-all и должен идти последним.
    """
    # Точные совпадения — регистрируем первыми внутри этого модуля
    app.add_handler(CallbackQueryHandler(handle_menu_main,  pattern=r"^menu:main$"))
    app.add_handler(CallbackQueryHandler(handle_natal_main, pattern=r"^natal:main$"))
    app.add_handler(CallbackQueryHandler(handle_natal_code, pattern=r"^natal:code$"))
    app.add_handler(CallbackQueryHandler(handle_adv_menu,   pattern=r"^adv:menu$"))

    # Catch-all для section: и regen: — в конце
    app.add_handler(CallbackQueryHandler(handle_regen,   pattern=r"^regen:"))
    app.add_handler(CallbackQueryHandler(handle_section, pattern=r"^section:"))

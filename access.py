"""
core/access.py
==============
Единый модуль проверки и расходования прав доступа.

Три уровня доступа:
  free     — всегда открыто (personality / natal)
  freemium — 1 бесплатный клик на ЛЮБУЮ тему; та же тема — всегда; иначе — paywall
  premium  — требует активной подписки

Принцип разделения обязанностей:
  • can_*()      — синхронные предикаты, НЕ меняют БД, вызываются в hot-path.
  • charge_*()   — async-мутации, вызываются ТОЛЬКО когда can_*() уже вернул True.
  • record_*()   — async-логирование событий (paywall, etc.).

Все мутации принимают AsyncSession снаружи: handlers несут ответственность
за commit/rollback-контекст.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from db.models import User

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Предикаты доступа (синхронные, без обращения к БД)
# ──────────────────────────────────────────────────────────────────────────────

def can_open_theme(user: "User", key: str) -> bool:
    """
    Проверяет право на открытие freemium-раздела.

    Возвращает True если хотя бы одно из:
      1. Активный Премиум.
      2. Эта же тема уже открыта бесплатно ранее (повторный просмотр — бесплатно).
      3. Бесплатный клик ещё не потрачен (первое открытие любой темы).
    """
    if user.is_premium_active:
        return True
    if user.free_section_used == key:
        # та же тема — всегда разрешено, включая перегенерацию
        return True
    if user.free_section_used is None:
        # бесплатный клик ещё не потрачен
        return True
    return False


def can_open_premium(user: "User") -> bool:
    """Требует активную подписку (freemium тут не проходит)."""
    return user.is_premium_active


def has_questions(user: "User") -> bool:
    """Есть ли вопросы в балансе (>0)."""
    return (user.questions_balance or 0) > 0


def freemium_lock_needed(user: "User", key: str) -> bool:
    """
    Нужно ли рисовать замок 🔒 на кнопке freemium-раздела.

    Используется keyboards.py при построении главного меню.
    """
    if user.is_premium_active:
        return False
    # Замок показываем только если бесплатный клик уже потрачен
    # и потрачен на ДРУГУЮ тему (эта остаётся открытой).
    return user.free_section_used is not None and user.free_section_used != key


# ──────────────────────────────────────────────────────────────────────────────
#  Мутации доступа
# ──────────────────────────────────────────────────────────────────────────────

async def charge_free_section(
    session: "AsyncSession",
    user: "User",
    key: str,
) -> bool:
    """
    Помечает раздел как использованный бесплатный клик.

    Вызывать только когда can_open_theme() вернул True.
    Ничего не делает, если:
      - пользователь уже премиум (клик не нужен)
      - этот раздел уже отмечен как использованный
      - клик уже потрачен на другую тему (тогда can_open_theme() вернул бы False)

    Возвращает True, если клик был фактически списан.
    """
    if user.is_premium_active:
        return False
    if user.free_section_used is not None:
        return False  # уже использован ранее

    user.free_section_used = key
    await session.commit()
    logger.info(
        "User %s spent free click on '%s'",
        user.telegram_id, key,
    )
    return True


async def deduct_question(
    session: "AsyncSession",
    user: "User",
) -> int:
    """
    Списывает 1 вопрос из баланса.

    Вызывать только после проверки has_questions().
    Возвращает остаток после списания.
    """
    before = user.questions_balance or 0
    user.questions_balance = max(0, before - 1)
    await session.commit()
    logger.info(
        "User %s: question deducted (%d → %d)",
        user.telegram_id, before, user.questions_balance,
    )
    return user.questions_balance


# ──────────────────────────────────────────────────────────────────────────────
#  Логирование событий
# ──────────────────────────────────────────────────────────────────────────────

async def record_paywall_shown(
    session: "AsyncSession",
    user: "User",
) -> None:
    """
    Фиксирует момент показа пейволла.

    Используется планировщиком для отправки paywall-ремайндера
    через PAYWALL_REMINDER_AFTER_DAYS дней.
    Не перезаписывает уже существующую дату (первый показ важнее).
    """
    if user.paywall_shown_at is None:
        user.paywall_shown_at = datetime.utcnow()
        await session.commit()
        logger.debug("User %s: paywall_shown_at recorded", user.telegram_id)

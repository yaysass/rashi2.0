"""
core/access.py
==============
Единственное место проверки прав.

Все разделы и хендлеры обращаются только сюда.
Никакой дублирующей логики в handlers/.

Главный контракт:
  check_and_grant(session, user, key) → AccessResult
    - Если FREEMIUM и бесплатный клик не потрачен → записывает key в
      user.free_section_used, делает commit, возвращает GRANTED.
    - Не вызывай его дважды для одной сессии — commit уже случился.

  is_section_locked(user, key) → bool
    - Чистая функция, без IO. Для рисования 🔒 в клавиатурах.

  check_regen_access(user, key) → bool
    - Чистая функция. Перегенерация не тратит клик.
"""
from __future__ import annotations

from enum import Enum, auto

from db.models import AsyncSession, User


class AccessResult(Enum):
    GRANTED          = auto()  # можно читать
    PAYWALL_FREEMIUM = auto()  # бесплатный клик потрачен, нужна подписка
    PAYWALL_PREMIUM  = auto()  # раздел только для подписки
    NOT_READY        = auto()  # онбординг не завершён или карта ещё считается


# ──────────────────────────────────────────────────────────────────────────────
#  Проверка с побочным эффектом (DB write)
# ──────────────────────────────────────────────────────────────────────────────

async def check_and_grant(
    session: AsyncSession,
    user: User,
    section_key: str,
) -> AccessResult:
    """
    Проверяет доступ к разделу и при необходимости тратит бесплатный клик.

    Побочный эффект: если бесплатный клик тратится — делает commit.
    Вызывается только при открытии раздела (не при перегенерации).
    """
    # Ленивый импорт, чтобы не было циклов
    from core.catalog import READINGS

    if not user.onboarding_complete:
        return AccessResult.NOT_READY

    config = READINGS.get(section_key)
    if not config:
        return AccessResult.GRANTED  # неизвестный ключ = разрешить

    access = config.access

    # ── Всегда бесплатно ──────────────────────────────────────────────────────
    if access == "free":
        return AccessResult.GRANTED

    # ── Freemium ──────────────────────────────────────────────────────────────
    if access == "freemium":
        if user.is_premium_active:
            return AccessResult.GRANTED

        # Тот же раздел, что открывали бесплатно → всегда разрешить
        if user.free_section_used == section_key:
            return AccessResult.GRANTED

        # Бесплатный клик ещё не потрачен → тратим
        if user.free_section_used is None:
            user.free_section_used = section_key
            await session.commit()
            return AccessResult.GRANTED

        # Клик потрачен на другой раздел
        return AccessResult.PAYWALL_FREEMIUM

    # ── Только подписка ───────────────────────────────────────────────────────
    if access == "premium":
        return (
            AccessResult.GRANTED
            if user.is_premium_active
            else AccessResult.PAYWALL_PREMIUM
        )

    return AccessResult.GRANTED


# ──────────────────────────────────────────────────────────────────────────────
#  Чистые функции (без IO) — для клавиатур и regen
# ──────────────────────────────────────────────────────────────────────────────

def is_section_locked(user: User, section_key: str) -> bool:
    """
    Нужно ли рисовать 🔒 на кнопке?

    Логика совпадает с check_and_grant, но без побочных эффектов.
    """
    from core.catalog import READINGS

    config = READINGS.get(section_key)
    if not config:
        return False

    if config.access == "free":
        return False

    if user.is_premium_active:
        return False

    if config.access == "freemium":
        # Не заблокировано, если:
        #   а) клик ещё не потрачен (любой раздел откроется бесплатно)
        #   б) клик потрачен именно на этот раздел
        if user.free_section_used is None:
            return False
        return user.free_section_used != section_key

    if config.access == "premium":
        return True  # для не-премиума всегда заблокировано

    return False


def check_regen_access(user: User, section_key: str) -> bool:
    """
    Можно ли перегенерировать разбор?

    Перегенерация не тратит клик, но требует того же уровня доступа.
    """
    from core.catalog import READINGS

    config = READINGS.get(section_key)
    if not config:
        return False

    if config.access == "free":
        return True

    if user.is_premium_active:
        return True

    if config.access == "freemium":
        # Можно перегенерировать только тот раздел, на который потрачен клик
        return user.free_section_used == section_key

    # access == "premium" и user не premium → нельзя
    return False


# ──────────────────────────────────────────────────────────────────────────────
#  Вопросы
# ──────────────────────────────────────────────────────────────────────────────

def has_questions(user: User) -> bool:
    """У пользователя есть платные/бонусные вопросы."""
    return (user.questions_balance or 0) > 0


async def spend_question(session: AsyncSession, user: User) -> bool:
    """
    Списать 1 вопрос. Возвращает True если успешно, False если баланс 0.
    """
    if (user.questions_balance or 0) <= 0:
        return False
    user.questions_balance -= 1
    await session.commit()
    return True

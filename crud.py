"""
db/crud.py
==========
Тонкий слой доступа к данным.
Бизнес-логика здесь не живёт — только чтение/запись.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from db.models import AsyncSession, Forecast, Payment, User


# ──────────────────────────────────────────────────────────────────────────────
#  User
# ──────────────────────────────────────────────────────────────────────────────

async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
    user = await get_user(session, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.flush()
    return user


async def update_last_active(session: AsyncSession, user: User) -> None:
    user.last_active_at = datetime.utcnow()
    await session.commit()


async def set_premium(
    session: AsyncSession,
    user: User,
    days: int = 30,
) -> None:
    """Activate / extend premium subscription."""
    now = datetime.utcnow()
    if user.premium_until and user.premium_until > now:
        from datetime import timedelta
        user.premium_until = user.premium_until + timedelta(days=days)
    else:
        from datetime import timedelta
        user.premium_until = now + timedelta(days=days)
    user.is_premium = True
    await session.commit()


# ──────────────────────────────────────────────────────────────────────────────
#  Questions balance
# ──────────────────────────────────────────────────────────────────────────────

async def add_questions(session: AsyncSession, user: User, amount: int) -> None:
    user.questions_balance = (user.questions_balance or 0) + amount
    await session.commit()


async def get_or_replenish_questions(session: AsyncSession, user: User) -> int:
    """
    Replenish monthly bonus for premium users if needed.
    Returns current balance.
    """
    from config import PREMIUM_MONTHLY_QUESTIONS
    now_month = datetime.utcnow().strftime("%Y-%m")

    if user.is_premium_active and user.questions_month != now_month:
        user.questions_balance = (user.questions_balance or 0) + PREMIUM_MONTHLY_QUESTIONS
        user.questions_month = now_month
        await session.commit()

    return user.questions_balance or 0


# ──────────────────────────────────────────────────────────────────────────────
#  Forecasts
# ──────────────────────────────────────────────────────────────────────────────

async def get_forecast(
    session: AsyncSession,
    telegram_id: int,
    period_key: str,
) -> Forecast | None:
    result = await session.execute(
        select(Forecast).where(
            Forecast.telegram_id == telegram_id,
            Forecast.period_key == period_key,
        )
    )
    return result.scalar_one_or_none()


async def save_forecast(
    session: AsyncSession,
    telegram_id: int,
    period_key: str,
    content: str,
) -> Forecast:
    fc = Forecast(
        telegram_id=telegram_id,
        period_key=period_key,
        content=content,
    )
    session.add(fc)
    await session.commit()
    return fc


async def mark_forecast_sent(session: AsyncSession, forecast: Forecast) -> None:
    forecast.sent = True
    await session.commit()


# ──────────────────────────────────────────────────────────────────────────────
#  Payments
# ──────────────────────────────────────────────────────────────────────────────

async def record_payment(
    session: AsyncSession,
    telegram_id: int,
    product: str,
    stars: int,
) -> Payment:
    p = Payment(telegram_id=telegram_id, product=product, stars=stars)
    session.add(p)
    await session.commit()
    return p

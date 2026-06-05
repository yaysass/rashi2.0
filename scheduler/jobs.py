"""
scheduler/jobs.py
=================
APScheduler-джобы для рассылок и автоматических уведомлений.

Джобы:
  job_weekly_focus      — interval  hours=1
      Для каждого активного Премиум-юзера: перевести UTC в локальное время,
      проверить weekday+hour, взять/сгенерировать Фокус недели, отправить,
      пометить sent=True. Анти-флуд: sleep(0.05) между отправками.

  job_daily_automations — cron  hour=4  UTC
      • День рождения: совпадение ДД.ММ → поздравление + промо.
      • Реактивация: last_active_at > REACTIVATION_AFTER_DAYS → мягкое возвращение.
      • Брошенный пейволл: paywall_shown_at > PAYWALL_REMINDER_AFTER_DAYS И не куплен.

  job_premium_expiry    — cron  hour=10  UTC
      Премиум заканчивается через 2-3 дня → напомнить продлить.

  job_ekadashi          — cron  hour=9   UTC
      Сегодня Экадаши (по EKADASHI_DATES) → рассылка всем с enabled уведомлениями.

setup_scheduler(bot) → AsyncIOScheduler  (стартует из main.py).

Все джобы уважают notifications_enabled.
Каждый джоб оборачивает логику в try/except чтобы один сломанный юзер
не ронял остальные отправки.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, select

from config import (
    EKADASHI_DATES,
    PAYWALL_REMINDER_AFTER_DAYS,
    PREMIUM_EXPIRY_REMIND_DAYS,
    REACTIVATION_AFTER_DAYS,
)
from core.prompts import build_user_prompt, get_system_prompt
from db.models import AsyncSession, Forecast, User
from services.ai import generate
from texts import TEXTS, pluralize

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

def _current_week_key() -> str:
    d = date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


async def _send_safe(bot: "Bot", chat_id: int, text: str, **kwargs) -> bool:
    """Отправить сообщение; вернуть False при ошибке (не бросать)."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as exc:
        logger.warning("Send failed chat_id=%d: %s", chat_id, exc)
        return False


async def _get_or_generate_forecast(
    session, user: User, period_key: str
) -> str | None:
    """
    Взять Фокус недели из кэша или сгенерировать.
    Сохраняет в Forecast (sent=False — джоб потом ставит True).
    """
    existing = (await session.execute(
        select(Forecast).where(
            Forecast.telegram_id == user.telegram_id,
            Forecast.period_key  == period_key,
        )
    )).scalar_one_or_none()

    if existing:
        return existing.content

    astro = user.get_astro()
    if not astro:
        return None

    try:
        prompt  = build_user_prompt("weekly", astro, user.name or "", user.gender or "unknown", week=period_key)
        system  = get_system_prompt(user.gender or "unknown")
        content = await generate(prompt=prompt, system=system, max_tokens=900)
    except RuntimeError as exc:
        logger.error("Forecast generation failed user=%d: %s", user.telegram_id, exc)
        return None

    session.add(Forecast(
        telegram_id=user.telegram_id,
        period_key=period_key,
        content=content,
        sent=False,
    ))
    await session.commit()
    return content


# ──────────────────────────────────────────────────────────────────────────────
#  Джоб 1: Фокус недели  (interval, hours=1)
# ──────────────────────────────────────────────────────────────────────────────

async def job_weekly_focus(bot: "Bot") -> None:
    """
    Почасовая проверка: кому из Премиум-юзеров сейчас нужно отправить Фокус.
    Условие: weekday == weekly_focus_day AND hour == weekly_focus_hour
             AND на эту ISO-неделю ещё не слали (sent=False или нет записи).
    """
    now_utc     = datetime.utcnow()
    period_key  = _current_week_key()

    async with AsyncSession() as session:
        result = await session.execute(
            select(User).where(
                User.is_premium          == True,
                User.onboarding_complete == True,
                User.notifications_enabled == True,
                User.premium_until       >  now_utc,
            )
        )
        premium_users = result.scalars().all()

    for user in premium_users:
        try:
            tz       = pytz.timezone(user.timezone or "UTC")
            now_local = datetime.now(tz)

            if now_local.weekday() != user.weekly_focus_day:
                continue
            if now_local.hour != user.weekly_focus_hour:
                continue

            # Уже отправляли на эту неделю?
            async with AsyncSession() as session:
                sent_row = (await session.execute(
                    select(Forecast).where(
                        Forecast.telegram_id == user.telegram_id,
                        Forecast.period_key  == period_key,
                        Forecast.sent        == True,
                    )
                )).scalar_one_or_none()

                if sent_row:
                    continue

                # Свежий объект пользователя в сессии
                u = (await session.execute(
                    select(User).where(User.telegram_id == user.telegram_id)
                )).scalar_one_or_none()
                if not u:
                    continue

                content = await _get_or_generate_forecast(session, u, period_key)
                if not content:
                    continue

                # Пометить как отправленный
                forecast = (await session.execute(
                    select(Forecast).where(
                        Forecast.telegram_id == user.telegram_id,
                        Forecast.period_key  == period_key,
                    )
                )).scalar_one_or_none()
                if forecast:
                    forecast.sent = True
                    await session.commit()

            header = TEXTS["scheduler"]["weekly_header"]
            await _send_safe(
                bot, user.telegram_id,
                header + content,
                parse_mode="HTML",
            )
            await asyncio.sleep(0.05)   # анти-флуд Telegram

        except Exception as exc:
            logger.error("weekly_focus error user=%d: %s", user.telegram_id, exc)


# ──────────────────────────────────────────────────────────────────────────────
#  Джоб 2: Ежедневные автоматизации  (cron, hour=4)
# ──────────────────────────────────────────────────────────────────────────────

async def job_daily_automations(bot: "Bot") -> None:
    """День рождения / реактивация / брошенный пейволл."""
    today    = date.today()
    now_utc  = datetime.utcnow()
    ts       = TEXTS["scheduler"]

    async with AsyncSession() as session:
        result = await session.execute(
            select(User).where(User.onboarding_complete == True)
        )
        users = result.scalars().all()

    for user in users:
        if not user.notifications_enabled:
            continue

        try:
            # ── День рождения ─────────────────────────────────────────────────
            if user.birth_date:
                try:
                    parts = user.birth_date.split(".")
                    bday  = date(today.year, int(parts[1]), int(parts[0]))
                except (ValueError, IndexError):
                    bday  = None
                if bday and bday == today:
                    greeting = ts["birthday_greeting"].format(name=user.name or "")
                    promo    = "" if user.is_premium_active else ts["birthday_promo"]
                    await _send_safe(bot, user.telegram_id, greeting + promo, parse_mode="HTML")
                    await asyncio.sleep(0.05)
                    continue   # в один день — только поздравление

            # ── Реактивация ───────────────────────────────────────────────────
            if user.last_active_at:
                inactive_days = (now_utc - user.last_active_at).days
                if inactive_days >= REACTIVATION_AFTER_DAYS:
                    msg = ts["reactivation"].format(name=user.name or "")
                    await _send_safe(bot, user.telegram_id, msg, parse_mode="HTML")
                    await asyncio.sleep(0.05)
                    continue

            # ── Брошенный пейволл ─────────────────────────────────────────────
            if user.paywall_shown_at and not user.is_premium_active:
                days_since = (now_utc - user.paywall_shown_at).days
                if days_since >= PAYWALL_REMINDER_AFTER_DAYS:
                    word = pluralize(days_since, ts["words_day"])
                    msg  = ts["paywall_reminder"].format(days=days_since, word=word)
                    await _send_safe(bot, user.telegram_id, msg, parse_mode="HTML")
                    await asyncio.sleep(0.05)

        except Exception as exc:
            logger.error("daily_automations error user=%d: %s", user.telegram_id, exc)


# ──────────────────────────────────────────────────────────────────────────────
#  Джоб 3: Напоминание о продлении Премиума  (cron, hour=10)
# ──────────────────────────────────────────────────────────────────────────────

async def job_premium_expiry(bot: "Bot") -> None:
    """Премиум заканчивается через PREMIUM_EXPIRY_REMIND_DAYS дней → напомнить."""
    now_utc      = datetime.utcnow()
    threshold    = now_utc + timedelta(days=PREMIUM_EXPIRY_REMIND_DAYS)
    ts           = TEXTS["scheduler"]

    async with AsyncSession() as session:
        result = await session.execute(
            select(User).where(
                User.is_premium            == True,
                User.notifications_enabled == True,
                User.premium_until         != None,
                User.premium_until         >  now_utc,
                User.premium_until         <= threshold,
            )
        )
        expiring = result.scalars().all()

    for user in expiring:
        try:
            days_left = (user.premium_until - now_utc).days + 1
            word = pluralize(days_left, ts["words_day"])
            msg  = ts["premium_expiry"].format(days=days_left, word=word)
            await _send_safe(bot, user.telegram_id, msg, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.error("premium_expiry error user=%d: %s", user.telegram_id, exc)


# ──────────────────────────────────────────────────────────────────────────────
#  Джоб 4: Экадаши  (cron, hour=9)
# ──────────────────────────────────────────────────────────────────────────────

_EKADASHI_MESSAGE = (
    "Хороший день для тишины и осознанности. "
    "Избегай важных решений и конфликтов, "
    "направь энергию внутрь."
)

async def job_ekadashi(bot: "Bot") -> None:
    """Если сегодня Экадаши — рассылка всем пользователям с enabled уведомлениями."""
    today_str = date.today().isoformat()
    if today_str not in EKADASHI_DATES:
        return

    ts = TEXTS["scheduler"]
    msg = ts["ekadashi"].format(text=_EKADASHI_MESSAGE)

    async with AsyncSession() as session:
        result = await session.execute(
            select(User).where(
                User.onboarding_complete   == True,
                User.notifications_enabled == True,
            )
        )
        users = result.scalars().all()

    logger.info("Ekadashi %s: sending to %d users", today_str, len(users))
    for user in users:
        try:
            await _send_safe(bot, user.telegram_id, msg, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.error("ekadashi error user=%d: %s", user.telegram_id, exc)


# ──────────────────────────────────────────────────────────────────────────────
#  Сборка планировщика
# ──────────────────────────────────────────────────────────────────────────────

def setup_scheduler(bot: "Bot") -> AsyncIOScheduler:
    """
    Создать и настроить AsyncIOScheduler.
    Вызывать из main.py после создания Application.
    Scheduler стартует вызывающим кодом: scheduler.start().

    Возвращает scheduler (вызывающий вызывает .start() и .shutdown()).
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        job_weekly_focus,
        trigger="interval",
        hours=1,
        id="weekly_focus",
        args=[bot],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_daily_automations,
        trigger="cron",
        hour=4,
        id="daily_automations",
        args=[bot],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_premium_expiry,
        trigger="cron",
        hour=10,
        id="premium_expiry",
        args=[bot],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_ekadashi,
        trigger="cron",
        hour=9,
        id="ekadashi",
        args=[bot],
        max_instances=1,
        coalesce=True,
    )

    logger.info(
        "Scheduler configured: %d jobs",
        len(scheduler.get_jobs()),
    )
    return scheduler

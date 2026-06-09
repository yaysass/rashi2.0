"""
handlers/debug.py
=================
Админская диагностика VedAstro. Команда /diagva.

Регистрируется в main.py добавлением:
    from handlers import debug as h_debug
    application.add_handler(CommandHandler("diagva", h_debug.cmd_diagva))
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS

logger = logging.getLogger(__name__)


async def cmd_diagva(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Тестирует vedastro на известной дате и сравнивает Sun vs Mars.
    Если Sun и Mars возвращают одинаковые данные — vedastro ломанный.
    Если разные — vedastro работает, проблема в чём-то ещё.
    """
    user = update.effective_user
    if user is None or user.id not in ADMIN_IDS:
        return

    msg = await update.message.reply_text("Запускаю диагностику vedastro…")

    try:
        from vedastro import Calculate, GeoLocation, Time, PlanetName  # type: ignore
        from services.astrology import va, _to_float, _to_str
    except ImportError as exc:
        await msg.edit_text(f"vedastro не установлен: {exc}")
        return

    # Известная дата: 1 января 2000, 12:00 UTC, Лондон
    try:
        geo = GeoLocation("London", -0.1276, 51.5074)
        t = Time("12:00 01/01/2000 +00:00", geo)
    except Exception as exc:
        await msg.edit_text(f"Не смог создать Time: {exc}")
        return

    lines: list[str] = ["<b>Диагностика VedAstro</b>", ""]
    lines.append(f"Дата: 1 января 2000, 12:00 UTC, Лондон")
    lines.append("")

    sun = getattr(PlanetName, "Sun", None)
    mars = getattr(PlanetName, "Mars", None)
    lines.append(f"<code>PlanetName.Sun  = {sun}</code>")
    lines.append(f"<code>PlanetName.Mars = {mars}</code>")
    lines.append(f"<code>Sun == Mars: {sun == mars}</code>")
    lines.append("")

    # Список методов для проверки
    methods_to_test = [
        "PlanetNirayanaLongitude",
        "PlanetZodiacSign",
        "PlanetRasiName",
        "PlanetRasiD1Sign",
        "PlanetSayanaLongitude",
        "PlanetSignName",
    ]

    for method_name in methods_to_test:
        fn = getattr(Calculate, method_name, None)
        if fn is None:
            lines.append(f"<b>{method_name}</b>: НЕТ в этой версии")
            continue
        lines.append(f"<b>{method_name}</b>")

        # Порядок 1: (planet, time)
        for label, planet in [("Sun ", sun), ("Mars", mars)]:
            try:
                res = await va(fn, planet, t)
                lines.append(f"  ({label}, t) → <code>{_to_str(res)[:40]}</code>")
            except Exception as exc:
                lines.append(f"  ({label}, t) → ERR: {str(exc)[:40]}")

        # Порядок 2: (time, planet)
        for label, planet in [("Sun ", sun), ("Mars", mars)]:
            try:
                res = await va(fn, t, planet)
                lines.append(f"  (t, {label}) → <code>{_to_str(res)[:40]}</code>")
            except Exception as exc:
                lines.append(f"  (t, {label}) → ERR: {str(exc)[:40]}")

        lines.append("")

    # Отправим разбитыми сообщениями если длинно
    text = "\n".join(lines)
    if len(text) > 3500:
        chunks = []
        cur = ""
        for line in lines:
            if len(cur) + len(line) > 3500:
                chunks.append(cur)
                cur = line + "\n"
            else:
                cur += line + "\n"
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="HTML")
        await msg.delete()
    else:
        await msg.edit_text(text, parse_mode="HTML")

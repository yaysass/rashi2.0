from __future__ import annotations
import asyncio, json, logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ConversationHandler,
    ContextTypes, MessageHandler, PreCheckoutQueryHandler, filters,
)
import ai
import keyboards as kb
from config import (
    ADMIN_IDS, WELCOME_IMAGE,
    STARS_PREMIUM_MONTH, STARS_Q1, STARS_Q3, STARS_Q10, STARS_SPECIAL,
    PREMIUM_MONTHLY_QUESTIONS,
)
from database import (
    AsyncSessionLocal, get_or_create_user, get_user, update_user,
    get_or_replenish_questions,
)
from geo import geocode_city
from astrology import collect_and_format_chart, collect_partner_text

logger = logging.getLogger(__name__)

TG_MAX_LEN = 4000  # Telegram limit is 4096, keep margin

(AWAIT_BEGIN, ASK_NAME, ASK_GENDER, ASK_DATE, ASK_TIME, ASK_CITY, CONFIRM_CITY) = range(7)

AWAIT_MUHURTA    = "muhurta"
AWAIT_QUESTION   = "question"
# Edit birth flow (outside ConversationHandler)
AWAIT_EDIT_DATE  = "edit_date"
AWAIT_EDIT_TIME  = "edit_time"
AWAIT_EDIT_CITY  = "edit_city"
# Synastry flow
AWAIT_SYN_NAME  = "syn_name"
AWAIT_SYN_DATE  = "syn_date"
AWAIT_SYN_TIME  = "syn_time"
AWAIT_SYN_CITY  = "syn_city"
# Child chart flow
AWAIT_CHILD_NAME = "child_name"
AWAIT_CHILD_DATE = "child_date"
AWAIT_CHILD_TIME = "child_time"
AWAIT_CHILD_CITY = "child_city"


# ── Utilities ─────────────────────────────────────────────────────────────────
async def _typing(context, chat_id: int, seconds: float = 2.5) -> None:
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await asyncio.sleep(seconds)


@asynccontextmanager
async def _typing_action(context, chat_id: int):
    """Keep the 'typing…' indicator alive for the whole block.

    Telegram's chat action expires after ~5s, so a single send_chat_action goes
    stale during a 10-30s AI generation and the bot looks frozen. This refreshes
    it every 4s in the background and stops cleanly on exit.
    """
    stop = asyncio.Event()

    async def _loop():
        while not stop.is_set():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _safe_html(text: str) -> str:
    """
    Render Telegram-safe HTML tags and escape everything else.
    Allowed tags: b, strong, i, em, u, s, code, pre, blockquote, tg-spoiler.
    Also converts **text** → <b>text</b> as a Markdown safety net.
    """
    import re
    ALLOWED = ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
               "code", "pre", "blockquote", "tg-spoiler")
    tag_re = re.compile(
        r"</?(?:" + "|".join(ALLOWED) + r")>",
        flags=re.IGNORECASE,
    )
    # 1) Сохранить разрешённые теги через плейсхолдеры
    placeholders: dict[str, str] = {}
    counter = [0]

    def _mask(m):
        token = f"\x00TG{counter[0]}\x00"
        counter[0] += 1
        placeholders[token] = m.group(0)
        return token

    masked = tag_re.sub(_mask, text)
    # 2) Эскейпнуть всё остальное
    masked = masked.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 3) Markdown bold → HTML bold (страховка на случай если Claude забудется)
    masked = re.sub(r"\*\*([^\n*]+?)\*\*", r"<b>\1</b>", masked)
    # 4) Вернуть сохранённые теги
    for token, original in placeholders.items():
        masked = masked.replace(token, original)
    return masked


def _split_for_telegram(text: str, limit: int = TG_MAX_LEN) -> list[str]:
    """Split text into Telegram-sized chunks on paragraph/line/space boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]


async def _safe_reply(message_obj, text: str, **kwargs) -> None:
    """Send an HTML message, splitting on safe boundaries if over Telegram's limit."""
    from telegram.constants import ParseMode as PM
    if 'parse_mode' not in kwargs:
        kwargs['parse_mode'] = PM.HTML
    raw_chunks = _split_for_telegram(text, TG_MAX_LEN)
    last = len(raw_chunks) - 1
    for i, raw in enumerate(raw_chunks):
        chunk = _safe_html(raw)
        if i == last:
            await message_obj.reply_text(chunk, **kwargs)
        else:
            await message_obj.reply_text(chunk, parse_mode=PM.HTML)
        if last > 0:
            await asyncio.sleep(0.4)


async def _send_long(context, chat_id: int, text: str, **kwargs) -> None:
    """Send a long HTML message via bot.send_message (no message_obj available)."""
    from telegram.constants import ParseMode as PM
    if 'parse_mode' not in kwargs:
        kwargs['parse_mode'] = PM.HTML
    raw_chunks = _split_for_telegram(text, TG_MAX_LEN)
    last = len(raw_chunks) - 1
    for i, raw in enumerate(raw_chunks):
        chunk = _safe_html(raw)
        if i == last:
            await context.bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
        else:
            await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=PM.HTML)
        if last > 0:
            await asyncio.sleep(0.4)


async def _get_astro(context, user) -> Optional[dict]:
    cached = context.user_data.get("astro_cache")
    if cached:
        return cached
    db_cache = user.get_astro_cache()
    if db_cache:
        context.user_data["astro_cache"] = db_cache
        return db_cache
    return None


async def _activate_premium(user, session):
    expires = datetime.utcnow() + timedelta(days=30)
    await update_user(session, user, is_premium=True, premium_until=expires,
                      premium_expired_notified=False, renewal_reminder_3d=False,
                      renewal_reminder_1d=False)


# ── Access control helpers ────────────────────────────────────────────────────
async def _require_premium(message_obj, user) -> bool:
    """Return True if premium, else send paywall message and return False."""
    if user.is_premium_active:
        return True
    await message_obj.reply_text(
        "Этот раздел открыт только в Премиуме ⭐️\n\n"
        "Все разборы карты, Продвинутый Джйотиш, Фокус недели "
        "и 3 вопроса в месяц бесплатно.",
        reply_markup=kb.premium_cta_kb(),
    )
    return False


async def _gate_free(message_obj, user, section_key: str) -> bool:
    """Allow one free thematic reading. Premium users are always allowed.

    Returns True → proceed. Returns False → paywall shown.
    """
    if user.is_premium_active:
        return True
    used = user.free_section_used
    if used == section_key:
        return True          # revisiting the same free section they already used
    if used is None:
        # Grant the free pick
        async with AsyncSessionLocal() as session:
            u = await get_user(session, user.telegram_id)
            await update_user(session, u, free_section_used=section_key)
        return True
    # Free pick already spent on a different section
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.telegram_id)
        if u and not u.paywall_shown_at:
            await update_user(session, u, paywall_shown_at=datetime.utcnow())
    await message_obj.reply_text(
        "Здесь скрыт глубокий разбор этой сферы твоей жизни 🗝️\n\n"
        "Чтобы открыть этот и все остальные разделы, перейди на Премиум-подписку ⭐️",
        reply_markup=kb.premium_cta_kb(),
    )
    return False


# ── Telegram Stars payments ───────────────────────────────────────────────────
STARS_PRODUCTS = {
    "premium_month": dict(
        title="Премиум-доступ — 30 дней",
        description=(
            "Все тематические разборы, Продвинутый Джйотиш, Фокус недели "
            f"и {PREMIUM_MONTHLY_QUESTIONS} вопроса каждый месяц."
        ),
        stars=STARS_PREMIUM_MONTH,
    ),
    "q1": dict(
        title="1 вопрос астрологу",
        description="Один персональный вопрос на основе твоей натальной карты.",
        stars=STARS_Q1,
    ),
    "q3": dict(
        title="3 вопроса астрологу",
        description="Пакет из 3 персональных вопросов.",
        stars=STARS_Q3,
    ),
    "q10": dict(
        title="10 вопросов астрологу",
        description="Пакет из 10 персональных вопросов.",
        stars=STARS_Q10,
    ),
    "special_synastry": dict(
        title="Зеркало отношений",
        description="Разбор совместимости двух карт.",
        stars=STARS_SPECIAL,
    ),
    "special_child": dict(
        title="Росток судьбы",
        description="Разбор натальной карты ребёнка для родителей.",
        stars=STARS_SPECIAL,
    ),
    "special_year": dict(
        title="Личный вектор года",
        description="Ключевые темы и периоды ближайших 12 месяцев.",
        stars=STARS_SPECIAL,
    ),
}

_Q_AMOUNTS = {"q1": 1, "q3": 3, "q10": 10}


async def _send_invoice(context, chat_id: int, payload: str) -> None:
    """Send a Telegram Stars invoice. For XTR: provider_token='' and amount = star count."""
    prod = STARS_PRODUCTS[payload]
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=prod["title"],
        description=prod["description"],
        payload=payload,
        provider_token="",        # empty string = Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(prod["title"], prod["stars"])],
        start_parameter="rashi",
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    if q.invoice_payload in STARS_PRODUCTS:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Этот товар сейчас недоступен 🕯")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payload  = update.message.successful_payment.invoice_payload
    user_tg  = update.effective_user
    chat_id  = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user_tg.id)
        if payload == "premium_month":
            await _activate_premium(u, session)
        elif payload in _Q_AMOUNTS:
            new_bal = (u.questions_balance or 0) + _Q_AMOUNTS[payload]
            await update_user(session, u, questions_balance=new_bal)

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)

    if payload == "premium_month":
        await _show_premium_welcome_msg(context, chat_id)
    elif payload in _Q_AMOUNTS:
        n = _Q_AMOUNTS[payload]
        bal = user.questions_balance or 0
        plural = "вопрос" if n == 1 else "вопроса" if 2 <= n <= 4 else "вопросов"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Куплено {n} {plural} ✨\n\nНа балансе: {bal}. Задавай!",
            reply_markup=kb.back_main_kb(),
        )
    elif payload == "special_synastry":
        await _start_synastry_msg(update.message, context)
    elif payload == "special_child":
        await _start_child_chart_msg(update.message, context)
    elif payload == "special_year":
        await _run_transit_year(update.message, context, user, chat_id)


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_tg = update.effective_user
    chat_id  = update.effective_chat.id
    context.user_data.clear()

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, user_tg.id, user_tg.username)
        if user.onboarding_complete:
            await _typing(context, chat_id, 1.0)
            await update.message.reply_text(
                f"С возвращением, {user.name} 🤍\n\nЧто изучаем сегодня?",
                reply_markup=kb.main_menu_kb(user.is_premium_active, user.free_section_used),
            )
            return ConversationHandler.END

    await _typing(context, chat_id, 1.5)
    greeting = "Мне нужна информация о тебе, чтобы построить твою натальную карту 📜 ✒️"
    if WELCOME_IMAGE:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=WELCOME_IMAGE,
                                         caption=greeting, reply_markup=kb.begin_kb())
        except Exception:
            await update.message.reply_text(greeting, reply_markup=kb.begin_kb())
    else:
        await update.message.reply_text(greeting, reply_markup=kb.begin_kb())
    return AWAIT_BEGIN


# ── Onboarding ────────────────────────────────────────────────────────────────
async def begin_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await _typing(context, query.message.chat_id)
    await query.message.reply_text("Как мне к тебе обращаться?")
    return ASK_NAME


async def recv_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 50:
        await update.message.reply_text("Пожалуйста, напиши своё имя 🤍")
        return ASK_NAME
    context.user_data["name"] = name
    await _typing(context, update.effective_chat.id)
    await update.message.reply_text(
        f"Прекрасное имя, {name} ✨\n\nЧтобы я мог точнее настроиться, подскажи свой пол.",
        reply_markup=kb.gender_kb(),
    )
    return ASK_GENDER


async def recv_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["gender"] = "male" if query.data == "gender_male" else "female"
    await _typing(context, query.message.chat_id)
    await query.message.reply_text(
        "Принято 🤍\n\nНапиши дату рождения в формате ДД.ММ.ГГГГ\nНапример: 12.02.2000"
    )
    return ASK_DATE


async def recv_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace("/", ".").replace("-", ".")
    try:
        parts = raw.split(".")
        if len(parts) != 3:
            raise ValueError
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        if not (1 <= d <= 31 and 1 <= m <= 12 and 1900 <= y <= datetime.now().year):
            raise ValueError
        datetime(y, m, d)
    except Exception:
        await update.message.reply_text("Формат: ДД.ММ.ГГГГ, например: 15.07.1995 🕯")
        return ASK_DATE
    context.user_data["birth_date"] = f"{d:02d}.{m:02d}.{y}"
    await _typing(context, update.effective_chat.id)
    await update.message.reply_text(
        "Записал ✨\n\nНапиши точное время рождения. Например: 11:00\n"
        "(Если сомневаешься, напиши примерное)"
    )
    return ASK_TIME


async def recv_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(".", ":").replace("-", ":")
    try:
        parts = raw.split(":")
        h, mi = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError
    except Exception:
        await update.message.reply_text("Формат ЧЧ:ММ, например: 14:30 🕯")
        return ASK_TIME
    context.user_data["birth_time"] = f"{h:02d}:{mi:02d}"
    await _typing(context, update.effective_chat.id)
    born_word = "родилась" if context.user_data.get("gender") == "female" else "родился"
    await update.message.reply_text(
        f"Почти готово 🪐\n\nВ каком городе ты {born_word}? Просто напиши название."
    )
    return ASK_CITY


async def recv_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1: geocode city, show confirmation with exact name found."""
    city_name = update.message.text.strip()
    chat_id   = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    geo = await geocode_city(city_name)
    if not geo:
        await update.message.reply_text(
            f"Не могу найти «{city_name}» 🕯\n\n"
            "Попробуй написать на английском или уточни название — "
            "например: «Иваново, Россия» или «Minsk, Belarus»."
        )
        return ASK_CITY

    context.user_data["pending_geo"] = geo
    short = geo.get("short_name") or geo.get("display_name", city_name)
    await update.message.reply_text(
        f"Я нашёл этот город:\n\n"
        f"📍 <b>{short}</b>\n\n"
        f"Это верно?",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.city_confirm_kb(),
    )
    return CONFIRM_CITY


async def confirm_city_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2a: user confirmed city — run Prokerala, then auto-generate personality."""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_tg = update.effective_user
    geo     = context.user_data.get("pending_geo", {})

    await query.message.reply_text(
        "Твои координаты приняты, начинаю расчет 🕯\n\n"
        "Джйотиш не просто предсказывает будущее. Это инструмент самопознания, "
        "который подсвечивает слепые зоны и показывает истинный потенциал 🧿\n\n"
        "Я могу помочь найти призвание, разобраться в отношениях, "
        "выбрать момент для важного решения ☄️"
    )

    birth_date = context.user_data["birth_date"]
    birth_time = context.user_data["birth_time"]
    name       = context.user_data["name"]
    gender     = context.user_data["gender"]
    city_name  = geo.get("short_name", "")

    # ── Сбор карты через VedAstro ─────────────────────────────────────────────
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    astro_cache = await collect_and_format_chart(birth_date, birth_time, geo)

    full_astro = astro_cache["text"]

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, user_tg.id, user_tg.username)
        await update_user(
            session, user,
            name=name, gender=gender,
            birth_date=birth_date, birth_time=birth_time, birth_city=city_name,
            birth_lat=geo["lat"], birth_lon=geo["lon"], timezone=geo["timezone"],
            astro_data_json=json.dumps(astro_cache, ensure_ascii=False),
            onboarding_complete=True,
        )

    context.user_data["astro_cache"] = astro_cache
    context.user_data.pop("pending_geo", None)

    # Dramatic pause, then generate personality eagerly
    await asyncio.sleep(4)
    await context.bot.send_message(chat_id=chat_id, text="Я почти закончил строить твою карту...")
    await asyncio.sleep(3)
    await context.bot.send_message(chat_id=chat_id, text="Собираю картину... 🪐")

    personality_text = ""
    try:
        async with _typing_action(context, chat_id):
            personality_text = await ai.gen_personality(full_astro, name, gender)
    except Exception as exc:
        logger.error("gen_personality onboarding: %s", exc)

    if personality_text:
        astro_cache["personality"] = personality_text
        async with AsyncSessionLocal() as session:
            u = await get_user(session, user_tg.id)
            if u:
                await update_user(session, u,
                                  astro_data_json=json.dumps(astro_cache, ensure_ascii=False))
        context.user_data["astro_cache"] = astro_cache

    msg = (f"📜 Твоя натальная карта\n\n{personality_text}"
           if personality_text else "Твоя карта готова 📜")
    await _send_long(context, chat_id, msg, reply_markup=kb.main_menu_kb(False, None))
    return ConversationHandler.END


async def confirm_city_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2b: user rejected city — ask to re-enter."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_geo", None)
    await query.message.reply_text(
        "Хорошо, уточни название 🗝️\n\n"
        "Напиши город подробнее — например:\n"
        "«Иваново, Ивановская область» или «Minsk, Belarus»"
    )
    return ASK_CITY


# ── Natal card (always free) ──────────────────────────────────────────────────
def _build_birth_info(user, astro: dict | None) -> str:
    """Сформировать «шапку» карты для gen_natal_chart: дата/время/место/Лагна."""
    lagna = ""
    if astro:
        lagna = ((astro.get("raw") or {}).get("ascendant") or {}).get("sign") or ""
    parts = []
    if user.birth_date:
        parts.append(f"Дата: {user.birth_date}")
    if user.birth_time:
        parts.append(f"Время: {user.birth_time}")
    if user.birth_city:
        parts.append(f"Место: {user.birth_city}")
    if lagna:
        parts.append(f"Лагна: {lagna}")
    return "\n".join(parts)


async def _show_natal(query, context, user, chat_id):
    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return

    pages = astro.get("personality_pages")
    if not pages:
        # Генерим свежий разбор и разбиваем на страницы
        try:
            async with _typing_action(context, chat_id):
                full_text = await ai.gen_natal_chart(
                    astro["text"],
                    user.name or "",
                    user.gender or "female",
                    birth_info=_build_birth_info(user, astro),
                )
            pages = _split_natal_pages(full_text)
            astro["personality"]       = full_text
            astro["personality_pages"] = pages
            async with AsyncSessionLocal() as session:
                u = await get_user(session, user.telegram_id)
                if u:
                    await update_user(session, u,
                                      astro_data_json=json.dumps(astro, ensure_ascii=False))
            context.user_data["astro_cache"] = astro
        except Exception as exc:
            logger.error("gen_natal_chart on-demand: %s", exc)
            await query.message.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
            return

    if not pages:
        await query.message.reply_text("Не удалось сформировать разбор 🕯")
        return

    regen_used = bool(astro.get("regen_used", False))
    await _safe_reply(
        query.message,
        pages[0],
        reply_markup=_natal_page_markup(0, len(pages), regen_used),
    )


# ── Thematic readings (freemium gated) ───────────────────────────────────────
THEME_CONFIG = {
    "love":   ("🤍 Любовь и отношения", ai.gen_love),
    "money":  ("🪙 Деньги и карьера",   ai.gen_money_career),
    "karma":  ("🗝 Карма и уроки",       ai.gen_karma_node),
    "family": ("🌿 Семья и дети",        ai.gen_family),
}


async def _show_theme(query, context, user, chat_id, theme: str):
    if not await _gate_free(query.message, user, theme):
        return
    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return

    if theme == "years":
        title = "⏳ Самые важные годы"
        async def _gen():
            return await ai.gen_dasha(astro["text"], astro.get("dasha", ""),
                                      user.name or "", user.gender or "female")
    else:
        title, fn = THEME_CONFIG[theme]
        async def _gen():
            return await fn(astro["text"], user.name or "", user.gender or "female")

    try:
        async with _typing_action(context, chat_id):
            text = await _gen()
    except Exception as exc:
        logger.error("theme %s: %s", theme, exc)
        await query.message.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
        return

    # Re-fetch user so free_section_used is up-to-date for the menu
    async with AsyncSessionLocal() as session:
        user = await get_user(session, user.telegram_id)

    # Build keyboard: next-section button (if any) on top of main menu
    base_menu = kb.main_menu_kb(user.is_premium_active, user.free_section_used)
    next_btn  = _next_section_button(theme)
    if next_btn:
        try:
            existing_rows = list(base_menu.inline_keyboard)
            combined = InlineKeyboardMarkup([[next_btn]] + existing_rows)
        except Exception:
            combined = InlineKeyboardMarkup([[next_btn]])
        markup = combined
    else:
        markup = base_menu

    await _safe_reply(query.message, f"{title}\n\n{text}", reply_markup=markup)


# ── Weekly Focus (premium only) ───────────────────────────────────────────────
async def _show_weekly_focus(query, context, user, chat_id):
    if not await _require_premium(query.message, user):
        return
    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return

    from database import get_forecast_for_date, save_forecast
    week_key = datetime.utcnow().strftime("%Y-W%V")

    async with AsyncSessionLocal() as session:
        existing = await get_forecast_for_date(session, user.telegram_id, week_key)

    if existing:
        await _safe_reply(query.message,
                          f"🗓 Фокус недели\n\n{existing.content}",
                          reply_markup=kb.back_main_kb())
        return

    # Generate for this week
    try:
        async with _typing_action(context, chat_id):
            text = await ai.gen_weekly_focus(
                astro["text"], user.name or "", user.gender or "female", week_key
            )
    except Exception as exc:
        logger.error("gen_weekly_focus: %s", exc)
        await query.message.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
        return

    async with AsyncSessionLocal() as session:
        await save_forecast(session, user.telegram_id, week_key, text)

    await _safe_reply(query.message, f"🗓 Фокус недели\n\n{text}",
                      reply_markup=kb.back_main_kb())


# ── Advanced Jyotish sections (premium) ──────────────────────────────────────
SECTION_CONFIG = {
    "karma_node": ("🗝️ Узлы Раху-Кету",      ai.gen_karma_node),
    "resources":  ("🌿 Точки ресурса",        ai.gen_resources),
    "atmakaraka": ("💎 Атмакарака",           ai.gen_atmakaraka),
    "navamsha":   ("🌑 Навамша D9",           ai.gen_navamsha),
    "yoga":       ("✨ Астрологические Йоги",  ai.gen_yoga),
    "sadesati":   ("🕯 Транзит Саде-Сати",     ai.gen_sadesati),
    "upaya":      ("📜 Ключи",                ai.gen_upaya),
    "shadbala":   ("⚖️ Сила Шадбала",          ai.gen_shadbala),
    "dashamsha":  ("💼 Дашамша D10",          ai.gen_dashamsha),
    "arudha":     ("🪞 Арудха Пады",          ai.gen_arudha),
    "transits":   ("🪐 Транзиты планет",       ai.gen_transits),
}


async def _show_section(query, context, user, chat_id, section: str):
    if not await _require_premium(query.message, user):
        return
    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return
    title, gen_fn = SECTION_CONFIG[section]
    try:
        async with _typing_action(context, chat_id):
            text = await gen_fn(astro["text"], user.name or "", user.gender or "female")
    except Exception as exc:
        logger.error("gen_%s: %s", section, exc)
        await query.message.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
        return
    await _safe_reply(query.message, f"{title}\n\n{text}", reply_markup=kb.back_advanced_kb())


async def _show_dasha(query, context, user, chat_id):
    if not await _require_premium(query.message, user):
        return
    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return
    try:
        async with _typing_action(context, chat_id):
            text = await ai.gen_dasha(astro["text"], astro.get("dasha", ""),
                                      user.name or "", user.gender or "female")
    except Exception as exc:
        logger.error("gen_dasha: %s", exc)
        await query.message.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
        return
    await _safe_reply(query.message, f"⏳ Периоды Даши\n\n{text}",
                      reply_markup=kb.back_advanced_kb())


async def _show_premium_house(query, context, user, house_num: int, chat_id: int):
    if not await _require_premium(query.message, user):
        return
    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return
    house_name = kb.HOUSE_NAMES.get(house_num, "")
    try:
        async with _typing_action(context, chat_id):
            text = await ai.gen_house_premium(
                house_num, house_name, astro["text"], user.name or "", user.gender or "female"
            )
    except Exception as exc:
        logger.error("gen_house_premium: %s", exc)
        await query.message.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
        return
    await _safe_reply(query.message, f"{house_num}-й дом: {house_name} 🏠\n\n{text}",
                      reply_markup=kb.back_houses_adv_kb())


# ── Special readings (launched after a Stars payment) ────────────────────────
async def _start_synastry_msg(message_obj, context):
    context.user_data["awaiting"] = AWAIT_SYN_NAME
    context.user_data["syn"] = {}
    await message_obj.reply_text(
        "Зеркало отношений 🪞\n\n"
        "Я проанализирую совместимость двух карт. "
        "Напиши имя партнёра или человека, о котором хочешь узнать."
    )


async def _start_child_chart_msg(message_obj, context):
    context.user_data["awaiting"] = AWAIT_CHILD_NAME
    context.user_data["child"] = {}
    await message_obj.reply_text(
        "Росток судьбы 🌿\n\n"
        "Я составлю разбор карты ребёнка: таланты, особенности психики, "
        "рекомендации для родителей.\n\nНапиши имя ребёнка."
    )


async def _run_transit_year(message_obj, context, user, chat_id):
    astro = await _get_astro(context, user)
    if not astro:
        await message_obj.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return
    try:
        async with _typing_action(context, chat_id):
            text = await ai.gen_transit_year(astro["text"], user.name or "", user.gender or "female")
    except Exception as exc:
        logger.error("gen_transit_year: %s", exc)
        await message_obj.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
        return
    await _safe_reply(message_obj, f"Личный вектор года ☄️\n\n{text}",
                      reply_markup=kb.back_main_kb())


# ── Questions ──────────────────────────────────────────────────────────────────
async def _handle_question_entry(query, context, user, chat_id):
    """Check question balance and start flow, or show the question shop."""
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user.telegram_id)
        balance = await get_or_replenish_questions(session, u)

    if balance <= 0:
        await query.message.reply_text(
            "✒️ Задать свой вопрос\n\n"
            "Вопросов на балансе нет 🌑 Пополни пакет или оформи Премиум "
            "и получай 3 вопроса каждый месяц.",
            reply_markup=kb.question_shop_kb(),
        )
        return

    plural = "вопрос" if balance == 1 else "вопроса" if 2 <= balance <= 4 else "вопросов"
    context.user_data["awaiting"] = AWAIT_QUESTION
    await query.message.reply_text(
        f"Задай свой вопрос ✒️\n\nНа балансе: {balance} {plural} 🤍"
    )


async def _process_question(update, context, user_tg, chat_id):
    question = update.message.text.strip()
    context.user_data.pop("awaiting", None)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user_tg.id)
        balance = await get_or_replenish_questions(session, u)

    if balance <= 0:
        await update.message.reply_text(
            "Вопросов на балансе нет 🌑",
            reply_markup=kb.question_shop_kb(),
        )
        return

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    astro = await _get_astro(context, user)
    if not astro:
        await update.message.reply_text("Не могу загрузить карту 🕯")
        return

    try:
        async with _typing_action(context, chat_id):
            answer = await ai.gen_question_answer(
                question, astro["text"], user.name or "", user.gender or "female"
            )
    except Exception as exc:
        logger.error("gen_question_answer: %s", exc)
        await update.message.reply_text("Произошла ошибка 🕯")
        return

    # Deduct 1 from balance
    async with AsyncSessionLocal() as session:
        u = await get_user(session, user_tg.id)
        new_balance = max(0, (u.questions_balance or 1) - 1)
        await update_user(session, u, questions_balance=new_balance)

    await _safe_reply(update.message, answer)
    await asyncio.sleep(0.5)

    if new_balance > 0:
        plural = "вопрос" if new_balance == 1 else "вопроса" if 2 <= new_balance <= 4 else "вопросов"
        await update.message.reply_text(
            f"На балансе осталось {new_balance} {plural} 🕯",
            reply_markup=kb.back_main_kb(),
        )
    else:
        await update.message.reply_text(
            "Вопросы использованы 🌑",
            reply_markup=kb.question_shop_kb(),
        )


# ── Settings: subscription info ───────────────────────────────────────────────
async def _show_subscription_info(query, context, user, chat_id):
    if user.is_premium_active and user.premium_until:
        days_left = max(0, (user.premium_until - datetime.utcnow()).days)
        balance   = user.questions_balance or 0
        await query.message.reply_text(
            f"⭐️ Премиум активен\n\n"
            f"Осталось дней: {days_left}\n"
            f"До: {user.premium_until.strftime('%d.%m.%Y')}\n\n"
            f"Вопросов на балансе: {balance}",
            reply_markup=kb.renewal_kb(),
        )
    else:
        balance = user.questions_balance or 0
        await query.message.reply_text(
            f"⭐️ Премиум не активен\n\n"
            f"Вопросов на балансе: {balance}\n\n"
            "Оформи Премиум и открой все разделы карты.",
            reply_markup=kb.premium_cta_kb(),
        )


async def _show_premium_welcome_msg(context, chat_id):
    await context.bot.send_message(
        chat_id=chat_id,
        text="Благодарю за доверие 🤍 Премиум активирован.\n\n"
             "Открыты все разделы карты, Продвинутый Джйотиш и Фокус недели. "
             f"Бонус — {PREMIUM_MONTHLY_QUESTIONS} вопроса каждый месяц. ✨",
    )
    await context.bot.send_message(
        chat_id=chat_id, text="Что изучаем прямо сейчас?",
        reply_markup=kb.main_menu_kb(True),
    )


# ── Edit birth data flow ──────────────────────────────────────────────────────
async def _recv_edit_date(update, context):
    raw = update.message.text.strip().replace("/", ".").replace("-", ".")
    try:
        parts = raw.split(".")
        if len(parts) != 3:
            raise ValueError
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        if not (1 <= d <= 31 and 1 <= m <= 12 and 1900 <= y <= datetime.now().year):
            raise ValueError
        datetime(y, m, d)
    except Exception:
        await update.message.reply_text("Формат: ДД.ММ.ГГГГ, например: 15.07.1995 🕯")
        return
    context.user_data.setdefault("edit_birth", {})["date"] = f"{d:02d}.{m:02d}.{y}"
    context.user_data["awaiting"] = AWAIT_EDIT_TIME
    await update.message.reply_text(
        "Время рождения? Формат ЧЧ:ММ\nНапример: 11:00 (примерное тоже подойдёт)"
    )


async def _recv_edit_time(update, context):
    raw = update.message.text.strip().replace(".", ":").replace("-", ":")
    try:
        parts = raw.split(":")
        h, mi = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError
    except Exception:
        await update.message.reply_text("Формат ЧЧ:ММ, например: 14:30 🕯")
        return
    context.user_data.setdefault("edit_birth", {})["time"] = f"{h:02d}:{mi:02d}"
    context.user_data["awaiting"] = AWAIT_EDIT_CITY
    await update.message.reply_text("В каком городе ты родился(ась)?")


async def _recv_edit_city(update, context):
    city_name = update.message.text.strip()
    chat_id   = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    geo = await geocode_city(city_name)
    if not geo:
        await update.message.reply_text(
            f"Не могу найти «{city_name}» 🕯\n\n"
            "Попробуй написать на английском или уточни название — "
            "например: «Иваново, Россия» или «Minsk, Belarus»."
        )
        return
    context.user_data["pending_edit_geo"] = geo
    short = geo.get("short_name") or geo.get("display_name", city_name)
    await update.message.reply_text(
        f"Я нашёл этот город:\n\n"
        f"📍 <b>{short}</b>\n\n"
        f"Это верно?",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.edit_city_confirm_kb(),
    )


async def _apply_edit_birth(query, context, user_tg, chat_id):
    """Apply new birth data: re-run Prokerala + regenerate personality."""
    geo  = context.user_data.pop("pending_edit_geo", {})
    edit = context.user_data.pop("edit_birth", {})
    context.user_data.pop("awaiting", None)

    birth_date = edit.get("date", "")
    birth_time = edit.get("time", "12:00")
    city_name  = geo.get("short_name", "")

    if not birth_date or not geo:
        await query.message.reply_text("Нет данных для обновления. Попробуй ещё раз через Настройки.")
        return

    await query.message.reply_text(
        "Данные обновлены, пересчитываю карту 🔄\n\nЭто займёт несколько секунд..."
    )

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    gender = user.gender if user else "female"
    name   = user.name   if user else ""

    # ── Пересчёт карты через VedAstro ─────────────────────────────────────────
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    astro_cache = await collect_and_format_chart(birth_date, birth_time, geo)
    full_astro  = astro_cache["text"]

    # Regenerate personality
    try:
        async with _typing_action(context, chat_id):
            personality = await ai.gen_personality(full_astro, name, gender)
        astro_cache["personality"] = personality
    except Exception as exc:
        logger.error("gen_personality after edit: %s", exc)

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user_tg.id)
        await update_user(
            session, u,
            birth_date=birth_date, birth_time=birth_time, birth_city=city_name,
            birth_lat=geo["lat"], birth_lon=geo["lon"], timezone=geo["timezone"],
            astro_data_json=json.dumps(astro_cache, ensure_ascii=False),
        )

    context.user_data["astro_cache"] = astro_cache

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)

    msg = "📜 Карта пересчитана ✅\n\n"
    if astro_cache.get("personality"):
        msg += astro_cache["personality"]
    else:
        msg += "Натальная карта обновлена по новым данным."

    await _send_long(
        context, chat_id, msg,
        reply_markup=kb.main_menu_kb(
            user.is_premium_active if user else False,
            user.free_section_used if user else None,
        ),
    )


# ── Main callback dispatcher ──────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id
    user_tg = update.effective_user

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, user_tg.id, user_tg.username)
        await update_user(session, user)           # bumps last_active_at
    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)

    # ── Navigation ─────────────────────────────────────────────────────────────
    if data in ("menu_main", "pm_main"):
        await query.message.reply_text(
            "Что изучаем сегодня? 🤍",
            reply_markup=kb.main_menu_kb(user.is_premium_active, user.free_section_used),
        )

    # ── Natal card (always free) ───────────────────────────────────────────────
    elif data == "m_natal":
        await _show_natal(query, context, user, chat_id)

    # ── Thematic sections ──────────────────────────────────────────────────────
    elif data == "m_love":
        await _show_love(query, context, user, chat_id)
    elif data == "m_money":
        await _show_theme(query, context, user, chat_id, "money")
    elif data == "m_karma":
        await _show_theme(query, context, user, chat_id, "karma")
    elif data == "m_family":
        await _show_theme(query, context, user, chat_id, "family")
    elif data == "m_years":
        await _show_theme(query, context, user, chat_id, "years")

    # ── Advanced Jyotish (premium) ─────────────────────────────────────────────
    elif data in ("m_advanced", "menu_advanced"):
        if not await _require_premium(query.message, user):
            return
        await query.message.reply_text(
            "Продвинутый Джйотиш 🪐\n\nГлубокие уровни карты. Выбери, куда заглянуть.",
            reply_markup=kb.advanced_menu_kb(),
        )
    elif data == "menu_houses_adv":
        if not await _require_premium(query.message, user):
            return
        await query.message.reply_text(
            "Разбор 12 Домов 🚪\n\nКаждый дом — отдельная сфера жизни. Выбери дом.",
            reply_markup=kb.houses_adv_kb(),
        )
    elif data == "adv_navamsha":
        await _show_section(query, context, user, chat_id, "navamsha")
    elif data == "adv_dasha":
        await _show_dasha(query, context, user, chat_id)
    elif data == "adv_nodes":
        await _show_section(query, context, user, chat_id, "karma_node")
    elif data == "adv_atmakaraka":
        await _show_section(query, context, user, chat_id, "atmakaraka")
    elif data == "adv_yoga":
        await _show_section(query, context, user, chat_id, "yoga")
    elif data == "adv_shadbala":
        await _show_section(query, context, user, chat_id, "shadbala")
    elif data == "adv_dashamsha":
        await _show_section(query, context, user, chat_id, "dashamsha")
    elif data == "adv_arudha":
        await _show_section(query, context, user, chat_id, "arudha")
    elif data == "adv_sadesati":
        await _show_section(query, context, user, chat_id, "sadesati")
    elif data == "adv_transits":
        await _show_section(query, context, user, chat_id, "transits")
    elif data.startswith("h_adv_"):
        await _show_premium_house(query, context, user, int(data.split("_")[-1]), chat_id)

    # ── Weekly Focus (premium) ─────────────────────────────────────────────────
    elif data == "m_weekly":
        await _show_weekly_focus(query, context, user, chat_id)

    # ── Special readings ───────────────────────────────────────────────────────
    elif data == "m_specials":
        await query.message.reply_text(
            "Особые разборы 🪞\n\nГлубокие разовые анализы с вводом новых данных. "
            "Оплата звёздами Telegram ⭐",
            reply_markup=kb.specials_menu_kb(),
        )

    # ── Questions ──────────────────────────────────────────────────────────────
    elif data == "m_question":
        await _handle_question_entry(query, context, user, chat_id)

    # ── Premium CTA / upsell page ─────────────────────────────────────────────
    elif data == "m_premium":
        text = (
            "⭐️ Премиум-доступ\n\n"
            f"За {STARS_PREMIUM_MONTH} ⭐️ / месяц ты получаешь:\n\n"
            "📜 Все тематические разборы без ограничений\n"
            "🔮 Продвинутый Джйотиш — 11 глубоких инструментов\n"
            "🗓 Фокус недели — твой личный вектор на 7 дней\n"
            f"✒️ {PREMIUM_MONTHLY_QUESTIONS} вопроса каждый месяц бесплатно\n\n"
            "Оплата звёздами Telegram ⭐️"
        )
        await query.message.reply_text(text, reply_markup=kb.premium_cta_kb())

    # ── Settings ───────────────────────────────────────────────────────────────
    elif data == "m_settings":
        notif = user.notifications_enabled if user.notifications_enabled is not None else True
        await query.message.reply_text("⚙️ Настройки",
                                       reply_markup=kb.settings_kb(notif))

    elif data == "set_edit_birth":
        context.user_data["edit_birth"] = {}
        context.user_data["awaiting"]   = AWAIT_EDIT_DATE
        await query.message.reply_text(
            "🔄 Изменить данные рождения\n\n"
            "Введи новую дату в формате ДД.ММ.ГГГГ\nНапример: 12.02.2000"
        )

    elif data == "set_weekly_focus":
        async with AsyncSessionLocal() as session:
            u = await get_user(session, user_tg.id)
        await query.message.reply_text(
            "⏰ Фокус недели — день недели:",
            reply_markup=kb.weekly_focus_day_kb(
                u.weekly_focus_day if u and u.weekly_focus_day is not None else 6
            ),
        )

    elif data == "set_toggle_notif":
        async with AsyncSessionLocal() as session:
            u = await get_user(session, user_tg.id)
            new_val = not (u.notifications_enabled if u.notifications_enabled is not None else True)
            await update_user(session, u, notifications_enabled=new_val)
        icon  = "🔔" if new_val else "🔕"
        state = "включены" if new_val else "выключены"
        await query.message.reply_text(
            f"{icon} Уведомления {state}.",
            reply_markup=kb.settings_kb(new_val),
        )

    elif data == "set_subscription":
        await _show_subscription_info(query, context, user, chat_id)

    elif data == "set_back":
        async with AsyncSessionLocal() as session:
            u = await get_user(session, user_tg.id)
        notif = (u.notifications_enabled if u and u.notifications_enabled is not None else True)
        await query.message.reply_text("⚙️ Настройки", reply_markup=kb.settings_kb(notif))

    elif data.startswith("wfd_"):          # weekly focus day selection
        day = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            u = await get_user(session, user_tg.id)
            cur_hour = u.weekly_focus_hour if u and u.weekly_focus_hour is not None else 19
            await update_user(session, u, weekly_focus_day=day)
        await query.message.reply_text(
            f"День выбран ✅\n\nТеперь выбери время:",
            reply_markup=kb.weekly_focus_hour_kb(cur_hour),
        )

    elif data.startswith("wfh_"):          # weekly focus hour selection
        hour = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            u = await get_user(session, user_tg.id)
            day_num = u.weekly_focus_day if u and u.weekly_focus_day is not None else 6
            await update_user(session, u, weekly_focus_hour=hour)
        day_name = kb.DAY_NAMES_RU[day_num]
        notif    = user.notifications_enabled if user.notifications_enabled is not None else True
        await query.message.reply_text(
            f"Фокус недели: каждые {day_name} в {hour:02d}:00 ✅",
            reply_markup=kb.settings_kb(notif),
        )

    # ── Edit birth city confirm ────────────────────────────────────────────────
    elif data == "edit_city_yes":
        await _apply_edit_birth(query, context, user_tg, chat_id)

    elif data == "edit_city_no":
        context.user_data.pop("pending_edit_geo", None)
        context.user_data["awaiting"] = AWAIT_EDIT_CITY
        await query.message.reply_text(
            "Хорошо, уточни название города 🗝️\n\n"
            "Напиши подробнее — например: «Иваново, Ивановская область» или «Minsk, Belarus»"
        )

    # ── Payments (Telegram Stars) ──────────────────────────────────────────────
    elif data in ("buy_premium", "show_paywall"):
        await _send_invoice(context, chat_id, "premium_month")
    elif data == "buy_q1":
        await _send_invoice(context, chat_id, "q1")
    elif data == "buy_q3":
        await _send_invoice(context, chat_id, "q3")
    elif data == "buy_q10":
        await _send_invoice(context, chat_id, "q10")
    elif data == "buy_synastry":
        await _send_invoice(context, chat_id, "special_synastry")
    elif data == "buy_child_chart":
        await _send_invoice(context, chat_id, "special_child")
    elif data == "buy_transit_year":
        await _send_invoice(context, chat_id, "special_year")

    # ── Legacy aliases (old inline buttons sitting in chat history) ────────────
    elif data in ("pm_natal_code", "pm_architecture", "m_whoami", "show_personality"):
        await _show_natal(query, context, user, chat_id)

    elif data == "show_hidden_talents":
        # Redirect to natal since the old hidden talents flow is removed
        await _show_natal(query, context, user, chat_id)

    elif data in ("m_lesson", "pm_karma_node"):
        await _show_theme(query, context, user, chat_id, "karma")

    elif data == "m_now":
        await _show_theme(query, context, user, chat_id, "years")

    elif data in ("m_purpose", "m_shadow", "m_strengths", "m_blocks", "m_success"):
        # Old theme keys — gate-free applies, but they now go through karma/family/etc.
        # For backward compat, map them to closest new theme
        _old_map = {
            "m_purpose": "karma", "m_shadow": "love",
            "m_strengths": "money", "m_blocks": "years", "m_success": "family",
        }
        await _show_theme(query, context, user, chat_id, _old_map.get(data, "karma"))

    elif data == "pm_houses":
        if not await _require_premium(query.message, user):
            return
        await query.message.reply_text("Разбор 12 Домов 🚪", reply_markup=kb.houses_adv_kb())

    elif data == "pm_dasha":
        await _show_dasha(query, context, user, chat_id)

    elif data in ("pm_daily_question",):
        await _handle_question_entry(query, context, user, chat_id)

    elif data in ("pm_settings", "m_settings_legacy"):
        notif = user.notifications_enabled if user.notifications_enabled is not None else True
        await query.message.reply_text("⚙️ Настройки", reply_markup=kb.settings_kb(notif))

    elif data == "pm_upsells":
        await query.message.reply_text("Особые разборы 🪞", reply_markup=kb.specials_menu_kb())

    elif data in ("pm_navamsha", "pm_atmakaraka", "pm_yoga", "pm_sadesati", "pm_upaya"):
        key = data.replace("pm_", "")
        if key in SECTION_CONFIG:
            await _show_section(query, context, user, chat_id, key)
        else:
            await _show_section(query, context, user, chat_id, "karma_node")

    elif data == "pm_karma_node":
        await _show_section(query, context, user, chat_id, "karma_node")

    elif data == "pm_resources":
        await _show_section(query, context, user, chat_id, "resources")

    elif data == "pm_muhurta":
        if not await _require_premium(query.message, user):
            return
        context.user_data["awaiting"] = AWAIT_MUHURTA
        await query.message.reply_text(
            "Время циклично 🤍\n\nНапиши событие и месяц.\nНапример: «Свадьба в августе»"
        )

    elif data in ("m_daily", "set_toggle_daily", "set_change_time"):
        # Old daily reading flow — redirect to weekly focus or premium page
        if user.is_premium_active:
            await _show_weekly_focus(query, context, user, chat_id)
        else:
            await query.message.reply_text(
                "Разбор дня заменён на Фокус недели 🗓\n\nПерсональный вектор на 7 дней "
                "доступен в Премиуме.",
                reply_markup=kb.premium_cta_kb(),
            )

    elif data in ("show_houses_menu",) or \
         data.startswith("house_free_") or data.startswith("house_prem_"):
        # Old free-houses funnel
        await query.message.reply_text(
            "Меню обновилось 🪐 Выбери раздел.",
            reply_markup=kb.main_menu_kb(user.is_premium_active, user.free_section_used),
        )


# ── Message handler + sub-flows ───────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    user_tg  = update.effective_user
    chat_id  = update.effective_chat.id

    if awaiting == AWAIT_MUHURTA:
        await _process_muhurta(update, context, user_tg, chat_id)
    elif awaiting == AWAIT_QUESTION:
        await _process_question(update, context, user_tg, chat_id)
    elif awaiting == AWAIT_EDIT_DATE:
        await _recv_edit_date(update, context)
    elif awaiting == AWAIT_EDIT_TIME:
        await _recv_edit_time(update, context)
    elif awaiting == AWAIT_EDIT_CITY:
        await _recv_edit_city(update, context)
    elif awaiting == AWAIT_SYN_NAME:
        await _syn_collect(update, context, "name", AWAIT_SYN_DATE,
                           "Дата рождения партнёра? Формат ДД.ММ.ГГГГ")
    elif awaiting == AWAIT_SYN_DATE:
        await _syn_collect(update, context, "date", AWAIT_SYN_TIME,
                           "Время рождения? Формат ЧЧ:ММ (примерное тоже подойдёт)")
    elif awaiting == AWAIT_SYN_TIME:
        await _syn_collect(update, context, "time", AWAIT_SYN_CITY,
                           "Город рождения партнёра?")
    elif awaiting == AWAIT_SYN_CITY:
        await _process_synastry(update, context, user_tg, chat_id)
    elif awaiting == AWAIT_CHILD_NAME:
        await _child_collect(update, context, "name", AWAIT_CHILD_DATE,
                             "Дата рождения ребёнка? Формат ДД.ММ.ГГГГ")
    elif awaiting == AWAIT_CHILD_DATE:
        await _child_collect(update, context, "date", AWAIT_CHILD_TIME,
                             "Время рождения? (примерное тоже подойдёт)")
    elif awaiting == AWAIT_CHILD_TIME:
        await _child_collect(update, context, "time", AWAIT_CHILD_CITY,
                             "Город рождения ребёнка?")
    elif awaiting == AWAIT_CHILD_CITY:
        await _process_child_chart(update, context, user_tg, chat_id)
    else:
        await update.message.reply_text(
            "Используй меню для навигации 🤍 Или /start, чтобы начать сначала."
        )


# ── Synastry sub-flow ─────────────────────────────────────────────────────────
async def _syn_collect(update, context, field: str, next_await: str, prompt: str):
    context.user_data["syn"][field] = update.message.text.strip()
    context.user_data["awaiting"]   = next_await
    await update.message.reply_text(prompt)


async def _process_synastry(update, context, user_tg, chat_id):
    syn = context.user_data.pop("syn", {})
    syn["city"] = update.message.text.strip()
    context.user_data.pop("awaiting", None)

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    astro = await _get_astro(context, user)
    if not astro:
        await update.message.reply_text("Не могу загрузить твою карту 🕯")
        return

    await update.message.reply_text("Строю карту совместимости... 🪞")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    partner_astro_text = ""
    try:
        geo = await geocode_city(syn.get("city", ""))
        if geo:
            partner_astro_text = await collect_partner_text(
                syn.get("date", "01.01.2000"), syn.get("time", "12:00"), geo
            )
    except Exception as exc:
        logger.warning("Synastry partner chart failed: %s", exc)
        partner_astro_text = (f"Партнёр: {syn.get('name','')}, "
                              f"дата {syn.get('date','')}, время {syn.get('time','')}, "
                              f"город {syn.get('city','')}")

    try:
        async with _typing_action(context, chat_id):
            text = await ai.gen_synastry(
                astro["text"], partner_astro_text,
                user.name or "", syn.get("name", "партнёр"),
                user.gender or "female",
            )
    except Exception as exc:
        logger.error("gen_synastry: %s", exc)
        await update.message.reply_text("Произошла ошибка 🕯 Попробуй ещё раз.")
        return

    await _safe_reply(
        update.message,
        f"Зеркало отношений 🪞\n{user.name or ''} + {syn.get('name','')}\n\n{text}",
        reply_markup=kb.back_main_kb(),
    )


# ── Child chart sub-flow ──────────────────────────────────────────────────────
async def _child_collect(update, context, field: str, next_await: str, prompt: str):
    context.user_data["child"][field] = update.message.text.strip()
    context.user_data["awaiting"]     = next_await
    await update.message.reply_text(prompt)


async def _process_child_chart(update, context, user_tg, chat_id):
    child = context.user_data.pop("child", {})
    child["city"] = update.message.text.strip()
    context.user_data.pop("awaiting", None)

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)

    await update.message.reply_text("Строю карту ребёнка... 🌿")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    child_astro_text = ""
    try:
        geo = await geocode_city(child.get("city", ""))
        if geo:
            child_astro_text = await collect_partner_text(
                child.get("date", "01.01.2010"), child.get("time", "12:00"), geo
            )
    except Exception as exc:
        logger.warning("Child chart failed: %s", exc)
        child_astro_text = (f"Ребёнок: {child.get('name','')}, "
                            f"дата {child.get('date','')}, время {child.get('time','')}, "
                            f"город {child.get('city','')}")

    try:
        async with _typing_action(context, chat_id):
            text = await ai.gen_child_chart(
                child_astro_text, child.get("name", ""), user.gender or "female"
            )
    except Exception as exc:
        logger.error("gen_child_chart: %s", exc)
        await update.message.reply_text("Произошла ошибка 🕯 Попробуй ещё раз.")
        return

    await _safe_reply(
        update.message,
        f"Росток судьбы 🌿\nКарта {child.get('name', 'ребёнка')}\n\n{text}",
        reply_markup=kb.back_main_kb(),
    )


# ── Muhurta sub-flow ──────────────────────────────────────────────────────────
async def _process_muhurta(update, context, user_tg, chat_id):
    text_input = update.message.text.strip()
    context.user_data.pop("awaiting", None)

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    if not user or not user.is_premium_active:
        await update.message.reply_text("Эта функция доступна в Премиуме ⭐️",
                                        reply_markup=kb.premium_cta_kb())
        return

    astro = await _get_astro(context, user)
    if not astro:
        await update.message.reply_text("Не могу загрузить карту 🕯")
        return

    parts  = text_input.rsplit(" в ", 1) if " в " in text_input else [text_input]
    event  = parts[0].strip()
    period = parts[1].strip() if len(parts) > 1 else "ближайшие месяцы"

    try:
        async with _typing_action(context, chat_id):
            result = await ai.gen_muhurta(event, period, astro["text"],
                                          user.name or "", user.gender or "female")
    except Exception as exc:
        logger.error("gen_muhurta: %s", exc)
        await update.message.reply_text("Произошла ошибка 🕯")
        return

    await _safe_reply(update.message, f"Навигатор времени ⏳\n\n{result}",
                      reply_markup=kb.back_main_kb())


# ── Commands ──────────────────────────────────────────────────────────────────
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_tg = update.effective_user
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, user_tg.id, user_tg.username)
    if not user.onboarding_complete:
        await update.message.reply_text("Сначала давай познакомимся 🤍 Напиши /start")
        return
    await update.message.reply_text(
        "Что изучаем сегодня? 🤍",
        reply_markup=kb.main_menu_kb(user.is_premium_active, user.free_section_used),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤍 Раши — ведический астролог\n\n/start — начать\n/menu — главное меню\n/help — помощь"
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, func
        from database import User
        total    = (await session.execute(select(func.count()).select_from(User))).scalar()
        premium  = (await session.execute(
            select(func.count()).select_from(User).where(User.is_premium == True))).scalar()
        complete = (await session.execute(
            select(func.count()).select_from(User).where(User.onboarding_complete == True))).scalar()
        free_used = (await session.execute(
            select(func.count()).select_from(User).where(User.free_section_used != None))).scalar()
    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"Всего пользователей: {total}\n"
        f"Завершили онбординг: {complete}\n"
        f"Активный Премиум: {premium}\n"
        f"Использовали фри-клик: {free_used}\n\n"
        f"Конверсия онб→прем: {round(premium/complete*100,1) if complete else 0}%"
    )


# ── Error handling ────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while handling update", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Что-то пошло не так 🕯 Попробуй ещё раз или напиши /start"
            )
    except Exception:
        pass


# ── Section navigation chain ─────────────────────────────────────────────────
# Порядок чтения разделов. После каждого — кнопка к следующему.
SECTION_CHAIN: list[tuple[str, str]] = [
    ("natal",  "📜 Моя натальная карта"),
    ("love",   "🤍 Любовь и отношения"),
    ("money",  "🪙 Деньги и карьера"),
    ("family", "🌿 Семья и дети"),
    ("karma",  "🗝 Карма и уроки"),
    ("years",  "⏳ Самые важные годы"),
]


def _next_section_button(current_key: str) -> InlineKeyboardButton | None:
    """Кнопка к следующему разделу в цепочке, или None если текущий — последний."""
    for i, (key, _) in enumerate(SECTION_CHAIN):
        if key == current_key and i + 1 < len(SECTION_CHAIN):
            next_key, next_label = SECTION_CHAIN[i + 1]
            return InlineKeyboardButton(
                f"Дальше: {next_label} →", callback_data=f"nav_{next_key}"
            )
    return None


# ── Natal chart: pagination (3 screens) + regen button (one-shot) ────────────

def _split_natal_pages(text: str) -> list[str]:
    """
    Разбить текст натальной карты на 3 экрана по заголовкам секций:
      Стр 1: Шапка 🔮 + Главная тема 🌟
      Стр 2: 📊 Планеты (таблица)
      Стр 3: 🌙 Луна — ключ судьбы
    Если маркеры не найдены — вернуть весь текст одной страницей.
    """
    if not text:
        return []
    PLANETS_MARK = "📊"
    MOON_MARK    = "🌙"
    p_pos = text.find(PLANETS_MARK)
    m_pos = text.find(MOON_MARK)
    if p_pos < 0 or m_pos < 0 or m_pos < p_pos:
        return [text.strip()]
    return [
        text[:p_pos].rstrip(),
        text[p_pos:m_pos].rstrip(),
        text[m_pos:].rstrip(),
    ]


def _natal_page_markup(page_idx: int, total: int, regen_used: bool) -> InlineKeyboardMarkup:
    """Клавиатура под страницей натальной карты."""
    rows: list[list[InlineKeyboardButton]] = []
    if page_idx < total - 1:
        rows.append([InlineKeyboardButton(
            "Продолжить →", callback_data=f"natal_page_{page_idx + 1}"
        )])
    else:
        # Последняя страница: regen (если ещё не использовалась) + следующий раздел + меню
        if not regen_used:
            rows.append([InlineKeyboardButton(
                "🔄 Обновить разбор", callback_data="natal_regen"
            )])
        next_btn = _next_section_button("natal")
        if next_btn:
            rows.append([next_btn])
        rows.append([InlineKeyboardButton(
            "↩️ В главное меню", callback_data="menu_main"
        )])
    return InlineKeyboardMarkup(rows)


async def _handle_natal_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Хендлер кнопок «Продолжить →» — показывает следующую страницу разбора."""
    query = update.callback_query
    await query.answer()

    try:
        page_idx = int(query.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return

    user_tg = update.effective_user
    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    if not user:
        return

    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту 🕯")
        return

    pages = astro.get("personality_pages") or []
    if not pages or page_idx < 0 or page_idx >= len(pages):
        return

    regen_used = bool(astro.get("regen_used", False))
    await _safe_reply(
        query.message,
        pages[page_idx],
        reply_markup=_natal_page_markup(page_idx, len(pages), regen_used),
    )


# ── Love & Relationships: pagination (2 screens) ─────────────────────────────

def _split_love_pages(text: str) -> list[str]:
    """
    Разбить текст «Любовь и отношения» на 2 страницы по подзаголовку
    «Кармический урок». Если подзаголовок не найден — одна страница.
    """
    if not text:
        return []
    pos = text.find("Кармический урок")
    if pos < 0:
        return [text.strip()]
    return [text[:pos].rstrip(), text[pos:].lstrip()]


def _love_page_markup(page_idx: int, total: int) -> InlineKeyboardMarkup:
    """Клавиатура под страницей раздела «Любовь и отношения»."""
    rows: list[list[InlineKeyboardButton]] = []
    if page_idx < total - 1:
        rows.append([InlineKeyboardButton(
            "Продолжить →", callback_data=f"love_page_{page_idx + 1}"
        )])
    else:
        next_btn = _next_section_button("love")
        if next_btn:
            rows.append([next_btn])
        rows.append([InlineKeyboardButton(
            "↩️ В главное меню", callback_data="menu_main"
        )])
    return InlineKeyboardMarkup(rows)


async def _show_love(query, context, user, chat_id):
    """Раздел «Любовь и отношения» с пагинацией и кэшем."""
    if not await _gate_free(query.message, user, "love"):
        return
    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту. Напиши /start 🕯")
        return

    pages = astro.get("love_pages")
    if not pages:
        try:
            async with _typing_action(context, chat_id):
                full_text = await ai.gen_love(
                    astro["text"], user.name or "", user.gender or "female"
                )
            pages = _split_love_pages(full_text)
            astro["love_text"]  = full_text
            astro["love_pages"] = pages
            async with AsyncSessionLocal() as session:
                u = await get_user(session, user.telegram_id)
                if u:
                    await update_user(session, u,
                                      astro_data_json=json.dumps(astro, ensure_ascii=False))
            context.user_data["astro_cache"] = astro
        except Exception as exc:
            logger.error("gen_love: %s", exc)
            await query.message.reply_text("Произошла ошибка. Попробуй ещё раз 🕯")
            return

    if not pages:
        await query.message.reply_text("Не удалось сформировать разбор 🕯")
        return

    await _safe_reply(
        query.message,
        pages[0],
        reply_markup=_love_page_markup(0, len(pages)),
    )


async def _handle_love_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Хендлер кнопок «Продолжить →» в разделе «Любовь и отношения»."""
    query = update.callback_query
    await query.answer()

    try:
        page_idx = int(query.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return

    user_tg = update.effective_user
    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    if not user:
        return

    astro = await _get_astro(context, user)
    if not astro:
        await query.message.reply_text("Не могу загрузить карту 🕯")
        return

    pages = astro.get("love_pages") or []
    if not pages or page_idx < 0 or page_idx >= len(pages):
        return

    await _safe_reply(
        query.message,
        pages[page_idx],
        reply_markup=_love_page_markup(page_idx, len(pages)),
    )


# ── Generic section navigation router ────────────────────────────────────────

async def _handle_section_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Маршрутизатор кнопок «Дальше: ... →» (callback_data `nav_X`).
    Запускает соответствующий раздел через фримиум-логику.
    """
    query   = update.callback_query
    user_tg = update.effective_user
    chat_id = update.effective_chat.id
    await query.answer()

    section = query.data[4:] if query.data.startswith("nav_") else ""
    if not section:
        return

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    if not user:
        return

    if section == "natal":
        await _show_natal(query, context, user, chat_id)
    elif section == "love":
        await _show_love(query, context, user, chat_id)
    else:
        # money, karma, family, years — обычный _show_theme
        await _show_theme(query, context, user, chat_id, section)


async def _handle_natal_regen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Пересчитать натальную карту через VedAstro + сгенерировать новый разбор.
    Работает ровно один раз: после использования кнопка исчезает.
    """
    query   = update.callback_query
    user_tg = update.effective_user
    chat_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        user = await get_user(session, user_tg.id)
    if not user or not user.onboarding_complete:
        await query.answer("Сначала заверши регистрацию 🕯", show_alert=True)
        return

    astro = await _get_astro(context, user)
    if astro and astro.get("regen_used"):
        await query.answer("Обновление уже использовано 🌑", show_alert=True)
        return

    await query.answer()
    await query.message.reply_text("Пересчитываю карту... 🪐")

    geo = {
        "lat":        user.birth_lat or 0.0,
        "lon":        user.birth_lon or 0.0,
        "timezone":   user.timezone or "UTC",
        "short_name": user.birth_city or "",
    }
    try:
        astro_fresh = await collect_and_format_chart(
            user.birth_date, user.birth_time, geo
        )
    except Exception as exc:
        logger.error("natal_regen collect_chart: %s", exc)
        await query.message.reply_text("Не удалось пересчитать карту 🕯")
        return

    try:
        async with _typing_action(context, chat_id):
            personality = await ai.gen_natal_chart(
                astro_fresh["text"],
                user.name or "",
                user.gender or "female",
                birth_info=_build_birth_info(user, astro_fresh),
            )
    except Exception as exc:
        logger.error("natal_regen gen_natal_chart: %s", exc)
        await query.message.reply_text("Ошибка генерации 🕯")
        return

    pages = _split_natal_pages(personality)
    astro_fresh["personality"]       = personality
    astro_fresh["personality_pages"] = pages
    astro_fresh["regen_used"]        = True

    async with AsyncSessionLocal() as session:
        u = await get_user(session, user_tg.id)
        if u:
            await update_user(session, u,
                              astro_data_json=json.dumps(astro_fresh, ensure_ascii=False))
    context.user_data["astro_cache"] = astro_fresh

    if not pages:
        await query.message.reply_text("Не удалось сформировать разбор 🕯")
        return

    await _safe_reply(
        query.message,
        pages[0],
        reply_markup=_natal_page_markup(0, len(pages), regen_used=True),
    )


# ── Registration ──────────────────────────────────────────────────────────────
def register_handlers(application: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAIT_BEGIN:  [CallbackQueryHandler(begin_onboarding, pattern="^onboarding_begin$")],
            ASK_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_name)],
            ASK_GENDER:   [CallbackQueryHandler(recv_gender, pattern="^gender_")],
            ASK_DATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_date)],
            ASK_TIME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_time)],
            ASK_CITY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_city)],
            CONFIRM_CITY: [
                CallbackQueryHandler(confirm_city_yes, pattern="^city_yes$"),
                CallbackQueryHandler(confirm_city_no,  pattern="^city_no$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    application.add_handler(CallbackQueryHandler(_handle_natal_regen, pattern="^natal_regen$"))
    application.add_handler(CallbackQueryHandler(_handle_natal_page,  pattern=r"^natal_page_\d+$"))
    application.add_handler(CallbackQueryHandler(_handle_love_page,   pattern=r"^love_page_\d+$"))
    application.add_handler(CallbackQueryHandler(_handle_section_nav, pattern=r"^nav_\w+$"))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

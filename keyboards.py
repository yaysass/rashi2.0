"""
keyboards/keyboards.py
======================
Все inline-клавиатуры бота.

Правила:
  - Подписи только из texts.TEXTS — никаких строк в коде.
  - Логика замков 🔒 только через core.access.is_section_locked.
  - Все функции чистые: принимают данные, возвращают InlineKeyboardMarkup.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.access import is_section_locked
from db.models import User
from texts import TEXTS


# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательный builder
# ──────────────────────────────────────────────────────────────────────────────

def _btn(label: str, cb: str, locked: bool = False) -> InlineKeyboardButton:
    """Кнопка с опциональным замком 🔒 в конце подписи."""
    suffix = TEXTS["menu"]["locked_suffix"] if locked else ""
    return InlineKeyboardButton(label + suffix, callback_data=cb)


# ──────────────────────────────────────────────────────────────────────────────
#  Главное меню
# ──────────────────────────────────────────────────────────────────────────────

def main_menu_kb(user: User) -> InlineKeyboardMarkup:
    """
    Клавиатура главного меню с актуальными замками для конкретного пользователя.
    Кнопка «Премиум-доступ» скрыта у активных подписчиков.
    """
    t   = TEXTS["menu"]
    pt  = TEXTS["premium"]
    lok = is_section_locked  # сокращение

    rows = [
        # ── Натальная карта (всегда бесплатно) ──────────────────────────────
        [_btn(t["btn_natal"], "natal:main")],

        # ── Freemium (2 в ряд) ───────────────────────────────────────────────
        [
            _btn(t["btn_love"],  "section:love",  locked=lok(user, "love")),
            _btn(t["btn_money"], "section:money", locked=lok(user, "money")),
        ],
        [
            _btn(t["btn_karma"],  "section:karma",  locked=lok(user, "karma")),
            _btn(t["btn_family"], "section:family", locked=lok(user, "family")),
        ],
        [_btn(t["btn_years"], "section:years", locked=lok(user, "years"))],

        # ── Только Premium ───────────────────────────────────────────────────
        [_btn(pt["adv_title"], "adv:menu",       locked=lok(user, "navamsha"))],
        [_btn(pt["weekly_title"], "section:weekly", locked=lok(user, "weekly"))],

        # ── Разовые покупки / всегда доступны ────────────────────────────────
        [_btn(t["btn_specials"], "menu:specials")],
        [_btn(t["btn_question"], "menu:question")],
    ]

    # Кнопка подписки скрыта у активных премиумов
    if not user.is_premium_active:
        rows.append([_btn(t["btn_premium"], "menu:premium")])

    rows.append([_btn(t["btn_settings"], "menu:settings")])

    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Пейволл
# ──────────────────────────────────────────────────────────────────────────────

def paywall_kb(premium_only: bool = False) -> InlineKeyboardMarkup:
    """
    Кнопки под сообщением пейволла.
    premium_only=True — раздел только для подписки (без фразы «1 клик потрачен»).
    """
    pw = TEXTS["paywall"]
    cm = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pw["cta_btn"],         callback_data="menu:premium")],
        [InlineKeyboardButton(cm["back_to_menu"],    callback_data="menu:main")],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Действия под разбором (Перегенерировать + Назад)
# ──────────────────────────────────────────────────────────────────────────────

def reading_actions_kb(section_key: str, *, show_regen: bool = True) -> InlineKeyboardMarkup:
    """
    Кнопки под каждым сгенерированным разбором.
    show_regen=False — не показывать перегенерацию (когда доступ уже исчерпан).
    """
    cm = TEXTS["common"]
    row_back = [InlineKeyboardButton(cm["back_to_menu"], callback_data="menu:main")]

    if not show_regen:
        return InlineKeyboardMarkup([row_back])

    row_regen = [
        InlineKeyboardButton(cm["regenerate"], callback_data=f"regen:{section_key}")
    ]
    return InlineKeyboardMarkup([row_regen, row_back])


# ──────────────────────────────────────────────────────────────────────────────
#  Натальная карта
# ──────────────────────────────────────────────────────────────────────────────

def natal_card_kb() -> InlineKeyboardMarkup:
    """Кнопки под натальной картой (personal reading)."""
    t  = TEXTS["menu"]
    cm = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t["btn_natal_code"], callback_data="natal:code")],
        [InlineKeyboardButton(cm["regenerate"],    callback_data="regen:personality")],
        [InlineKeyboardButton(cm["back_to_menu"],  callback_data="menu:main")],
    ])


def natal_code_kb() -> InlineKeyboardMarkup:
    """Кнопки под натальным кодом (raw data, без AI)."""
    cm = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(cm["back_to_menu"], callback_data="menu:main")],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Продвинутый Джйотиш
# ──────────────────────────────────────────────────────────────────────────────

def adv_jyotish_kb() -> InlineKeyboardMarkup:
    """Подменю «Продвинутый Джйотиш» (13 инструментов + дома + назад)."""
    p  = TEXTS["premium"]
    cm = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(p["btn_houses"],    callback_data="adv:houses")],
        [
            InlineKeyboardButton(p["btn_navamsha"],  callback_data="section:navamsha"),
            InlineKeyboardButton(p["btn_dasha"],     callback_data="section:dasha_adv"),
        ],
        [
            InlineKeyboardButton(p["btn_nodes"],     callback_data="section:nodes"),
            InlineKeyboardButton(p["btn_atma"],      callback_data="section:atmakaraka"),
        ],
        [
            InlineKeyboardButton(p["btn_yoga"],      callback_data="section:yoga"),
            InlineKeyboardButton(p["btn_shadbala"],  callback_data="section:shadbala"),
        ],
        [
            InlineKeyboardButton(p["btn_dashamsha"], callback_data="section:dashamsha"),
            InlineKeyboardButton(p["btn_arudha"],    callback_data="section:arudha"),
        ],
        [
            InlineKeyboardButton(p["btn_sadesati"],  callback_data="section:sadesati"),
            InlineKeyboardButton(p["btn_transits"],  callback_data="section:transits"),
        ],
        [
            InlineKeyboardButton(p["btn_muhurta"],   callback_data="section:muhurta"),
            InlineKeyboardButton(p["btn_upaya"],     callback_data="section:upaya"),
        ],
        [InlineKeyboardButton(cm["back_to_menu"],    callback_data="menu:main")],
    ])


def houses_kb() -> InlineKeyboardMarkup:
    """Подменю «Разбор 12 домов»."""
    p  = TEXTS["premium"]
    cm = TEXTS["common"]
    rows = []
    # По 3 кнопки в ряд: 1-2-3, 4-5-6, 7-8-9, 10-11-12
    for start in range(1, 13, 3):
        row = [
            InlineKeyboardButton(
                p["btn_house"].format(n=n),
                callback_data=f"house:{n}",
            )
            for n in range(start, min(start + 3, 13))
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton(cm["back"], callback_data="adv:menu")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Особые разборы
# ──────────────────────────────────────────────────────────────────────────────

def specials_kb() -> InlineKeyboardMarkup:
    s  = TEXTS["specials"]
    cm = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["btn_synastry"], callback_data="special:synastry")],
        [InlineKeyboardButton(s["btn_child"],    callback_data="special:child")],
        [InlineKeyboardButton(s["btn_year"],     callback_data="special:year")],
        [InlineKeyboardButton(cm["back_to_menu"], callback_data="menu:main")],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Магазин вопросов
# ──────────────────────────────────────────────────────────────────────────────

def questions_shop_kb() -> InlineKeyboardMarkup:
    s  = TEXTS["shop"]
    cm = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["btn_q1"],  callback_data="buy:q1")],
        [InlineKeyboardButton(s["btn_q3"],  callback_data="buy:q3")],
        [InlineKeyboardButton(s["btn_q10"], callback_data="buy:q10")],
        [InlineKeyboardButton(cm["back_to_menu"], callback_data="menu:main")],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Настройки
# ──────────────────────────────────────────────────────────────────────────────

def settings_kb() -> InlineKeyboardMarkup:
    s  = TEXTS["settings"]
    cm = TEXTS["common"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["btn_edit_birth"],    callback_data="settings:edit_birth")],
        [InlineKeyboardButton(s["btn_weekly_time"],   callback_data="settings:weekly_time")],
        [InlineKeyboardButton(s["btn_notifications"], callback_data="settings:notifications")],
        [InlineKeyboardButton(s["btn_subscription"],  callback_data="settings:subscription")],
        [InlineKeyboardButton(cm["back_to_menu"],     callback_data="menu:main")],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Универсальная кнопка «Назад в меню»
# ──────────────────────────────────────────────────────────────────────────────

def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS["common"]["back_to_menu"], callback_data="menu:main")
    ]])

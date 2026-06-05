"""
keyboards/keyboards.py
======================
Единственное место для сборки всех inline-клавиатур бота.

Принципы:
  • Все пользовательские строки — только из texts.py (через TEXTS).
    Фоллбэки для ещё не заполненных ключей — в catalog.resolve_title().
  • Кнопки НЕ скрываются при блокировке — добавляется замок 🔒
    (визуальный маркер, клик ведёт на пейволл).
  • Кнопка «Премиум-доступ» скрыта у активных подписчиков.
  • Кнопка «Перегенерировать 🔄» показывается только при наличии доступа.
  • Ни одного if/elif-каскада по ключам — всё через данные из catalog.py.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.access import can_open_premium, can_open_theme, freemium_lock_needed
from core.catalog import READINGS, ReadingEntry, resolve_title
from texts import TEXTS

if TYPE_CHECKING:
    from db.models import User


# ──────────────────────────────────────────────────────────────────────────────
#  Внутренние хелперы
# ──────────────────────────────────────────────────────────────────────────────

def _btn(label: str, callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=callback)


def _lock(label: str) -> str:
    """Добавляет суффикс 🔒 к тексту кнопки."""
    return label + TEXTS["menu"]["locked_suffix"]


# ──────────────────────────────────────────────────────────────────────────────
#  Главное меню
# ──────────────────────────────────────────────────────────────────────────────

def main_menu(user: "User") -> InlineKeyboardMarkup:
    """
    Динамическая клавиатура главного меню.

    Логика замков (🔒):
      - freemium-раздел: замок, если бесплатный клик потрачен на ДРУГУЮ тему
        и пользователь не премиум.
      - premium-раздел: замок, если нет активного премиума.

    Кнопка «Премиум-доступ» скрыта у активных подписчиков.
    """
    t    = TEXTS["menu"]
    tc   = TEXTS["common"]
    prem = user.is_premium_active

    # ── Freemium: добавить замок если нужно ────────────────────────────────
    def freemium_btn(key: str, text_key: str) -> InlineKeyboardButton:
        label = t[text_key]
        if freemium_lock_needed(user, key):
            label = _lock(label)
        return _btn(label, f"section:{key}")

    # ── Premium-раздел: замок если нет подписки ────────────────────────────
    def premium_btn(text_key: str, callback: str) -> InlineKeyboardButton:
        label = t[text_key]
        if not prem:
            label = _lock(label)
        return _btn(label, callback)

    rows = [
        # ── Натальная карта — всегда бесплатно ──────────────────────────────
        [_btn(t["btn_natal"],     "natal:main")],
        [_btn(t["btn_natal_code"],"natal:code")],

        # ── Freemium-темы: по 2 в ряд ───────────────────────────────────────
        [
            freemium_btn("love",  "btn_love"),
            freemium_btn("money", "btn_money"),
        ],
        [
            freemium_btn("karma",  "btn_karma"),
            freemium_btn("family", "btn_family"),
        ],
        # ── Важные годы — на всю ширину (длинная подпись) ───────────────────
        [freemium_btn("years", "btn_years")],

        # ── Premium-разделы ──────────────────────────────────────────────────
        [premium_btn("btn_adv",    "adv:menu")],
        [premium_btn("btn_weekly", "section:weekly")],

        # ── Особые разборы — всегда видны (оплата за каждый разбор) ─────────
        [_btn(t["btn_specials"], "menu:specials")],

        # ── Вопросы и настройки ─────────────────────────────────────────────
        [_btn(t["btn_question"], "menu:question")],
        [_btn(t["btn_settings"], "menu:settings")],
    ]

    # Кнопка «Премиум-доступ» скрыта у активных подписчиков
    if not prem:
        rows.append([_btn(t["btn_premium"], "menu:premium")])

    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Кнопки под готовым разбором
# ──────────────────────────────────────────────────────────────────────────────

def reading_actions(
    reading_key: str,
    user: "User",
) -> InlineKeyboardMarkup:
    """
    Кнопки после сгенерированного разбора.

    «Перегенерировать 🔄» — показывается только если у пользователя
    есть право на повторный запрос (тот же раздел не тратит второй клик):
      • free/premium разбор → всегда показывается.
      • freemium → показывается если can_open_theme() вернёт True
        (это True для своей темы и для премиума).

    «В главное меню» — всегда.
    """
    tc    = TEXTS["common"]
    entry = READINGS.get(reading_key)

    rows: list[list[InlineKeyboardButton]] = []

    # Определяем, показывать ли кнопку регенерации
    can_regen = False
    if entry is not None:
        if entry.access == "free":
            can_regen = True
        elif entry.access == "freemium":
            # can_open_theme учитывает: та же тема → True (повторный просмотр бесплатен)
            can_regen = can_open_theme(user, reading_key)
        elif entry.access == "premium":
            can_regen = can_open_premium(user)

    if can_regen:
        rows.append([_btn(tc["regenerate"], f"regen:{reading_key}")])

    rows.append([_btn(tc["back_to_menu"], "menu:main")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Пейволл
# ──────────────────────────────────────────────────────────────────────────────

def paywall_keyboard() -> InlineKeyboardMarkup:
    """
    Пейволл для freemium-разделов.
    Показывается когда бесплатный клик уже потрачен на другую тему.
    """
    t_pw = TEXTS["paywall"]
    tc   = TEXTS["common"]
    return InlineKeyboardMarkup([
        [_btn(t_pw["cta_btn"],    "menu:premium")],
        [_btn(tc["back_to_menu"], "menu:main")],
    ])


def premium_only_keyboard() -> InlineKeyboardMarkup:
    """
    Пейволл для premium-разделов (Продвинутый Джйотиш, Фокус недели).
    """
    t_pw = TEXTS["paywall"]
    tc   = TEXTS["common"]
    return InlineKeyboardMarkup([
        [_btn(t_pw["cta_btn"],    "menu:premium")],
        [_btn(tc["back_to_menu"], "menu:main")],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Продвинутый Джйотиш — подменю
# ──────────────────────────────────────────────────────────────────────────────

# Порядок кнопок и их раскладка (пары — по 2 в ряд).
# Длинные заголовки — на всю ширину (пустой второй элемент → одиночная кнопка).
_ADV_LAYOUT: list[tuple[str, str] | tuple[str, str, str, str]] = [
    ("navamsha",  "dashamsha"),   # 2 в ряд
    ("dasha_adv", "transits"),    # 2 в ряд
    ("nodes",     "atmakaraka"),  # 2 в ряд
    ("yoga",      "shadbala"),    # 2 в ряд
    ("arudha",    "sadesati"),    # 2 в ряд
    ("upaya",),                   # на всю ширину
    ("muhurta",),                 # на всю ширину
]


def advanced_menu() -> InlineKeyboardMarkup:
    """
    Подменю «Продвинутый Джйотиш».

    Кнопки строятся из catalog.READINGS: заголовки берутся через
    resolve_title() с фоллбэком, поэтому не нужен «adv»-раздел в texts.py.
    Если секция будет добавлена — автоматически подтянутся правильные строки.
    """
    tc   = TEXTS["common"]
    rows = []

    for group in _ADV_LAYOUT:
        if len(group) == 2:
            k1, k2 = group
            e1 = READINGS.get(k1)
            e2 = READINGS.get(k2)
            row = []
            if e1:
                row.append(_btn(resolve_title(e1, TEXTS), f"section:{k1}"))
            if e2:
                row.append(_btn(resolve_title(e2, TEXTS), f"section:{k2}"))
            if row:
                rows.append(row)
        else:
            k1 = group[0]
            e1 = READINGS.get(k1)
            if e1:
                rows.append([_btn(resolve_title(e1, TEXTS), f"section:{k1}")])

    rows.append([_btn(tc["back_to_menu"], "menu:main")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Прочие клавиатуры
# ──────────────────────────────────────────────────────────────────────────────

def back_to_menu() -> InlineKeyboardMarkup:
    """Одна кнопка «В главное меню» — используется для простых экранов."""
    return InlineKeyboardMarkup([
        [_btn(TEXTS["common"]["back_to_menu"], "menu:main")]
    ])


def natal_code_back() -> InlineKeyboardMarkup:
    """После Натального кода — только кнопка «Назад»."""
    return InlineKeyboardMarkup([
        [_btn(TEXTS["common"]["back_to_menu"], "menu:main")]
    ])

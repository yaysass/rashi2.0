"""
core/catalog.py
===============
Реестр всех разборов (READINGS).
Один хендлер читает этот каталог вместо 30+ if/elif.

access values:
  "free"     — открыт всегда
  "freemium" — 1 бесплатный клик, потом пейволл
  "premium"  — только подписка
"""
from __future__ import annotations

from typing import NamedTuple


class ReadingConfig(NamedTuple):
    title_key:  str           # ключ в TEXTS для заголовка кнопки
    access:     str           # "free" | "freemium" | "premium"
    max_tokens: int = 1500    # лимит AI-ответа


# ──────────────────────────────────────────────────────────────────────────────
#  Главный реестр
# ──────────────────────────────────────────────────────────────────────────────
READINGS: dict[str, ReadingConfig] = {

    # ── Базовые (бесплатно) ───────────────────────────────────────────────────
    "personality": ReadingConfig(
        title_key  = "natal.header",
        access     = "free",
        max_tokens = 1600,
    ),

    # ── Freemium (1 бесплатный клик, потом подписка) ─────────────────────────
    "love": ReadingConfig(
        title_key  = "menu.btn_love",
        access     = "freemium",
    ),
    "money": ReadingConfig(
        title_key  = "menu.btn_money",
        access     = "freemium",
    ),
    "karma": ReadingConfig(
        title_key  = "menu.btn_karma",
        access     = "freemium",
    ),
    "family": ReadingConfig(
        title_key  = "menu.btn_family",
        access     = "freemium",
    ),
    "years": ReadingConfig(
        title_key  = "menu.btn_years",
        access     = "freemium",
    ),

    # ── Только подписка ───────────────────────────────────────────────────────
    "weekly": ReadingConfig(
        title_key  = "premium.weekly_title",
        access     = "premium",
        max_tokens = 900,
    ),
    "navamsha": ReadingConfig(
        title_key  = "premium.btn_navamsha",
        access     = "premium",
        max_tokens = 1400,
    ),
    "dashamsha": ReadingConfig(
        title_key  = "premium.btn_dashamsha",
        access     = "premium",
        max_tokens = 1400,
    ),
    "dasha_adv": ReadingConfig(
        title_key  = "premium.btn_dasha",
        access     = "premium",
        max_tokens = 1400,
    ),
    "transits": ReadingConfig(
        title_key  = "premium.btn_transits",
        access     = "premium",
        max_tokens = 1400,
    ),
    "nodes": ReadingConfig(
        title_key  = "premium.btn_nodes",
        access     = "premium",
        max_tokens = 1400,
    ),
    "atmakaraka": ReadingConfig(
        title_key  = "premium.btn_atma",
        access     = "premium",
        max_tokens = 1400,
    ),
    "yoga": ReadingConfig(
        title_key  = "premium.btn_yoga",
        access     = "premium",
        max_tokens = 1400,
    ),
    "shadbala": ReadingConfig(
        title_key  = "premium.btn_shadbala",
        access     = "premium",
        max_tokens = 1400,
    ),
    "arudha": ReadingConfig(
        title_key  = "premium.btn_arudha",
        access     = "premium",
        max_tokens = 1400,
    ),
    "sadesati": ReadingConfig(
        title_key  = "premium.btn_sadesati",
        access     = "premium",
        max_tokens = 1400,
    ),
    "upaya": ReadingConfig(
        title_key  = "premium.btn_upaya",
        access     = "premium",
        max_tokens = 1400,
    ),
    "muhurta": ReadingConfig(
        title_key  = "premium.btn_muhurta",
        access     = "premium",
        max_tokens = 1200,
    ),
    # Houses 1..12 — параметризованный разбор (ключ динамический: "house_1".."house_12")
    # Регистрируются в READINGS через цикл ниже
}

# Добавляем 12 домов
for _n in range(1, 13):
    READINGS[f"house_{_n}"] = ReadingConfig(
        title_key  = "premium.btn_houses",   # "Дом N" подставляется в клавиатуре
        access     = "premium",
        max_tokens = 1400,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Сгруппированные ключи (используются в клавиатурах и планировщике)
# ──────────────────────────────────────────────────────────────────────────────

#: Freemium-разделы главного меню (в порядке показа)
FREEMIUM_SECTIONS: list[str] = ["love", "money", "karma", "family", "years"]

#: Разделы «Продвинутого Джйотиша» (в порядке кнопок)
PREMIUM_ADV_SECTIONS: list[str] = [
    "navamsha", "dashamsha", "dasha_adv", "transits",
    "nodes", "atmakaraka", "yoga", "shadbala",
    "arudha", "sadesati", "upaya", "muhurta",
]

#: Ключи домов
HOUSE_KEYS: list[str] = [f"house_{n}" for n in range(1, 13)]

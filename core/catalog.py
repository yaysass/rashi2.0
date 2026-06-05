"""
core/catalog.py
===============
Единый каталог всех разборов (reading catalog).

Структура ReadingEntry:
  title_key  — dot-path в TEXTS ("menu.btn_love") для заголовка/кнопки
  access     — "free" | "freemium" | "premium"
  prompt_key — ключ в prompts._SECTION_INSTRUCTIONS
               Для стандартных разборов совпадает с ключом READINGS.
               Для параметризованных (muhurta, weekly, question) — тот же ключ,
               но хендлер обязан передать **extra с нужными params.
  max_tokens — лимит токенов для Claude

Почему prompt_key ≡ reading key:
  build_user_prompt() использует section_key и для фильтрации тем (SECTION_AREAS),
  и для поиска инструкции (_SECTION_INSTRUCTIONS). Оба словаря используют один
  и тот же набор ключей, поэтому явное хранение prompt_key нужно только
  для документирования и будущего расширения.

Один хендлер run_reading() в handlers/menu.py закрывает все разборы.
НИКАКОГО if/elif по ключам — только catalog[key] + access.can_*(user, key).
"""
from __future__ import annotations

from typing import NamedTuple


# ──────────────────────────────────────────────────────────────────────────────
#  Запись каталога
# ──────────────────────────────────────────────────────────────────────────────

class ReadingEntry(NamedTuple):
    title_key:  str    # "section.btn_key" → TEXTS[section][btn_key]
    access:     str    # "free" | "freemium" | "premium"
    prompt_key: str    # key in prompts._SECTION_INSTRUCTIONS
    max_tokens: int


# ──────────────────────────────────────────────────────────────────────────────
#  Каталог разборов
# ──────────────────────────────────────────────────────────────────────────────

READINGS: dict[str, ReadingEntry] = {

    # ── Бесплатные ──────────────────────────────────────────────────────────
    "personality": ReadingEntry(
        title_key="menu.btn_natal",
        access="free",
        prompt_key="personality",
        max_tokens=1600,
    ),

    # ── Freemium: 1 бесплатный клик на любую тему ───────────────────────────
    "love": ReadingEntry(
        title_key="menu.btn_love",
        access="freemium",
        prompt_key="love",
        max_tokens=1500,
    ),
    "money": ReadingEntry(
        title_key="menu.btn_money",
        access="freemium",
        prompt_key="money",    # _SECTION_INSTRUCTIONS["money"] — «Деньги и карьера»
        max_tokens=1500,
    ),
    "karma": ReadingEntry(
        title_key="menu.btn_karma",
        access="freemium",
        prompt_key="karma",
        max_tokens=1500,
    ),
    "family": ReadingEntry(
        title_key="menu.btn_family",
        access="freemium",
        prompt_key="family",
        max_tokens=1500,
    ),
    "years": ReadingEntry(
        title_key="menu.btn_years",
        access="freemium",
        prompt_key="years",    # _SECTION_INSTRUCTIONS["years"] — Дашa/важные годы
        max_tokens=1500,
    ),

    # ── Premium: Продвинутый Джйотиш ────────────────────────────────────────
    "navamsha": ReadingEntry(
        title_key="adv.btn_navamsha",
        access="premium",
        prompt_key="navamsha",
        max_tokens=1400,
    ),
    "dashamsha": ReadingEntry(
        title_key="adv.btn_dashamsha",
        access="premium",
        prompt_key="dashamsha",
        max_tokens=1400,
    ),
    "dasha_adv": ReadingEntry(
        title_key="adv.btn_dasha",
        access="premium",
        prompt_key="dasha_adv",
        max_tokens=1400,
    ),
    "transits": ReadingEntry(
        title_key="adv.btn_transits",
        access="premium",
        prompt_key="transits",
        max_tokens=1400,
    ),
    "nodes": ReadingEntry(
        title_key="adv.btn_nodes",
        access="premium",
        prompt_key="nodes",
        max_tokens=1400,
    ),
    "atmakaraka": ReadingEntry(
        title_key="adv.btn_atma",
        access="premium",
        prompt_key="atmakaraka",
        max_tokens=1400,
    ),
    "yoga": ReadingEntry(
        title_key="adv.btn_yoga",
        access="premium",
        prompt_key="yoga",
        max_tokens=1400,
    ),
    "shadbala": ReadingEntry(
        title_key="adv.btn_shadbala",
        access="premium",
        prompt_key="shadbala",
        max_tokens=1400,
    ),
    "arudha": ReadingEntry(
        title_key="adv.btn_arudha",
        access="premium",
        prompt_key="arudha",
        max_tokens=1400,
    ),
    "sadesati": ReadingEntry(
        title_key="adv.btn_sadesati",
        access="premium",
        prompt_key="sadesati",
        max_tokens=1400,
    ),
    "upaya": ReadingEntry(
        title_key="adv.btn_upaya",
        access="premium",
        prompt_key="upaya",
        max_tokens=1400,
    ),

    # ── Premium: параметризованные (хендлер передаёт **extra) ───────────────
    # muhurta требует extra={"event": str, "period": str}
    "muhurta": ReadingEntry(
        title_key="adv.btn_muhurta",
        access="premium",
        prompt_key="muhurta",
        max_tokens=1200,
    ),

    # weekly требует extra={"week": "YYYY-Www"}; обрабатывается в handlers/premium.py
    "weekly": ReadingEntry(
        title_key="menu.btn_weekly",
        access="premium",
        prompt_key="weekly",
        max_tokens=900,
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
#  Производные множества — используются keyboards.py и handlers
# ──────────────────────────────────────────────────────────────────────────────

# Все freemium-ключи — нужны для построения меню с замками
FREEMIUM_KEYS: frozenset[str] = frozenset(
    k for k, v in READINGS.items() if v.access == "freemium"
)

# Ключи Продвинутого Джйотиша (premium, кроме weekly)
ADVANCED_KEYS: frozenset[str] = frozenset(
    k for k, v in READINGS.items()
    if v.access == "premium" and k != "weekly"
)

# Параметризованные разборы — требуют **extra, обрабатываются отдельными хендлерами
PARAMETRIZED_KEYS: frozenset[str] = frozenset({"muhurta", "weekly"})


# ──────────────────────────────────────────────────────────────────────────────
#  Хелпер резолюции title_key
# ──────────────────────────────────────────────────────────────────────────────

# Фоллбэки для кнопок Продвинутого Джйотиша на случай,
# если секция "adv" ещё не добавлена в texts.py.
_ADV_FALLBACKS: dict[str, str] = {
    "btn_navamsha":  "🌙 Карта отношений (D9)",
    "btn_dashamsha": "💼 Карта карьеры (D10)",
    "btn_dasha":     "⏳ Периоды жизни",
    "btn_transits":  "🪐 Транзиты планет",
    "btn_nodes":     "🗝 Узлы судьбы",
    "btn_atma":      "✨ Планета Души",
    "btn_yoga":      "⚖️ Астрологические йоги",
    "btn_shadbala":  "📊 Сила планет",
    "btn_arudha":    "🪞 Образ в глазах других",
    "btn_sadesati":  "🪐 Период Сатурна",
    "btn_upaya":     "🌿 Упайи - рекомендации",
    "btn_muhurta":   "🕯 Выбрать лучшее время",
}


def resolve_title(entry: ReadingEntry, texts: dict) -> str:
    """
    Резолвит title_key ("section.btn_key") → строку из TEXTS с фоллбэком.

    Пример: "menu.btn_love" → TEXTS["menu"]["btn_love"] → "🤍 Любовь и отношения"
    """
    parts = entry.title_key.split(".", 1)
    if len(parts) != 2:
        return entry.title_key

    section, key = parts
    section_dict = texts.get(section, {})

    if isinstance(section_dict, dict) and key in section_dict:
        return section_dict[key]

    # Фоллбэк для adv.*
    if section == "adv":
        return _ADV_FALLBACKS.get(key, key)

    return entry.title_key  # последний резерв — сам ключ

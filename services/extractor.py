"""
services/extractor.py
=====================
Heart of the product.  Translates raw VedAstro data into psychological
tags (dominant_themes) and a single central_conflict sentence.

Usage:
    chart = {
        "raw":     astro_json["raw"],
        "metrics": astro_json["metrics"],
    }
    result = FeatureExtractor().extract(chart)
    # result = {"dominant_themes": [...], "central_conflict": "..."}
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Type aliases
# ──────────────────────────────────────────────────────────────────────────────
Tag   = dict[str, Any]   # {"tag": str, "weight": int, "areas": list[str]}
Rule  = dict[str, Any]   # {"when": dict, "themes": list[Tag]}
Chart = dict[str, Any]   # {"raw": {...}, "metrics": {...}}

# Number of top themes to keep after merging
TOP_N = 8

# ──────────────────────────────────────────────────────────────────────────────
#  Conflict templates
#  Each entry: (pole_a_keywords, pole_b_keywords, conflict_sentence)
#  Matched against the joined text of the top-6 theme tags (lowercased).
# ──────────────────────────────────────────────────────────────────────────────
_CONFLICT_TEMPLATES: list[tuple[set[str], set[str], str]] = [
    (
        {"контроль", "анализ", "перфекционизм", "проверяет", "сдержанность",
         "ограничение", "подавленн"},
        {"страсть", "проявляться", "открыться", "живой", "желание", "экспансия"},
        "Между потребностью всё держать под контролем и анализировать - "
        "и живой частью себя, которая хочет проявляться, но боится оказаться неидеальной.",
    ),
    (
        {"стабильность", "безопасность", "материальное", "накопление", "постоянство"},
        {"риск", "свобода", "перемены", "непредсказуемость", "движение", "импульс"},
        "Между стремлением к стабильности и безопасности - "
        "и внутренним запросом на перемены и новый опыт.",
    ),
    (
        {"партнёрство", "зависимость от другого", "потребность в паре",
         "со-действие", "ориентация на другого"},
        {"автономия", "независимость", "самодостаточность", "одиночество", "свобода"},
        "Между глубокой потребностью в партнёрстве - "
        "и не менее сильной тягой к независимости и самодостаточности.",
    ),
    (
        {"тревога", "страх", "уязвимость", "защита", "осторожность"},
        {"доверие", "открытость", "близость", "расслабление", "принятие"},
        "Между постоянной внутренней тревогой и защитными реакциями - "
        "и желанием по-настоящему открыться и довериться другому человеку.",
    ),
    (
        {"долг", "ответственность", "самоограничение", "обязательства", "строгость"},
        {"удовольствие", "радость", "спонтанность", "желания", "наслаждение"},
        "Между ощущением долга и строгими требованиями к себе - "
        "и живым желанием радоваться, наслаждаться жизнью и быть собой.",
    ),
    (
        {"самокритика", "высокие требования к себе", "строгость к себе",
         "не заслуживает"},
        {"ценность", "принятие", "достаточно хорош", "самоуважение", "признание"},
        "Между жёсткой самокритикой и завышенными требованиями к себе - "
        "и глубинной потребностью в принятии и ощущении собственной ценности.",
    ),
    (
        {"гармония", "мир", "избегание конфликта", "дипломатичность", "уступает"},
        {"прямота", "честность", "отстаивание границ", "злость", "прямое выражение"},
        "Между стремлением сохранить мир и гармонию любой ценой - "
        "и подавленной потребностью честно обозначать свои границы и желания.",
    ),
]

# ──────────────────────────────────────────────────────────────────────────────
#  RULES
# ──────────────────────────────────────────────────────────────────────────────
#
# Rule condition keys ("when" dict):
#   lagna:        str  — ascendant sign in Russian
#   planet:       str  — planet name in English (e.g. "Venus")
#   sign:         str  — planet's sign in Russian
#   house:        int  — planet's house number (1–12)
#   avastha_in:   list[str]  — matches if planet's avastha is in this list
#   shadbala_min: float      — matches if planet's shadbala >= value
#   shadbala_max: float      — matches if planet's shadbala <= value
#
# All keys in "when" must match simultaneously (AND logic).
# Rules without a "planet" key check only lagna / chart-level conditions.
# ──────────────────────────────────────────────────────────────────────────────

RULES: list[Rule] = [

    # ══════════════════════════════════════════════════════════════════════════
    #  ЛАГНА В ВЕСАХ  (Libra Ascendant)
    # ══════════════════════════════════════════════════════════════════════════

    # Core Libra ascendant — always fires when Lagna = Весы
    {
        "when": {"lagna": "Весы"},
        "themes": [
            {
                "tag":    "потребность в гармонии как основа внутренней безопасности",
                "weight": 88,
                "areas":  ["self", "love", "family"],
            },
            {
                "tag":    "глубокая ориентация на партнёрство и со-действие",
                "weight": 87,
                "areas":  ["love", "self"],
            },
            {
                "tag":    "трудность принятия решений - взвешивание вместо действия",
                "weight": 85,
                "areas":  ["self", "work"],
            },
            {
                "tag":    "склонность адаптироваться под ожидания окружающих",
                "weight": 84,
                "areas":  ["self", "love", "family"],
            },
            {
                "tag":    "дипломатичность и умение видеть чужую точку зрения",
                "weight": 83,
                "areas":  ["self", "work"],
            },
            {
                "tag":    "конфликт воспринимается как угроза, а не как инструмент",
                "weight": 82,
                "areas":  ["self", "love", "family"],
            },
            {
                "tag":    "острое чувство справедливости и неприязнь к несправедливости",
                "weight": 81,
                "areas":  ["self", "karma", "work"],
            },
            {
                "tag":    "эстетическое восприятие мира и тяга к красоте во всём",
                "weight": 78,
                "areas":  ["self"],
            },
        ],
    },

    # Libra lagna + weak Mars (lagnesh of natural enemy = difficulty with assertion)
    {
        "when": {"lagna": "Весы", "planet": "Mars", "shadbala_max": 320},
        "themes": [
            {
                "tag":    "сложность с прямым выражением злости и отстаиванием границ",
                "weight": 86,
                "areas":  ["self", "love"],
            },
            {
                "tag":    "избегание прямой конфронтации даже там, где она необходима",
                "weight": 84,
                "areas":  ["self", "work", "love"],
            },
        ],
    },

    # Libra lagna + weak Venus (own lagnesh in poor state = low self-worth)
    {
        "when": {"lagna": "Весы", "planet": "Venus", "avastha_in": ["Sleeping", "Degraded"]},
        "themes": [
            {
                "tag":    "неуверенность в собственной ценности и привлекательности",
                "weight": 89,
                "areas":  ["self", "love"],
            },
            {
                "tag":    "поиск одобрения через угождение и соответствие чужим ожиданиям",
                "weight": 87,
                "areas":  ["self", "love", "work"],
            },
        ],
    },

    # Libra lagna + strong Venus (lagnesh thriving = powerful aesthetic identity)
    {
        "when": {"lagna": "Весы", "planet": "Venus", "shadbala_min": 430},
        "themes": [
            {
                "tag":    "сильная эстетическая личность - создаёт красоту и гармонию вокруг себя",
                "weight": 85,
                "areas":  ["self", "work"],
            },
            {
                "tag":    "харизма через мягкость - влияние без давления и принуждения",
                "weight": 82,
                "areas":  ["self", "love", "work"],
            },
        ],
    },

    # Libra lagna + Saturn in 1st/7th (common placements that reinforce restriction)
    {
        "when": {"lagna": "Весы", "planet": "Saturn", "house": 7},
        "themes": [
            {
                "tag":    "партнёрство как урок дисциплины и ответственности",
                "weight": 84,
                "areas":  ["love", "karma"],
            },
            {
                "tag":    "задержки или испытания в браке и долгосрочных отношениях",
                "weight": 82,
                "areas":  ["love", "family"],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    #  ВЕНЕРА В ДЕВЕ В 12 ДОМЕ
    # ══════════════════════════════════════════════════════════════════════════

    # Base rule — fires for any Venus in Virgo, 12th house (any avastha/shadbala)
    {
        "when": {"planet": "Venus", "sign": "Дева", "house": 12},
        "themes": [
            {
                "tag":    "анализ чувств вместо их проживания",
                "weight": 90,
                "areas":  ["love", "self"],
            },
            {
                "tag":    "перфекционизм как защита от уязвимости в любви",
                "weight": 88,
                "areas":  ["love", "self", "work"],
            },
            {
                "tag":    "страх открыться - сначала нужно убедиться, что всё правильно",
                "weight": 87,
                "areas":  ["love"],
            },
            {
                "tag":    "высокие стандарты в отношениях, которые почти невозможно воплотить",
                "weight": 85,
                "areas":  ["love"],
            },
            {
                "tag":    "любовь часто остаётся невысказанной или скрытой",
                "weight": 84,
                "areas":  ["love"],
            },
            {
                "tag":    "склонность отдавать больше, чем получать в отношениях",
                "weight": 82,
                "areas":  ["love", "family"],
            },
            {
                "tag":    "творчество или духовная практика как способ выразить любовь",
                "weight": 76,
                "areas":  ["love", "karma", "self"],
            },
        ],
    },

    # Venus in Virgo 12H + Sleeping / Degraded avastha (suppressed feelings)
    {
        "when": {
            "planet":     "Venus",
            "sign":       "Дева",
            "house":      12,
            "avastha_in": ["Sleeping", "Degraded"],
        },
        "themes": [
            {
                "tag":    "подавленные чувства - разум постоянно анализирует и контролирует эмоции",
                "weight": 93,
                "areas":  ["love", "self"],
            },
            {
                "tag":    "перфекционизм как защита от критики и отвержения",
                "weight": 91,
                "areas":  ["love", "self", "work"],
            },
            {
                "tag":    "страх открыться в любви из-за ощущения собственного несовершенства",
                "weight": 89,
                "areas":  ["love"],
            },
            {
                "tag":    "тенденция к самопожертвованию и растворению в партнёре",
                "weight": 84,
                "areas":  ["love"],
            },
        ],
    },

    # Venus in Virgo 12H + Awake / Proud avastha (more conscious analytical nature)
    {
        "when": {
            "planet":     "Venus",
            "sign":       "Дева",
            "house":      12,
            "avastha_in": ["Awake", "Proud"],
        },
        "themes": [
            {
                "tag":    "аналитический подход к чувствам - глубокое понимание своих паттернов",
                "weight": 86,
                "areas":  ["love", "self"],
            },
            {
                "tag":    "любовь как служение - глубокая забота, выраженная действиями, а не словами",
                "weight": 82,
                "areas":  ["love", "family"],
            },
            {
                "tag":    "высокая избирательность в отношениях - не каждый заслуживает близости",
                "weight": 85,
                "areas":  ["love"],
            },
        ],
    },

    # Venus in Virgo 12H + very low shadbala (< 300) — vulnerability, fear
    {
        "when": {
            "planet":      "Venus",
            "sign":        "Дева",
            "house":       12,
            "shadbala_max": 299,
        },
        "themes": [
            {
                "tag":    "уязвимость в любви - страх быть отвергнутой из-за несовершенства",
                "weight": 92,
                "areas":  ["love", "self"],
            },
            {
                "tag":    "трудность принимать любовь - ощущение, что не заслуживает",
                "weight": 88,
                "areas":  ["love", "self"],
            },
        ],
    },

    # Venus in Virgo 12H + high shadbala (> 450) — strong but over-analytical
    {
        "when": {
            "planet":      "Venus",
            "sign":        "Дева",
            "house":       12,
            "shadbala_min": 450,
        },
        "themes": [
            {
                "tag":    "сила через дисциплину чувств - умеет управлять привязанностью осознанно",
                "weight": 80,
                "areas":  ["love", "self"],
            },
            {
                "tag":    "глубокая интуиция в отношениях несмотря на скованность выражения",
                "weight": 78,
                "areas":  ["love"],
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    #  ЗАГОТОВКИ — остальные планеты и лагны
    #  TODO Phase 2: добавить полные правила для каждой.
    # ══════════════════════════════════════════════════════════════════════════

    # ── Sun (Солнце) ──────────────────────────────────────────────────────────
    # TODO: Sun in each sign/house combination + avastha modifiers

    # ── Moon (Луна) ───────────────────────────────────────────────────────────
    # TODO: Moon in each sign/house + shadbala modifiers (low = emotional fragility,
    #       high = strong intuition)

    # ── Mars (Марс) ───────────────────────────────────────────────────────────
    # TODO: Mars (drive/assertion) in each sign/house

    # ── Mercury (Меркурий) ────────────────────────────────────────────────────
    # TODO: Mercury (communication/mind) in each sign/house

    # ── Jupiter (Юпитер) ─────────────────────────────────────────────────────
    # TODO: Jupiter (wisdom/expansion) in each sign/house

    # ── Saturn (Сатурн) ──────────────────────────────────────────────────────
    # TODO: Saturn (discipline/restriction) in each sign/house + Sade-Sati flag

    # ── Rahu (Северный узел) ─────────────────────────────────────────────────
    # TODO: Rahu axis themes by house (obsession/hunger pattern)

    # ── Ketu (Южный узел) ────────────────────────────────────────────────────
    # TODO: Ketu axis themes by house (detachment/past life pattern)

    # ── Other Lagna signs ────────────────────────────────────────────────────
    # TODO: Aries, Taurus, Gemini, Cancer, Leo, Virgo,
    #       Scorpio, Sagittarius, Capricorn, Aquarius, Pisces
]


# ──────────────────────────────────────────────────────────────────────────────
#  FeatureExtractor
# ──────────────────────────────────────────────────────────────────────────────

class FeatureExtractor:
    """
    Translates raw astro data into psychological tags and a central conflict.

    Input:
        chart = {"raw": astro_json["raw"], "metrics": astro_json["metrics"]}

    Output:
        {"dominant_themes": [Tag, ...], "central_conflict": str}
    """

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules: list[Rule] = rules if rules is not None else RULES

    # ── Condition evaluation ─────────────────────────────────────────────────

    def _check_condition(self, when: dict, chart: Chart) -> bool:
        """
        Return True if ALL conditions in `when` are satisfied by `chart`.
        Short-circuits on first mismatch.
        """
        raw      = chart.get("raw", {})
        metrics  = chart.get("metrics", {})
        planets  = raw.get("planets", {})
        lagna    = raw.get("ascendant", {})
        shadbala = metrics.get("shadbala", {})
        avastha  = metrics.get("avastha", {})

        # Lagna sign match
        if "lagna" in when:
            if lagna.get("sign", "") != when["lagna"]:
                return False

        # Planet-specific conditions
        if "planet" in when:
            p_en   = when["planet"]
            p_data = planets.get(p_en, {})

            if "sign" in when and p_data.get("sign", "") != when["sign"]:
                return False

            if "house" in when and p_data.get("house", 0) != when["house"]:
                return False

            if "avastha_in" in when:
                if avastha.get(p_en, "") not in when["avastha_in"]:
                    return False

            if "shadbala_min" in when:
                if shadbala.get(p_en, 0) < when["shadbala_min"]:
                    return False

            if "shadbala_max" in when:
                if shadbala.get(p_en, 0) > when["shadbala_max"]:
                    return False

        return True

    # ── Weight merger ────────────────────────────────────────────────────────

    @staticmethod
    def _merge_weights(collected: list[Tag]) -> list[Tag]:
        """
        Merge duplicate tags: keep the highest weight, union the areas lists.
        This lets multiple rules reinforce the same theme with escalating weights.
        """
        merged: dict[str, Tag] = {}
        for theme in collected:
            tag = theme["tag"]
            if tag not in merged:
                merged[tag] = {
                    "tag":    tag,
                    "weight": theme["weight"],
                    "areas":  list(theme["areas"]),
                }
            else:
                entry = merged[tag]
                if theme["weight"] > entry["weight"]:
                    entry["weight"] = theme["weight"]
                for area in theme["areas"]:
                    if area not in entry["areas"]:
                        entry["areas"].append(area)
        return list(merged.values())

    # ── Central conflict builder ─────────────────────────────────────────────

    def _build_central_conflict(self, themes: list[Tag]) -> str:
        """
        Produce one sentence describing the core tension in the chart.
        Strategy:
          1. Join the text of top-6 themes and scan for predefined conflict pairs.
          2. If a pair matches, return its pre-written sentence.
          3. Fallback: compose a generic sentence from the top-2 themes.
        """
        if not themes:
            return ""

        combined = " ".join(t["tag"] for t in themes[:6]).lower()

        for kw_a, kw_b, sentence in _CONFLICT_TEMPLATES:
            has_a = any(kw in combined for kw in kw_a)
            has_b = any(kw in combined for kw in kw_b)
            if has_a and has_b:
                return sentence

        # Generic fallback
        if len(themes) >= 2:
            return (
                f"Между {themes[0]['tag']} - "
                f"и одновременно {themes[1]['tag']}: "
                "это основное напряжение, из которого вырастают все остальные паттерны."
            )
        return f"Ключевой вектор: {themes[0]['tag']}."

    # ── Main entry point ─────────────────────────────────────────────────────

    def extract(self, chart: Chart) -> dict[str, Any]:
        """
        Run all rules against the chart.

        Args:
            chart: dict with keys "raw" and "metrics" from astro_json.

        Returns:
            {
                "dominant_themes": [{"tag": str, "weight": int, "areas": [str]}, ...],
                "central_conflict": str,
            }
        """
        collected: list[Tag] = []

        for rule in self._rules:
            try:
                if self._check_condition(rule["when"], chart):
                    for theme in rule["themes"]:
                        collected.append(dict(theme))  # shallow copy is enough
            except Exception as exc:
                logger.warning(
                    "Rule evaluation error (when=%s): %s",
                    rule.get("when", {}),
                    exc,
                )

        if not collected:
            logger.warning("FeatureExtractor: no rules fired — chart may be empty or unrecognised")
            return {"dominant_themes": [], "central_conflict": ""}

        merged   = self._merge_weights(collected)
        top      = sorted(merged, key=lambda t: t["weight"], reverse=True)[:TOP_N]
        conflict = self._build_central_conflict(top)

        logger.info(
            "FeatureExtractor: %d tag instances → %d unique → %d dominant",
            len(collected), len(merged), len(top),
        )

        return {
            "dominant_themes":  top,
            "central_conflict": conflict,
        }

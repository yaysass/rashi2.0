"""
services/astrology.py
=====================
VedAstro client (sync → async via to_thread) + geocoding.

PHASE 1 NOTE:
  Verify all Calculate.* method names by running:
      Calculate.ListAPICalls()
  in a standalone Python script with vedastro installed.
  Methods marked # [verify] below are best-guesses from the spec.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from datetime import datetime
from typing import Any

import pytz
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

from config import VEDASTRO_API_KEY

logger = logging.getLogger(__name__)

# Выделенный однопоточный executor для всех вызовов VedAstro.
# VedAstro Python-биндинги не являются thread-safe: параллельные вызовы из
# разных системных потоков перезаписывают внутреннее состояние библиотеки и
# возвращают одинаковые (неверные) результаты для разных планет.
# Запуск ВСЕХ вызовов на одном выделенном потоке исключает race condition.
_VA_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="vedastro"
)

# ──────────────────────────────────────────────────────────────────────────────
#  VedAstro bootstrap
# ──────────────────────────────────────────────────────────────────────────────
_VA_AVAILABLE = False
Calculate = GeoLocation = Time = None  # type: ignore[assignment]

try:
    from vedastro import Calculate, GeoLocation, Time, PlanetName, HouseName  # type: ignore[no-redef]
    _VA_AVAILABLE = True
    Calculate.SetAPIKey(VEDASTRO_API_KEY)
    logger.info("VedAstro initialised OK")
except ImportError:
    logger.warning("vedastro not installed — astrology features disabled")
except Exception as exc:
    logger.warning("VedAstro init error: %s", exc)


# Знаки в фиксированном порядке для перевода долготы → знак (0=Овен, 11=Рыбы)
ZODIAC_INDEX_EN: list[str] = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]


def _sign_from_longitude_ru(lon: float | None) -> str:
    """Перевести нираяна-долготу (0-360°) в русское имя знака."""
    if lon is None:
        return ""
    sign_en = ZODIAC_INDEX_EN[int(lon // 30) % 12]
    return ZODIAC_EN_TO_RU.get(sign_en, sign_en)


def _navamsha_from_longitude_ru(lon: float | None) -> str:
    """D9-знак по формуле (lon*9) % 360."""
    if lon is None:
        return ""
    nav_lon = (lon * 9) % 360
    sign_en = ZODIAC_INDEX_EN[int(nav_lon // 30) % 12]
    return ZODIAC_EN_TO_RU.get(sign_en, sign_en)


def _d10_from_longitude_ru(lon: float | None) -> str:
    """D10-знак по Парашаре (нечётные знаки от себя, чётные от 9-го)."""
    if lon is None:
        return ""
    sign_idx = int(lon // 30) % 12
    part = int((lon % 30) // 3)  # 0..9
    is_odd = (sign_idx % 2 == 0)
    start = sign_idx if is_odd else (sign_idx + 8) % 12
    sign_en = ZODIAC_INDEX_EN[(start + part) % 12]
    return ZODIAC_EN_TO_RU.get(sign_en, sign_en)


def _house_from_lagna(planet_sign_ru: str, lagna_sign_ru: str) -> int:
    """Whole-sign house: какой по счёту дом от Лагны занимает данный знак."""
    SIGNS_RU = [
        "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
        "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы",
    ]
    try:
        p_idx = SIGNS_RU.index(planet_sign_ru)
        l_idx = SIGNS_RU.index(lagna_sign_ru)
    except ValueError:
        return 0
    return ((p_idx - l_idx) % 12) + 1


# ──────────────────────────────────────────────────────────────────────────────
#  Method-name resolver
#  VedAstro Python API differs between versions. Instead of hard-coding one
#  name per metric, we try a list of candidates and cache the first match.
#  Any metric whose method can't be resolved gracefully returns None.
# ──────────────────────────────────────────────────────────────────────────────
_METHOD_CANDIDATES: dict[str, list[str]] = {
    "all_planets":  ["AllPlanetData", "AllPlanetsData", "AllPlanets",
                     "PlanetData", "AllPlanetDataList"],
    "shadbala":     ["PlanetSthanaBala", "PlanetShadbalaTotal", "PlanetShadbala",
                     "PlanetTotalShadbalaInRupa", "PlanetSthanaBalaChart"],
    "avastha":      ["PlanetAvasta", "PlanetAvastha", "PlanetBalaAvastha",
                     "PlanetJagradadiAvastha", "PlanetAvasthaName"],
    "aspects":      ["PlanetsInAspect", "PlanetAspects", "PlanetAspectsList",
                     "AllPlanetAspects", "PlanetAspectedBy"],
    "navamsha":     ["NavamshaChart", "NavamsaChart", "NavamsaD9Chart",
                     "NavamsaSignAllPlanets", "AllPlanetD9Sign"],
    "dashamsha":    ["DashamshaChart", "DasamsaChart", "DashamnshaChart",
                     "D10Chart", "DashamamshaChart", "AllPlanetD10Sign"],
    "dasha":        ["VimshottariDasha", "VimshottariDasa", "DasaAtBirth",
                     "CurrentDasaAtBirth", "CurrentDasaForPerson"],
    "yogas":        ["Yoga", "AllYogas", "YogasChart", "AllYogasInChart",
                     "YogaList", "YogaTable"],
    "sade_sati":    ["SadeSati", "IsSadeSati", "SadeSatiSummary",
                     "SadeSatiStatus", "SadeSatiSummaryReport"],
}

_RESOLVED_METHODS: dict[str, Any] = {}


def _resolve_method(key: str) -> Any | None:
    """
    Return the first Calculate.* method whose name appears in the candidate list
    for `key`. Cached on first call. Returns None if nothing matches.
    """
    if key in _RESOLVED_METHODS:
        return _RESOLVED_METHODS[key]
    if Calculate is None:
        _RESOLVED_METHODS[key] = None
        return None
    candidates = _METHOD_CANDIDATES.get(key, [])
    for name in candidates:
        fn = getattr(Calculate, name, None)
        if callable(fn):
            _RESOLVED_METHODS[key] = fn
            logger.info("VedAstro: %s → Calculate.%s", key, name)
            return fn
    _RESOLVED_METHODS[key] = None
    logger.warning(
        "VedAstro: %s НЕ найдено (пробовал: %s)",
        key, ", ".join(candidates),
    )
    return None


def _list_available_methods(prefix_filter: str = "") -> list[str]:
    """Names of all public callables on Calculate (optionally filtered by prefix)."""
    if Calculate is None:
        return []
    out: list[str] = []
    for name in dir(Calculate):
        if name.startswith("_"):
            continue
        if prefix_filter and not name.startswith(prefix_filter):
            continue
        if callable(getattr(Calculate, name, None)):
            out.append(name)
    return sorted(out)


def _log_resolver_diagnostics() -> None:
    """At first chart build, log: (a) which methods resolved, (b) what's actually available."""
    for key in _METRIC_KEYS:
        _resolve_method(key)  # populates cache, logs each
    # Snapshot of all Planet/Chart/Yoga methods for debugging unresolved ones
    sample = _list_available_methods()
    relevant = [n for n in sample if any(
        kw in n for kw in ("Planet", "Chart", "Yoga", "Dasa", "Dasha", "Avast",
                           "Sade", "Aspect", "Navams", "Dasham", "All", "House"))]
    logger.info(
        "VedAstro: установленная версия предоставляет %d публичных методов; "
        "релевантных: %d. Список: %s",
        len(sample), len(relevant), ", ".join(relevant) if relevant else "(none)",
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Static lookup tables
# ──────────────────────────────────────────────────────────────────────────────
PLANET_NAMES_EN: list[str] = [
    "Sun", "Moon", "Mars", "Mercury",
    "Jupiter", "Venus", "Saturn", "Rahu", "Ketu",
]

PLANET_NAMES_RU: dict[str, str] = {
    "Sun":     "Солнце",
    "Moon":    "Луна",
    "Mars":    "Марс",
    "Mercury": "Меркурий",
    "Jupiter": "Юпитер",
    "Venus":   "Венера",
    "Saturn":  "Сатурн",
    "Rahu":    "Раху",
    "Ketu":    "Кету",
}

ZODIAC_EN_TO_RU: dict[str, str] = {
    "Aries":       "Овен",
    "Taurus":      "Телец",
    "Gemini":      "Близнецы",
    "Cancer":      "Рак",
    "Leo":         "Лев",
    "Virgo":       "Дева",
    "Libra":       "Весы",
    "Scorpio":     "Скорпион",
    "Sagittarius": "Стрелец",
    "Capricorn":   "Козерог",
    "Aquarius":    "Водолей",
    "Pisces":      "Рыбы",
}

SIGN_RULERS: dict[str, str] = {
    "Овен": "Марс",      "Телец": "Венера",   "Близнецы": "Меркурий",
    "Рак": "Луна",       "Лев": "Солнце",     "Дева": "Меркурий",
    "Весы": "Венера",    "Скорпион": "Марс",  "Стрелец": "Юпитер",
    "Козерог": "Сатурн", "Водолей": "Сатурн", "Рыбы": "Юпитер",
}

# Predefined Vedic aspect psychological meanings (planet_en, planet_en) → Russian label
ASPECT_KINDS: dict[tuple[str, str], str] = {
    ("Saturn",  "Moon"):    "сдержанность эмоций",
    ("Saturn",  "Venus"):   "контроль над чувствами",
    ("Saturn",  "Sun"):     "борьба долга и воли",
    ("Saturn",  "Mars"):    "торможение действия",
    ("Saturn",  "Mercury"): "критическое мышление",
    ("Saturn",  "Jupiter"): "конфликт расширения и ограничения",
    ("Mars",    "Moon"):    "эмоциональная энергичность",
    ("Mars",    "Venus"):   "страсть и напряжение в отношениях",
    ("Mars",    "Mercury"): "острый и решительный ум",
    ("Mars",    "Jupiter"): "напор и мудрость",
    ("Jupiter", "Moon"):    "оптимизм и интуиция",
    ("Jupiter", "Venus"):   "щедрость в любви",
    ("Jupiter", "Mercury"): "философский и широкий ум",
    ("Jupiter", "Saturn"):  "оптимизм сдерживается дисциплиной",
    ("Rahu",    "Moon"):    "нестандартные желания",
    ("Rahu",    "Sun"):     "амбиции вне привычных рамок",
    ("Rahu",    "Venus"):   "необычные паттерны в любви",
    ("Ketu",    "Moon"):    "отстранённость от эмоций",
    ("Ketu",    "Sun"):     "неотождествление с эго",
    ("Ketu",    "Venus"):   "разочарование в отношениях как путь к внутреннему",
}

# ──────────────────────────────────────────────────────────────────────────────
#  Generic type coercers (VedAstro objects vary by version)
# ──────────────────────────────────────────────────────────────────────────────

def _to_str(val: Any) -> str:
    if val is None:
        return ""
    for attr in ("ToString", "Name", "name"):
        method = getattr(val, attr, None)
        if callable(method):
            return str(method()).strip()
        if method is not None:
            return str(method).strip()
    return str(val).strip()


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    for attr in ("TotalDegrees", "Degrees", "Value"):
        sub = getattr(val, attr, None)
        if sub is not None:
            try:
                return float(sub)
            except (TypeError, ValueError):
                pass
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _to_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_sign(raw: Any) -> str:
    s = _to_str(raw)
    return ZODIAC_EN_TO_RU.get(s, s)


# ──────────────────────────────────────────────────────────────────────────────
#  Geocoding
# ──────────────────────────────────────────────────────────────────────────────
_geolocator = Nominatim(user_agent="rashi_bot_v2", timeout=10)
_tf = TimezoneFinder()


def _geocode_sync(city: str) -> tuple[float, float, str, str]:
    """
    Synchronous geocode.  Always call via geocode_city() for async.
    Returns: (latitude, longitude, short_city_name, timezone_str)
    """
    try:
        loc = _geolocator.geocode(city, language="ru", addressdetails=True)
    except (GeocoderTimedOut, GeocoderUnavailable) as exc:
        raise RuntimeError(f"Geocoder unavailable: {exc}") from exc

    if loc is None:
        raise ValueError(f"Город не найден: {city!r}")

    lat, lon = loc.latitude, loc.longitude
    tz_str = _tf.timezone_at(lat=lat, lng=lon) or "UTC"

    addr = loc.raw.get("address", {})
    short_name = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or loc.address.split(",")[0].strip()
    )
    return lat, lon, short_name, tz_str


async def geocode_city(city: str) -> tuple[float, float, str, str]:
    """Async geocode: city name → (lat, lon, short_name, timezone_str)."""
    return await asyncio.to_thread(_geocode_sync, city)


def _tz_offset_str(tz_name: str) -> str:
    """Return ±HH:MM UTC offset for a timezone, accounting for DST."""
    try:
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        offset = now.utcoffset()
        total_sec = int(offset.total_seconds())  # type: ignore[union-attr]
        sign = "+" if total_sec >= 0 else "-"
        h, m = divmod(abs(total_sec) // 60, 60)
        return f"{sign}{h:02d}:{m:02d}"
    except Exception:
        return "+00:00"


# ──────────────────────────────────────────────────────────────────────────────
#  VedAstro sync → async wrapper
# ──────────────────────────────────────────────────────────────────────────────

def _va_call(fn, *args):
    return fn(*args)


async def va(fn, *args) -> Any:
    """
    Запустить синхронный вызов VedAstro на выделенном однопоточном executor.
    Все вызовы гарантированно выполняются последовательно на одном потоке —
    это критично для корректной работы VedAstro Python-биндингов.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_VA_EXECUTOR, _va_call, fn, *args)


def _make_birth_time_sync(
    birth_date: str,     # ДД.ММ.ГГГГ
    birth_time_str: str, # ЧЧ:ММ
    tz_str: str,
    lat: float,
    lon: float,
    city_name: str,
) -> Any:
    """
    Build a VedAstro Time object.
    Must run in a thread (VedAstro is synchronous).
    GeoLocation arg order: (name, longitude, latitude)  — [verify in Phase 1]
    """
    if not _VA_AVAILABLE:
        raise RuntimeError("vedastro library not installed")

    day, month, year = birth_date.split(".")
    offset = _tz_offset_str(tz_str)
    time_str = f"{birth_time_str} {day}/{month}/{year} {offset}"

    geo = GeoLocation(city_name, lon, lat)  # [verify arg order in Phase 1]
    return Time(time_str, geo)


# ──────────────────────────────────────────────────────────────────────────────
#  Parallel metric collection
# ──────────────────────────────────────────────────────────────────────────────
_METRIC_KEYS = [
    "all_planets",
    "shadbala",
    "avastha",
    "aspects",
    "navamsha",
    "dashamsha",
    "dasha",
    "yogas",
    "sade_sati",
]


async def collect_all_metrics(birth_time_obj: Any) -> dict[str, Any]:
    """
    Per-planet collection через правильные сигнатуры VedAstro.

    Возвращает уже-распарсенные структуры (не сырые объекты), поэтому функции
    parse_planets/parse_lagna и т.п. больше НЕ применяются — build_astro_data
    использует результат напрямую.
    """
    if not _VA_AVAILABLE:
        return {
            "planets": {}, "lagna": {"sign": "", "degree": 0.0, "nakshatra": ""},
            "navamsha": {}, "dasha": {"maha": "", "antar": "", "until": ""},
            "shadbala": {}, "avastha": {}, "aspects": [], "yogas": [], "sade_sati": "",
        }

    # Одноразовая диагностика
    if not _RESOLVED_METHODS:
        _log_resolver_diagnostics()

    # Резолвим per-planet методы один раз
    fn_lon       = getattr(Calculate, "PlanetNirayanaLongitude", None)
    fn_retro     = getattr(Calculate, "IsPlanetRetrograde", None)
    fn_nak       = getattr(Calculate, "PlanetConstellation", None)
    fn_exalt     = getattr(Calculate, "IsPlanetExalted", None)
    fn_debil     = getattr(Calculate, "IsPlanetDebilitated", None)
    fn_own       = getattr(Calculate, "IsPlanetInOwnSign", None)
    fn_friend    = getattr(Calculate, "IsPlanetInFriendSign", None)
    fn_enemy     = getattr(Calculate, "IsPlanetInEnemySign", None)
    fn_combust   = getattr(Calculate, "IsPlanetCombust", None)
    fn_vargot    = getattr(Calculate, "IsPlanetVargottama", None)
    fn_d9        = getattr(Calculate, "PlanetNavamshaD9Sign", None)
    fn_d10       = getattr(Calculate, "PlanetDashamamshaD10Sign", None)
    fn_avastha   = getattr(Calculate, "PlanetAvasta", None)
    fn_shadbala  = getattr(Calculate, "PlanetSthanaBala", None)
    fn_strength  = getattr(Calculate, "PlanetStrength", None)

    # Per-planet enum
    planet_enums: dict[str, Any] = {}
    for name in PLANET_NAMES_EN:
        planet_enums[name] = getattr(PlanetName, name, None) if PlanetName else None

    async def _safe(fn, *args, default=None):
        if fn is None or any(a is None for a in args):
            return default
        try:
            return await va(fn, *args)
        except Exception as exc:
            logger.debug("Call %s%r failed: %s", getattr(fn, "__name__", "?"), args, exc)
            return default

    # ─── Per-planet collection (параллельно для каждой планеты) ──────────────
    async def _collect_planet(name_en: str) -> tuple[str, dict]:
        enum = planet_enums.get(name_en)
        if enum is None:
            return name_en, {}
        # Параллельно собираем всё про эту планету
        (lon_raw, retro_raw, nak_raw, exalt_raw, debil_raw, own_raw,
         friend_raw, enemy_raw, combust_raw, vargot_raw,
         d9_raw, d10_raw, avastha_raw, shadbala_raw, strength_raw) = await asyncio.gather(
            _safe(fn_lon,      enum, birth_time_obj),
            _safe(fn_retro,    enum, birth_time_obj),
            _safe(fn_nak,      enum, birth_time_obj),
            _safe(fn_exalt,    enum, birth_time_obj, default=False),
            _safe(fn_debil,    enum, birth_time_obj, default=False),
            _safe(fn_own,      enum, birth_time_obj, default=False),
            _safe(fn_friend,   enum, birth_time_obj, default=False),
            _safe(fn_enemy,    enum, birth_time_obj, default=False),
            _safe(fn_combust,  enum, birth_time_obj, default=False),
            _safe(fn_vargot,   enum, birth_time_obj, default=False),
            _safe(fn_d9,       enum, birth_time_obj),
            _safe(fn_d10,      enum, birth_time_obj),
            _safe(fn_avastha,  enum, birth_time_obj),
            _safe(fn_shadbala, enum, birth_time_obj),
            _safe(fn_strength, enum, birth_time_obj),
        )

        lon = _to_float(lon_raw) if lon_raw is not None else None
        sign_ru = _sign_from_longitude_ru(lon) if lon is not None else ""
        degree = round(lon % 30, 2) if lon is not None else 0.0

        # Дигнити: приоритет экзальт→падение→своя→друг→враг→нейтрал
        if bool(exalt_raw):
            dignity = "экзальтация"
        elif bool(debil_raw):
            dignity = "падение"
        elif bool(own_raw):
            dignity = "своя обитель"
        elif bool(friend_raw):
            dignity = "знак друга"
        elif bool(enemy_raw):
            dignity = "знак врага"
        else:
            dignity = "нейтральный знак"

        nakshatra_str = _to_str(nak_raw) if nak_raw is not None else ""

        # D9 / D10 — берём от vedastro если ответил, иначе вычисляем математически
        d9_sign = _parse_sign(d9_raw) if d9_raw is not None else ""
        if not d9_sign:
            d9_sign = _navamsha_from_longitude_ru(lon)
        d10_sign = _parse_sign(d10_raw) if d10_raw is not None else ""
        if not d10_sign:
            d10_sign = _d10_from_longitude_ru(lon)

        avastha_str = _to_str(avastha_raw) if avastha_raw is not None else ""
        shad_val = _to_float(shadbala_raw) if shadbala_raw is not None else None
        if shad_val is None and strength_raw is not None:
            shad_val = _to_float(strength_raw)

        return name_en, {
            "sign":          sign_ru,
            "degree":        degree,
            "nakshatra":     nakshatra_str,
            "pada":          0,
            "retro":         bool(retro_raw),
            "combust":       bool(combust_raw),
            "vargottama":    bool(vargot_raw),
            "dignity":       dignity,
            "dispositor":    SIGN_RULERS.get(sign_ru, ""),
            "navamsha_sign": d9_sign,
            "d10_sign":      d10_sign,
            "avastha":       avastha_str,
            "shadbala":      round(shad_val, 1) if shad_val is not None else None,
            "lon_full":      lon,
            "house":         0,  # заполнится после получения Лагны
        }

    # ─── Per-planet collection (ПОСЛЕДОВАТЕЛЬНО, не параллельно!) ────────────
    # asyncio.gather по 9 планетам × 15 методов = 135 параллельных потоков →
    # race condition в VedAstro (все планеты возвращают одну долготу).
    # Однопоточный executor + последовательный обход планет исключают проблему.
    planets: dict[str, dict] = {}
    for name_en in PLANET_NAMES_EN:
        _, planet_data = await _collect_planet(name_en)
        planets[name_en] = planet_data
        # Диагностика: видно в Railway logs после деплоя — проверить разнообразие
        logger.info(
            "[LON] %-8s lon=%-7s sign=%-12s house=%s retro=%s",
            name_en,
            f"{planet_data.get('lon_full'):.1f}" if planet_data.get("lon_full") is not None else "None",
            planet_data.get("sign", ""),
            planet_data.get("house", 0),
            planet_data.get("retro"),
        )

    # ─── Lagna (House1) ──────────────────────────────────────────────────────
    fn_house_sign = getattr(Calculate, "HouseSignName", None) or getattr(Calculate, "HouseRasiSign", None)
    fn_house_const = getattr(Calculate, "HouseConstellation", None)
    h1_enum = getattr(HouseName, "House1", None) if HouseName else None

    lagna_sign_ru = ""
    lagna_nak = ""
    if fn_house_sign and h1_enum is not None:
        sign_raw = await _safe(fn_house_sign, h1_enum, birth_time_obj)
        lagna_sign_ru = _parse_sign(sign_raw) if sign_raw is not None else ""
    if fn_house_const and h1_enum is not None:
        nak_raw = await _safe(fn_house_const, h1_enum, birth_time_obj)
        lagna_nak = _to_str(nak_raw) if nak_raw is not None else ""

    lagna = {"sign": lagna_sign_ru, "degree": 0.0, "nakshatra": lagna_nak}

    # ─── Раставляем планетам номера домов от Лагны (whole-sign) ──────────────
    if lagna_sign_ru:
        for name_en, p in planets.items():
            if p.get("sign"):
                p["house"] = _house_from_lagna(p["sign"], lagna_sign_ru)

    # ─── Dasha (chart-level, takes just time) ────────────────────────────────
    fn_dasha = (getattr(Calculate, "DasaForNow", None)
                or getattr(Calculate, "DasaAtTime", None))
    dasha_raw = await _safe(fn_dasha, birth_time_obj) if fn_dasha else None
    dasha = parse_dasha(dasha_raw) if dasha_raw is not None else {"maha": "", "antar": "", "until": ""}

    # ─── Navamsha (D9) sign map ──────────────────────────────────────────────
    navamsha = {name: p.get("navamsha_sign", "") for name, p in planets.items()
                if p.get("navamsha_sign")}

    # ─── Shadbala / Avastha maps ─────────────────────────────────────────────
    shadbala = {name: p["shadbala"] for name, p in planets.items()
                if p.get("shadbala") is not None}
    avastha = {name: p["avastha"] for name, p in planets.items()
               if p.get("avastha")}

    # ─── Aspects (per-planet through PlanetsAspectingPlanet) ─────────────────
    aspects: list[dict] = []
    fn_aspect = getattr(Calculate, "PlanetsAspectingPlanet", None)
    if fn_aspect:
        async def _planet_aspects(target_en: str) -> list[dict]:
            target_enum = planet_enums.get(target_en)
            if target_enum is None:
                return []
            raw = await _safe(fn_aspect, target_enum, birth_time_obj)
            if raw is None:
                return []
            out = []
            for item in _iter_safe(raw):
                src_en = _to_str(item)
                if src_en in PLANET_NAMES_EN:
                    out.append({
                        "from": PLANET_NAMES_RU.get(src_en, src_en),
                        "to":   PLANET_NAMES_RU.get(target_en, target_en),
                        "kind": ASPECT_KINDS.get((src_en, target_en), ""),
                    })
            return out

        aspect_lists = await asyncio.gather(*[_planet_aspects(n) for n in PLANET_NAMES_EN])
        for lst in aspect_lists:
            aspects.extend(lst)

    # ─── Sade Sati: попробуем разные методы, fallback "не идёт" ──────────────
    sade_sati = "не идёт"
    fn_sade = (getattr(Calculate, "IsPlanetGocharaBindu", None)
               or getattr(Calculate, "SadeSatiSummary", None))
    if fn_sade:
        try:
            res = await va(fn_sade, birth_time_obj)
            if res:
                sade_sati = _to_str(res) or "не идёт"
        except Exception:
            pass

    # ─── Yogas: пока не реализованы (vedastro нет bulk-метода). Передадим пустой список.
    yogas: list[str] = []

    return {
        "planets":   planets,
        "lagna":     lagna,
        "navamsha":  navamsha,
        "dasha":     dasha,
        "shadbala":  shadbala,
        "avastha":   avastha,
        "aspects":   aspects,
        "yogas":     yogas,
        "sade_sati": sade_sati,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Parsers
# ──────────────────────────────────────────────────────────────────────────────

def _iter_safe(raw: Any) -> list:
    if raw is None:
        return []
    try:
        return list(raw)
    except TypeError:
        return []


def _planet_name_from(obj: Any) -> str:
    for attr in ("Name", "PlanetName", "Planet"):
        v = getattr(obj, attr, None)
        if v is not None:
            return _to_str(v)
    return ""


def parse_lagna(raw_all: Any) -> dict[str, Any]:
    """Extract ascendant data from AllPlanetData result."""
    result: dict[str, Any] = {"sign": "", "degree": 0.0, "nakshatra": ""}
    for item in _iter_safe(raw_all):
        name = _planet_name_from(item)
        if "lagna" in name.lower() or "ascendant" in name.lower():
            result["sign"] = _parse_sign(
                getattr(item, "Sign", None)
                or getattr(item, "RisingSign", None)
            )
            result["degree"] = round(
                _to_float(
                    getattr(item, "Longitude", None)
                    or getattr(item, "PlanetLongitude", None)
                ) % 30,
                2,
            )
            nak = getattr(item, "Nakshatra", None) or getattr(item, "PlanetNakshatra", None)
            result["nakshatra"] = _to_str(nak) if nak else ""
            break
    return result


def parse_planets(raw_all: Any) -> dict[str, dict]:
    """Parse AllPlanetData into {planet_en: {sign, house, degree, ...}}."""
    planets: dict[str, dict] = {}
    for item in _iter_safe(raw_all):
        name_en = _planet_name_from(item)
        if name_en not in PLANET_NAMES_EN:
            continue

        sign = _parse_sign(
            getattr(item, "Sign", None)
            or getattr(item, "PlanetSign", None)
        )
        house = _to_int(
            getattr(item, "HouseNumber", None)
            or getattr(item, "PlanetHouseNumber", None)
            or getattr(item, "House", None),
        )
        raw_deg = (
            getattr(item, "Longitude", None)
            or getattr(item, "PlanetLongitude", None)
            or getattr(item, "Degree", None)
        )
        degree = round(_to_float(raw_deg) % 30, 2)

        nak_obj = (
            getattr(item, "Nakshatra", None)
            or getattr(item, "PlanetNakshatra", None)
        )
        nakshatra = _to_str(nak_obj) if nak_obj else ""
        pada = _to_int(
            getattr(item, "Pada", None)
            or getattr(item, "NakshatraPada", None),
        )
        retro = bool(
            getattr(item, "IsRetrograde", False)
            or getattr(item, "PlanetIsRetrograde", False)
        )

        planets[name_en] = {
            "sign":       sign,
            "house":      house,
            "degree":     degree,
            "nakshatra":  nakshatra,
            "pada":       pada,
            "retro":      retro,
            "dispositor": SIGN_RULERS.get(sign, ""),
        }

    return planets


def parse_shadbala(raw: Any) -> dict[str, float]:
    """Parse shadbala into {planet_en: total_score}."""
    result: dict[str, float] = {}
    for item in _iter_safe(raw):
        name = _planet_name_from(item)
        if name not in PLANET_NAMES_EN:
            continue
        score = _to_float(
            getattr(item, "TotalStrength", None)
            or getattr(item, "ShadBalaScore", None)
            or getattr(item, "Total", None)
            or getattr(item, "Value", None)
        )
        result[name] = round(score, 1)
    return result


def parse_avastha(raw: Any) -> dict[str, str]:
    """Parse avastha into {planet_en: state_str}."""
    result: dict[str, str] = {}
    for item in _iter_safe(raw):
        name = _planet_name_from(item)
        if name not in PLANET_NAMES_EN:
            continue
        state = _to_str(
            getattr(item, "Avastha", None)
            or getattr(item, "PlanetAvastha", None)
            or getattr(item, "State", None)
        )
        result[name] = state
    return result


def parse_aspects(raw: Any) -> list[dict]:
    """Parse aspect list into [{"from": RU_name, "to": RU_name, "kind": str}]."""
    result: list[dict] = []
    for item in _iter_safe(raw):
        from_en = _to_str(
            getattr(item, "AspectingPlanet", None)
            or getattr(item, "FromPlanet", None)
            or getattr(item, "Planet1", None)
        )
        to_en = _to_str(
            getattr(item, "AspectedPlanet", None)
            or getattr(item, "ToPlanet", None)
            or getattr(item, "Planet2", None)
        )
        if from_en and to_en:
            kind = ASPECT_KINDS.get((from_en, to_en), "")
            result.append({
                "from": PLANET_NAMES_RU.get(from_en, from_en),
                "to":   PLANET_NAMES_RU.get(to_en, to_en),
                "kind": kind,
            })
    return result


def parse_navamsha(raw: Any) -> dict[str, str]:
    """Parse D9 chart into {planet_en: sign_ru}."""
    result: dict[str, str] = {}
    for item in _iter_safe(raw):
        name = _planet_name_from(item)
        if name not in PLANET_NAMES_EN:
            continue
        sign = _parse_sign(
            getattr(item, "Sign", None)
            or getattr(item, "PlanetSign", None)
        )
        result[name] = sign
    return result


def parse_dasha(raw: Any) -> dict[str, str]:
    """Parse Vimshottari dasha into {maha, antar, until}."""
    empty = {"maha": "", "antar": "", "until": ""}
    if raw is None:
        return empty

    maha_raw = (
        getattr(raw, "MahaDasha", None)
        or getattr(raw, "CurrentMahaDasha", None)
        or getattr(raw, "Maha", None)
    )
    antar_raw = (
        getattr(raw, "AntarDasha", None)
        or getattr(raw, "CurrentAntarDasha", None)
        or getattr(raw, "Antar", None)
    )
    until_raw = (
        getattr(raw, "AntarDashaEndTime", None)
        or getattr(raw, "EndDate", None)
        or getattr(raw, "Until", None)
    )

    maha_en  = _to_str(maha_raw)
    antar_en = _to_str(antar_raw)
    return {
        "maha":  PLANET_NAMES_RU.get(maha_en, maha_en),
        "antar": PLANET_NAMES_RU.get(antar_en, antar_en),
        "until": _to_str(until_raw),
    }


def parse_sade_sati(raw: Any) -> str:
    if raw is None:
        return "неизвестно"
    val = _to_str(raw)
    return val if val else "не идёт"


# ──────────────────────────────────────────────────────────────────────────────
#  Main build function  (called from onboarding handler)
# ──────────────────────────────────────────────────────────────────────────────

async def build_astro_data(
    birth_date: str,      # ДД.ММ.ГГГГ
    birth_time_str: str,  # ЧЧ:ММ
    lat: float,
    lon: float,
    tz_str: str,
    city_name: str,
) -> dict[str, Any]:
    """
    Build the raw + metrics portions of astro_json.
    Runs all VedAstro calls in parallel via asyncio.gather.

    Returns the full astro_json skeleton ready for User.set_astro().
    The 'features' and 'personality' keys are left empty — filled
    by FeatureExtractor and AI generation respectively.
    """
    if not _VA_AVAILABLE:
        raise RuntimeError("vedastro library not installed")

    # Build VedAstro Time object in a thread (it's synchronous)
    birth_time_obj = await asyncio.to_thread(
        _make_birth_time_sync,
        birth_date, birth_time_str, tz_str, lat, lon, city_name,
    )

    # Параллельный сбор + parsing внутри collect_all_metrics — возвращает уже
    # готовые структуры (не сырые VedAstro объекты)
    data = await collect_all_metrics(birth_time_obj)

    return {
        "raw": {
            "planets":   data["planets"],
            "ascendant": data["lagna"],
            "navamsha":  data["navamsha"],
            "dashamsha": {},   # populated lazily when dashamsha section is opened
            "dasha":     data["dasha"],
            "yogas":     data["yogas"],
            "sade_sati": data["sade_sati"],
        },
        "metrics": {
            "shadbala": data["shadbala"],
            "avastha":  data["avastha"],
            "aspects":  data["aspects"],
        },
        # Filled in by FeatureExtractor right after this call
        "features": {
            "dominant_themes":  [],
            "central_conflict": "",
        },
        # Filled in by AI generation during onboarding
        "personality": "",
        "card_text":   "",
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Natal code formatter  (no AI — instant, copyable)
# ──────────────────────────────────────────────────────────────────────────────

def format_natal_code(astro: dict[str, Any]) -> str:
    """
    Produce a human-readable Natal Code text from cached astro_json.
    Called when the user taps 'Натальный код' — no generation, instant.
    """
    raw     = astro.get("raw", {})
    planets = raw.get("planets", {})
    asc     = raw.get("ascendant", {})
    dasha   = raw.get("dasha", {})
    yogas   = raw.get("yogas", [])
    sati    = raw.get("sade_sati", "")
    metrics = astro.get("metrics", {})
    shadbala = metrics.get("shadbala", {})

    lines: list[str] = ["Натальная карта", ""]

    if asc.get("sign"):
        deg_str = f" {asc['degree']}°" if asc.get("degree") else ""
        lines.append(f"Лагна (Асцендент): {asc['sign']}{deg_str}")
        if asc.get("nakshatra"):
            lines.append(f"Накшатра Лагны: {asc['nakshatra']}")
        lines.append("")

    if planets:
        lines.append("Позиции планет")
        for en in PLANET_NAMES_EN:
            p = planets.get(en)
            if not p:
                continue
            ru        = PLANET_NAMES_RU[en]
            retro     = " Ретро" if p.get("retro") else ""
            deg_str   = f" {p['degree']}°" if p.get("degree") else ""
            house_str = f" | Дом {p['house']}" if p.get("house") else ""
            nak_str   = f" | {p['nakshatra']}" if p.get("nakshatra") else ""
            shad      = shadbala.get(en)
            shad_str  = f" | Шадбала: {shad}" if shad else ""
            lines.append(
                f"{ru}: {p['sign']}{deg_str}{retro}{house_str}{nak_str}{shad_str}"
            )
        lines.append("")

    if dasha.get("maha"):
        lines.append("Период Вимшоттари Даши")
        lines.append(f"Маха-даша: {dasha['maha']}")
        if dasha.get("antar"):
            lines.append(f"Антар-даша: {dasha['antar']}")
        if dasha.get("until"):
            lines.append(f"До: {dasha['until']}")
        lines.append("")

    if yogas:
        lines.append("Астрологические йоги")
        for y in yogas[:10]:
            if y:
                lines.append(f"- {y}")
        lines.append("")

    if sati and sati not in ("не идёт", "неизвестно", ""):
        lines.append(f"Саде-Сати: {sati}")

    return "\n".join(lines).strip()

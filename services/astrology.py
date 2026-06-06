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
import logging
from datetime import datetime
from typing import Any

import pytz
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

from config import VEDASTRO_API_KEY

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  VedAstro bootstrap
# ──────────────────────────────────────────────────────────────────────────────
_VA_AVAILABLE = False
Calculate = GeoLocation = Time = None  # type: ignore[assignment]

try:
    from vedastro import Calculate, GeoLocation, Time  # type: ignore[no-redef]
    _VA_AVAILABLE = True
    Calculate.SetAPIKey(VEDASTRO_API_KEY)
    logger.info("VedAstro initialised OK")
except ImportError:
    logger.warning("vedastro not installed — astrology features disabled")
except Exception as exc:
    logger.warning("VedAstro init error: %s", exc)

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
    """Run any synchronous VedAstro call in a thread pool."""
    return await asyncio.to_thread(_va_call, fn, *args)


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
    Fire all VedAstro calls in parallel.
    Each call runs in its own thread (VedAstro is blocking I/O).
    return_exceptions=True ensures one failure never kills the rest.

    [Phase 1] Verify every method name via Calculate.ListAPICalls().
    """
    tasks = [
        va(Calculate.AllPlanetData,    birth_time_obj),  # [verify]
        va(Calculate.PlanetSthanaBala,   birth_time_obj),  # [verify]
        va(Calculate.PlanetAvasta,    birth_time_obj),  # [verify]
        va(Calculate.PlanetAspects,    birth_time_obj),  # [verify]
        va(Calculate.NavamshaChart,    birth_time_obj),  # [verify]
        va(Calculate.DashamnshaChart,  birth_time_obj),  # [verify]
        va(Calculate.VimshottariDasha, birth_time_obj),  # [verify]
        va(Calculate.Yoga,             birth_time_obj),  # [verify]
        va(Calculate.SadeSati,         birth_time_obj),  # [verify]
    ]

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    result: dict[str, Any] = {}
    for key, value in zip(_METRIC_KEYS, raw_results):
        if isinstance(value, BaseException):
            logger.warning("VedAstro call %r failed: %s", key, value)
            result[key] = None
        else:
            result[key] = value

    return result


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

    # Parallel collection — one HTTP call per thread
    raw_results = await collect_all_metrics(birth_time_obj)

    # Parse each metric
    planets  = parse_planets(raw_results["all_planets"])
    lagna    = parse_lagna(raw_results["all_planets"])

    # Fallback: some VedAstro versions expose lagna via a separate call
    if not lagna["sign"]:
        try:
            lagna_sign_raw = await va(Calculate.LagnaSign, birth_time_obj)  # [verify]
            lagna["sign"] = _parse_sign(lagna_sign_raw)
        except Exception as exc:
            logger.warning("LagnaSign fallback failed: %s", exc)

    navamsha  = parse_navamsha(raw_results["navamsha"])
    dasha     = parse_dasha(raw_results["dasha"])
    shadbala  = parse_shadbala(raw_results["shadbala"])
    avastha   = parse_avastha(raw_results["avastha"])
    aspects   = parse_aspects(raw_results["aspects"])
    sade_sati = parse_sade_sati(raw_results["sade_sati"])

    yogas_raw = raw_results.get("yogas") or []
    yogas = [_to_str(y) for y in _iter_safe(yogas_raw) if _to_str(y)]

    return {
        "raw": {
            "planets":   planets,
            "ascendant": lagna,
            "navamsha":  navamsha,
            "dashamsha": {},   # populated lazily when dashamsha section is opened
            "dasha":     dasha,
            "yogas":     yogas,
            "sade_sati": sade_sati,
        },
        "metrics": {
            "shadbala": shadbala,
            "avastha":  avastha,
            "aspects":  aspects,
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

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
import re
from datetime import datetime
from typing import Any

import pytz
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

from config import VEDASTRO_API_KEY

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Константы (имена планет, переводы, управители знаков)
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



# ──────────────────────────────────────────────────────────────────────────────
#  Geocoding (импортируется handlers/settings.py — обязателен для онбординга)
# ──────────────────────────────────────────────────────────────────────────────
_geolocator = Nominatim(user_agent="rashi_bot_v2", timeout=10)
_tf = TimezoneFinder()


def _geocode_sync(city: str) -> tuple[float, float, str, str]:
    """Sync geocode. Returns (lat, lon, short_name, tz_str)."""
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
    """Async geocode: city name → (lat, lon, short_name, tz_str)."""
    return await asyncio.to_thread(_geocode_sync, city)


def _tz_offset_str(tz_name: str) -> str:
    """±HH:MM UTC offset для tz, с учётом DST."""
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


def _make_birth_time_sync(
    birth_date: str,      # ДД.ММ.ГГГГ
    birth_time_str: str,  # ЧЧ:ММ
    tz_str: str,
    lat: float,
    lon: float,
    city_name: str,
) -> Any:
    """Build a VedAstro Time object. Must run in a thread."""
    if not _VA_AVAILABLE:
        raise RuntimeError("vedastro library not installed")
    day, month, year = birth_date.split(".")
    offset = _tz_offset_str(tz_str)
    time_str = f"{birth_time_str} {day}/{month}/{year} {offset}"
    geo = GeoLocation(city_name, lon, lat)
    return Time(time_str, geo)


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
    Calculate = GeoLocation = Time = PlanetName = HouseName = None  # type: ignore
except Exception as exc:
    logger.warning("VedAstro init error: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
#  Single-thread executor: VedAstro biндинги не thread-safe.
#  Все вызовы выполняются на одном выделенном потоке последовательно.
# ──────────────────────────────────────────────────────────────────────────────
_VA_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="vedastro"
)


def _va_call(fn, *args):
    return fn(*args)


async def va(fn, *args) -> Any:
    """Запустить sync VedAstro вызов на выделенном single-thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_VA_EXECUTOR, _va_call, fn, *args)


# ──────────────────────────────────────────────────────────────────────────────
#  Кандидаты имён методов VedAstro (как в работающем Раши 1.0).
#  Имена различаются между версиями — пробуем по очереди, кешируем найденный.
# ──────────────────────────────────────────────────────────────────────────────
_HOUSE_PLANET_CANDIDATES: list[str] = [
    "HousePlanetIsIn", "PlanetHouseName", "PlanetHouse",
]

_CAND: dict[str, list[str]] = {
    "PlanetSign":  ["PlanetZodiacSign", "PlanetRasiName", "PlanetSignName",
                    "PlanetSign", "PlanetSignNiryana", "PlanetSignNirayana",
                    "PlanetRasi", "PlanetRasiNirayana", "PlanetRasiD1Sign",
                    "PlanetInSign"],
    "PlanetLon":   ["PlanetNirayanaLongitude", "PlanetSayanaLongitude",
                    "PlanetLongitude", "PlanetTropicalLongitude"],
    "PlanetConst": ["PlanetConstellation", "PlanetNakshatra",
                    "PlanetRulingConstellation"],
    "PlanetRetro": ["IsPlanetRetrograde", "PlanetRetrograde", "PlanetIsRetrograde"],
    "PlanetAva":   ["PlanetAvasta", "PlanetAvastha", "PlanetJagradadiAvastha"],
    "PlanetShad":  ["PlanetShadbalaPinda", "PlanetSthanaBala", "PlanetStrength"],
    "PlanetCombust": ["IsPlanetCombust", "PlanetCombust"],
    "PlanetVargot":  ["IsPlanetVargottama", "PlanetVargottama"],
    "PlanetD9":    ["PlanetNavamshaD9Sign", "NavamshaSignName"],
    "PlanetD10":   ["PlanetDashamamshaD10Sign", "DashamamshaSignName"],
    "HouseSign":   ["HouseSignName", "HouseRasiSign", "HouseRasi"],
    "HouseConst":  ["HouseConstellation", "HouseNakshatra"],
    "HouseLord":   ["LordOfHouse", "HouseLord", "HouseLordName"],
    "Dasha":       ["DasaForNow", "DasaAtTime", "DasaAtBirth", "CurrentDasa"],
}

_METHODS: dict[str, Any] = {}
_DIAG_LOGGED: bool = False


def _resolve_method(*names: str) -> Any | None:
    """Вернуть первый существующий callable метод Calculate из списка имён."""
    if Calculate is None:
        return None
    for n in names:
        fn = getattr(Calculate, n, None)
        if callable(fn):
            return fn
    return None


def _m(key: str) -> Any | None:
    """Получить кешированный метод по ключу из _CAND."""
    if key in _METHODS:
        return _METHODS[key]
    fn = _resolve_method(*_CAND.get(key, []))
    _METHODS[key] = fn
    return fn


def _resolve_all_methods() -> None:
    """Один раз разрешить все методы и залогировать что нашлось."""
    if not _VA_AVAILABLE or _METHODS:
        return
    for key, cands in _CAND.items():
        fn = _resolve_method(*cands)
        _METHODS[key] = fn
        if fn:
            logger.info("VedAstro: %s → Calculate.%s", key, getattr(fn, "__name__", "?"))
        else:
            logger.warning("VedAstro: %s НЕ найдено (пробовал: %s)",
                           key, ", ".join(cands))
    # Дополнительно — house-planet method
    hp_fn = _resolve_method(*_HOUSE_PLANET_CANDIDATES)
    if hp_fn:
        logger.info("VedAstro: PlanetHouse → Calculate.%s",
                    getattr(hp_fn, "__name__", "?"))
    else:
        logger.warning("VedAstro: PlanetHouse НЕ найдено")
    # PlanetName enum sample
    if PlanetName is not None:
        sample = {n: str(getattr(PlanetName, n, "?"))[:35]
                  for n in ["Sun", "Moon", "Mars", "Rahu", "Ketu"]}
        logger.info("[ENUM] PlanetName: %s", sample)


def _list_available_methods() -> list[str]:
    if Calculate is None:
        return []
    return sorted(n for n in dir(Calculate)
                  if not n.startswith("_") and callable(getattr(Calculate, n, None)))


# ──────────────────────────────────────────────────────────────────────────────
#  Низкоуровневая обёртка вызова с переборотом порядка аргументов.
#  В разных версиях vedastro методы принимают то (planet, time), то (time, planet).
#  Пробуем оба и берём тот, который вернул что-то осмысленное.
# ──────────────────────────────────────────────────────────────────────────────

async def _call_try_orders(fn: Any, *core_args) -> Any:
    """
    Вызвать fn пробуя оба порядка core_args. Возвращает первый результат,
    у которого либо число, либо непустая строка/объект. None если оба упали.
    """
    if fn is None:
        return None
    for args in [core_args, tuple(reversed(core_args))]:
        try:
            res = await va(fn, *args)
            if res is None:
                continue
            if isinstance(res, (int, float)) and not isinstance(res, bool):
                # Числа принимаем — даже 0.0 валиден (но lon=0 будет помечен на верхнем уровне)
                return res
            if str(res).strip():
                return res
        except Exception:
            continue
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  Type coercers — vedastro объекты бывают разных форматов.
# ──────────────────────────────────────────────────────────────────────────────

def _to_str(val: Any) -> str:
    if val is None:
        return ""
    for attr in ("ToString", "Name", "name"):
        m = getattr(val, attr, None)
        if callable(m):
            try:
                return str(m()).strip()
            except Exception:
                pass
        elif m is not None:
            return str(m).strip()
    return str(val).strip()


def _to_float(val: Any, default: float | None = None) -> float | None:
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


def _to_int(val: Any, default: int | None = None) -> int | None:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        # Может быть строка типа "House7" — вытащим число
        s = str(val)
        m = re.search(r"\d+", s)
        if m:
            try:
                return int(m.group())
            except ValueError:
                pass
        return default


def _to_bool(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    s = _to_str(val).lower()
    if s in ("true", "1", "yes", "retrograde", "r"):
        return True
    return bool(val)


def _parse_sign(raw: Any) -> str:
    s = _to_str(raw)
    return ZODIAC_EN_TO_RU.get(s, s)


def _parse_constellation(raw: Any) -> tuple[str, int | None]:
    s = _to_str(raw)
    if not s:
        return "", None
    pada: int | None = None
    pm = re.search(r"\b([1-4])\b", s)
    if pm:
        try:
            pada = int(pm.group(1))
        except ValueError:
            pada = None
    name_clean = re.sub(r"[\s\-–—]+(?:pada|quarter)?\s*[1-4].*", "", s,
                        flags=re.IGNORECASE).strip()
    return name_clean, pada


# ──────────────────────────────────────────────────────────────────────────────
#  Helper: компьютим знак из долготы (нираяна, Lahiri)
# ──────────────────────────────────────────────────────────────────────────────

ZODIAC_INDEX_EN: list[str] = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]


def _sign_from_longitude_ru(lon: float | None) -> str:
    if lon is None:
        return ""
    sign_en = ZODIAC_INDEX_EN[int(lon // 30) % 12]
    return ZODIAC_EN_TO_RU.get(sign_en, sign_en)


def _navamsha_from_longitude_ru(lon: float | None) -> str:
    if lon is None:
        return ""
    nav_lon = (lon * 9) % 360
    sign_en = ZODIAC_INDEX_EN[int(nav_lon // 30) % 12]
    return ZODIAC_EN_TO_RU.get(sign_en, sign_en)


def _d10_from_longitude_ru(lon: float | None) -> str:
    if lon is None:
        return ""
    sign_idx = int(lon // 30) % 12
    part = int((lon % 30) // 3)
    is_odd = (sign_idx % 2 == 0)
    start = sign_idx if is_odd else (sign_idx + 8) % 12
    sign_en = ZODIAC_INDEX_EN[(start + part) % 12]
    return ZODIAC_EN_TO_RU.get(sign_en, sign_en)


def _house_from_lagna(planet_sign_ru: str, lagna_sign_ru: str) -> int:
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


# Классические правила джйотиш для дигнити (если vedastro не отдал)
_EXALTATION_RU = {
    "Sun": "Овен", "Moon": "Телец", "Mars": "Козерог",
    "Mercury": "Дева", "Jupiter": "Рак", "Venus": "Рыбы", "Saturn": "Весы",
}
_DEBILITATION_RU = {
    "Sun": "Весы", "Moon": "Скорпион", "Mars": "Рак",
    "Mercury": "Рыбы", "Jupiter": "Козерог", "Venus": "Дева", "Saturn": "Овен",
}
_OWN_SIGN_RU = {
    "Sun":     ["Лев"],
    "Moon":    ["Рак"],
    "Mars":    ["Овен", "Скорпион"],
    "Mercury": ["Близнецы", "Дева"],
    "Jupiter": ["Стрелец", "Рыбы"],
    "Venus":   ["Телец", "Весы"],
    "Saturn":  ["Козерог", "Водолей"],
}
_FRIEND_SIGN_RU = {
    "Sun":     ["Близнецы", "Стрелец", "Рыбы", "Козерог"],
    "Moon":    ["Близнецы", "Дева", "Лев"],
    "Mars":    ["Близнецы", "Лев", "Стрелец", "Козерог"],
    "Mercury": ["Овен", "Козерог", "Водолей"],
    "Jupiter": ["Овен", "Близнецы", "Лев", "Рак"],
    "Venus":   ["Рак", "Скорпион", "Стрелец"],
    "Saturn":  ["Дева", "Близнецы", "Рыбы"],
}


def _compute_dignity_from_sign(planet_en: str, sign_ru: str) -> str:
    if not sign_ru:
        return "нейтральный знак"
    if _EXALTATION_RU.get(planet_en) == sign_ru:
        return "экзальтация"
    if _DEBILITATION_RU.get(planet_en) == sign_ru:
        return "падение"
    if sign_ru in _OWN_SIGN_RU.get(planet_en, []):
        return "своя обитель"
    if sign_ru in _FRIEND_SIGN_RU.get(planet_en, []):
        return "знак друга"
    return "нейтральный знак"


# ──────────────────────────────────────────────────────────────────────────────
#  Per-planet collection — копия рабочей логики из Раши 1.0
# ──────────────────────────────────────────────────────────────────────────────

async def _planet_house(enum: Any, t: Any) -> int | None:
    """Дом планеты — оба порядка аргументов."""
    fn = _resolve_method(*_HOUSE_PLANET_CANDIDATES)
    if fn is None:
        return None
    for args in [(enum, t), (t, enum)]:
        try:
            res = await va(fn, *args)
            h = _to_int(res)
            if h is not None and 1 <= h <= 12:
                return h
        except Exception:
            continue
    return None


async def _get_one_planet(name_en: str, enum: Any, t: Any) -> dict:
    """Все поля одной планеты через try-both-orders для каждого вызова."""
    sign_fn    = _m("PlanetSign")
    const_fn   = _m("PlanetConst")
    lon_fn     = _m("PlanetLon")
    retro_fn   = _m("PlanetRetro")
    ava_fn     = _m("PlanetAva")
    shad_fn    = _m("PlanetShad")
    combust_fn = _m("PlanetCombust")
    vargot_fn  = _m("PlanetVargot")
    d9_fn      = _m("PlanetD9")
    d10_fn     = _m("PlanetD10")

    sign_raw, house_raw, const_raw, lon_raw, retro_raw, ava_raw, shad_raw, combust_raw, vargot_raw, d9_raw, d10_raw = await asyncio.gather(
        _call_try_orders(sign_fn,    enum, t),
        _planet_house(enum, t),
        _call_try_orders(const_fn,   enum, t),
        _call_try_orders(lon_fn,     enum, t),
        _call_try_orders(retro_fn,   enum, t),
        _call_try_orders(ava_fn,     enum, t),
        _call_try_orders(shad_fn,    enum, t),
        _call_try_orders(combust_fn, enum, t),
        _call_try_orders(vargot_fn,  enum, t),
        _call_try_orders(d9_fn,      enum, t),
        _call_try_orders(d10_fn,     enum, t),
        return_exceptions=True,
    )

    # Защита от исключений в gather (return_exceptions=True)
    def _ok(val):
        return None if isinstance(val, BaseException) else val

    sign_en = _to_str(_ok(sign_raw))
    house   = _ok(house_raw)
    nak, pada = _parse_constellation(_ok(const_raw))
    lon     = _to_float(_ok(lon_raw))
    retro   = _to_bool(_ok(retro_raw))
    ava_str = _to_str(_ok(ava_raw))
    shad    = _to_float(_ok(shad_raw))
    combust = _to_bool(_ok(combust_raw))
    vargot  = _to_bool(_ok(vargot_raw))
    d9_raw_str  = _to_str(_ok(d9_raw))
    d10_raw_str = _to_str(_ok(d10_raw))

    # Fallback: если знака нет — вычисляем из долготы
    sign_ru = _parse_sign(sign_en) if sign_en else _sign_from_longitude_ru(lon)
    degree = round(lon % 30, 2) if lon is not None else 0.0

    # D9 / D10 — берём от API, иначе математически
    d9_sign  = _parse_sign(d9_raw_str)  if d9_raw_str  else _navamsha_from_longitude_ru(lon)
    d10_sign = _parse_sign(d10_raw_str) if d10_raw_str else _d10_from_longitude_ru(lon)

    # Защита: Sun/Moon никогда не ретроградные; Rahu/Ketu всегда
    if name_en in ("Sun", "Moon"):
        retro = False
    elif name_en in ("Rahu", "Ketu"):
        retro = True

    dignity = _compute_dignity_from_sign(name_en, sign_ru)

    logger.info(
        "[DIAG] %-8s lon=%-7s sign=%-12s house=%s retro=%s",
        name_en,
        f"{lon:.1f}" if lon is not None else "None",
        sign_ru or "(empty)",
        house if house else "?",
        retro,
    )

    return {
        "sign":          sign_ru or "",
        "degree":        degree,
        "nakshatra":     nak or "",
        "pada":          pada or 0,
        "retro":         retro,
        "combust":       combust,
        "vargottama":    vargot,
        "dignity":       dignity,
        "dispositor":    SIGN_RULERS.get(sign_ru, ""),
        "navamsha_sign": d9_sign,
        "d10_sign":      d10_sign,
        "avastha":       ava_str or "",
        "shadbala":      round(shad, 1) if shad is not None else None,
        "lon_full":      lon,
        "house":         house if house else 0,
    }


def parse_dasha(raw: Any) -> dict[str, str]:
    """Parse Vimshottari dasha → {maha, antar, until}."""
    empty = {"maha": "", "antar": "", "until": ""}
    if raw is None:
        return empty
    maha_raw  = (getattr(raw, "MahaDasha", None) or
                 getattr(raw, "CurrentMahaDasha", None) or
                 getattr(raw, "Maha", None))
    antar_raw = (getattr(raw, "AntarDasha", None) or
                 getattr(raw, "CurrentAntarDasha", None) or
                 getattr(raw, "Antar", None))
    until_raw = (getattr(raw, "AntarDashaEndTime", None) or
                 getattr(raw, "EndDate", None) or
                 getattr(raw, "Until", None))
    maha_en  = _to_str(maha_raw)
    antar_en = _to_str(antar_raw)
    return {
        "maha":  PLANET_NAMES_RU.get(maha_en, maha_en),
        "antar": PLANET_NAMES_RU.get(antar_en, antar_en),
        "until": _to_str(until_raw),
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Main collection function (called from build_astro_data)
# ──────────────────────────────────────────────────────────────────────────────

async def collect_all_metrics(birth_time_obj: Any) -> dict[str, Any]:
    """Собрать все данные карты. Возвращает уже-распарсенный dict."""
    if not _VA_AVAILABLE:
        return {
            "planets": {}, "lagna": {"sign": "", "degree": 0.0, "nakshatra": ""},
            "navamsha": {}, "dasha": {"maha": "", "antar": "", "until": ""},
            "shadbala": {}, "avastha": {}, "aspects": [], "yogas": [], "sade_sati": "",
        }

    _resolve_all_methods()

    # ─── Встроенная диагностика: одна на сессию ─────────────────────────────
    # Логгирует что vedastro возвращает для Sun vs Mars в обоих порядках для
    # КАЖДОГО метода-знака. По логам видно: какой метод даёт ДИФФЕРЕНЦИРОВАННУЮ
    # информацию для разных планет и в каком порядке аргументов.
    global _DIAG_LOGGED
    if not _DIAG_LOGGED:
        _DIAG_LOGGED = True
        sun_enum = getattr(PlanetName, "Sun", None) if PlanetName else None
        mars_enum = getattr(PlanetName, "Mars", None) if PlanetName else None
        logger.info("[DIAGVA] === START vedastro diagnostic ===")
        logger.info("[DIAGVA] Sun enum: %r", sun_enum)
        logger.info("[DIAGVA] Mars enum: %r", mars_enum)
        logger.info("[DIAGVA] Sun == Mars: %s", sun_enum == mars_enum)
        # Все методы для разбивки данных по планетам
        for method_name in [
            "PlanetNirayanaLongitude",
            "PlanetSayanaLongitude",
            "PlanetZodiacSign",
            "PlanetRasiName",
            "PlanetRasiD1Sign",
            "PlanetRasiNirayana",
            "PlanetSign",
            "PlanetSignName",
            "PlanetInSign",
        ]:
            fn = getattr(Calculate, method_name, None)
            if fn is None:
                logger.info("[DIAGVA] %s: НЕ существует", method_name)
                continue
            # Sun + (planet, time)
            try:
                r_sun_pt = await va(fn, sun_enum, birth_time_obj)
                r_sun_pt_str = _to_str(r_sun_pt)[:30]
            except Exception as exc:
                r_sun_pt_str = f"ERR({str(exc)[:25]})"
            # Mars + (planet, time)
            try:
                r_mars_pt = await va(fn, mars_enum, birth_time_obj)
                r_mars_pt_str = _to_str(r_mars_pt)[:30]
            except Exception as exc:
                r_mars_pt_str = f"ERR({str(exc)[:25]})"
            # Sun + (time, planet)
            try:
                r_sun_tp = await va(fn, birth_time_obj, sun_enum)
                r_sun_tp_str = _to_str(r_sun_tp)[:30]
            except Exception as exc:
                r_sun_tp_str = f"ERR({str(exc)[:25]})"
            # Mars + (time, planet)
            try:
                r_mars_tp = await va(fn, birth_time_obj, mars_enum)
                r_mars_tp_str = _to_str(r_mars_tp)[:30]
            except Exception as exc:
                r_mars_tp_str = f"ERR({str(exc)[:25]})"
            # Различия — главное что нам нужно знать
            diff_pt = r_sun_pt_str != r_mars_pt_str
            diff_tp = r_sun_tp_str != r_mars_tp_str
            logger.info(
                "[DIAGVA] %s | (p,t): Sun=%-20s Mars=%-20s diff=%s | (t,p): Sun=%-20s Mars=%-20s diff=%s",
                method_name, r_sun_pt_str, r_mars_pt_str, diff_pt,
                r_sun_tp_str, r_mars_tp_str, diff_tp,
            )
        logger.info("[DIAGVA] === END vedastro diagnostic ===")
    # ────────────────────────────────────────────────────────────────────────

    # Подготовим список планет с их enum-значениями
    planet_list: list[tuple[str, Any]] = []
    for name_en in PLANET_NAMES_EN:
        enum = getattr(PlanetName, name_en, None) if PlanetName else None
        planet_list.append((name_en, enum))

    # ─── Собираем планеты ПОСЛЕДОВАТЕЛЬНО (single-thread executor → точно безопасно)
    planets: dict[str, dict] = {}
    for name_en, enum in planet_list:
        if enum is None:
            planets[name_en] = {}
            continue
        try:
            planets[name_en] = await _get_one_planet(name_en, enum, birth_time_obj)
        except Exception as exc:
            logger.warning("Position %s failed: %s", name_en, exc)
            planets[name_en] = {}

    # ─── Лагна (House1) ─────────────────────────────────────────────────────
    h_sign_fn = _m("HouseSign")
    h1_enum = getattr(HouseName, "House1", None) if HouseName else None
    lagna_sign_ru = ""
    if h_sign_fn and h1_enum is not None:
        raw = await _call_try_orders(h_sign_fn, h1_enum, birth_time_obj)
        lagna_sign_ru = _parse_sign(raw) if raw is not None else ""

    h_const_fn = _m("HouseConst")
    lagna_nak = ""
    if h_const_fn and h1_enum is not None:
        raw = await _call_try_orders(h_const_fn, h1_enum, birth_time_obj)
        lagna_nak = _to_str(raw) if raw is not None else ""

    lagna = {"sign": lagna_sign_ru, "degree": 0.0, "nakshatra": lagna_nak}
    logger.info("[DIAG] Lagna: sign=%s nak=%s", lagna_sign_ru or "(empty)", lagna_nak or "(empty)")

    # ─── Расставляем дома планетам whole-sign от Лагны (если API не дал) ─────
    if lagna_sign_ru:
        for name_en, p in planets.items():
            if not p.get("house") and p.get("sign"):
                p["house"] = _house_from_lagna(p["sign"], lagna_sign_ru)

    # ─── Даша (chart-level — только time) ───────────────────────────────────
    dasha_fn = _m("Dasha")
    dasha = {"maha": "", "antar": "", "until": ""}
    if dasha_fn is not None:
        try:
            raw = await va(dasha_fn, birth_time_obj)
            if raw is not None:
                dasha_parsed = parse_dasha(raw)
                if dasha_parsed:
                    dasha = dasha_parsed
        except Exception as exc:
            logger.debug("Dasha failed: %s", exc)

    # ─── Сводные структуры ──────────────────────────────────────────────────
    navamsha = {n: p.get("navamsha_sign", "") for n, p in planets.items()
                if p.get("navamsha_sign")}
    shadbala = {n: p["shadbala"] for n, p in planets.items()
                if p.get("shadbala") is not None}
    avastha = {n: p["avastha"] for n, p in planets.items()
               if p.get("avastha")}

    return {
        "planets":   planets,
        "lagna":     lagna,
        "navamsha":  navamsha,
        "dasha":     dasha,
        "shadbala":  shadbala,
        "avastha":   avastha,
        "aspects":   [],
        "yogas":     [],
        "sade_sati": "не идёт",
    }


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

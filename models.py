import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime,
    Float, Integer, String, Text,
)
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config import (
    DATABASE_URL,
    WEEKLY_FOCUS_DEFAULT_DAY,
    WEEKLY_FOCUS_DEFAULT_HOUR,
)

# ──────────────────────────────────────────────────────────────────────────────
#  Engine & session factory
# ──────────────────────────────────────────────────────────────────────────────
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

AsyncSession = async_sessionmaker(engine, expire_on_commit=False)


# ──────────────────────────────────────────────────────────────────────────────
#  Declarative base
# ──────────────────────────────────────────────────────────────────────────────
class Base(AsyncAttrs, DeclarativeBase):
    pass


# ──────────────────────────────────────────────────────────────────────────────
#  User
# ──────────────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    # Identity
    telegram_id = Column(BigInteger, primary_key=True, index=True)
    username    = Column(String,     nullable=True)
    name        = Column(String,     nullable=True)
    gender      = Column(String(10), nullable=True)   # "male" | "female"

    # Birth data
    birth_date  = Column(String(10), nullable=True)   # ДД.ММ.ГГГГ
    birth_time  = Column(String(5),  nullable=True)   # ЧЧ:ММ
    birth_city  = Column(String,     nullable=True)
    birth_lat   = Column(Float,      nullable=True)
    birth_lon   = Column(Float,      nullable=True)
    timezone    = Column(String,     nullable=True)   # e.g. "Europe/Moscow"

    # Astrology cache — single JSON blob, computed once, updated on birth data change.
    # Structure:
    #   { "raw": {...}, "metrics": {...}, "features": {...},
    #     "personality": "...", "card_text": "..." }
    astro_json  = Column(Text, nullable=True)

    # Onboarding
    onboarding_complete = Column(Boolean, default=False)

    # Premium
    is_premium    = Column(Boolean,  default=False)
    premium_until = Column(DateTime, nullable=True)

    # Questions
    questions_balance = Column(Integer,  default=0)
    questions_month   = Column(String(7), nullable=True)   # YYYY-MM

    # Freemium gate
    free_section_used = Column(String(40), nullable=True)  # reading key
    paywall_shown_at  = Column(DateTime,   nullable=True)

    # Scheduler preferences
    weekly_focus_day  = Column(Integer, default=WEEKLY_FOCUS_DEFAULT_DAY)
    weekly_focus_hour = Column(Integer, default=WEEKLY_FOCUS_DEFAULT_HOUR)
    notifications_enabled = Column(Boolean, default=True)

    # Timestamps
    created_at     = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.utcnow)

    # ── Properties ────────────────────────────────────────────────────────
    @property
    def is_premium_active(self) -> bool:
        if not self.is_premium:
            return False
        if self.premium_until is None:
            return False
        return self.premium_until > datetime.utcnow()

    def get_astro(self) -> dict[str, Any] | None:
        if not self.astro_json:
            return None
        try:
            return json.loads(self.astro_json)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_astro(self, data: dict[str, Any]) -> None:
        self.astro_json = json.dumps(data, ensure_ascii=False)

    def __repr__(self) -> str:
        return f"<User id={self.telegram_id} name={self.name!r}>"


# ──────────────────────────────────────────────────────────────────────────────
#  Forecast  (weekly focus cache + delivery tracking)
# ──────────────────────────────────────────────────────────────────────────────
class Forecast(Base):
    __tablename__ = "forecasts"

    id          = Column(Integer,    primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    period_key  = Column(String(20), nullable=False)   # "YYYY-Www"  ISO week
    content     = Column(Text,       nullable=False)
    sent        = Column(Boolean,    default=False)
    created_at  = Column(DateTime,   default=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────────────
#  Payment  (analytics ledger, not authoritative for access decisions)
# ──────────────────────────────────────────────────────────────────────────────
class Payment(Base):
    __tablename__ = "payments"

    id          = Column(Integer,    primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    product     = Column(String(40), nullable=False)   # key from PRODUCTS
    stars       = Column(Integer,    nullable=False)
    created_at  = Column(DateTime,   default=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────────────
#  Schema initialisation
# ──────────────────────────────────────────────────────────────────────────────
async def init_db() -> None:
    """Create all tables that do not yet exist. Safe to call on every startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

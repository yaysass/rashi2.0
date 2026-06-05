"""
services/ai.py
==============
Асинхронный клиент Anthropic + санитайзер форматирования.

generate() — единственная точка входа для всех AI-вызовов.
Каждый ответ Claude проходит через sanitize() до отправки.
"""
from __future__ import annotations

import logging
import re

from anthropic import AsyncAnthropic, APIError, APITimeoutError, RateLimitError

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from core.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Singleton async client
_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ──────────────────────────────────────────────────────────────────────────────
#  Санитайзер — гарантия в коде
# ──────────────────────────────────────────────────────────────────────────────
ALLOWED_EMOJI: frozenset[str] = frozenset({
    "🤍", "🌑", "🪐", "🕯", "🕯️", "✨", "🪞",
    "🗝", "🗝️", "⏳", "⚖", "⚖️", "🚪", "📜",
    "✒", "✒️", "🌿", "🕊", "🕊️",
})

_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF"
    r"\U00002600-\U000027BF"
    r"\U0001F1E6-\U0001F1FF"
    r"\U00002190-\U000021FF"
    r"\U00002B00-\U00002BFF"
    r"\U0001F900-\U0001F9FF"
    r"\U0000FE00-\U0000FE0F]+"
    r"\uFE0F?"
)


def _filter_emoji(text: str, max_count: int = 3) -> str:
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        e = m.group(0)
        base = e.rstrip("\uFE0F")
        if (e in ALLOWED_EMOJI or base in ALLOWED_EMOJI) and count < max_count:
            count += 1
            return e
        return ""

    return _EMOJI_RE.sub(repl, text)


def sanitize(text: str, max_emoji: int = 3) -> str:
    """
    Post-process Claude output:
      - Strip long/medium dashes → hyphen
      - Remove markdown headers
      - Remove ** bold markers (keep <b> HTML)
      - Remove horizontal rule lines
      - Filter emoji to allowed set
      - Collapse excess blank lines / trailing spaces
    """
    if not text:
        return text

    # Dashes
    text = text.replace("\u2014", "-").replace("\u2013", "-").replace("\u2015", "-")

    # Markdown headers
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)

    # Bold via asterisks or underscores
    text = text.replace("**", "").replace("__", "")

    # Inline single asterisk italics (not HTML)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)

    # Horizontal rules
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Emoji filter
    text = _filter_emoji(text, max_emoji)

    # Normalise whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.!?:;])", r"\1", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()

    # Length warnings
    length = len(text)
    if length < 900:
        logger.warning("Разбор короче 900 знаков (%d)", length)
    elif length > 1700:
        logger.warning("Разбор длиннее 1700 знаков (%d)", length)

    return text


# ──────────────────────────────────────────────────────────────────────────────
#  Главная функция генерации
# ──────────────────────────────────────────────────────────────────────────────

async def generate(
    prompt: str,
    system: str | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.7,
) -> str:
    """
    Call Claude and return sanitised text.

    Raises:
        RuntimeError — on API / network failure (caller shows TEXTS error message)
    """
    system_prompt = system or SYSTEM_PROMPT

    try:
        msg = await _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
    except RateLimitError as exc:
        logger.error("Claude rate limit: %s", exc)
        raise RuntimeError("rate_limit") from exc
    except APITimeoutError as exc:
        logger.error("Claude timeout: %s", exc)
        raise RuntimeError("timeout") from exc
    except APIError as exc:
        logger.error("Claude API error: %s", exc)
        raise RuntimeError("api_error") from exc
    except Exception as exc:
        logger.error("Claude unexpected error: %s", exc, exc_info=True)
        raise RuntimeError("unknown") from exc

    if msg.stop_reason == "max_tokens":
        logger.warning("Ответ обрезан по max_tokens (section prompt may be too long)")

    raw = msg.content[0].text.strip() if msg.content else ""
    return sanitize(raw, max_emoji=3)

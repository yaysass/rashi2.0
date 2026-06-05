# Раши 2.1 — Telegram-бот ведического астролога

Персональный бот на базе Джйотиша: натальные разборы, тематические секции,
Продвинутый Джйотиш, Фокус недели, вопросы, синастрия. Монетизация — Telegram Stars.

---

## Стек

| Слой | Библиотека |
|------|-----------|
| Telegram Bot | python-telegram-bot 21.x |
| AI (Claude) | anthropic (AsyncAnthropic) |
| БД | SQLAlchemy 2.x + aiosqlite / asyncpg |
| Астрология | VedAstro (Python-обёртка) |
| Геокодинг | geopy (Nominatim) + timezonefinder |
| Планировщик | APScheduler 3.x (AsyncIOScheduler) |

---

## Структура файлов

Скопируй файлы в соответствии с таблицей:

```
rashi_bot/                          ← корень проекта (любое имя)
│
├── main.py                         ← ШАГ 5  (outputs/main.py)
├── config.py                       ← ШАГ 0  (project/config.py)
├── texts.py                        ← ШАГ 0  (project/texts.py)
│
├── db/
│   ├── __init__.py                 ← пустой файл
│   └── models.py                   ← ШАГ 0  (project/models.py)
│
├── services/
│   ├── __init__.py                 ← пустой файл
│   ├── astrology.py                ← ШАГ 0  (project/astrology.py)
│   ├── ai.py                       ← ШАГ 0  (project/ai.py)
│   └── extractor.py                ← ШАГ 0  (project/extractor.py)
│
├── core/
│   ├── __init__.py                 ← пустой файл
│   ├── prompts.py                  ← ШАГ 0  (project/prompts.py)
│   ├── access.py                   ← ШАГ 3  (outputs/core/access.py)
│   └── catalog.py                  ← ШАГ 3  (outputs/core/catalog.py)
│
├── handlers/
│   ├── __init__.py                 ← пустой файл
│   ├── onboarding.py               ← ШАГ 0  (project/onboarding.py)
│   ├── menu.py                     ← ШАГ 3  (outputs/handlers/menu.py)
│   ├── payments.py                 ← ШАГ 4  (outputs/handlers/payments.py)
│   ├── premium.py                  ← ШАГ 4  (outputs/handlers/premium.py)
│   ├── questions.py                ← ШАГ 4  (outputs/handlers/questions.py)
│   ├── specials.py                 ← ШАГ 5  (outputs/handlers/specials.py)
│   └── settings.py                 ← ШАГ 5  (outputs/handlers/settings.py)
│
├── keyboards/
│   ├── __init__.py                 ← пустой файл
│   └── keyboards.py                ← ШАГ 3  (outputs/keyboards/keyboards.py)
│
├── scheduler/
│   ├── __init__.py                 ← пустой файл
│   └── jobs.py                     ← ШАГ 5  (outputs/scheduler/jobs.py)
│
├── requirements.txt
├── .env                            ← создать вручную из .env.example (не коммитить!)
├── .env.example
├── Procfile                        ← для Railway
└── runtime.txt                     ← для Railway
```

> **`__init__.py`** — создай пустые файлы командой:
> ```bash
> touch db/__init__.py services/__init__.py core/__init__.py \
>       handlers/__init__.py keyboards/__init__.py scheduler/__init__.py
> ```

---

## 1. Клонирование и окружение

```bash
# Создать папку и войти в неё
mkdir rashi_bot && cd rashi_bot

# Виртуальное окружение (рекомендуется Python 3.12)
python3.12 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows

# Установить зависимости
pip install -r requirements.txt
```

---

## 2. Переменные окружения

Скопируй шаблон и заполни значения:

```bash
cp .env.example .env
```

`.env.example`:

```dotenv
# ── Telegram ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN=         # Bot token от @BotFather

# ── Anthropic / Claude ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY=      # API-ключ с platform.anthropic.com
CLAUDE_MODEL=claude-sonnet-4-6   # или claude-opus-4-6 для качества выше

# ── VedAstro ────────────────────────────────────────────────────────────────
VEDASTRO_API_KEY=       # ключ с vedastro.org (если требует)

# ── База данных ─────────────────────────────────────────────────────────────
DATABASE_URL=sqlite+aiosqlite:///bot.db   # SQLite локально
# DATABASE_URL=postgresql+asyncpg://user:pass@host/dbname  # Railway Postgres

# ── Администраторы (опционально) ────────────────────────────────────────────
ADMIN_IDS=              # Telegram ID через запятую, напр: 123456789,987654321

# ── Приветственная картинка (опционально) ───────────────────────────────────
WELCOME_IMAGE=          # file_id или URL изображения для /start
```

> **Важно:** `.env` не должен попасть в репозиторий. Добавь его в `.gitignore`.

---

## 3. Запуск локально

```bash
python main.py
```

Бот использует **long polling**. При первом запуске автоматически создаются таблицы БД.

Убедись, что VedAstro установлен корректно:

```bash
python -c "from vedastro import Calculate, GeoLocation, Time; print('VedAstro OK')"
```

Если VedAstro недоступен — бот запустится, но расчёт карт будет заблокирован
(onboarding завершится с ошибкой при попытке расчёта).

---

## 4. Деплой на Railway

### 4.1 Подготовка файлов

**`Procfile`**:
```
worker: python main.py
```

**`runtime.txt`**:
```
python-3.12.4
```

### 4.2 Шаги деплоя

1. Залей проект на GitHub.
2. Создай новый проект на [railway.app](https://railway.app).
3. Подключи GitHub-репозиторий.
4. Добавь **PostgreSQL** через `+ New → Database → PostgreSQL`.
5. Скопируй `DATABASE_URL` из настроек PostgreSQL и вставь в переменные проекта.
6. В разделе **Variables** добавь все переменные из `.env.example`.
7. Railway автоматически задеплоит при пуше в `main`.

> **Разрешить исходящие на** `api.vedastro.org` — в Railway это разрешено по умолчанию.

---

## 5. Первый запуск — что проверить

| Проверка | Ожидаемый результат |
|----------|---------------------|
| `/start` | Приветственное сообщение + кнопка «Начать» |
| Онбординг до конца | Разбор личности в чат (через 15-30 сек) |
| Кнопка «Любовь и отношения» | Разбор с кнопками «Перегенерировать» + «В меню» |
| Повторный клик другой темы | Сообщение о пейволле + кнопка «Оформить Премиум» |
| Оплата 500 ⭐ | Сообщение «Премиум активирован», замки в меню исчезают |
| «Задать вопрос» при балансе 0 | Магазин вопросов (1 / 3 / 10) |
| `/menu` | Главное меню с актуальными замками |

---

## 6. Архитектурные заметки

- **Все строки** только из `texts.py`. Никаких хардкодов в handlers.
- **Доступ** управляется исключительно через `core/access.py`.
- **Каталог разборов** — `core/catalog.py`. Добавить новый раздел = одна строка в `READINGS`.
- **Порядок регистрации** хендлеров в `main.py` критичен: `menu.register()` — последний,
  т.к. содержит catch-all паттерны `section:*` и `regen:*`.
- **APScheduler** стартует через `post_init` ApplicationBuilder — правильная интеграция
  с asyncio event loop PTB v21.
- **БД инициализируется** в `post_init` (`init_db()`), таблицы создаются при первом запуске.
- **Telegram Stars** (`currency="XTR"`, `provider_token=""`) — только PTB v21+.

---

## 7. Часто задаваемые вопросы

**Q: VedAstro не устанавливается через pip?**
Убедись, что у тебя Python 3.12 и актуальная версия пакета.
Проверь: `pip install vedastro --upgrade`. Если пакета нет на PyPI, смотри
официальную документацию vedastro.org для альтернативных способов установки.

**Q: Тесты Stars не работают локально?**
Для тестирования Telegram Stars нужен продакшн-бот (не работает с Test Mode).
Используй небольшую сумму реальных Stars для проверки полного цикла.

**Q: Как добавить новый раздел в меню?**
1. Добавить запись в `READINGS` в `core/catalog.py`.
2. Добавить `_SECTION_INSTRUCTIONS[key]` в `core/prompts.py`.
3. Добавить кнопку в `keyboards/keyboards.py` → `main_menu()`.
4. Добавить строки в `texts.py`.

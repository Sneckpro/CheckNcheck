# checkNcheck

Telegram бот для трекинга расходов с AI-парсингом чеков.

## Возможности

- **Текст** — `кофе 350` или `Zara 5000 динар одежда`
- **Фото чека** — GPT Vision читает сумму, магазин, категорию
- **Пересылка** — форвард подтверждения заказа (Wolt, Bolt, etc.)
- **Отчёты** — по дням, неделям, месяцам с категориями

## Команды

| Команда | Описание |
|---------|----------|
| `/today` | Расходы за сегодня |
| `/week` | Расходы за неделю |
| `/month` | Отчёт за месяц по категориям |
| `/history [N]` | Последние N расходов |
| `/delete <id>` | Удалить запись |
| `/currency RSD` | Валюта по умолчанию |
| `/timezone CET` | Часовой пояс |

## Запуск

```bash
cp .env.example .env
# Заполни TELEGRAM_BOT_TOKEN и OPENAI_API_KEY

# Локально
pip install -r requirements.txt
python bot.py

# Docker
docker compose up --build -d
```

## Переменные окружения

| Переменная | Обязательна | Описание |
|-----------|:-----------:|----------|
| `TELEGRAM_BOT_TOKEN` | да | Токен от @BotFather |
| `OPENAI_API_KEY` | да | Ключ OpenAI API |
| `ALLOWED_USER_IDS` | нет | Белый список Telegram ID через запятую |
| `DB_PATH` | нет | Путь к SQLite (по умолчанию `./expenses.db`) |

## Деплой

Push в `main` → GitHub Actions → SSH на VDS → `docker compose up --build -d`.

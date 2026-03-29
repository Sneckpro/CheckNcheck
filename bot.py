import os
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import (
    init_db, save_expense, get_expenses, get_recent_expenses, delete_expense,
    get_default_currency, set_default_currency, get_timezone, set_timezone,
)
from ai import (
    parse_receipt_photo,
    parse_text_expense,
    parse_forwarded_expense,
    generate_expense_report,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ALLOWED_USER_IDS: set[int] = set()
raw = os.getenv("ALLOWED_USER_IDS", "")
if raw:
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw.split(",") if uid.strip()}

TIMEZONE_ALIASES = {
    "cet": "Europe/Berlin", "cest": "Europe/Berlin",
    "msk": "Europe/Moscow",
    "est": "America/New_York", "edt": "America/New_York",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "gmt": "UTC", "utc": "UTC",
}

CURRENCY_ALIASES = {
    "евро": "EUR", "eur": "EUR",
    "доллар": "USD", "usd": "USD",
    "динар": "RSD", "rsd": "RSD",
    "рубль": "RUB", "rub": "RUB",
}


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def _format_expense(e: dict) -> str:
    desc = e.get("description") or e.get("merchant") or "—"
    cat = f" [{e['category']}]" if e.get("category") else ""
    merchant = f" ({e['merchant']})" if e.get("merchant") and e.get("description") else ""
    return f"{e['amount']:.2f} {e['currency']} — {desc}{merchant}{cat}"


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Привет! Я трекер расходов.\n\n"
        "Просто напиши: кофе 350\n"
        "Или сфоткай чек.\n\n"
        "/help — все команды"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "💰 *Как пользоваться*\n\n"
        "*Добавить расход:*\n"
        "  Текстом: `кофе 350` или `Zara 5000 динар одежда`\n"
        "  Фото: сфоткай чек\n"
        "  Пересылка: перешли подтверждение заказа\n\n"
        "*Команды:*\n"
        "📊 /today — расходы за сегодня\n"
        "📊 /week — расходы за неделю\n"
        "📊 /month — отчёт за месяц\n"
        "📋 /history — последние записи\n"
        "🗑 /delete <id> — удалить запись\n"
        "💱 /currency RSD — валюта по умолчанию\n"
        "🌍 /timezone CET — часовой пояс\n",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    user_id = update.effective_user.id
    text = update.message.text
    currency = await get_default_currency(user_id)

    parsed = await parse_text_expense(text, currency)
    if not parsed or "amount" not in parsed:
        await update.message.reply_text("Не понял расход. Напиши например: кофе 350")
        return

    amount = float(parsed["amount"])
    cur = parsed.get("currency", currency)
    category = parsed.get("category")
    description = parsed.get("description")
    merchant = parsed.get("merchant")

    eid = await save_expense(user_id, amount, cur, category, description, merchant)
    await update.message.reply_text(f"✅ {_format_expense(parsed)}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    await update.message.reply_text("📸 Читаю чек...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_url = file.file_path

    parsed = await parse_receipt_photo(image_url)
    if not parsed or "amount" not in parsed:
        await update.message.reply_text("Не удалось прочитать чек. Попробуй сфоткать ровнее или напиши текстом.")
        return

    user_id = update.effective_user.id
    amount = float(parsed["amount"])
    currency = parsed.get("currency", await get_default_currency(user_id))
    category = parsed.get("category")
    description = parsed.get("description")
    merchant = parsed.get("merchant")

    eid = await save_expense(user_id, amount, currency, category, description, merchant)
    await update.message.reply_text(f"✅ {_format_expense(parsed)}")


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    msg = update.message
    text = msg.text or msg.caption
    if not text:
        await msg.reply_text("Не могу прочитать это сообщение.")
        return

    user_id = msg.from_user.id
    currency = await get_default_currency(user_id)
    parsed = await parse_forwarded_expense(text, currency)

    if not parsed or "amount" not in parsed:
        await msg.reply_text("Не нашёл расход в этом сообщении.")
        return

    amount = float(parsed["amount"])
    cur = parsed.get("currency", currency)
    category = parsed.get("category")
    description = parsed.get("description")
    merchant = parsed.get("merchant")

    eid = await save_expense(user_id, amount, cur, category, description, merchant)
    await msg.reply_text(f"✅ {_format_expense(parsed)}")


async def _get_user_tz(user_id: int) -> ZoneInfo | timezone:
    tz_name = await get_timezone(user_id)
    return ZoneInfo(tz_name) if tz_name else timezone.utc


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)
    start_of_day = datetime(now_local.year, now_local.month, now_local.day, tzinfo=user_tz)
    since_utc = start_of_day.astimezone(timezone.utc)

    expenses = await get_expenses(user_id, since=since_utc)
    report = await generate_expense_report(expenses, "сегодня")
    await update.message.reply_text(report)


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    since = datetime.now(timezone.utc) - timedelta(days=7)
    expenses = await get_expenses(user_id, since=since)
    report = await generate_expense_report(expenses, "неделю")
    await update.message.reply_text(report)


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)
    start_of_month = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz)
    since_utc = start_of_month.astimezone(timezone.utc)

    month_name = now_local.strftime("%B %Y")
    expenses = await get_expenses(user_id, since=since_utc)
    report = await generate_expense_report(expenses, month_name)
    await update.message.reply_text(report)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            pass

    expenses = await get_recent_expenses(update.effective_user.id, limit)
    if not expenses:
        await update.message.reply_text("Расходов пока нет.")
        return

    lines = ["Последние расходы:\n"]
    for e in expenses:
        lines.append(f"{e['id']} — {_format_expense(e)}")
    lines.append("\nУдалить: /delete <id>")
    await update.message.reply_text("\n".join(lines))


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /delete <id>")
        return
    try:
        eid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    deleted = await delete_expense(eid, update.effective_user.id)
    if deleted:
        await update.message.reply_text(f"Расход #{eid} удалён.")
    else:
        await update.message.reply_text("Расход не найден.")


async def currency_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        current = await get_default_currency(update.effective_user.id)
        await update.message.reply_text(
            f"Текущая валюта: {current}\n"
            "Изменить: /currency EUR"
        )
        return

    raw = context.args[0].lower()
    cur = CURRENCY_ALIASES.get(raw, raw.upper())
    if len(cur) != 3:
        await update.message.reply_text("Валюта — 3-буквенный код (EUR, RSD, USD).")
        return

    await set_default_currency(update.effective_user.id, cur)
    await update.message.reply_text(f"Валюта по умолчанию: {cur}")


async def timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        current = await get_timezone(update.effective_user.id)
        if current:
            await update.message.reply_text(f"Часовой пояс: {current}")
        else:
            await update.message.reply_text("Укажи часовой пояс: /timezone CET")
        return

    tz_input = context.args[0]
    tz_name = TIMEZONE_ALIASES.get(tz_input.lower(), tz_input)
    try:
        ZoneInfo(tz_name)
    except (KeyError, Exception):
        await update.message.reply_text(f"Неизвестный часовой пояс: {tz_input}")
        return

    await set_timezone(update.effective_user.id, tz_name)
    now_local = datetime.now(ZoneInfo(tz_name)).strftime("%H:%M")
    await update.message.reply_text(f"Часовой пояс: {tz_name} (сейчас {now_local})")


# --- Main ---

async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    await init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("currency", currency_cmd))
    app.add_handler(CommandHandler("timezone", timezone_cmd))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Expense bot started!")
    async with app:
        await app.bot.set_my_commands([
            BotCommand("help", "Как пользоваться"),
            BotCommand("today", "Расходы за сегодня"),
            BotCommand("week", "Расходы за неделю"),
            BotCommand("month", "Отчёт за месяц"),
            BotCommand("history", "Последние расходы"),
            BotCommand("delete", "Удалить расход"),
            BotCommand("currency", "Валюта по умолчанию"),
            BotCommand("timezone", "Часовой пояс"),
        ])

        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        import asyncio
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

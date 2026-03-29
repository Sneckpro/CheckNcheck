import asyncio
import os
import logging
import signal
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
    set_budget, get_budget, get_all_budgets, delete_budget, get_category_total,
    set_email_settings, get_email_settings, disable_email, get_all_email_users,
    is_email_processed, mark_email_processed,
)
from ai import (
    parse_receipt_photo,
    parse_text_expense,
    parse_forwarded_expense,
    parse_email_receipt,
    generate_expense_report,
)
from charts import generate_pie_chart, generate_monthly_bars
from email_parser import fetch_emails

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
    amount = float(e.get("amount", 0))
    currency = e.get("currency", "RSD")
    return f"{amount:.2f} {currency} — {desc}{merchant}{cat}"


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
        "🌍 /timezone CET — часовой пояс\n\n"
        "*Бюджеты:*\n"
        "💰 /budget еда 20000 — установить лимит\n"
        "💰 /budgets — все бюджеты\n\n"
        "*Графики:*\n"
        "📈 /chart — круговая диаграмма за месяц\n"
        "📈 /chart 3 — сравнение 3 месяцев\n\n"
        "*Почта:*\n"
        "📧 /email — подключить автосканирование чеков\n"
        "📧 /email off — отключить\n",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    # Check if in email setup flow
    if await handle_email_setup(update, context):
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
    category = (parsed.get("category") or "").lower() or None
    description = parsed.get("description")
    merchant = parsed.get("merchant")

    await save_expense(user_id, amount, cur, category, description, merchant)
    await update.message.reply_text(f"✅ {_format_expense(parsed)}")
    await _check_budget_warning(update.message, user_id, category)


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
    category = (parsed.get("category") or "").lower() or None
    description = parsed.get("description")
    merchant = parsed.get("merchant")

    await save_expense(user_id, amount, currency, category, description, merchant)
    await update.message.reply_text(f"✅ {_format_expense(parsed)}")
    await _check_budget_warning(update.message, user_id, category)


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
    category = (parsed.get("category") or "").lower() or None
    description = parsed.get("description")
    merchant = parsed.get("merchant")

    await save_expense(user_id, amount, cur, category, description, merchant)
    await msg.reply_text(f"✅ {_format_expense(parsed)}")
    await _check_budget_warning(msg, user_id, category)


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
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)
    start_of_today = datetime(now_local.year, now_local.month, now_local.day, tzinfo=user_tz)
    since = (start_of_today - timedelta(days=6)).astimezone(timezone.utc)
    expenses = await get_expenses(user_id, since=since)
    report = await generate_expense_report(expenses, "неделю")
    await update.message.reply_text(report)


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)

    # /month 2 → February, /month 12 → December
    if context.args:
        try:
            month = int(context.args[0])
            if not 1 <= month <= 12:
                await update.message.reply_text("Месяц от 1 до 12.")
                return
            year = now_local.year if month <= now_local.month else now_local.year - 1
        except ValueError:
            await update.message.reply_text("Формат: /month или /month 2")
            return
    else:
        month = now_local.month
        year = now_local.year

    start_of_month = datetime(year, month, 1, tzinfo=user_tz)
    if month == 12:
        end_of_month = datetime(year + 1, 1, 1, tzinfo=user_tz)
    else:
        end_of_month = datetime(year, month + 1, 1, tzinfo=user_tz)

    since_utc = start_of_month.astimezone(timezone.utc)
    until_utc = end_of_month.astimezone(timezone.utc)

    month_name = start_of_month.strftime("%B %Y")
    expenses = await get_expenses(user_id, since=since_utc, until=until_utc)
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


async def _check_budget_warning(message, user_id: int, category: str):
    """Send warning if expense category is near/over budget."""
    if not category:
        return
    budget = await get_budget(user_id, category)
    if not budget:
        return
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)
    start = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz).astimezone(timezone.utc)
    if now_local.month == 12:
        end = datetime(now_local.year + 1, 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
    else:
        end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
    spent = await get_category_total(user_id, category, start, end, currency=budget["currency"])
    pct = spent / budget["amount"] * 100 if budget["amount"] > 0 else 0
    if pct >= 100:
        await message.reply_text(f"🔴 Бюджет на {category} превышен: {spent:.0f}/{budget['amount']:.0f} {budget['currency']} ({pct:.0f}%)")
    elif pct >= 80:
        await message.reply_text(f"⚠️ Бюджет на {category}: {spent:.0f}/{budget['amount']:.0f} {budget['currency']} ({pct:.0f}%)")


def _progress_bar(pct: float) -> str:
    filled = min(int(pct / 10), 10)
    return "█" * filled + "░" * (10 - filled)


async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Формат: /budget еда 20000\nПосмотреть все: /budgets")
        return

    category = context.args[0].lower()

    # /budget еда — show status
    if len(context.args) == 1:
        budget = await get_budget(user_id, category)
        if not budget:
            await update.message.reply_text(f"Бюджет на {category} не установлен.")
            return
        user_tz = await _get_user_tz(user_id)
        now_local = datetime.now(user_tz)
        start = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz).astimezone(timezone.utc)
        if now_local.month == 12:
            end = datetime(now_local.year + 1, 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
        else:
            end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
        spent = await get_category_total(user_id, category, start, end, currency=budget["currency"])
        pct = spent / budget["amount"] * 100 if budget["amount"] > 0 else 0
        bar = _progress_bar(pct)
        await update.message.reply_text(f"{category}: {spent:.0f}/{budget['amount']:.0f} {budget['currency']} [{bar}] {pct:.0f}%")
        return

    # /budget еда 20000
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return

    if amount <= 0:
        await delete_budget(user_id, category)
        await update.message.reply_text(f"Бюджет на {category} удалён.")
        return

    currency = await get_default_currency(user_id)
    await set_budget(user_id, category, amount, currency)
    await update.message.reply_text(f"Бюджет на {category}: {amount:.0f} {currency}/мес")


async def budgets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    budgets = await get_all_budgets(user_id)
    if not budgets:
        await update.message.reply_text("Бюджеты не установлены.\nУстановить: /budget еда 20000")
        return

    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)
    start = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz).astimezone(timezone.utc)
    if now_local.month == 12:
        end = datetime(now_local.year + 1, 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
    else:
        end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=user_tz).astimezone(timezone.utc)

    lines = [f"📊 Бюджеты на {now_local.strftime('%B %Y')}\n"]
    for b in budgets:
        spent = await get_category_total(user_id, b["category"], start, end, currency=b["currency"])
        pct = spent / b["amount"] * 100 if b["amount"] > 0 else 0
        bar = _progress_bar(pct)
        warn = " 🔴" if pct >= 100 else " ⚠️" if pct >= 80 else ""
        lines.append(f"{b['category']}: {spent:.0f}/{b['amount']:.0f} {b['currency']} [{bar}] {pct:.0f}%{warn}")
    await update.message.reply_text("\n".join(lines))


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)

    # /chart 3 — bar chart for last N months
    if context.args:
        try:
            n_months = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Формат: /chart или /chart 3")
            return
        n_months = min(n_months, 12)
        currency = await get_default_currency(user_id)
        months_data = []
        for i in range(n_months - 1, -1, -1):
            m = now_local.month - i
            y = now_local.year
            while m <= 0:
                m += 12
                y -= 1
            start = datetime(y, m, 1, tzinfo=user_tz).astimezone(timezone.utc)
            end_m = m + 1 if m < 12 else 1
            end_y = y if m < 12 else y + 1
            end = datetime(end_y, end_m, 1, tzinfo=user_tz).astimezone(timezone.utc)
            expenses = await get_expenses(user_id, since=start, until=end)
            expenses = [e for e in expenses if e.get("currency", "RSD") == currency]
            total = sum(e["amount"] for e in expenses)
            month_label = datetime(y, m, 1).strftime("%b %Y")
            months_data.append({"month": month_label, "total": total})
        buf = generate_monthly_bars(months_data, currency)
        if buf:
            await update.message.reply_photo(photo=buf)
        else:
            await update.message.reply_text("Нет данных для графика.")
        return

    # /chart — pie chart for current month
    start = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz).astimezone(timezone.utc)
    if now_local.month == 12:
        end = datetime(now_local.year + 1, 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
    else:
        end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=user_tz).astimezone(timezone.utc)

    currency = await get_default_currency(user_id)
    expenses = await get_expenses(user_id, since=start, until=end)
    expenses = [e for e in expenses if e.get("currency", "RSD") == currency]
    if not expenses:
        await update.message.reply_text("Нет расходов за этот месяц.")
        return

    by_category: dict[str, float] = {}
    for e in expenses:
        cat = e.get("category") or "другое"
        by_category[cat] = by_category.get(cat, 0) + e["amount"]
    period = now_local.strftime("%B %Y")
    buf = generate_pie_chart(by_category, currency, period)
    if buf:
        await update.message.reply_photo(photo=buf)
    else:
        await update.message.reply_text("Не удалось создать график.")


# --- Email ---

EMAIL_SETUP_STEP = {}  # user_id -> {"step": 1/2/3, "server": ..., "address": ...}

IMAP_HINTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "mail.ru": "imap.mail.ru",
    "yandex.ru": "imap.yandex.ru",
}


async def email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id

    if context.args and context.args[0].lower() == "off":
        await disable_email(user_id)
        await update.message.reply_text("Сканирование почты отключено.")
        return

    if context.args and context.args[0].lower() == "status":
        settings = await get_email_settings(user_id)
        if settings:
            await update.message.reply_text(f"Почта: {settings['email_address']}\nСервер: {settings['email_server']}\nСтатус: активна")
        else:
            await update.message.reply_text("Почта не подключена.")
        return

    # /email scan 30 — scan last N days
    if context.args and context.args[0].lower() == "scan":
        settings = await get_email_settings(user_id)
        if not settings:
            await update.message.reply_text("Сначала подключи почту: /email")
            return
        days = 30
        if len(context.args) > 1:
            try:
                days = int(context.args[1])
            except ValueError:
                pass
        await update.message.reply_text(f"Сканирую почту за {days} дней...")
        try:
            count = await _scan_emails(context, user_id, settings, since_days=days)
            await update.message.reply_text(f"Найдено чеков: {count}")
        except Exception as e:
            logger.error(f"Email scan failed for {user_id}: {e}")
            await update.message.reply_text(f"Ошибка сканирования: {e}")
        return

    # Start setup flow
    EMAIL_SETUP_STEP[user_id] = {"step": 1, "started_at": datetime.now()}
    await update.message.reply_text(
        "Настройка почты. Шаг 1/3:\n\n"
        "Введи email адрес:\n"
        "(Отмена: /cancel)"
    )


async def handle_email_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle email setup conversation. Returns True if message was consumed."""
    user_id = update.effective_user.id
    if user_id not in EMAIL_SETUP_STEP:
        return False

    step_data = EMAIL_SETUP_STEP[user_id]
    text = update.message.text.strip()

    # Cancel
    if text.lower() in ("/cancel", "отмена", "cancel"):
        del EMAIL_SETUP_STEP[user_id]
        await update.message.reply_text("Настройка почты отменена.")
        return True

    # Timeout (5 min)
    if (datetime.now() - step_data.get("started_at", datetime.now())).total_seconds() > 300:
        del EMAIL_SETUP_STEP[user_id]
        await update.message.reply_text("Таймаут настройки. Начни заново: /email")
        return True

    if step_data["step"] == 1:
        # Validate email
        if "@" not in text or "." not in text.split("@")[-1]:
            await update.message.reply_text("Некорректный email. Попробуй ещё раз:")
            return True
        # Got email address, guess IMAP server
        step_data["address"] = text
        domain = text.split("@")[-1].lower() if "@" in text else ""
        guessed = IMAP_HINTS.get(domain)
        if guessed:
            step_data["server"] = guessed
            step_data["step"] = 3
            await update.message.reply_text(
                f"Сервер: {guessed}\n\n"
                "Шаг 2/2: Введи App Password\n"
                "(Для Gmail: myaccount.google.com → Security → App passwords)"
            )
        else:
            step_data["step"] = 2
            await update.message.reply_text("Шаг 2/3: Введи IMAP-сервер (например imap.gmail.com):")
        return True

    if step_data["step"] == 2:
        step_data["server"] = text
        step_data["step"] = 3
        await update.message.reply_text(
            "Шаг 3/3: Введи App Password\n"
            "(Для Gmail: myaccount.google.com → Security → App passwords)"
        )
        return True

    if step_data["step"] == 3:
        password = text
        server = step_data["server"]
        address = step_data["address"]
        del EMAIL_SETUP_STEP[user_id]

        # Test connection
        try:
            import imaplib
            mail = imaplib.IMAP4_SSL(server)
            mail.login(address, password)
            mail.logout()
        except Exception as e:
            await update.message.reply_text(f"Не удалось подключиться: {e}\nПопробуй ещё раз: /email")
            return True

        await set_email_settings(user_id, server, address, password)
        await update.message.reply_text(
            f"Почта подключена: {address}\n"
            "Бот будет проверять новые чеки каждые 15 минут.\n"
            "Отключить: /email off"
        )
        return True

    return False


async def _scan_emails(context, user_id: int, settings: dict, since_days: int | None = None) -> int:
    """Scan emails, skip already processed, save new receipts. Returns count found."""
    emails = await asyncio.to_thread(
        fetch_emails, settings["email_server"], settings["email_address"],
        settings["email_password"], since_days=since_days
    )
    currency = await get_default_currency(user_id)
    count = 0
    for em in emails:
        if await is_email_processed(user_id, em["uid"]):
            continue
        parsed = await parse_email_receipt(em["from"], em["subject"], em["body"], currency)
        await mark_email_processed(user_id, em["uid"])
        if not parsed or "amount" not in parsed:
            continue
        amount = float(parsed["amount"])
        cur = parsed.get("currency", currency)
        category = (parsed.get("category") or "").lower() or None
        await save_expense(user_id, amount, cur,
                           category, parsed.get("description"), parsed.get("merchant"))
        text = _format_expense(parsed)
        await context.bot.send_message(chat_id=user_id, text=f"📧 Чек из почты:\n✅ {text}")
        count += 1
    return count


async def check_emails_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job: check all users' emails for receipts."""
    users = await get_all_email_users()
    for u in users:
        try:
            settings = {"email_server": u["email_server"], "email_address": u["email_address"],
                        "email_password": u["email_password"]}
            await _scan_emails(context, u["user_id"], settings)
        except Exception as e:
            logger.error(f"Email check failed for {u['user_id']}: {e}")


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
    app.add_handler(CommandHandler("budget", budget_cmd))
    app.add_handler(CommandHandler("budgets", budgets_cmd))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("email", email_cmd))
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
            BotCommand("budget", "Бюджет по категории"),
            BotCommand("budgets", "Все бюджеты"),
            BotCommand("chart", "График расходов"),
            BotCommand("email", "Подключить почту"),
        ])

        app.job_queue.run_repeating(check_emails_job, interval=900, first=30, name="email_check")
        logger.info("Email check scheduled (every 15 min)")

        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()

        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())

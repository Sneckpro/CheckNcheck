import asyncio
import os
import logging
import signal
import calendar
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
    init_db, save_expense, get_expenses, get_recent_expenses, delete_expense, clear_all_expenses, get_top_expenses,
    search_expenses, get_expense_by_id, update_expense, get_all_active_users,
    get_default_currency, set_default_currency, get_timezone, set_timezone,
    set_budget, get_budget, get_all_budgets, delete_budget, get_category_total,
    set_email_settings, get_email_settings, disable_email, get_all_email_users,
    is_email_processed, mark_email_processed, clear_processed_emails,
    get_total_spent,
    add_subscription, get_active_subscriptions, deactivate_subscription,
    get_all_due_subscriptions, mark_subscription_charged,
)
from ai import (
    CATEGORIES,
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
        "👋 Привет! Я помогу отслеживать расходы.\n\n"
        "🚀 *Быстрый старт:*\n\n"
        "*1.* Установи часовой пояс:\n"
        "   /timezone CET\n\n"
        "*2.* Установи валюту (по умолчанию RSD):\n"
        "   /currency EUR\n\n"
        "*3.* Добавь расход любым способом:\n"
        "   • Напиши: `кофе 350`\n"
        "   • Сфоткай чек\n"
        "   • Перешли подтверждение заказа\n\n"
        "*4.* Смотри отчёты:\n"
        "   /today — за сегодня\n"
        "   /week — за неделю\n"
        "   /month — за месяц\n\n"
        "📧 Бонус: подключи почту (/email) — бот сам найдёт чеки от Wolt, Bolt, Amazon и других.\n\n"
        "/help — полный список команд",
        parse_mode="Markdown",
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
        "🔍 /search <запрос> — поиск расходов\n"
        "✏️ /edit <id> <значение> — редактировать\n"
        "🗑 /delete <id> — удалить запись\n"
        "💱 /currency RSD — валюта по умолчанию\n"
        "🌍 /timezone CET — часовой пояс\n\n"
        "*Бюджеты:*\n"
        "💰 /budget еда 20000 — установить лимит\n"
        "💰 /budgets — все бюджеты\n\n"
        "*Подписки:*\n"
        "📌 /sub Netflix 1500 — добавить подписку\n"
        "📌 /subs — мои подписки\n"
        "📌 /unsub Netflix — отменить\n\n"
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
    currency = await get_default_currency(user_id)
    report = await generate_expense_report(expenses, "сегодня", target_currency=currency)
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
    currency = await get_default_currency(user_id)
    report = await generate_expense_report(expenses, "неделю", target_currency=currency)
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
    currency = await get_default_currency(user_id)

    # Analytics: days passed, days left, budget, prev month
    total_days = calendar.monthrange(year, month)[1]
    is_current = (month == now_local.month and year == now_local.year)
    days_passed = now_local.day if is_current else total_days
    days_left = total_days - days_passed if is_current else 0

    # Previous month total (with currency conversion)
    if month == 1:
        prev_start = datetime(year - 1, 12, 1, tzinfo=user_tz).astimezone(timezone.utc)
        prev_end = start_of_month.astimezone(timezone.utc)
    else:
        prev_start = datetime(year, month - 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
        prev_end = since_utc
    prev_expenses = await get_expenses(user_id, since=prev_start, until=prev_end)
    prev_total = None
    if prev_expenses:
        from currency import convert
        prev_total = 0.0
        for e in prev_expenses:
            amt = e["amount"]
            cur = e.get("currency", currency)
            if cur != currency:
                converted = convert(amt, cur, currency)
                if converted is not None:
                    amt = converted
            prev_total += amt

    # Total budget
    total_budget = await get_budget(user_id, "_общий")
    budget_amount = total_budget["amount"] if total_budget else None

    report = await generate_expense_report(
        expenses, month_name, target_currency=currency,
        days_passed=days_passed, days_left=days_left,
        budget_amount=budget_amount, prev_period_total=prev_total,
    )
    await update.message.reply_text(report)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)
    start = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz).astimezone(timezone.utc)
    if now_local.month == 12:
        end = datetime(now_local.year + 1, 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
    else:
        end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=user_tz).astimezone(timezone.utc)

    limit = 5
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            pass

    top = await get_top_expenses(user_id, start, end, limit=limit)
    if not top:
        await update.message.reply_text("Нет расходов за этот месяц.")
        return

    lines = [f"🏆 Топ расходов за {now_local.strftime('%B %Y')}\n"]
    for i, e in enumerate(top, 1):
        lines.append(f"{i}. {_format_expense(e)}")
    await update.message.reply_text("\n".join(lines))


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


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /search <запрос>\nПример: /search кофе")
        return

    query = " ".join(context.args)
    results = await search_expenses(update.effective_user.id, query)
    if not results:
        await update.message.reply_text(f"Ничего не найдено: {query}")
        return

    lines = [f"🔍 Результаты по «{query}»:\n"]
    for e in results:
        lines.append(f"{e['id']} — {_format_expense(e)}")
    lines.append(f"\nНайдено: {len(results)}")
    await update.message.reply_text("\n".join(lines))


async def clearall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    deleted = await clear_all_expenses(update.effective_user.id)
    await clear_processed_emails(update.effective_user.id)
    await update.message.reply_text(f"Удалено расходов: {deleted}. История и обработанные письма очищены.")


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


FIELD_ALIASES = {
    "amount": "amount", "сумма": "amount",
    "category": "category", "категория": "category",
    "description": "description", "описание": "description",
    "merchant": "merchant", "магазин": "merchant",
    "currency": "currency", "валюта": "currency",
}


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "/edit <id> <сумма> — изменить сумму\n"
            "/edit <id> <категория> — изменить категорию\n"
            "/edit <id> сумма 500\n"
            "/edit <id> категория еда"
        )
        return

    try:
        eid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    user_id = update.effective_user.id
    expense = await get_expense_by_id(eid, user_id)
    if not expense:
        await update.message.reply_text("Расход не найден.")
        return

    changes = {}
    rest = context.args[1:]

    # Explicit field: /edit 42 amount 500
    if rest[0].lower() in FIELD_ALIASES:
        if len(rest) < 2:
            await update.message.reply_text(f"Укажи значение: /edit {eid} {rest[0]} <значение>")
            return
        field = FIELD_ALIASES[rest[0].lower()]
        value = " ".join(rest[1:])
        if field == "amount":
            try:
                changes["amount"] = float(value)
                if changes["amount"] <= 0:
                    await update.message.reply_text("Сумма должна быть больше нуля.")
                    return
            except ValueError:
                await update.message.reply_text("Некорректная сумма.")
                return
        elif field == "currency":
            changes["currency"] = value.upper()[:3]
        elif field == "category":
            changes["category"] = value.lower()
        else:
            changes[field] = value
    else:
        # Smart mode: /edit 42 500 or /edit 42 еда
        value = " ".join(rest)
        try:
            amount = float(value)
            if amount <= 0:
                await update.message.reply_text("Сумма должна быть больше нуля.")
                return
            changes["amount"] = amount
        except ValueError:
            if value.lower() in CATEGORIES:
                changes["category"] = value.lower()
            else:
                changes["description"] = value

    await update_expense(eid, user_id, **changes)
    updated = await get_expense_by_id(eid, user_id)
    await update.message.reply_text(f"Обновлено: {_format_expense(updated)}")

    if "category" in changes:
        await _check_budget_warning(update.message, user_id, changes["category"])


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
    """Send warning if expense category or total budget is near/over."""
    user_tz = await _get_user_tz(user_id)
    now_local = datetime.now(user_tz)
    start = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz).astimezone(timezone.utc)
    if now_local.month == 12:
        end = datetime(now_local.year + 1, 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
    else:
        end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=user_tz).astimezone(timezone.utc)

    # Check category budget
    if category:
        budget = await get_budget(user_id, category)
        if budget:
            spent = await get_category_total(user_id, category, start, end, currency=budget["currency"])
            pct = spent / budget["amount"] * 100 if budget["amount"] > 0 else 0
            if pct >= 100:
                await message.reply_text(f"🔴 Бюджет на {category} превышен: {spent:.0f}/{budget['amount']:.0f} {budget['currency']} ({pct:.0f}%)")
            elif pct >= 80:
                await message.reply_text(f"⚠️ Бюджет на {category}: {spent:.0f}/{budget['amount']:.0f} {budget['currency']} ({pct:.0f}%)")

    # Check total budget
    total_budget = await get_budget(user_id, "_общий")
    if total_budget:
        spent = await get_total_spent(user_id, start, end, currency=total_budget["currency"])
        pct = spent / total_budget["amount"] * 100 if total_budget["amount"] > 0 else 0
        if pct >= 100:
            await message.reply_text(f"🔴 Общий бюджет превышен: {spent:.0f}/{total_budget['amount']:.0f} {total_budget['currency']} ({pct:.0f}%)")
        elif pct >= 80:
            await message.reply_text(f"⚠️ Общий бюджет: {spent:.0f}/{total_budget['amount']:.0f} {total_budget['currency']} ({pct:.0f}%)")


def _progress_bar(pct: float) -> str:
    filled = min(int(pct / 10), 10)
    return "█" * filled + "░" * (10 - filled)


async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Формат:\n"
            "/budget 280000 — общий бюджет на месяц\n"
            "/budget еда 20000 — бюджет на категорию\n"
            "/budgets — посмотреть все"
        )
        return

    # /budget 280000 — total budget (first arg is a number)
    try:
        total_amount = float(context.args[0])
        currency = await get_default_currency(user_id)
        if total_amount <= 0:
            await delete_budget(user_id, "_общий")
            await update.message.reply_text("Общий бюджет удалён.")
        else:
            await set_budget(user_id, "_общий", total_amount, currency)
            await update.message.reply_text(f"Общий бюджет: {total_amount:.0f} {currency}/мес")
        return
    except ValueError:
        pass

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
        if b["category"] == "_общий":
            spent = await get_total_spent(user_id, start, end, currency=b["currency"])
            label = "Общий"
        else:
            spent = await get_category_total(user_id, b["category"], start, end, currency=b["currency"])
            label = b["category"]
        pct = spent / b["amount"] * 100 if b["amount"] > 0 else 0
        bar = _progress_bar(pct)
        warn = " 🔴" if pct >= 100 else " ⚠️" if pct >= 80 else ""
        lines.append(f"{label}: {spent:.0f}/{b['amount']:.0f} {b['currency']} [{bar}] {pct:.0f}%{warn}")
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
            stats = await _scan_emails(context, user_id, settings, since_days=days)
            await update.message.reply_text(
                f"📧 Результат сканирования:\n"
                f"Писем всего: {stats['total']}\n"
                f"Похожи на чеки: {stats['filtered']}\n"
                f"Уже обработаны: {stats['already']}\n"
                f"Новых чеков: {stats['found']}"
            )
        except Exception as e:
            logger.error(f"Email scan failed for {user_id}: {e}")
            await update.message.reply_text(f"Ошибка сканирования: {e}")
        return

    # /email debug 7 — show what filter sees
    if context.args and context.args[0].lower() == "debug":
        settings = await get_email_settings(user_id)
        if not settings:
            await update.message.reply_text("Сначала подключи почту: /email")
            return
        days = 7
        if len(context.args) > 1:
            try:
                days = int(context.args[1])
            except ValueError:
                pass
        await update.message.reply_text(f"Дебаг: сканирую {days} дней...")
        try:
            stats = await _scan_emails(context, user_id, settings, since_days=days, debug=True)
            lines = [
                f"📧 Дебаг за {days} дней:\n",
                f"Писем всего: {stats['total']}",
                f"Прошли фильтр: {stats['filtered']}",
                f"Отброшены фильтром: {stats['total'] - stats['filtered']}",
            ]
            if stats["skipped"]:
                lines.append(f"\nОтброшенные (последние 10):")
                for s in stats["skipped"][-10:]:
                    lines.append(f"  ✗ {s['from'][:40]}")
                    lines.append(f"    {s['subject'][:50]}")
            if stats["filtered"] > 0:
                lines.append(f"\nПрошли фильтр → GPT: {stats['filtered']}")
                lines.append(f"Из них чеки: {stats['found']}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            logger.error(f"Email debug failed: {e}")
            await update.message.reply_text(f"Ошибка: {e}")
        return

    # /email reset — clear processed emails and rescan
    if context.args and context.args[0].lower() == "reset":
        settings = await get_email_settings(user_id)
        if not settings:
            await update.message.reply_text("Сначала подключи почту: /email")
            return
        cleared = await clear_processed_emails(user_id)
        await update.message.reply_text(f"Сброшено {cleared} обработанных писем. Теперь сделай /email scan 30")
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
        APP_PASSWORD_HINTS = {
            "imap.gmail.com": "Для Gmail: myaccount.google.com → Security → App passwords",
            "imap.yandex.ru": "Для Яндекс: id.yandex.ru → Безопасность → Пароли приложений",
            "outlook.office365.com": "Для Outlook: account.microsoft.com → Security → App passwords",
            "imap.mail.yahoo.com": "Для Yahoo: login.yahoo.com → Account Security → App passwords",
            "imap.mail.ru": "Для Mail.ru: id.mail.ru → Безопасность → Пароли приложений",
        }
        if guessed:
            step_data["server"] = guessed
            step_data["step"] = 3
            hint = APP_PASSWORD_HINTS.get(guessed, "Создай пароль приложения в настройках безопасности почты")
            await update.message.reply_text(
                f"Сервер: {guessed}\n\n"
                f"Шаг 2/2: Введи App Password\n"
                f"({hint})"
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
            "Бот будет проверять новые чеки каждые 2 часа.\n"
            "Отключить: /email off"
        )
        return True

    return False


async def _scan_emails(context, user_id: int, settings: dict,
                       since_days: int | None = None, debug: bool = False) -> dict:
    """Scan emails, skip already processed, save new receipts. Returns stats."""
    data = await asyncio.to_thread(
        fetch_emails, settings["email_server"], settings["email_address"],
        settings["email_password"], since_days=since_days, debug=debug
    )
    emails = data["results"]
    currency = await get_default_currency(user_id)
    found = 0
    already = 0
    for em in emails:
        if await is_email_processed(user_id, em["uid"]):
            already += 1
            continue
        parsed = await parse_email_receipt(em["from"], em["subject"], em["body"], currency)
        await mark_email_processed(user_id, em["uid"])
        if not parsed or "amount" not in parsed:
            logger.info("Email NOT parsed as expense: from=%s subj=%s parsed=%s",
                        em["from"][:50], em["subject"][:60], parsed)
            continue
        logger.info("Email parsed OK: from=%s subj=%s amount=%s %s",
                    em["from"][:50], em["subject"][:60],
                    parsed.get("amount"), parsed.get("currency"))
        amount = float(parsed["amount"])
        cur = parsed.get("currency", currency)
        category = (parsed.get("category") or "").lower() or None
        await save_expense(user_id, amount, cur,
                           category, parsed.get("description"), parsed.get("merchant"))
        text = _format_expense(parsed)
        await context.bot.send_message(chat_id=user_id, text=f"📧 Чек из почты:\n✅ {text}")
        found += 1
    return {
        "total": data["total"],
        "filtered": len(emails),
        "already": already,
        "found": found,
        "skipped": data.get("skipped", []),
    }


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


_last_digest_sent: dict[int, str] = {}

EMOJI_MAP = {
    "еда": "\U0001f354", "продукты": "\U0001f6d2", "транспорт": "\U0001f697",
    "покупки": "\U0001f6cd\ufe0f", "развлечения": "\U0001f3ac",
    "здоровье": "\U0001f3e5", "жильё": "\U0001f3e0",
    "подписки": "\U0001f4f1", "другое": "\U0001f4cc",
}


async def weekly_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Send weekly expense digest every Sunday at 20:00 user local time."""
    user_ids = await get_all_active_users()
    for user_id in user_ids:
        try:
            user_tz = await _get_user_tz(user_id)
            now_local = datetime.now(user_tz)

            if now_local.weekday() != 6 or now_local.hour != 20:
                continue

            today_str = now_local.strftime("%Y-%m-%d")
            if _last_digest_sent.get(user_id) == today_str:
                continue

            # This week: Monday to now
            days_since_monday = now_local.weekday()
            monday = datetime(now_local.year, now_local.month, now_local.day, tzinfo=user_tz) - timedelta(days=days_since_monday)
            sunday_end = monday + timedelta(days=7)
            week_start_utc = monday.astimezone(timezone.utc)
            week_end_utc = sunday_end.astimezone(timezone.utc)

            # Previous week
            prev_monday = monday - timedelta(days=7)
            prev_start_utc = prev_monday.astimezone(timezone.utc)

            expenses = await get_expenses(user_id, since=week_start_utc, until=week_end_utc)
            if not expenses:
                continue

            currency = await get_default_currency(user_id)
            from currency import convert

            # Totals with conversion
            total = 0.0
            by_cat: dict[str, float] = {}
            for e in expenses:
                amt = e["amount"]
                cur = e.get("currency", currency)
                if cur != currency:
                    converted = convert(amt, cur, currency)
                    if converted is not None:
                        amt = converted
                total += amt
                cat = e.get("category") or "другое"
                by_cat[cat] = by_cat.get(cat, 0) + amt

            # Previous week total
            prev_expenses = await get_expenses(user_id, since=prev_start_utc, until=week_start_utc)
            prev_total = 0.0
            for e in prev_expenses:
                amt = e["amount"]
                cur = e.get("currency", currency)
                if cur != currency:
                    converted = convert(amt, cur, currency)
                    if converted is not None:
                        amt = converted
                prev_total += amt

            # Format message
            lines = [f"📅 Еженедельный отчёт\n", f"💰 За неделю: {total:,.0f} {currency}"]

            if prev_total > 0:
                change = ((total - prev_total) / prev_total) * 100
                sign = "+" if change >= 0 else ""
                lines.append(f"📊 vs прошлая: {sign}{change:.0f}%")
            elif prev_total == 0 and prev_expenses:
                lines.append("📊 vs прошлая: нет данных")

            top3 = sorted(by_cat.items(), key=lambda x: -x[1])[:3]
            if top3:
                lines.append("\n🏆 Топ категории:")
                for i, (cat, amt) in enumerate(top3, 1):
                    emoji = EMOJI_MAP.get(cat, "📌")
                    lines.append(f"  {i}. {emoji} {cat} — {amt:,.0f} {currency}")

            await context.bot.send_message(chat_id=user_id, text="\n".join(lines))
            _last_digest_sent[user_id] = today_str

        except Exception as e:
            logger.error(f"Weekly digest failed for {user_id}: {e}")


# --- Subscriptions ---

async def sub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "/sub Netflix 1500\n"
            "/sub Аренда 45000 15 — 15-го числа\n"
            "/sub Spotify 500 EUR 1 подписки\n\n"
            "/subs — мои подписки\n"
            "/unsub Netflix — отменить"
        )
        return

    user_id = update.effective_user.id

    # Parse: scan args until we find a number (amount)
    name_parts = []
    amount = None
    rest_args = []
    for i, arg in enumerate(context.args):
        try:
            amount = float(arg)
            rest_args = list(context.args[i + 1:])
            break
        except ValueError:
            name_parts.append(arg)

    if not name_parts or amount is None or amount <= 0:
        await update.message.reply_text("Формат: /sub <название> <сумма> [день] [валюта] [категория]")
        return

    name = " ".join(name_parts)
    currency = await get_default_currency(user_id)
    day_of_month = 1
    category = "подписки"

    for arg in rest_args:
        try:
            d = int(arg)
            if 1 <= d <= 31:
                day_of_month = d
                continue
        except ValueError:
            pass
        if len(arg) == 3 and arg.upper() in ("EUR", "USD", "RSD", "RUB"):
            currency = arg.upper()
        elif arg.upper() in CURRENCY_ALIASES.values() or arg.lower() in CURRENCY_ALIASES:
            currency = CURRENCY_ALIASES.get(arg.lower(), arg.upper())
        elif arg.lower() in CATEGORIES:
            category = arg.lower()
        else:
            category = arg.lower()

    await add_subscription(user_id, name, amount, currency, category, day_of_month)
    await update.message.reply_text(
        f"📌 Подписка добавлена: {name} — {amount:.0f} {currency}, "
        f"{day_of_month}-го числа [{category}]"
    )


async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    subs = await get_active_subscriptions(user_id)
    if not subs:
        await update.message.reply_text("Нет активных подписок.\nДобавить: /sub Netflix 1500")
        return

    lines = ["📌 Активные подписки:\n"]
    total = 0.0
    main_currency = None
    for i, s in enumerate(subs, 1):
        lines.append(f"{i}. {s['name']} — {s['amount']:.0f} {s['currency']} ({s['day_of_month']}-го) [{s['category']}]")
        total += s["amount"]
        if main_currency is None:
            main_currency = s["currency"]

    if main_currency and all(s["currency"] == main_currency for s in subs):
        lines.append(f"\n💰 Итого: {total:,.0f} {main_currency}/мес")

    lines.append("\nОтменить: /unsub <название>")
    await update.message.reply_text("\n".join(lines))


async def unsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /unsub <название>")
        return

    name = " ".join(context.args)
    if await deactivate_subscription(update.effective_user.id, name):
        await update.message.reply_text(f"Подписка «{name}» отменена.")
    else:
        await update.message.reply_text(f"Подписка «{name}» не найдена.")


async def process_subscriptions_job(context: ContextTypes.DEFAULT_TYPE):
    """Hourly job: auto-add expenses for due subscriptions."""
    subs = await get_all_due_subscriptions()
    for s in subs:
        try:
            user_id = s["user_id"]
            user_tz = await _get_user_tz(user_id)
            now_local = datetime.now(user_tz)

            # Check if today is the due day
            day = s["day_of_month"]
            last_day = calendar.monthrange(now_local.year, now_local.month)[1]
            due_day = min(day, last_day)

            if now_local.day != due_day or now_local.hour != 9:
                continue

            # Check not already charged this month (compare in user's timezone)
            if s["last_charged_at"]:
                last_utc = datetime.fromisoformat(s["last_charged_at"])
                last_local = last_utc.astimezone(user_tz)
                if last_local.year == now_local.year and last_local.month == now_local.month:
                    continue

            # Create expense
            await save_expense(
                user_id, s["amount"], s["currency"],
                s["category"], f"Подписка: {s['name']}", s["name"],
            )
            await mark_subscription_charged(s["id"], datetime.now(timezone.utc).isoformat())

            await context.bot.send_message(
                chat_id=user_id,
                text=f"📌 Подписка: {s['name']} — {s['amount']:.0f} {s['currency']}",
            )

            # Budget warning
            if s["category"]:
                start = datetime(now_local.year, now_local.month, 1, tzinfo=user_tz).astimezone(timezone.utc)
                if now_local.month == 12:
                    end = datetime(now_local.year + 1, 1, 1, tzinfo=user_tz).astimezone(timezone.utc)
                else:
                    end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=user_tz).astimezone(timezone.utc)

                budget = await get_budget(user_id, s["category"])
                if budget:
                    spent = await get_category_total(user_id, s["category"], start, end, currency=budget["currency"])
                    pct = spent / budget["amount"] * 100 if budget["amount"] > 0 else 0
                    if pct >= 100:
                        await context.bot.send_message(chat_id=user_id,
                            text=f"🔴 Бюджет на {s['category']} превышен: {spent:.0f}/{budget['amount']:.0f} {budget['currency']} ({pct:.0f}%)")
                    elif pct >= 80:
                        await context.bot.send_message(chat_id=user_id,
                            text=f"⚠️ Бюджет на {s['category']}: {spent:.0f}/{budget['amount']:.0f} {budget['currency']} ({pct:.0f}%)")

        except Exception as e:
            logger.error(f"Subscription job failed for sub {s.get('id')}: {e}")


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
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("clearall", clearall_cmd))
    app.add_handler(CommandHandler("currency", currency_cmd))
    app.add_handler(CommandHandler("timezone", timezone_cmd))
    app.add_handler(CommandHandler("budget", budget_cmd))
    app.add_handler(CommandHandler("budgets", budgets_cmd))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("email", email_cmd))
    app.add_handler(CommandHandler("sub", sub_cmd))
    app.add_handler(CommandHandler("subs", subs_cmd))
    app.add_handler(CommandHandler("unsub", unsub_cmd))
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
            BotCommand("top", "Топ расходов за месяц"),
            BotCommand("history", "Последние расходы"),
            BotCommand("search", "Поиск расходов"),
            BotCommand("delete", "Удалить расход"),
            BotCommand("edit", "Редактировать расход"),
            BotCommand("currency", "Валюта по умолчанию"),
            BotCommand("timezone", "Часовой пояс"),
            BotCommand("budget", "Бюджет по категории"),
            BotCommand("budgets", "Все бюджеты"),
            BotCommand("chart", "График расходов"),
            BotCommand("email", "Подключить почту"),
            BotCommand("sub", "Добавить подписку"),
            BotCommand("subs", "Мои подписки"),
            BotCommand("unsub", "Отменить подписку"),
        ])

        app.job_queue.run_repeating(check_emails_job, interval=7200, first=60, name="email_check")
        app.job_queue.run_repeating(weekly_digest_job, interval=3600, first=120, name="weekly_digest")
        app.job_queue.run_repeating(process_subscriptions_job, interval=3600, first=180, name="subscriptions")
        logger.info("Jobs scheduled: email (2h), weekly digest (1h), subscriptions (1h)")

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

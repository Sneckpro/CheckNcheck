import os
import json
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

MODEL = "gpt-4o-mini"

CATEGORIES = ["еда", "продукты", "транспорт", "покупки", "развлечения",
              "здоровье", "жильё", "подписки", "другое"]


async def parse_receipt_photo(image_url: str) -> dict | None:
    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=512,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Extract expense data from this receipt photo. "
                "Return JSON with fields:\n"
                '- "amount": number (total amount)\n'
                '- "currency": string (3-letter code, e.g. "RSD", "EUR", "USD")\n'
                '- "merchant": string (store/restaurant name)\n'
                f'- "category": one of {CATEGORIES}\n'
                '- "description": string (brief summary of purchase)\n\n'
                "If you can't read the receipt, return {\"error\": true}."
            )},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": "Parse this receipt."},
            ]},
        ],
    )
    try:
        result = json.loads(response.choices[0].message.content)
        if result.get("error"):
            return None
        return result
    except (json.JSONDecodeError, KeyError):
        return None


async def parse_text_expense(text: str, default_currency: str = "RSD") -> dict | None:
    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=256,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Parse an expense from the user's message. "
                f"Default currency if not specified: {default_currency}. "
                "Return JSON with fields:\n"
                '- "amount": number\n'
                '- "currency": string (3-letter code)\n'
                f'- "category": one of {CATEGORIES}\n'
                '- "description": string (what was purchased)\n'
                '- "merchant": string or null\n\n'
                "Currency hints: евро/eur=EUR, долларов/usd=USD, динар/rsd=RSD, рублей/rub=RUB.\n"
                "If the message is not about an expense, return {\"error\": true}."
            )},
            {"role": "user", "content": text},
        ],
    )
    try:
        result = json.loads(response.choices[0].message.content)
        if result.get("error"):
            return None
        return result
    except (json.JSONDecodeError, KeyError):
        return None


async def parse_forwarded_expense(text: str, default_currency: str = "RSD") -> dict | None:
    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=256,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Extract expense data from this forwarded message (order confirmation, payment receipt, etc). "
                f"Default currency: {default_currency}. "
                "Return JSON with fields:\n"
                '- "amount": number\n'
                '- "currency": string (3-letter code)\n'
                f'- "category": one of {CATEGORIES}\n'
                '- "description": string\n'
                '- "merchant": string or null\n\n'
                "If this is not an expense/payment message, return {\"error\": true}."
            )},
            {"role": "user", "content": text},
        ],
    )
    try:
        result = json.loads(response.choices[0].message.content)
        if result.get("error"):
            return None
        return result
    except (json.JSONDecodeError, KeyError):
        return None


async def parse_email_receipt(sender: str, subject: str, body: str,
                              default_currency: str = "RSD") -> dict | None:
    text = f"From: {sender}\nSubject: {subject}\n\n{body[:2000]}"
    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=256,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Extract expense/payment data from this email. "
                f"Default currency: {default_currency}. "
                "Return JSON with fields:\n"
                '- "amount": number\n'
                '- "currency": string (3-letter code)\n'
                f'- "category": one of {CATEGORIES}\n'
                '- "description": string\n'
                '- "merchant": string or null\n\n'
                "IMPORTANT: Only extract OUTGOING expenses (purchases, orders, subscriptions, deliveries). "
                "If this email is about INCOMING money (salary, payroll, refund, cashback, "
                "bank transfer TO user, payment FROM employer/company to user), return {\"error\": true}.\n"
                "If this email is NOT a receipt/payment/order confirmation, return {\"error\": true}."
            )},
            {"role": "user", "content": text},
        ],
    )
    try:
        result = json.loads(response.choices[0].message.content)
        if result.get("error"):
            return None
        return result
    except (json.JSONDecodeError, KeyError):
        return None


async def generate_expense_report(expenses: list[dict], period_name: str,
                                   target_currency: str | None = None,
                                   days_passed: int | None = None,
                                   days_left: int | None = None,
                                   budget_amount: float | None = None,
                                   prev_period_total: float | None = None) -> str:
    if not expenses:
        return f"Нет расходов за {period_name}."

    from currency import convert

    EMOJI = {
        "еда": "\U0001f354", "продукты": "\U0001f6d2", "транспорт": "\U0001f697",
        "покупки": "\U0001f6cd\ufe0f", "развлечения": "\U0001f3ac",
        "здоровье": "\U0001f3e5", "жильё": "\U0001f3e0",
        "подписки": "\U0001f4f1", "другое": "\U0001f4cc",
    }

    # Group by category, convert to target currency if set
    by_category: dict[str, float] = {}
    total = 0.0
    display_cur = target_currency or "RSD"
    has_mixed = len(set(e.get("currency", "RSD") for e in expenses)) > 1

    for e in expenses:
        cat = e.get("category") or "другое"
        amount = e["amount"]
        cur = e.get("currency", "RSD")

        if target_currency and cur != target_currency:
            converted = convert(amount, cur, target_currency)
            if converted is not None:
                amount = converted
            # else keep original — can't convert
        elif not target_currency:
            display_cur = cur

        by_category[cat] = by_category.get(cat, 0) + amount
        total += amount

    lines = [f"📊 Расходы за {period_name}\n"]
    for cat, amount in sorted(by_category.items(), key=lambda x: -x[1]):
        emoji = EMOJI.get(cat, "📌")
        lines.append(f"{emoji} {cat.capitalize()}: {amount:.2f} {display_cur}")
    lines.append("─────────────")
    lines.append(f"💰 Итого: {total:.2f} {display_cur}")
    if has_mixed and target_currency:
        lines.append(f"(конвертировано в {target_currency} по текущему курсу)")

    # Analytics
    if days_passed and days_passed > 0:
        avg_per_day = total / days_passed
        lines.append(f"\n📈 В среднем: {avg_per_day:.0f} {display_cur}/день")

        if days_left is not None and days_left > 0:
            if budget_amount and budget_amount > total:
                remaining = budget_amount - total
                can_spend = remaining / days_left
                lines.append(f"📅 Осталось {days_left} дн. → можно ~{can_spend:.0f} {display_cur}/день")
            elif budget_amount:
                over = total - budget_amount
                lines.append(f"📅 Осталось {days_left} дн. Бюджет превышен на {over:.0f} {display_cur}")

    if prev_period_total is not None and prev_period_total > 0:
        change = ((total - prev_period_total) / prev_period_total) * 100
        sign = "+" if change >= 0 else ""
        lines.append(f"📊 vs прошлый: {sign}{change:.0f}% (было {prev_period_total:.0f} {display_cur})")

    return "\n".join(lines)

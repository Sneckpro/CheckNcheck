import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

MODEL = "gpt-4o-mini"

CATEGORIES = ["еда", "продукты", "транспорт", "покупки", "развлечения",
              "здоровье", "жильё", "подписки", "другое"]


async def parse_receipt_photo(image_url: str) -> dict | None:
    response = client.chat.completions.create(
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
    response = client.chat.completions.create(
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
    response = client.chat.completions.create(
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


async def generate_expense_report(expenses: list[dict], period_name: str) -> str:
    if not expenses:
        return f"Нет расходов за {period_name}."

    # Group by category
    by_category: dict[str, float] = {}
    total = 0.0
    currency = expenses[0].get("currency", "RSD")
    for e in expenses:
        cat = e.get("category") or "другое"
        by_category[cat] = by_category.get(cat, 0) + e["amount"]
        total += e["amount"]

    EMOJI = {
        "еда": "\U0001f354", "продукты": "\U0001f6d2", "транспорт": "\U0001f697",
        "покупки": "\U0001f6cd\ufe0f", "развлечения": "\U0001f3ac",
        "здоровье": "\U0001f3e5", "жильё": "\U0001f3e0",
        "подписки": "\U0001f4f1", "другое": "\U0001f4cc",
    }

    lines = [f"📊 Расходы за {period_name}\n"]
    for cat, amount in sorted(by_category.items(), key=lambda x: -x[1]):
        emoji = EMOJI.get(cat, "📌")
        lines.append(f"{emoji} {cat.capitalize()}: {amount:.2f} {currency}")
    lines.append("─────────────")
    lines.append(f"💰 Итого: {total:.2f} {currency}")

    return "\n".join(lines)

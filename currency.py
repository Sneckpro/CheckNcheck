import urllib.request
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_cache: dict = {"rates": {}, "updated_at": None}
CACHE_TTL = 3600  # 1 hour


def get_rates(base: str = "USD") -> dict[str, float]:
    """Fetch exchange rates. Uses free API, caches for 1 hour."""
    now = datetime.now(timezone.utc)
    if _cache["rates"] and _cache["updated_at"]:
        age = (now - _cache["updated_at"]).total_seconds()
        if age < CACHE_TTL:
            return _cache["rates"]

    try:
        url = f"https://open.er-api.com/v6/latest/{base}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("result") == "success":
            _cache["rates"] = data["rates"]
            _cache["updated_at"] = now
            return _cache["rates"]
    except Exception as e:
        logger.error(f"Currency API error: {e}")

    return _cache.get("rates", {})


def convert(amount: float, from_cur: str, to_cur: str) -> float | None:
    """Convert amount between currencies. Returns None if rate unavailable."""
    if from_cur == to_cur:
        return amount
    rates = get_rates("USD")
    if not rates:
        return None
    from_rate = rates.get(from_cur)
    to_rate = rates.get(to_cur)
    if not from_rate or not to_rate:
        return None
    # amount in from_cur → USD → to_cur
    return amount / from_rate * to_rate

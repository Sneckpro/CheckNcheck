import aiosqlite
import os
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "expenses.db"))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RSD',
                category TEXT,
                description TEXT,
                merchant TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                default_currency TEXT DEFAULT 'RSD',
                timezone TEXT
            )
        """)
        await db.commit()


async def save_expense(user_id: int, amount: float, currency: str,
                       category: str | None = None, description: str | None = None,
                       merchant: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO expenses (user_id, amount, currency, category, description, merchant, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, amount, currency, category, description, merchant,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_expenses(user_id: int, since: datetime | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if since:
            cursor = await db.execute(
                "SELECT id, amount, currency, category, description, merchant, created_at "
                "FROM expenses WHERE user_id = ? AND created_at >= ? ORDER BY created_at ASC",
                (user_id, since.isoformat()),
            )
        else:
            cursor = await db.execute(
                "SELECT id, amount, currency, category, description, merchant, created_at "
                "FROM expenses WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_recent_expenses(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, amount, currency, category, description, merchant, created_at "
            "FROM expenses WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_expense(expense_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_default_currency(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT default_currency FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else "RSD"


async def set_default_currency(user_id: int, currency: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (user_id, default_currency) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET default_currency = ?",
            (user_id, currency, currency),
        )
        await db.commit()


async def get_timezone(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT timezone FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_timezone(user_id: int, tz: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (user_id, timezone) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET timezone = ?",
            (user_id, tz, tz),
        )
        await db.commit()

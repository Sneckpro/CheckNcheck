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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                UNIQUE(user_id, category)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email_uid TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                UNIQUE(user_id, email_uid)
            )
        """)
        for col in ("email_server TEXT", "email_address TEXT", "email_password TEXT", "email_enabled INTEGER DEFAULT 0"):
            try:
                await db.execute(f"ALTER TABLE user_settings ADD COLUMN {col}")
            except Exception:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_expenses_user_cat_date ON expenses(user_id, category, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_processed_emails_user_uid ON processed_emails(user_id, email_uid)")
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


async def get_expenses(user_id: int, since: datetime | None = None,
                       until: datetime | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        conditions = ["user_id = ?"]
        params: list = [user_id]
        if since:
            conditions.append("created_at >= ?")
            params.append(since.isoformat())
        if until:
            conditions.append("created_at < ?")
            params.append(until.isoformat())
        where = " AND ".join(conditions)
        cursor = await db.execute(
            f"SELECT id, amount, currency, category, description, merchant, created_at "
            f"FROM expenses WHERE {where} ORDER BY created_at ASC",
            params,
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


async def get_top_expenses(user_id: int, since: datetime, until: datetime,
                           limit: int = 5) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, amount, currency, category, description, merchant, created_at "
            "FROM expenses WHERE user_id = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY amount DESC LIMIT ?",
            (user_id, since.isoformat(), until.isoformat(), limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def clear_all_expenses(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount


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


# --- Budgets ---

async def set_budget(user_id: int, category: str, amount: float, currency: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO budgets (user_id, category, amount, currency) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, category) DO UPDATE SET amount = ?, currency = ?",
            (user_id, category, amount, currency, amount, currency),
        )
        await db.commit()


async def get_budget(user_id: int, category: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT category, amount, currency FROM budgets WHERE user_id = ? AND category = ?",
            (user_id, category),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_budgets(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT category, amount, currency FROM budgets WHERE user_id = ? ORDER BY amount DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_budget(user_id: int, category: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM budgets WHERE user_id = ? AND category = ?", (user_id, category)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_total_spent(user_id: int, since: datetime, until: datetime,
                          currency: str | None = None) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        query = ("SELECT COALESCE(SUM(amount), 0) FROM expenses "
                 "WHERE user_id = ? AND created_at >= ? AND created_at < ?")
        params: list = [user_id, since.isoformat(), until.isoformat()]
        if currency:
            query += " AND currency = ?"
            params.append(currency)
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        return row[0]


async def get_category_total(user_id: int, category: str, since: datetime, until: datetime,
                             currency: str | None = None) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        query = ("SELECT COALESCE(SUM(amount), 0) FROM expenses "
                 "WHERE user_id = ? AND category = ? AND created_at >= ? AND created_at < ?")
        params: list = [user_id, category, since.isoformat(), until.isoformat()]
        if currency:
            query += " AND currency = ?"
            params.append(currency)
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        return row[0]


# --- Email settings ---

async def set_email_settings(user_id: int, server: str, address: str, password: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (user_id, email_server, email_address, email_password, email_enabled) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET email_server = ?, email_address = ?, email_password = ?, email_enabled = 1",
            (user_id, server, address, password, server, address, password),
        )
        await db.commit()


async def get_email_settings(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT email_server, email_address, email_password, email_enabled "
            "FROM user_settings WHERE user_id = ? AND email_enabled = 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def disable_email(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_settings SET email_enabled = 0 WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def get_all_email_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, email_server, email_address, email_password "
            "FROM user_settings WHERE email_enabled = 1"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def is_email_processed(user_id: int, email_uid: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM processed_emails WHERE user_id = ? AND email_uid = ?",
            (user_id, email_uid),
        )
        return await cursor.fetchone() is not None


async def mark_email_processed(user_id: int, email_uid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO processed_emails (user_id, email_uid, processed_at) VALUES (?, ?, ?)",
            (user_id, email_uid, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def clear_processed_emails(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM processed_emails WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        return cursor.rowcount

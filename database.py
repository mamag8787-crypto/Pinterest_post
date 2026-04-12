import aiosqlite
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH   = os.getenv("DB_PATH", "queue.db")
QUEUE_MAX = int(os.getenv("QUEUE_MAX", "200"))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS queue (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id        TEXT NOT NULL,
                file_unique_id TEXT NOT NULL UNIQUE,
                added_at       TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                retry_count    INTEGER NOT NULL DEFAULT 0,
                last_error     TEXT
            );
            CREATE TABLE IF NOT EXISTS posts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                file_unique_id TEXT NOT NULL,
                pin_id         TEXT,
                title          TEXT,
                posted_at      TEXT NOT NULL,
                success        INTEGER NOT NULL DEFAULT 1,
                error          TEXT
            );
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await db.commit()


# ── Очередь ──────────────────────────────────────────────────────────────────

async def add_to_queue(file_id: str, file_unique_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM queue WHERE status='pending'")
        pending_count = (await cursor.fetchone())[0]
        if pending_count >= QUEUE_MAX:
            return {"added": False, "reason": "limit", "queue_size": pending_count}
        try:
            await db.execute(
                "INSERT INTO queue (file_id, file_unique_id, added_at) VALUES (?,?,?)",
                (file_id, file_unique_id, datetime.utcnow().isoformat())
            )
            await db.commit()
            return {"added": True, "reason": "ok", "queue_size": pending_count + 1}
        except aiosqlite.IntegrityError:
            return {"added": False, "reason": "duplicate", "queue_size": pending_count}


async def get_next_pending(count: int = 1) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM queue WHERE status='pending' ORDER BY added_at ASC LIMIT ?", (count,)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def mark_posted(queue_id, file_unique_id, pin_id, title):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE queue SET status='posted' WHERE id=?", (queue_id,))
        await db.execute(
            "INSERT INTO posts (file_unique_id,pin_id,title,posted_at) VALUES (?,?,?,?)",
            (file_unique_id, pin_id, title, datetime.utcnow().isoformat())
        )
        await db.commit()


async def mark_failed(queue_id, file_unique_id, error):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE queue SET status='failed', last_error=? WHERE id=?", (error, queue_id)
        )
        await db.execute(
            "INSERT INTO posts (file_unique_id,posted_at,success,error) VALUES (?,?,0,?)",
            (file_unique_id, datetime.utcnow().isoformat(), error)
        )
        await db.commit()


async def mark_retry(queue_id, error):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE queue SET status='pending', retry_count=retry_count+1, last_error=? WHERE id=?",
            (error, queue_id)
        )
        await db.commit()


async def reset_failed_to_pending() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute(
            "UPDATE queue SET status='pending', retry_count=0, last_error=NULL WHERE status='failed'"
        )
        await db.commit()
        return c.rowcount


async def get_queue_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT status, COUNT(*) FROM queue GROUP BY status")
        counts = {r[0]: r[1] for r in await cursor.fetchall()}
        cursor = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE posted_at>=datetime('now','-7 days') AND success=1"
        )
        week_posted = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE posted_at>=datetime('now','-7 days') AND success=0"
        )
        week_errors = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM posts WHERE success=1")
        total_posted = (await cursor.fetchone())[0]
        return {
            "pending": counts.get("pending", 0),
            "posted_total_queue": counts.get("posted", 0),
            "failed": counts.get("failed", 0),
            "week_posted": week_posted,
            "week_errors": week_errors,
            "total_posted": total_posted,
            "queue_max": QUEUE_MAX,
        }


# ── Состояние (пауза) ─────────────────────────────────────────────────────────

async def set_state(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await db.commit()


async def get_state(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM state WHERE key=?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def is_paused() -> bool:
    return await get_state("paused", "0") == "1"

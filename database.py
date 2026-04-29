import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).parent / "diagnostics.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue TEXT NOT NULL,
                keywords TEXT,
                diagnosis TEXT NOT NULL,
                solution TEXT NOT NULL,
                urgency TEXT DEFAULT 'Medium'
            )
        """)
        # Full‑text search table (FTS5)
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS diag_fts USING fts5(
                issue, keywords, diagnosis, solution,
                content='diagnostics', content_rowid='id'
            )
        """)
        # Keep FTS in sync
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS diag_ai AFTER INSERT ON diagnostics BEGIN
                INSERT INTO diag_fts(rowid, issue, keywords, diagnosis, solution)
                VALUES (new.id, new.issue, new.keywords, new.diagnosis, new.solution);
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS diag_ad AFTER DELETE ON diagnostics BEGIN
                INSERT INTO diag_fts(diag_fts, rowid, issue, keywords, diagnosis, solution)
                VALUES('delete', old.id, old.issue, old.keywords, old.diagnosis, old.solution);
            END
        """)
        # Insert sample data if empty
        cursor = await db.execute("SELECT COUNT(*) FROM diagnostics")
        count = (await cursor.fetchone())[0]
        if count == 0:
            samples = [
                ("Engine overheating", "overheat,hot,temp", "Coolant leak or faulty thermostat",
                 "Check coolant level, radiator cap, thermostat", "High"),
                ("ABS warning light", "abs,brake", "Wheel speed sensor or module failure",
                 "Inspect sensors and wiring", "Medium"),
                ("Turbo underboost", "turbo,boost,pressure", "Boost leak or wastegate stuck",
                 "Smoke test the intake, check vacuum lines", "Medium"),
                ("DEF contamination", "def,scr,adblue", "Mixed fluids or bad DEF quality",
                 "Drain and flush DEF system, refill with certified DEF", "High")
            ]
            await db.executemany(
                "INSERT INTO diagnostics(issue, keywords, diagnosis, solution, urgency) VALUES(?,?,?,?,?)",
                samples
            )
            await db.commit()

async def get_diagnosis(text: str, limit: int = 3):
    async with aiosqlite.connect(DB_PATH) as db:
        # Try FTS5 phrase search
        try:
            cursor = await db.execute("""
                SELECT d.id, d.issue, d.diagnosis, d.solution, d.urgency
                FROM diagnostics d
                JOIN diag_fts fts ON d.id = fts.rowid
                WHERE diag_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (f'"{text}"', limit))
            rows = await cursor.fetchall()
            if rows:
                return rows
        except Exception:
            pass
        # Fallback LIKE search
        cursor = await db.execute("""
            SELECT id, issue, diagnosis, solution, urgency FROM diagnostics
            WHERE issue LIKE ? OR keywords LIKE ?
            LIMIT ?
        """, (f"%{text}%", f"%{text}%", limit))
        return await cursor.fetchall()

async def log_feedback(user_id: int, query: str, rating: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                query TEXT,
                rating INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("INSERT INTO feedback(user_id, query, rating) VALUES(?,?,?)",
                         (user_id, query, rating))
        await db.commit()

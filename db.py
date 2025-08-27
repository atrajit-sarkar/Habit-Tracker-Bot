import sqlite3
import threading
import json
from datetime import datetime, time


class StreakDB:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            # Original streaks table
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS streaks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    task_id INTEGER DEFAULT 1,
                    UNIQUE(user_id, date, task_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
                """
            )
            
            # Tasks table for custom habit tracking
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    frequency TEXT DEFAULT 'daily',
                    schedule_time TEXT,
                    created_at TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1
                )
                """
            )
            
            # Schedules table for automated polls
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    schedule_time TEXT NOT NULL,
                    timezone TEXT DEFAULT 'UTC',
                    is_active INTEGER DEFAULT 1,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
                """
            )
            
            # Insert default "Study" task if not exists
            cur.execute(
                "INSERT OR IGNORE INTO tasks (id, user_id, name, description, created_at) VALUES (1, 0, 'Study', 'Default study habit', ?)",
                (datetime.now().isoformat(),)
            )
            
            conn.commit()
            conn.close()

    def add_streak_if_missing(self, user_id: int, date_iso: str, task_id: int = 1) -> bool:
        """Add a record for user/date/task. Returns True if inserted, False if already exists."""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO streaks (user_id, date, task_id) VALUES (?, ?, ?)",
                    (user_id, date_iso, task_id),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            finally:
                conn.close()

    def get_dates_for_month(self, user_id: int, year: int, month: int, task_id: int = None):
        """Get dates for a specific month, optionally filtered by task_id"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            if task_id:
                cur.execute(
                    "SELECT date FROM streaks WHERE user_id=? AND date LIKE ? AND task_id=?",
                    (user_id, f"{year:04d}-{month:02d}-%", task_id),
                )
            else:
                cur.execute(
                    "SELECT date FROM streaks WHERE user_id=? AND date LIKE ?",
                    (user_id, f"{year:04d}-{month:02d}-%"),
                )
            rows = cur.fetchall()
            conn.close()
        return [r[0] for r in rows]

    def total_days(self, user_id: int, task_id: int = None):
        """Get total days recorded, optionally filtered by task_id"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            if task_id:
                cur.execute("SELECT COUNT(*) FROM streaks WHERE user_id=? AND task_id=?", (user_id, task_id))
            else:
                cur.execute("SELECT COUNT(*) FROM streaks WHERE user_id=?", (user_id,))
            (n,) = cur.fetchone()
            conn.close()
        return n

    # Task management methods
    def create_task(self, user_id: int, name: str, description: str = "", frequency: str = "daily", schedule_time: str = "") -> int:
        """Create a new task and return its ID"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO tasks (user_id, name, description, frequency, schedule_time, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, name, description, frequency, schedule_time, datetime.now().isoformat())
            )
            task_id = cur.lastrowid
            conn.commit()
            conn.close()
        return task_id

    def get_user_tasks(self, user_id: int, active_only: bool = True):
        """Get all tasks for a user"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            if active_only:
                cur.execute(
                    "SELECT id, name, description, frequency, schedule_time, created_at FROM tasks WHERE user_id=? AND is_active=1 ORDER BY name",
                    (user_id,)
                )
            else:
                cur.execute(
                    "SELECT id, name, description, frequency, schedule_time, created_at FROM tasks WHERE user_id=? ORDER BY name",
                    (user_id,)
                )
            rows = cur.fetchall()
            conn.close()
        return rows

    def delete_task(self, user_id: int, task_id: int) -> bool:
        """Mark a task as inactive (soft delete) and clean up associated data"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            
            # First, verify the task belongs to the user
            cur.execute(
                "SELECT id FROM tasks WHERE id=? AND user_id=? AND is_active=1",
                (task_id, user_id)
            )
            if not cur.fetchone():
                conn.close()
                return False
            
            # Mark task as inactive
            cur.execute(
                "UPDATE tasks SET is_active=0 WHERE id=? AND user_id=?",
                (task_id, user_id)
            )
            task_affected = cur.rowcount
            
            # Also deactivate any schedules for this task
            cur.execute(
                "UPDATE schedules SET is_active=0 WHERE task_id=? AND user_id=?",
                (task_id, user_id)
            )
            
            # Note: We keep streak data for historical purposes, but it won't show
            # in reports because the task is inactive
            
            conn.commit()
            conn.close()
        return task_affected > 0

    def get_task_by_id(self, task_id: int, user_id: int = None):
        """Get task details by ID"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            if user_id:
                cur.execute(
                    "SELECT id, name, description, frequency, schedule_time, created_at FROM tasks WHERE id=? AND user_id=? AND is_active=1",
                    (task_id, user_id)
                )
            else:
                cur.execute(
                    "SELECT id, name, description, frequency, schedule_time, created_at FROM tasks WHERE id=? AND is_active=1",
                    (task_id,)
                )
            row = cur.fetchone()
            conn.close()
        return row

    # Schedule management methods
    def create_schedule(self, user_id: int, task_id: int, schedule_time: str, timezone: str = 'UTC') -> int:
        """Create a schedule for automated polls"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO schedules (user_id, task_id, schedule_time, timezone) VALUES (?, ?, ?, ?)",
                (user_id, task_id, schedule_time, timezone)
            )
            schedule_id = cur.lastrowid
            conn.commit()
            conn.close()
        return schedule_id

    def get_user_schedules(self, user_id: int):
        """Get all active schedules for a user"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT s.id, s.task_id, t.name, s.schedule_time, s.timezone 
                FROM schedules s 
                JOIN tasks t ON s.task_id = t.id 
                WHERE s.user_id=? AND s.is_active=1 AND t.is_active=1
                ORDER BY s.schedule_time
                """,
                (user_id,)
            )
            rows = cur.fetchall()
            conn.close()
        return rows

    def delete_schedule(self, user_id: int, schedule_id: int) -> bool:
        """Delete a schedule"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE schedules SET is_active=0 WHERE id=? AND user_id=?",
                (schedule_id, user_id)
            )
            affected = cur.rowcount
            conn.commit()
            conn.close()
        return affected > 0

    def get_task_stats(self, user_id: int):
        """Get statistics for all user tasks"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.id, t.name, COUNT(s.id) as total_days,
                       COUNT(CASE WHEN s.date LIKE strftime('%Y-%m-%%', 'now') THEN 1 END) as this_month
                FROM tasks t
                LEFT JOIN streaks s ON t.id = s.task_id AND s.user_id = ?
                WHERE t.user_id = ? AND t.is_active = 1
                GROUP BY t.id, t.name
                ORDER BY total_days DESC
                """,
                (user_id, user_id)
            )
            rows = cur.fetchall()
            conn.close()
        return rows

    # --- Additional metadata helpers ---
    def get_task_last_done(self, user_id: int, task_id: int):
        """Return the most recent completion date (ISO string) for a task, or None"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(date) FROM streaks WHERE user_id=? AND task_id=?",
                (user_id, task_id)
            )
            row = cur.fetchone()
            conn.close()
        return row[0] if row and row[0] else None

    def get_task_month_count(self, user_id: int, task_id: int):
        """Return number of completions for the current calendar month"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            # YYYY-MM-
            cur.execute(
                "SELECT COUNT(*) FROM streaks WHERE user_id=? AND task_id=? AND date LIKE strftime('%Y-%m-%%', 'now')",
                (user_id, task_id)
            )
            n = cur.fetchone()[0]
            conn.close()
        return n

    def get_schedules_for_task(self, user_id: int, task_id: int):
        """Return active schedules for a specific task"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, schedule_time, timezone FROM schedules WHERE user_id=? AND task_id=? AND is_active=1 ORDER BY schedule_time",
                (user_id, task_id)
            )
            rows = cur.fetchall()
            conn.close()
        return rows

    def get_task_streaks(self, user_id: int, task_id: int):
        """Compute (current_streak, best_streak) in days for a task"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT date FROM streaks WHERE user_id=? AND task_id=?",
                (user_id, task_id)
            )
            rows = cur.fetchall()
            conn.close()

        if not rows:
            return 0, 0

        from datetime import datetime, timedelta
        dates = sorted({datetime.fromisoformat(r[0]).date() for r in rows})
        date_set = set(dates)

        # Current streak: count back from today
        today = datetime.now().date()
        cur_streak = 0
        d = today
        while d in date_set:
            cur_streak += 1
            d = d - timedelta(days=1)

        # Best streak: longest consecutive run
        best = 1
        run = 1
        for i in range(1, len(dates)):
            if (dates[i] - dates[i-1]).days == 1:
                run += 1
                if run > best:
                    best = run
            else:
                run = 1
        best_streak = best if dates else 0
        return cur_streak, best_streak

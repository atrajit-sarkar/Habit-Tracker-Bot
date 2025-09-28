import os
import threading
import sqlite3
from datetime import datetime, timedelta, date
from typing import List, Tuple, Optional

try:
    from google.cloud import firestore  # type: ignore
    from google.cloud.firestore_v1 import FieldFilter  # type: ignore
except Exception as _e:  # allow import in environments without package
    firestore = None  # type: ignore
    FieldFilter = None  # type: ignore


class FirestoreDB:
    """
    Firestore-backed implementation matching the StreakDB interface used by the bot.

    Data layout:
      - Collection users/{user_id}/tasks/{task_id}
      - Collection users/{user_id}/streaks/{date}_{task_id}
      - Collection users/{user_id}/schedules/{schedule_id}

    All IDs for tasks/schedules are integers preserved from SQLite for compatibility.
    """

    def __init__(self, sqlite_path: str | None = None, project_id: Optional[str] = None):
        if firestore is None:
            raise RuntimeError("google-cloud-firestore is not installed. Install it to use FirestoreDB.")

        # Initialize client (uses GOOGLE_APPLICATION_CREDENTIALS by default)
        self.client = firestore.Client(project=project_id)  # type: ignore
        self._lock = threading.Lock()
        self.sqlite_path = sqlite_path

        # One-time migration from SQLite if Firestore is empty
        try:
            self._maybe_migrate_from_sqlite()
        except Exception as e:
            print(f"⚠️ Firestore migration skipped due to error: {e}")

    # ---------- Utility helpers ----------
    def _user_col(self, user_id: int):
        return self.client.collection("users").document(str(user_id))

    def _tasks_col(self, user_id: int):
        return self._user_col(user_id).collection("tasks")

    def _streaks_col(self, user_id: int):
        return self._user_col(user_id).collection("streaks")

    def _schedules_col(self, user_id: int):
        return self._user_col(user_id).collection("schedules")

    def _next_id(self, docs) -> int:
        max_id = 0
        for d in docs:
            try:
                max_id = max(max_id, int(d.id))
            except Exception:
                continue
        return max_id + 1

    def _maybe_migrate_from_sqlite(self):
        # If there is already at least one task document anywhere, assume migrated
        any_user = list(self.client.collection("users").list_documents(page_size=1))
        if any_user:
            return

        if not self.sqlite_path:
            return
        if not os.path.exists(self.sqlite_path):
            return

        print("🔁 Migrating existing SQLite data to Firestore...")
        conn = sqlite3.connect(self.sqlite_path)
        cur = conn.cursor()

        # Check if there is any data
        try:
            cur.execute("SELECT COUNT(*) FROM tasks")
            task_count = cur.fetchone()[0]
        except Exception:
            task_count = 0
        try:
            cur.execute("SELECT COUNT(*) FROM streaks")
            streak_count = cur.fetchone()[0]
        except Exception:
            streak_count = 0
        try:
            cur.execute("SELECT COUNT(*) FROM schedules")
            schedule_count = cur.fetchone()[0]
        except Exception:
            schedule_count = 0

        if task_count == 0 and streak_count == 0 and schedule_count == 0:
            conn.close()
            print("ℹ️ No SQLite data found to migrate.")
            return

        # Migrate tasks
        cur.execute("SELECT id, user_id, name, description, frequency, schedule_time, created_at, is_active FROM tasks")
        tasks_rows = cur.fetchall()
        for (tid, user_id, name, description, frequency, schedule_time, created_at, is_active) in tasks_rows:
            self._tasks_col(user_id).document(str(tid)).set({
                "id": int(tid),
                "user_id": int(user_id),
                "name": name,
                "description": description,
                "frequency": frequency,
                "schedule_time": schedule_time,
                "created_at": created_at,
                "is_active": int(is_active),
            })

        # Migrate streaks
        cur.execute("SELECT user_id, date, task_id FROM streaks")
        streak_rows = cur.fetchall()
        for (user_id, date_iso, task_id) in streak_rows:
            doc_id = f"{date_iso}_{int(task_id)}"
            self._streaks_col(user_id).document(doc_id).set({
                "user_id": int(user_id),
                "date": date_iso,
                "task_id": int(task_id),
            })

        # Migrate schedules
        try:
            cur.execute("SELECT id, user_id, task_id, schedule_time, timezone, is_active FROM schedules")
            schedule_rows = cur.fetchall()
        except Exception:
            schedule_rows = []
        for (sid, user_id, task_id, schedule_time, timezone, is_active) in schedule_rows:
            self._schedules_col(user_id).document(str(sid)).set({
                "id": int(sid),
                "user_id": int(user_id),
                "task_id": int(task_id),
                "schedule_time": schedule_time,
                "timezone": timezone or "UTC",
                "is_active": int(is_active),
            })

        conn.close()
        print("✅ Migration to Firestore completed.")

    # ---------- Public API (compat) ----------
    def add_streak_if_missing(self, user_id: int, date_iso: str, task_id: int = 1) -> bool:
        with self._lock:
            doc_id = f"{date_iso}_{task_id}"
            doc_ref = self._streaks_col(user_id).document(doc_id)
            if doc_ref.get().exists:
                return False
            doc_ref.set({"user_id": user_id, "date": date_iso, "task_id": task_id})
            return True

    def get_dates_for_month(self, user_id: int, year: int, month: int, task_id: int | None = None) -> List[str]:
        with self._lock:
            start = f"{year:04d}-{month:02d}-01"
            # max 31 to include all days
            end = f"{year:04d}-{month:02d}-31"
            q = self._streaks_col(user_id).where(filter=FieldFilter("date", ">=", start)).where(filter=FieldFilter("date", "<=", end))
            if task_id:
                q = q.where(filter=FieldFilter("task_id", "==", int(task_id)))
            docs = q.stream()
            return [d.to_dict().get("date") for d in docs]

    def total_days(self, user_id: int, task_id: int | None = None) -> int:
        with self._lock:
            q = self._streaks_col(user_id)
            if task_id:
                q = q.where(filter=FieldFilter("task_id", "==", int(task_id)))
            return sum(1 for _ in q.stream())

    # Task management
    def create_task(self, user_id: int, name: str, description: str = "", frequency: str = "daily", schedule_time: str = "") -> int:
        with self._lock:
            # determine next id
            docs = list(self._tasks_col(user_id).stream())
            next_id = self._next_id(docs)
            self._tasks_col(user_id).document(str(next_id)).set({
                "id": next_id,
                "user_id": user_id,
                "name": name,
                "description": description,
                "frequency": frequency,
                "schedule_time": schedule_time,
                "created_at": datetime.now().isoformat(),
                "is_active": 1,
            })
            return next_id

    def get_user_tasks(self, user_id: int, active_only: bool = True):
        with self._lock:
            q = self._tasks_col(user_id)
            if active_only:
                # Keep for backward compatibility; with hard deletes, tasks simply won't exist.
                q = q.where(filter=FieldFilter("is_active", "==", 1))
            docs = sorted(q.stream(), key=lambda d: d.to_dict().get("name", ""))
            rows = []
            for d in docs:
                v = d.to_dict()
                rows.append((v.get("id"), v.get("name"), v.get("description"), v.get("frequency"), v.get("schedule_time"), v.get("created_at")))
            return rows

    def delete_task(self, user_id: int, task_id: int) -> bool:
        """Hard delete the task and all related schedules and streaks."""
        with self._lock:
            task_ref = self._tasks_col(user_id).document(str(task_id))
            if not task_ref.get().exists:
                return False

            batch = self.client.batch()
            deletions = 0

            # Delete task document
            batch.delete(task_ref)
            deletions += 1

            # Delete schedules for this task
            sched_query = self._schedules_col(user_id).where(filter=FieldFilter("task_id", "==", int(task_id)))
            pending = []
            for d in sched_query.stream():
                pending.append(d.reference)
            for ref in pending:
                batch.delete(ref)
                deletions += 1
                if deletions % 450 == 0:
                    batch.commit()
                    batch = self.client.batch()

            # Delete streaks for this task
            streak_query = self._streaks_col(user_id).where(filter=FieldFilter("task_id", "==", int(task_id)))
            pending = []
            for d in streak_query.stream():
                pending.append(d.reference)
            for ref in pending:
                batch.delete(ref)
                deletions += 1
                if deletions % 450 == 0:
                    batch.commit()
                    batch = self.client.batch()

            batch.commit()
            return True

    def get_task_by_id(self, task_id: int, user_id: int | None = None):
        with self._lock:
            if user_id is None:
                # Not expected in current bot, but provide a fallback search across users (slow)
                for user_doc in self.client.collection("users").stream():
                    snap = user_doc.reference.collection("tasks").document(str(task_id)).get()
                    if snap.exists and snap.to_dict().get("is_active", 1) == 1:
                        v = snap.to_dict()
                        return (v.get("id"), v.get("name"), v.get("description"), v.get("frequency"), v.get("schedule_time"), v.get("created_at"))
                return None
            snap = self._tasks_col(user_id).document(str(task_id)).get()
            if not snap.exists or snap.to_dict().get("is_active", 1) != 1:
                return None
            v = snap.to_dict()
            return (v.get("id"), v.get("name"), v.get("description"), v.get("frequency"), v.get("schedule_time"), v.get("created_at"))

    # Schedules
    def create_schedule(self, user_id: int, task_id: int, schedule_time: str, timezone: str = 'UTC') -> int:
        with self._lock:
            docs = list(self._schedules_col(user_id).stream())
            next_id = self._next_id(docs)
            self._schedules_col(user_id).document(str(next_id)).set({
                "id": next_id,
                "user_id": user_id,
                "task_id": int(task_id),
                "schedule_time": schedule_time,
                "timezone": timezone or "UTC",
                "is_active": 1,
            })
            return next_id

    def get_user_schedules(self, user_id: int):
        with self._lock:
            rows = []
            for d in self._schedules_col(user_id).where(filter=FieldFilter("is_active", "==", 1)).stream():
                s = d.to_dict()
                # join with task name
                t = self.get_task_by_id(int(s.get("task_id")), user_id)
                tname = t[1] if t else ""
                rows.append((s.get("id"), s.get("task_id"), tname, s.get("schedule_time"), s.get("timezone")))
            # Sort by schedule_time
            rows.sort(key=lambda r: (r[3] or ""))
            return rows

    def delete_schedule(self, user_id: int, schedule_id: int) -> bool:
        with self._lock:
            ref = self._schedules_col(user_id).document(str(schedule_id))
            snap = ref.get()
            if not snap.exists:
                return False
            ref.delete()
            return True

    def get_task_stats(self, user_id: int):
        with self._lock:
            # Build totals per task
            tasks = {t[0]: t for t in self.get_user_tasks(user_id)}
            totals = {tid: 0 for tid in tasks.keys()}
            for s in self._streaks_col(user_id).stream():
                tid = int(s.to_dict().get("task_id", 1))
                if tid in totals:
                    totals[tid] += 1
            # This month
            today = date.today()
            start = f"{today.year:04d}-{today.month:02d}-01"
            end = f"{today.year:04d}-{today.month:02d}-31"
            month_counts = {tid: 0 for tid in tasks.keys()}
            for s in self._streaks_col(user_id).where(filter=FieldFilter("date", ">=", start)).where(filter=FieldFilter("date", "<=", end)).stream():
                tid = int(s.to_dict().get("task_id", 1))
                if tid in month_counts:
                    month_counts[tid] += 1
            rows = []
            for tid, t in tasks.items():
                rows.append((tid, t[1], totals.get(tid, 0), month_counts.get(tid, 0)))
            rows.sort(key=lambda r: r[2], reverse=True)
            return rows

    def get_task_last_done(self, user_id: int, task_id: int):
        with self._lock:
            dates = [s.to_dict().get("date") for s in self._streaks_col(user_id).where(filter=FieldFilter("task_id", "==", int(task_id))).stream()]
            if not dates:
                return None
            return max(dates)

    def get_task_month_count(self, user_id: int, task_id: int):
        with self._lock:
            today = date.today()
            start = f"{today.year:04d}-{today.month:02d}-01"
            end = f"{today.year:04d}-{today.month:02d}-31"
            return sum(1 for _ in self._streaks_col(user_id)
                       .where(filter=FieldFilter("task_id", "==", int(task_id)))
                       .where(filter=FieldFilter("date", ">=", start))
                       .where(filter=FieldFilter("date", "<=", end))
                       .stream())

    def get_schedules_for_task(self, user_id: int, task_id: int):
        with self._lock:
            rows = []
            for d in self._schedules_col(user_id).where(filter=FieldFilter("task_id", "==", int(task_id))).where(filter=FieldFilter("is_active", "==", 1)).stream():
                s = d.to_dict()
                rows.append((s.get("id"), s.get("schedule_time"), s.get("timezone")))
            rows.sort(key=lambda r: (r[1] or ""))
            return rows

    def get_task_streaks(self, user_id: int, task_id: int):
        with self._lock:
            rows = [s.to_dict().get("date") for s in self._streaks_col(user_id).where(filter=FieldFilter("task_id", "==", int(task_id))).stream()]
        if not rows:
            return 0, 0
        dates = sorted({datetime.fromisoformat(r).date() for r in rows})
        date_set = set(dates)
        today = datetime.now().date()
        cur_streak = 0
        d = today
        while d in date_set:
            cur_streak += 1
            d = d - timedelta(days=1)
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

    # --- Helpers used by bot scheduler (replacing raw SQL) ---
    def get_tasks_scheduled_at(self, time_str: str) -> List[Tuple[int, int, str, str]]:
        """Return list of (user_id, task_id, task_name, schedule_time) that are active and match time_str.

        Implementation detail:
          Uses a collection group query over all 'tasks' subcollections so we don't
          need a hard‑coded user list. Each task document stores user_id already.
          Falls back gracefully if collection group queries are unavailable.
        """
        results: List[Tuple[int, int, str, str]] = []
        try:
            # Prefer collection group query (fast, single round trip)
            cg = self.client.collection_group('tasks') \
                .where(filter=FieldFilter('schedule_time', '==', time_str)) \
                .where(filter=FieldFilter('is_active', '==', 1))
            for doc in cg.stream():
                data = doc.to_dict() or {}
                # Ensure required fields present
                if not data.get('user_id') or data.get('schedule_time') != time_str:
                    continue
                results.append((int(data.get('user_id')), int(data.get('id')), data.get('name'), data.get('schedule_time')))
            return results
        except Exception as e:
            print(f"⚠️ Collection group query failed ({e}); falling back to per-user scan.")
            # Fallback: iterate user documents (could be slower with many users)
            try:
                for user_doc in self.client.collection('users').list_documents():
                    try:
                        user_id = int(user_doc.id)
                    except ValueError:
                        continue
                    try:
                        q = user_doc.collection('tasks') \
                            .where(filter=FieldFilter('is_active', '==', 1)) \
                            .where(filter=FieldFilter('schedule_time', '==', time_str))
                        for tdoc in q.stream():
                            t = tdoc.to_dict() or {}
                            results.append((user_id, int(t.get('id')), t.get('name'), t.get('schedule_time')))
                    except Exception:
                        continue
            except Exception:
                pass
        return results

    def is_completed_on_date(self, user_id: int, task_id: int, date_iso: str) -> bool:
        doc_id = f"{date_iso}_{task_id}"
        return self._streaks_col(user_id).document(doc_id).get().exists

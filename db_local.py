"""
db_local.py
-----------
Local SQLite mirror of the cloud data, plus an offline change queue.

Why: the widget must stay usable with no internet connection. Every local
write (create/update/move/delete a card or column) is applied to SQLite
immediately (so the UI feels instant) AND appended to a `sync_queue` table.
A background worker in sync_manager.py drains that queue against Supabase
whenever connectivity is available, and reconciles incoming realtime
events back into this same SQLite cache.

This module has zero Qt or network dependencies -- it's pure sqlite3 --
so it's easy to unit test in isolation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from config import LOCAL_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS columns (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    column_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    color_label TEXT DEFAULT '#313244',
    position INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    deadline TEXT,
    is_done INTEGER NOT NULL DEFAULT 0
);

-- Queue of local mutations that still need to be pushed to Supabase.
-- `entity` is 'board' | 'column' | 'card'; `op` is 'insert' | 'update' | 'delete'.
CREATE TABLE IF NOT EXISTS sync_queue (
    queue_id TEXT PRIMARY KEY,
    entity TEXT NOT NULL,
    op TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class QueuedChange:
    queue_id: str
    entity: str
    op: str
    entity_id: str
    payload: dict = field(default_factory=dict)
    created_at: float = 0.0


class LocalStore:
    """Owns the sqlite3 connection and all local-cache operations."""

    def __init__(self, db_path: str = LOCAL_DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()  # realtime thread and GUI thread share this connection
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Adds columns introduced after the initial release to any cache.db
        that already exists on disk. CREATE TABLE IF NOT EXISTS (above) only
        helps brand-new installs; existing local caches need an explicit
        ALTER TABLE. Safe to run on every startup -- each ALTER is skipped
        if the column is already present."""
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(cards);")
        existing_columns = {row[1] for row in cur.fetchall()}

        if "deadline" not in existing_columns:
            cur.execute("ALTER TABLE cards ADD COLUMN deadline TEXT;")
        if "is_done" not in existing_columns:
            cur.execute("ALTER TABLE cards ADD COLUMN is_done INTEGER NOT NULL DEFAULT 0;")

        self._conn.commit()
        cur.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ------------------------------------------------------------------
    # Board / column / card reads
    # ------------------------------------------------------------------
    def get_primary_board(self) -> Optional[dict]:
        """The board the widget displays: the OLDEST board in the cache.
        Oldest-first matters when the same account has ended up with more
        than one board row (e.g. a second device that created its own
        empty board before this fix) -- the original board holds the data."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM boards;")
            boards = [dict(r) for r in cur.fetchall()]
        if not boards:
            return None
        boards.sort(key=lambda b: _parse_ts(b.get("created_at")))
        return boards[0]

    def create_board(self, title: str = "My Board") -> dict:
        board_id = str(uuid.uuid4())
        created = _now_iso()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO boards (id, title, created_at) VALUES (?, ?, ?);",
                (board_id, title, created),
            )
        self.enqueue("board", "insert", board_id, {"id": board_id, "title": title})
        return {"id": board_id, "title": title, "created_at": created}

    # ------------------------------------------------------------------
    # Account binding -- keeps one user's cached data from ever showing
    # up under another account on the same machine.
    # ------------------------------------------------------------------
    def bind_user(self, user_id: str) -> bool:
        """Associates the cache with `user_id`. If the cache belonged to a
        different account it is wiped first. Returns True if it was wiped.
        A cache with no recorded owner (created by an older version) is
        adopted as-is."""
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = 'user_id';")
            row = cur.fetchone()
        owner = row["value"] if row else None
        wiped = False
        if owner is not None and owner != user_id:
            self.clear_all()
            wiped = True
        with self._cursor() as cur:
            cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('user_id', ?);", (user_id,))
        return wiped

    def clear_all(self) -> None:
        """Deletes every cached row AND the pending sync queue."""
        with self._cursor() as cur:
            for table in ("boards", "columns", "cards", "sync_queue", "meta"):
                cur.execute(f"DELETE FROM {table};")

    def pending_count(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM sync_queue;")
            return int(cur.fetchone()["n"])

    def get_columns(self, board_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM columns WHERE board_id = ? ORDER BY position ASC;",
                (board_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_cards(self, column_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM cards WHERE column_id = ? AND deleted = 0 ORDER BY position ASC;",
                (column_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Mutations -- each applies locally AND enqueues for cloud sync
    # ------------------------------------------------------------------
    def add_column(self, board_id: str, title: str, position: int) -> dict:
        column_id = str(uuid.uuid4())
        payload = {"id": column_id, "board_id": board_id, "title": title, "position": position}
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO columns (id, board_id, title, position, created_at) VALUES (?, ?, ?, ?, ?);",
                (column_id, board_id, title, position, _now_iso()),
            )
        self.enqueue("column", "insert", column_id, payload)
        return payload

    def rename_column(self, column_id: str, title: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE columns SET title = ? WHERE id = ?;", (title, column_id))
        self.enqueue("column", "update", column_id, {"id": column_id, "title": title})

    def delete_column(self, column_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM columns WHERE id = ?;", (column_id,))
            cur.execute("DELETE FROM cards WHERE column_id = ?;", (column_id,))
        self.enqueue("column", "delete", column_id, {"id": column_id})

    def add_card(
        self,
        column_id: str,
        title: str,
        description: str,
        color_label: str,
        position: int,
        deadline: Optional[str] = None,
        is_done: bool = False,
    ) -> dict:
        card_id = str(uuid.uuid4())
        payload = {
            "id": card_id,
            "column_id": column_id,
            "title": title,
            "description": description,
            "color_label": color_label,
            "position": position,
            "deadline": deadline,
            "is_done": int(is_done),
        }
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO cards
                   (id, column_id, title, description, color_label, position, updated_at, deadline, is_done)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                (card_id, column_id, title, description, color_label, position, _now_iso(), deadline, int(is_done)),
            )
        self.enqueue("card", "insert", card_id, payload)
        return payload

    def update_card(self, card_id: str, **fields: Any) -> None:
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [_now_iso(), card_id]
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE cards SET {set_clause}, updated_at = ? WHERE id = ?;",
                values,
            )
        self.enqueue("card", "update", card_id, {"id": card_id, **fields})

    def move_card(self, card_id: str, new_column_id: str, new_position: int) -> None:
        self.update_card(card_id, column_id=new_column_id, position=new_position)

    def delete_card(self, card_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE cards SET deleted = 1 WHERE id = ?;", (card_id,))
        self.enqueue("card", "delete", card_id, {"id": card_id})

    # ------------------------------------------------------------------
    # Reconciliation -- apply a change that came FROM Supabase realtime,
    # without re-enqueueing it (it's already the source of truth).
    # ------------------------------------------------------------------
    def apply_remote_upsert(self, entity: str, record: dict) -> None:
        with self._cursor() as cur:
            if entity == "board":
                cur.execute(
                    "INSERT OR REPLACE INTO boards (id, title, created_at) VALUES (?, ?, ?);",
                    (record["id"], record["title"], record.get("created_at")),
                )
            elif entity == "column":
                cur.execute(
                    """INSERT OR REPLACE INTO columns (id, board_id, title, position, created_at)
                       VALUES (?, ?, ?, ?, ?);""",
                    (
                        record["id"],
                        record["board_id"],
                        record["title"],
                        record.get("position", 0),
                        record.get("created_at"),
                    ),
                )
            elif entity == "card":
                cur.execute(
                    """INSERT OR REPLACE INTO cards
                       (id, column_id, title, description, color_label, position, updated_at, deleted, deadline, is_done)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?);""",
                    (
                        record["id"],
                        record["column_id"],
                        record["title"],
                        record.get("description"),
                        record.get("color_label", "#313244"),
                        record.get("position", 0),
                        record.get("updated_at"),
                        record.get("deadline"),
                        int(record.get("is_done") or 0),
                    ),
                )

    def apply_remote_delete(self, entity: str, entity_id: str) -> None:
        with self._cursor() as cur:
            table = {"board": "boards", "column": "columns", "card": "cards"}[entity]
            if entity == "card":
                cur.execute("UPDATE cards SET deleted = 1 WHERE id = ?;", (entity_id,))
            else:
                cur.execute(f"DELETE FROM {table} WHERE id = ?;", (entity_id,))

    # ------------------------------------------------------------------
    # Sync queue management
    # ------------------------------------------------------------------
    def enqueue(self, entity: str, op: str, entity_id: str, payload: dict) -> None:
        queue_id = str(uuid.uuid4())
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO sync_queue (queue_id, entity, op, entity_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?);",
                (queue_id, entity, op, entity_id, json.dumps(payload), time.time()),
            )

    def pending_changes(self) -> list[QueuedChange]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM sync_queue ORDER BY created_at ASC;")
            rows = cur.fetchall()
        return [
            QueuedChange(
                queue_id=r["queue_id"],
                entity=r["entity"],
                op=r["op"],
                entity_id=r["entity_id"],
                payload=json.loads(r["payload"]) if r["payload"] else {},
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def remove_from_queue(self, queue_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM sync_queue WHERE queue_id = ?;", (queue_id,))

    # ------------------------------------------------------------------
    # Analytics (used by the AnalyticsPanel in expanded/fullscreen mode)
    # ------------------------------------------------------------------
    def get_board_analytics(self, board_id: str) -> dict:
        """Returns aggregate stats for a board:
          total_cards, done_cards, overdue_cards, due_soon_cards,
          per_column: [{title, total, done}, ...]
        Deadline bucketing (overdue / due soon) is left to the caller
        (ui_widget.deadline_status) so there's one source of truth for
        that logic, shared with the per-card badge rendering.
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT c.id, c.deadline, c.is_done, col.title AS column_title
                   FROM cards c JOIN columns col ON col.id = c.column_id
                   WHERE col.board_id = ? AND c.deleted = 0;""",
                (board_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]

        per_column: dict[str, dict] = {}
        for row in rows:
            bucket = per_column.setdefault(row["column_title"], {"title": row["column_title"], "total": 0, "done": 0})
            bucket["total"] += 1
            if row["is_done"]:
                bucket["done"] += 1

        return {
            "rows": rows,  # raw rows so the panel can compute overdue/due-soon with shared logic
            "per_column": list(per_column.values()),
            "total_cards": len(rows),
            "done_cards": sum(1 for r in rows if r["is_done"]),
        }

    def close(self) -> None:
        self._conn.close()


def _parse_ts(value: Optional[str]) -> datetime:
    """Parses both our own '...Z' timestamps and Postgres' '...+00:00' ones so
    boards created locally and boards pulled from the cloud sort correctly."""
    if not value:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

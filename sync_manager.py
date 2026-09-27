"""
sync_manager.py
----------------
Bridges the local SQLite cache (db_local.py) with Supabase:

  * Auth: sign in / sign up / restore session from stored tokens / sign out.
  * CRUD: push queued local changes to Postgres via the Supabase REST API.
  * Realtime: subscribe to postgres_changes on columns/cards, and marshal
    events from Supabase's background thread into the Qt main thread via
    pyqtSignal (Qt widgets must only be touched from the GUI thread).
  * A QTimer-driven retry loop drains the offline queue whenever the app
    regains connectivity, so edits made offline aren't lost.

Threading model:
  supabase-py's realtime client runs its own asyncio event loop in a
  background thread. We never touch QWidgets from that thread directly --
  every callback just `.emit()`s a Qt signal, and the slot connected to it
  (living in the GUI thread, via Qt's queued connection across threads)
  does the actual widget update.
"""

from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from supabase import create_client, Client

from config import (
    SUPABASE_URL,
    SUPABASE_ANON_KEY,
    SYNC_RETRY_INTERVAL_SECONDS,
    REALTIME_RECONNECT_DELAY_SECONDS,
)
from db_local import LocalStore, QueuedChange
from security import TokenVault


class SyncManager(QObject):
    # ---- signals consumed by the GUI (always emitted, never called direct) ----
    connection_status_changed = pyqtSignal(str)       # "online" | "offline" | "syncing"
    remote_change_applied = pyqtSignal(str, dict)      # (entity, record) -- upsert
    remote_delete_applied = pyqtSignal(str, str)       # (entity, entity_id)
    auth_state_changed = pyqtSignal(bool)              # True = logged in
    sync_error = pyqtSignal(str)

    def __init__(self, local_store: LocalStore):
        super().__init__()
        self.local_store = local_store
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        self.user_id: Optional[str] = None
        self._realtime_channel = None
        self._realtime_thread: Optional[threading.Thread] = None

        # Periodically flush the offline queue. This also acts as our
        # "are we online again yet?" probe -- no separate connectivity
        # check needed, we just try the write and see if it succeeds.
        self.retry_timer = QTimer()
        self.retry_timer.setInterval(SYNC_RETRY_INTERVAL_SECONDS * 1000)
        self.retry_timer.timeout.connect(self.drain_queue)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def try_restore_session(self) -> bool:
        """Attempt to log in silently using tokens saved in the OS keyring.
        Returns True if a session was restored."""
        access_token, refresh_token, _email = TokenVault.load_session()
        if not access_token or not refresh_token:
            return False
        try:
            session = self.client.auth.set_session(access_token, refresh_token)
            if session and session.user:
                self.user_id = session.user.id
                # set_session may have rotated the refresh token; persist the
                # latest pair so the *next* launch also succeeds.
                new_session = self.client.auth.get_session()
                if new_session:
                    TokenVault.save_session(
                        new_session.access_token, new_session.refresh_token, new_session.user.email or ""
                    )
                self._start_realtime()
                self.retry_timer.start()
                self.auth_state_changed.emit(True)
                return True
        except Exception as exc:  # noqa: BLE001
            self.sync_error.emit(f"Session restore failed: {exc}")
        return False

    def sign_in_or_up(self, email: str, password: str, mode: str) -> Optional[tuple[str, str]]:
        """Called from the AuthDialog's callback. Returns (access_token,
        refresh_token) on success, or None. Raises on hard errors so the
        dialog can show the message."""
        if mode == "Sign Up":
            resp = self.client.auth.sign_up({"email": email, "password": password})
        else:
            resp = self.client.auth.sign_in_with_password({"email": email, "password": password})

        if not resp or not resp.session:
            return None

        self.user_id = resp.user.id
        TokenVault.save_session(resp.session.access_token, resp.session.refresh_token, email)
        self._start_realtime()
        self.retry_timer.start()
        self.auth_state_changed.emit(True)
        return resp.session.access_token, resp.session.refresh_token

    def sign_out(self) -> None:
        try:
            self.client.auth.sign_out()
        except Exception:
            pass  # best-effort -- we're clearing local state regardless
        TokenVault.clear_session()
        self._stop_realtime()
        self.retry_timer.stop()
        self.user_id = None
        self.auth_state_changed.emit(False)

    # ------------------------------------------------------------------
    # Outbound: push local queue to Supabase
    # ------------------------------------------------------------------
    def drain_queue(self) -> None:
        if not self.user_id:
            return
        pending = self.local_store.pending_changes()
        if not pending:
            self.connection_status_changed.emit("online")
            return

        self.connection_status_changed.emit("syncing")
        for change in pending:
            try:
                self._push_change(change)
                self.local_store.remove_from_queue(change.queue_id)
            except Exception as exc:  # noqa: BLE001
                # Leave it queued -- likely offline or a transient error.
                # Stop processing further items to preserve order.
                self.connection_status_changed.emit("offline")
                self.sync_error.emit(f"Sync deferred: {exc}")
                return
        self.connection_status_changed.emit("online")

    def _push_change(self, change: QueuedChange) -> None:
        table = {"board": "boards", "column": "columns", "card": "cards"}[change.entity]

        if change.op == "delete":
            self.client.table(table).delete().eq("id", change.entity_id).execute()
            return

        record = dict(change.payload)
        record["user_id"] = self.user_id
        record.setdefault("id", change.entity_id)

        if change.op == "insert":
            self.client.table(table).upsert(record).execute()
        elif change.op == "update":
            self.client.table(table).update(record).eq("id", change.entity_id).execute()

    # ------------------------------------------------------------------
    # Initial full pull (used right after login / on cold start online)
    # ------------------------------------------------------------------
    def pull_all(self, board_id: str) -> None:
        if not self.user_id:
            return
        try:
            columns_resp = self.client.table("columns").select("*").eq("board_id", board_id).execute()
            for row in columns_resp.data or []:
                self.local_store.apply_remote_upsert("column", row)
                self.remote_change_applied.emit("column", row)

            cards_resp = (
                self.client.table("cards")
                .select("*, columns!inner(board_id)")
                .eq("columns.board_id", board_id)
                .execute()
            )
            for row in cards_resp.data or []:
                row.pop("columns", None)
                self.local_store.apply_remote_upsert("card", row)
                self.remote_change_applied.emit("card", row)
            self.connection_status_changed.emit("online")
        except Exception as exc:  # noqa: BLE001
            self.connection_status_changed.emit("offline")
            self.sync_error.emit(f"Initial sync failed, using local cache: {exc}")

    # ------------------------------------------------------------------
    # Realtime subscription
    # ------------------------------------------------------------------
    def _start_realtime(self) -> None:
        if self._realtime_thread and self._realtime_thread.is_alive():
            return

        def run() -> None:
            try:
                channel = self.client.channel("kanban-changes")
                channel.on_postgres_changes(
                    event="*", schema="public", table="columns", callback=self._on_column_event
                )
                channel.on_postgres_changes(
                    event="*", schema="public", table="cards", callback=self._on_card_event
                )
                channel.subscribe()
                self._realtime_channel = channel
                # supabase-py's realtime client manages its own asyncio loop
                # internally once subscribed; this thread just needs to stay
                # alive to keep that loop's owning thread context valid.
                threading.Event().wait()
            except Exception as exc:  # noqa: BLE001
                self.sync_error.emit(f"Realtime connection lost: {exc}")

        self._realtime_thread = threading.Thread(target=run, daemon=True)
        self._realtime_thread.start()

    def _stop_realtime(self) -> None:
        if self._realtime_channel is not None:
            try:
                self.client.remove_channel(self._realtime_channel)
            except Exception:
                pass
            self._realtime_channel = None

    def _on_column_event(self, payload: dict) -> None:
        self._handle_realtime_payload("column", payload)

    def _on_card_event(self, payload: dict) -> None:
        self._handle_realtime_payload("card", payload)

    def _handle_realtime_payload(self, entity: str, payload: dict) -> None:
        """Runs on the realtime client's background thread. Only emits Qt
        signals here -- never touches widgets directly."""
        event_type = payload.get("eventType") or payload.get("type")
        record = payload.get("new") or {}
        old_record = payload.get("old") or {}

        if event_type == "DELETE":
            entity_id = old_record.get("id")
            if entity_id:
                self.local_store.apply_remote_delete(entity, entity_id)
                self.remote_delete_applied.emit(entity, entity_id)
        elif event_type in ("INSERT", "UPDATE") and record:
            self.local_store.apply_remote_upsert(entity, record)
            self.remote_change_applied.emit(entity, record)

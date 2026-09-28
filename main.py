"""
main.py
-------
Application entry point. Wires together:
  * QApplication (kept alive in the tray even when the window is hidden)
  * Auth (silent session restore, or the login dialog on first run)
  * LocalStore (SQLite cache) + SyncManager (Supabase online sync)
  * KanbanWindow (the floating widget itself)
  * TrayIcon + GlobalHotkeyListener (OS integration)

Run with:  python main.py
Package with PyInstaller for a distributable .exe (see README.md).
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt6.QtWidgets import QApplication, QMessageBox

from config import APP_NAME, SYNC_RETRY_INTERVAL_SECONDS
from db_local import LocalStore
from sync_manager import SyncManager
from security import AuthDialog, confirm_logout
from ui_widget import KanbanWindow
from os_integration import TrayIcon, GlobalHotkeyListener, set_startup_enabled, is_startup_enabled


def make_fallback_icon() -> QIcon:
    """A simple generated icon so the app doesn't need a bundled .ico file
    to run out of the box. Replace with a real asset for distribution."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setBrush(QColor("#89b4fa"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 28, 28, 6, 6)
    painter.end()
    return QIcon(pixmap)


class Application:
    def __init__(self):
        self.qapp = QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)  # stay alive in the tray
        self.qapp.setApplicationName(APP_NAME)

        self.local_store = LocalStore()
        self.sync_manager = SyncManager(self.local_store)

        self.icon = make_fallback_icon()
        self.tray = TrayIcon(self.icon)
        self.hotkey_listener = GlobalHotkeyListener()

        self.window: KanbanWindow | None = None

        # Used when we're signed in but couldn't reach the server on a
        # device with an empty cache -- we retry instead of inventing a
        # new (empty) board.
        self._bootstrap_timer = QTimer()
        self._bootstrap_timer.setInterval(SYNC_RETRY_INTERVAL_SECONDS * 1000)
        self._bootstrap_timer.timeout.connect(self._start_session)

        self._wire_signals()

    def _wire_signals(self) -> None:
        self.sync_manager.connection_status_changed.connect(self.tray.set_sync_status)
        self.sync_manager.remote_change_applied.connect(self._on_remote_change)
        self.sync_manager.remote_delete_applied.connect(self._on_remote_delete)
        self.sync_manager.sync_error.connect(self._on_sync_error)

        self.tray.show_hide_requested.connect(self._toggle_window)
        self.tray.login_requested.connect(self._show_login)
        self.tray.logout_requested.connect(self._on_logout_requested)
        self.tray.exit_requested.connect(self._on_exit_requested)
        self.tray.startup_toggle_requested.connect(self._on_startup_toggled)

        self.hotkey_listener.hotkey_triggered.connect(self._toggle_window)

    # ------------------------------------------------------------------
    def run(self) -> int:
        self.tray.show()
        self.hotkey_listener.start()

        if self.sync_manager.try_restore_session():
            self._start_session()
        else:
            self._show_login()

        return self.qapp.exec()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def _show_login(self) -> None:
        dialog = AuthDialog(self.sync_manager.sign_in_or_up)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self._start_session()
        # If cancelled we keep running in the tray ("Sign in..." is there).

    def _start_session(self) -> None:
        """Runs after every successful sign-in / session restore.

        Order matters, and is the fix for "my other device shows an empty
        board":
          1. bind the local cache to this account (wipe if it was another's)
          2. push any pending local edits (so the pull can't overwrite them)
          3. pull the account's boards from the cloud FIRST
          4. only create a new board if the cloud genuinely has none
          5. pull that board's columns/cards, then build the window
        """
        user_id = self.sync_manager.user_id
        if not user_id:
            return
        self._bootstrap_timer.stop()

        self.local_store.bind_user(user_id)
        self.tray.set_signed_in(True, self.sync_manager.user_email)

        self.sync_manager.drain_queue()
        reached_server = self.sync_manager.pull_boards()

        board = self.local_store.get_primary_board()
        if board is None:
            if not reached_server:
                # Signed in, empty cache, and offline: creating a board now
                # would fork the account's data. Wait and retry instead.
                self.tray.setToolTip("Floating Kanban - waiting for connection to load your board...")
                self._bootstrap_timer.start()
                return
            board = self.local_store.create_board("My Board")
            self.sync_manager.drain_queue()

        self.sync_manager.pull_all(board["id"])

        self._close_window()
        self.window = KanbanWindow(self.local_store, board)
        self.window.header.set_account(self.sync_manager.user_email)
        self.window.logout_requested.connect(self._on_logout_requested)
        self.window.show()

        self.sync_manager.start_background_sync()

    def _close_window(self) -> None:
        if self.window is not None:
            self.window.deadline_timer.stop()
            self.window.hide()
            self.window.deleteLater()
            self.window = None

    def _on_logout_requested(self) -> None:
        # Try to flush unsynced edits first so logging out doesn't lose them.
        if self.local_store.pending_count() > 0:
            self.sync_manager.drain_queue()
        pending = self.local_store.pending_count()

        if not confirm_logout(self.window, pending):
            return

        self._bootstrap_timer.stop()
        self.sync_manager.sign_out()      # this device only; other devices stay signed in
        self.local_store.clear_all()      # never leave one account's data cached after logout
        self._close_window()
        self.tray.set_signed_in(False)
        self.tray.setToolTip("Floating Kanban")
        self._show_login()

    # ------------------------------------------------------------------
    # Realtime + misc handlers
    # ------------------------------------------------------------------
    def _on_remote_change(self, entity: str, record: dict) -> None:
        if self.window is None:
            return
        if entity == "card":
            self.window.refresh_column(record.get("column_id"))
        elif entity == "column":
            self.window.reload_from_local()

    def _on_remote_delete(self, entity: str, entity_id: str) -> None:
        if self.window is None:
            return
        self.window.reload_from_local()

    def _on_sync_error(self, message: str) -> None:
        # Non-blocking: surface via tray tooltip rather than a popup, so
        # transient offline periods don't spam the user with dialogs.
        self.tray.setToolTip(f"Floating Kanban - {message}")

    def _toggle_window(self) -> None:
        if self.window is not None:
            self.window.toggle_visibility()

    def _on_startup_toggled(self, enabled: bool) -> None:
        success = set_startup_enabled(enabled)
        if not success:
            QMessageBox.warning(
                None,
                "Startup setting",
                "Could not update the Windows startup registry entry. "
                "Try running the app as your normal user (not elevated), "
                "or check Windows permissions.",
            )
        self.tray.startup_action.setChecked(is_startup_enabled())

    def _on_exit_requested(self) -> None:
        self.hotkey_listener.stop()
        self.sync_manager.retry_timer.stop()
        self.local_store.close()
        self.qapp.quit()


def main() -> int:
    app = Application()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())

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

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt6.QtWidgets import QApplication, QMessageBox

from config import APP_NAME
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

        self._wire_signals()

    def _wire_signals(self) -> None:
        self.sync_manager.auth_state_changed.connect(self._on_auth_state_changed)
        self.sync_manager.connection_status_changed.connect(self.tray.set_sync_status)
        self.sync_manager.remote_change_applied.connect(self._on_remote_change)
        self.sync_manager.remote_delete_applied.connect(self._on_remote_delete)
        self.sync_manager.sync_error.connect(self._on_sync_error)

        self.tray.show_hide_requested.connect(self._toggle_window)
        self.tray.logout_requested.connect(self._on_logout_requested)
        self.tray.exit_requested.connect(self._on_exit_requested)
        self.tray.startup_toggle_requested.connect(self._on_startup_toggled)

        self.hotkey_listener.hotkey_triggered.connect(self._toggle_window)

    # ------------------------------------------------------------------
    def run(self) -> int:
        self.tray.show()
        self.hotkey_listener.start()

        if not self.sync_manager.try_restore_session():
            self._show_login()
        else:
            self._on_auth_state_changed(True)

        return self.qapp.exec()

    # ------------------------------------------------------------------
    def _show_login(self) -> None:
        dialog = AuthDialog(self.sync_manager.sign_in_or_up)
        dialog.authenticated.connect(lambda *_: None)  # sign_in_or_up already persisted state
        if dialog.exec() != dialog.DialogCode.Accepted:
            # user closed the dialog without signing in -- keep running in
            # the tray so they can retry, rather than force-quitting.
            return

    def _on_auth_state_changed(self, logged_in: bool) -> None:
        if not logged_in:
            if self.window:
                self.window.hide()
            return

        board = self.local_store.get_or_create_default_board()
        self.sync_manager.pull_all(board["id"])

        if self.window is None:
            self.window = KanbanWindow(self.local_store, board)
        else:
            self.window.reload_from_local()

        self.window.show()
        self.sync_manager.drain_queue()

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

    def _on_logout_requested(self) -> None:
        if confirm_logout(None):
            self.sync_manager.sign_out()
            if self.window:
                self.window.hide()
            self._show_login()

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
        self.local_store.close()
        self.qapp.quit()


def main() -> int:
    app = Application()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())

"""
os_integration.py
------------------
Windows-specific integration:
  * Add/remove the app from Windows startup (HKCU\\...\\Run) via `winreg`.
  * A QSystemTrayIcon with Show/Hide, Toggle Startup, Sync Status, Logout, Exit.
  * A global hotkey (default Ctrl+Alt+T) using `pynput`, running in its own
    listener thread, that toggles the widget's visibility from anywhere in
    the OS -- even when the widget doesn't have focus.

`winreg` and global keyboard hooks only make sense on Windows; on other
platforms these functions degrade to no-ops (with a warning) so the app
doesn't crash during cross-platform development/testing.
"""

from __future__ import annotations

import sys
import threading

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

from config import STARTUP_REGISTRY_KEY_NAME, GLOBAL_HOTKEY

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import winreg  # type: ignore

try:
    from pynput import keyboard  # type: ignore
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False


# ============================================================================
# Startup registry management
# ============================================================================
_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_startup_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, STARTUP_REGISTRY_KEY_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup_enabled(enabled: bool, executable_path: str | None = None) -> bool:
    """Returns True on success. `executable_path` should be the absolute
    path to the frozen .exe (e.g. from PyInstaller) -- pass sys.executable
    plus script args if running unfrozen, though for production use a
    packaged exe."""
    if not IS_WINDOWS:
        return False
    executable_path = executable_path or sys.executable
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, STARTUP_REGISTRY_KEY_NAME, 0, winreg.REG_SZ, f'"{executable_path}"')
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_REGISTRY_KEY_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# ============================================================================
# Global hotkey listener (show/hide toggle)
# ============================================================================
class GlobalHotkeyListener(QObject):
    """Runs pynput's GlobalHotKeys in a background thread and re-emits the
    trigger as a Qt signal so the GUI thread handles the actual show/hide."""

    hotkey_triggered = pyqtSignal()

    def __init__(self, hotkey: str = GLOBAL_HOTKEY):
        super().__init__()
        self.hotkey = hotkey
        self._listener: "keyboard.GlobalHotKeys | None" = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not HAS_PYNPUT:
            return

        def run() -> None:
            self._listener = keyboard.GlobalHotKeys({self.hotkey: self._on_trigger})
            self._listener.run()  # blocks this thread until stop()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _on_trigger(self) -> None:
        self.hotkey_triggered.emit()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()


# ============================================================================
# System tray icon
# ============================================================================
class TrayIcon(QSystemTrayIcon):
    show_hide_requested = pyqtSignal()
    logout_requested = pyqtSignal()
    exit_requested = pyqtSignal()
    startup_toggle_requested = pyqtSignal(bool)

    def __init__(self, icon: QIcon, parent=None):
        super().__init__(icon, parent)
        self.setToolTip("Floating Kanban")

        menu = QMenu()

        self.show_hide_action = QAction("Show / Hide")
        self.show_hide_action.triggered.connect(self.show_hide_requested.emit)
        menu.addAction(self.show_hide_action)

        menu.addSeparator()

        self.startup_action = QAction("Run at Windows startup")
        self.startup_action.setCheckable(True)
        self.startup_action.setChecked(is_startup_enabled())
        self.startup_action.triggered.connect(
            lambda checked: self.startup_toggle_requested.emit(checked)
        )
        menu.addAction(self.startup_action)

        self.status_action = QAction("Sync status: unknown")
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)

        menu.addSeparator()

        logout_action = QAction("Log out")
        logout_action.triggered.connect(self.logout_requested.emit)
        menu.addAction(logout_action)

        exit_action = QAction("Exit")
        exit_action.triggered.connect(self.exit_requested.emit)
        menu.addAction(exit_action)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_hide_requested.emit()

    def set_sync_status(self, status: str) -> None:
        icons = {"online": "🟢", "offline": "🔴", "syncing": "🟡"}
        self.status_action.setText(f"Sync status: {icons.get(status, '⚪')} {status}")

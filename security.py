"""
security.py
-----------
Handles:
  * Secure storage/retrieval of Supabase JWTs using the `keyring` library,
    which on Windows backs onto the Windows Credential Manager (DPAPI-
    encrypted, per-OS-user). Tokens never touch a plaintext file.
  * A small PyQt6 login dialog for email/password sign-in and sign-up.

Design notes:
  * We store access_token, refresh_token, and the user's email. On next
    launch we use the refresh_token to mint a fresh session with Supabase
    via `supabase.auth.set_session(access_token, refresh_token)` --
    Supabase's client automatically refreshes and calls back if you wire
    the `on_auth_state_change` listener (done in sync_manager.py).
  * If keyring has no backend available (rare, e.g. minimal Linux CI
    containers), we fail loudly rather than silently falling back to an
    insecure store -- silently downgrading security is worse than crashing.
"""

from __future__ import annotations

import keyring
import keyring.errors

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QTabWidget,
    QWidget,
)

from config import (
    KEYRING_SERVICE_NAME,
    KEYRING_ACCESS_TOKEN_KEY,
    KEYRING_REFRESH_TOKEN_KEY,
    KEYRING_USER_EMAIL_KEY,
    ACCENT_COLOR,
)


# ============================================================================
# Token vault
# ============================================================================
class TokenVault:
    """Thin wrapper around `keyring` for storing/retrieving Supabase session
    tokens. All methods are static; there's no reason to instantiate this."""

    @staticmethod
    def save_session(access_token: str, refresh_token: str, email: str) -> None:
        try:
            keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_ACCESS_TOKEN_KEY, access_token)
            keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_REFRESH_TOKEN_KEY, refresh_token)
            keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_USER_EMAIL_KEY, email)
        except keyring.errors.KeyringError as exc:
            raise RuntimeError(
                f"Could not write to the OS credential store: {exc}"
            ) from exc

    @staticmethod
    def load_session() -> tuple[str | None, str | None, str | None]:
        try:
            access_token = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_ACCESS_TOKEN_KEY)
            refresh_token = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_REFRESH_TOKEN_KEY)
            email = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_USER_EMAIL_KEY)
            return access_token, refresh_token, email
        except keyring.errors.KeyringError:
            return None, None, None

    @staticmethod
    def clear_session() -> None:
        for key in (KEYRING_ACCESS_TOKEN_KEY, KEYRING_REFRESH_TOKEN_KEY, KEYRING_USER_EMAIL_KEY):
            try:
                keyring.delete_password(KEYRING_SERVICE_NAME, key)
            except keyring.errors.PasswordDeleteError:
                pass  # already absent -- fine
            except keyring.errors.KeyringError:
                pass


# ============================================================================
# Login / sign-up dialog
# ============================================================================
class AuthDialog(QDialog):
    """Modal dialog collecting email/password, delegating the actual network
    call to whatever `auth_callback(email, password, mode)` the caller wires
    up (kept decoupled from sync_manager to avoid a circular import and to
    make this dialog independently testable)."""

    # emitted with (access_token, refresh_token, email) on success
    authenticated = pyqtSignal(str, str, str)

    def __init__(self, auth_callback, parent=None):
        super().__init__(parent)
        self.auth_callback = auth_callback  # Callable[[str, str, str], tuple|Exception]
        self.setWindowTitle("Sign in - Floating Kanban")
        self.setFixedSize(360, 260)
        self.setStyleSheet(self._stylesheet())
        self._build_ui()

    def _stylesheet(self) -> str:
        return f"""
            QDialog {{ background-color: #1e1e2e; }}
            QLabel {{ color: #cdd6f4; font-size: 12px; }}
            QLineEdit {{
                background-color: #313244; color: #cdd6f4; border-radius: 6px;
                padding: 8px; border: 1px solid #45475a;
            }}
            QLineEdit:focus {{ border: 1px solid {ACCENT_COLOR}; }}
            QPushButton {{
                background-color: {ACCENT_COLOR}; color: #1e1e2e; border-radius: 6px;
                padding: 8px; font-weight: 600;
            }}
            QPushButton:hover {{ background-color: #74a8f7; }}
            QTabWidget::pane {{ border: none; }}
            QTabBar::tab {{
                background: #313244; color: #cdd6f4; padding: 6px 14px;
                border-top-left-radius: 6px; border-top-right-radius: 6px;
            }}
            QTabBar::tab:selected {{ background: {ACCENT_COLOR}; color: #1e1e2e; }}
        """

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_form("Sign In"), "Sign In")
        tabs.addTab(self._build_form("Sign Up"), "Sign Up")
        outer.addWidget(tabs)

    def _build_form(self, mode: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("Email"))
        email_edit = QLineEdit()
        email_edit.setPlaceholderText("you@example.com")
        layout.addWidget(email_edit)

        layout.addWidget(QLabel("Password"))
        password_edit = QLineEdit()
        password_edit.setPlaceholderText("••••••••")
        password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(password_edit)

        submit_btn = QPushButton(mode)
        layout.addWidget(submit_btn)

        status_label = QLabel("")
        status_label.setStyleSheet("color: #f38ba8;")
        status_label.setWordWrap(True)
        layout.addWidget(status_label)

        submit_btn.clicked.connect(
            lambda: self._submit(mode, email_edit.text().strip(), password_edit.text(), status_label)
        )
        layout.addStretch()
        return page

    def _submit(self, mode: str, email: str, password: str, status_label: QLabel) -> None:
        if not email or not password:
            status_label.setText("Email and password are required.")
            return
        if len(password) < 6:
            status_label.setText("Password must be at least 6 characters.")
            return

        status_label.setText("Connecting...")
        try:
            result = self.auth_callback(email, password, mode)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
            status_label.setText(f"Failed: {exc}")
            return

        if result is None:
            status_label.setText("Authentication failed. Check your credentials.")
            return

        access_token, refresh_token = result
        self.authenticated.emit(access_token, refresh_token, email)
        self.accept()


def confirm_logout(parent, pending_changes: int = 0) -> bool:
    """Confirmation prompt for Log out. If edits haven't reached the server
    yet, say so -- logging out clears this device's local cache."""
    text = "This will sign you out on this device and clear its local copy of your board."
    if pending_changes > 0:
        text += (
            f"\n\n{pending_changes} change(s) have not synced to the server yet and "
            "will be lost. Connect to the internet and try again to keep them."
        )
    else:
        text += "\nYour data stays safe in your account and will reappear when you sign back in."
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if pending_changes else QMessageBox.Icon.Question)
    box.setWindowTitle("Log out")
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes

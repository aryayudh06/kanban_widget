"""
ui_widget.py
------------
The floating, borderless Kanban board itself:
  * Frameless, translucent, dark, rounded-corner window.
  * Custom header bar for dragging + pin (always-on-top) + close-to-tray.
  * Columns of cards with native Qt drag-and-drop to reorder / move cards
    between columns.
  * Inline "+ Add card" / "+ Add column" affordances and a simple edit
    dialog for card title/description/color.

This module only talks to `db_local.LocalStore` for reads/writes (so the
UI works fully offline) and listens to `SyncManager` signals to reflect
remote changes live. It never calls Supabase directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import Qt, QPoint, QMimeData, QTimer, QDateTime, pyqtSignal
from PyQt6.QtGui import QDrag, QColor, QCursor, QIcon, QPixmap, QPainter
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QFrame,
    QLineEdit,
    QTextEdit,
    QDialog,
    QDialogButtonBox,
    QColorDialog,
    QSizePolicy,
    QMenu,
    QCheckBox,
    QDateTimeEdit,
    QSizeGrip,
    QSplitter,
    QGridLayout,
)

from config import (
    WINDOW_WIDTH,
    WINDOW_HEIGHT,
    MIN_EXPANDED_WIDTH,
    MIN_EXPANDED_HEIGHT,
    COMPACT_WIDTH,
    COMPACT_HEIGHT,
    MIN_COMPACT_WIDTH,
    MIN_COMPACT_HEIGHT,
    WINDOW_OPACITY_BG,
    ACCENT_COLOR,
    DEFAULT_CARD_COLOR,
    DEADLINE_DUE_SOON_HOURS,
    DEADLINE_CHECK_INTERVAL_MS,
    COLOR_OVERDUE,
    COLOR_DUE_SOON,
    COLOR_DONE,
)
from db_local import LocalStore

CARD_MIME_TYPE = "application/x-kanban-card-id"


# ============================================================================
# Deadline helpers -- single source of truth shared by card badges and the
# analytics panel so "overdue" / "due soon" always mean the same thing.
# ============================================================================
def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Accept both "...Z" (our own writer) and full ISO with offset.
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def deadline_status(deadline_iso: str | None, is_done: bool) -> tuple[str, str]:
    """Returns (status, label) where status is one of:
    'none' | 'done' | 'overdue' | 'due_soon' | 'upcoming'."""
    if is_done:
        return "done", "Done"
    dt = _parse_iso(deadline_iso)
    if dt is None:
        return "none", ""

    now = datetime.now(timezone.utc)
    delta_hours = (dt - now).total_seconds() / 3600.0
    display = dt.astimezone().strftime("%b %d, %H:%M")

    if delta_hours < 0:
        return "overdue", f"Overdue - {display}"
    if delta_hours <= DEADLINE_DUE_SOON_HOURS:
        return "due_soon", f"Due soon - {display}"
    return "upcoming", display


# ============================================================================
# Card edit dialog
# ============================================================================
class CardEditDialog(QDialog):
    def __init__(
        self,
        title: str = "",
        description: str = "",
        color: str = DEFAULT_CARD_COLOR,
        deadline_iso: str | None = None,
        is_done: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Edit Card")
        self.setFixedSize(360, 420)
        self._color = color
        self.setStyleSheet(
            """
            QDialog { background-color: #1e1e2e; }
            QLabel { color: #cdd6f4; }
            QLineEdit, QTextEdit, QDateTimeEdit {
                background-color: #313244; color: #cdd6f4; border-radius: 6px;
                padding: 6px; border: 1px solid #45475a;
            }
            QPushButton { background-color: #45475a; color: #cdd6f4; border-radius: 6px; padding: 6px 12px; }
            QPushButton:hover { background-color: #585b70; }
            QCheckBox { color: #cdd6f4; }
            """
        )

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(title)
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Description"))
        self.desc_edit = QTextEdit(description)
        layout.addWidget(self.desc_edit)

        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Color label"))
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(28, 28)
        self._update_color_btn()
        self.color_btn.clicked.connect(self._pick_color)
        color_row.addWidget(self.color_btn)
        color_row.addStretch()
        layout.addLayout(color_row)

        # ---- optional deadline ----
        self.deadline_checkbox = QCheckBox("Set a deadline")
        layout.addWidget(self.deadline_checkbox)

        self.deadline_edit = QDateTimeEdit()
        self.deadline_edit.setCalendarPopup(True)
        self.deadline_edit.setDisplayFormat("MMM d, yyyy  HH:mm")
        existing_dt = _parse_iso(deadline_iso)
        if existing_dt:
            local_dt = existing_dt.astimezone()
            self.deadline_edit.setDateTime(
                QDateTime(local_dt.year, local_dt.month, local_dt.day, local_dt.hour, local_dt.minute)
            )
            self.deadline_checkbox.setChecked(True)
        else:
            self.deadline_edit.setDateTime(QDateTime.currentDateTime().addDays(1))
            self.deadline_edit.setEnabled(False)
        self.deadline_checkbox.toggled.connect(self.deadline_edit.setEnabled)
        layout.addWidget(self.deadline_edit)

        # ---- done checkbox ----
        self.done_checkbox = QCheckBox("Mark as done")
        self.done_checkbox.setChecked(is_done)
        layout.addWidget(self.done_checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_color_btn(self) -> None:
        self.color_btn.setStyleSheet(f"background-color: {self._color}; border-radius: 4px;")

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._color), self)
        if color.isValid():
            self._color = color.name()
            self._update_color_btn()

    def values(self) -> tuple[str, str, str, Optional[str], bool]:
        """Returns (title, description, color, deadline_iso_or_None, is_done)."""
        deadline_iso = None
        if self.deadline_checkbox.isChecked():
            qdt = self.deadline_edit.dateTime()
            py_dt = qdt.toPyDateTime().astimezone()  # local -> aware
            deadline_iso = py_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (
            self.title_edit.text().strip(),
            self.desc_edit.toPlainText().strip(),
            self._color,
            deadline_iso,
            self.done_checkbox.isChecked(),
        )


# ============================================================================
# A single draggable card widget
# ============================================================================
class CardWidget(QFrame):
    edit_requested = pyqtSignal(str)          # card_id
    delete_requested = pyqtSignal(str)        # card_id
    done_toggled = pyqtSignal(str, bool)      # card_id, is_done

    def __init__(self, card: dict, parent=None):
        super().__init__(parent)
        self.card_id = card["id"]
        self.card = card
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedWidth(220)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_start_pos: QPoint | None = None
        self._badge_label: QLabel | None = None
        self._build_ui(card)

    def _build_ui(self, card: dict) -> None:
        color = card.get("color_label") or DEFAULT_CARD_COLOR
        is_done = bool(card.get("is_done"))
        status, status_text = deadline_status(card.get("deadline"), is_done)

        body_color = COLOR_DONE if is_done else color
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {body_color};
                border-radius: 8px;
                {"border: 1px solid " + COLOR_OVERDUE + ";" if status == "overdue" else ""}
                {"border: 1px solid " + COLOR_DUE_SOON + ";" if status == "due_soon" else ""}
            }}
            QLabel {{ color: #cdd6f4; }}
            QCheckBox {{ color: #cdd6f4; }}
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self.done_checkbox = QCheckBox()
        self.done_checkbox.setChecked(is_done)
        self.done_checkbox.setToolTip("Mark done")
        self.done_checkbox.toggled.connect(lambda checked: self.done_toggled.emit(self.card_id, checked))
        top_row.addWidget(self.done_checkbox)

        title_lbl = QLabel(card.get("title", ""))
        title_lbl.setWordWrap(True)
        title_style = "font-weight: 600; font-size: 13px;"
        if is_done:
            title_style += " text-decoration: line-through; color: #a6adc8;"
        title_lbl.setStyleSheet(title_style)
        top_row.addWidget(title_lbl, stretch=1)
        layout.addLayout(top_row)

        desc = card.get("description") or ""
        if desc:
            desc_lbl = QLabel(desc if len(desc) < 90 else desc[:87] + "...")
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("font-size: 11px; color: #a6adc8;")
            layout.addWidget(desc_lbl)

        if status_text:
            badge = QLabel(("⏰ " if status in ("overdue", "due_soon") else "🗓 ") + status_text)
            badge_color = {
                "overdue": COLOR_OVERDUE,
                "due_soon": COLOR_DUE_SOON,
                "done": "#a6adc8",
                "upcoming": "#a6adc8",
            }.get(status, "#a6adc8")
            badge.setStyleSheet(f"font-size: 10px; font-weight: 600; color: {badge_color};")
            layout.addWidget(badge)
            self._badge_label = badge

    def refresh_deadline_style(self) -> None:
        """Re-derives the overdue/due-soon color without a full DB round
        trip -- called periodically by KanbanWindow's QTimer so a card that
        was 'upcoming' turns amber/red as its deadline approaches while the
        board is just sitting open."""
        status, status_text = deadline_status(self.card.get("deadline"), bool(self.card.get("is_done")))
        if self._badge_label and status_text:
            badge_color = {
                "overdue": COLOR_OVERDUE,
                "due_soon": COLOR_DUE_SOON,
                "done": "#a6adc8",
                "upcoming": "#a6adc8",
            }.get(status, "#a6adc8")
            self._badge_label.setText(("⏰ " if status in ("overdue", "due_soon") else "🗓 ") + status_text)
            self._badge_label.setStyleSheet(f"font-size: 10px; font-weight: 600; color: {badge_color};")
        border = ""
        if status == "overdue":
            border = f"border: 1px solid {COLOR_OVERDUE};"
        elif status == "due_soon":
            border = f"border: 1px solid {COLOR_DUE_SOON};"
        body_color = COLOR_DONE if self.card.get("is_done") else (self.card.get("color_label") or DEFAULT_CARD_COLOR)
        self.setStyleSheet(
            f"QFrame {{ background-color: {body_color}; border-radius: 8px; {border} }} "
            f"QLabel {{ color: #cdd6f4; }} QCheckBox {{ color: #cdd6f4; }}"
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self._drag_start_pos is None:
            return
        if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < 10:
            return

        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(CARD_MIME_TYPE, self.card_id.encode("utf-8"))
        drag.setMimeData(mime)

        pixmap = QPixmap(self.size())
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.render(painter)
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.position().toPoint())

        drag.exec(Qt.DropAction.MoveAction)

    def mouseDoubleClickEvent(self, event) -> None:
        self.edit_requested.emit(self.card_id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        edit_action = menu.addAction("Edit")
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(event.globalPos())
        if chosen == edit_action:
            self.edit_requested.emit(self.card_id)
        elif chosen == delete_action:
            self.delete_requested.emit(self.card_id)


# ============================================================================
# A column: header + scrollable card list + "add card" affordance
# ============================================================================
class ColumnWidget(QFrame):
    card_moved = pyqtSignal(str, str, int)   # card_id, target_column_id, index
    card_add_requested = pyqtSignal(str)     # column_id
    card_edit_requested = pyqtSignal(str)    # card_id
    card_delete_requested = pyqtSignal(str)  # card_id
    card_done_toggled = pyqtSignal(str, bool)  # card_id, is_done
    column_rename_requested = pyqtSignal(str, str)  # column_id, new_title
    column_delete_requested = pyqtSignal(str)       # column_id

    def __init__(self, column: dict, parent=None):
        super().__init__(parent)
        self.column_id = column["id"]
        self.setAcceptDrops(True)
        self.setFixedWidth(250)
        self.setStyleSheet(
            """
            QFrame#columnBody { background-color: rgba(24, 24, 37, 0.75); border-radius: 10px; }
            QLabel#columnTitle { color: #cdd6f4; font-weight: 700; font-size: 13px; }
            """
        )
        self.setObjectName("columnBody")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        header = QHBoxLayout()
        self.title_label = QLabel(column.get("title", ""))
        self.title_label.setObjectName("columnTitle")
        header.addWidget(self.title_label)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: #a6adc8; font-size: 11px; font-weight: 500;")
        header.addWidget(self.progress_label)
        header.addStretch()

        menu_btn = QPushButton("⋮")
        menu_btn.setFixedSize(22, 22)
        menu_btn.setStyleSheet("background: transparent; color: #a6adc8; font-weight: bold;")
        menu_btn.clicked.connect(self._show_column_menu)
        header.addWidget(menu_btn)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background: transparent;")
        self.card_container = QWidget()
        self.card_container.setStyleSheet("background: transparent;")
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setSpacing(6)
        self.card_layout.addStretch()
        self.scroll.setWidget(self.card_container)
        outer.addWidget(self.scroll)

        add_btn = QPushButton("+ Add card")
        add_btn.setStyleSheet(
            f"""
            QPushButton {{ background: transparent; color: {ACCENT_COLOR}; text-align: left; padding: 4px; }}
            QPushButton:hover {{ color: #74a8f7; }}
            """
        )
        add_btn.clicked.connect(lambda: self.card_add_requested.emit(self.column_id))
        outer.addWidget(add_btn)

    def _show_column_menu(self) -> None:
        menu = QMenu(self)
        rename_action = menu.addAction("Rename column")
        delete_action = menu.addAction("Delete column")
        chosen = menu.exec(QCursor.pos())
        if chosen == rename_action:
            from PyQt6.QtWidgets import QInputDialog

            new_title, ok = QInputDialog.getText(self, "Rename column", "New title:", text=self.title_label.text())
            if ok and new_title.strip():
                self.column_rename_requested.emit(self.column_id, new_title.strip())
        elif chosen == delete_action:
            self.column_delete_requested.emit(self.column_id)

    def set_cards(self, cards: list[dict]) -> None:
        # clear existing card widgets (keep the trailing stretch)
        while self.card_layout.count() > 1:
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for card in cards:
            card_widget = CardWidget(card)
            card_widget.edit_requested.connect(self.card_edit_requested.emit)
            card_widget.delete_requested.connect(self.card_delete_requested.emit)
            card_widget.done_toggled.connect(self.card_done_toggled.emit)
            self.card_layout.insertWidget(self.card_layout.count() - 1, card_widget)

        done_count = sum(1 for c in cards if c.get("is_done"))
        self.progress_label.setText(f"({done_count}/{len(cards)})" if cards else "")

    def iter_card_widgets(self):
        """Yields every CardWidget currently shown in this column (used by
        the deadline-refresh timer)."""
        for i in range(self.card_layout.count() - 1):  # exclude trailing stretch
            item = self.card_layout.itemAt(i)
            if item and item.widget():
                yield item.widget()

    # ---- drag-and-drop target behavior ----
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(CARD_MIME_TYPE):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(CARD_MIME_TYPE):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(CARD_MIME_TYPE):
            return
        card_id = bytes(event.mimeData().data(CARD_MIME_TYPE)).decode("utf-8")
        drop_y = event.position().toPoint().y()
        index = self._index_for_y(drop_y)
        self.card_moved.emit(card_id, self.column_id, index)
        event.acceptProposedAction()

    def _index_for_y(self, y: int) -> int:
        # Determine insert index based on vertical position relative to
        # existing card widgets so drops land where visually expected.
        for i in range(self.card_layout.count() - 1):  # exclude stretch
            widget = self.card_layout.itemAt(i).widget()
            if widget and y < widget.geometry().center().y():
                return i
        return max(self.card_layout.count() - 1, 0)


# ============================================================================
# Header bar: drag-to-move, pin, close-to-tray
# ============================================================================
class HeaderBar(QFrame):
    pin_toggled = pyqtSignal(bool)
    close_requested = pyqtSignal()
    add_column_requested = pyqtSignal()
    view_mode_toggle_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(38)
        self.setStyleSheet(
            f"""
            QFrame {{ background-color: rgba(17, 17, 27, 0.95); border-top-left-radius: 12px; border-top-right-radius: 12px; }}
            QLabel {{ color: #cdd6f4; font-weight: 600; }}
            QPushButton {{ background: transparent; color: #cdd6f4; border-radius: 4px; padding: 4px 8px; }}
            QPushButton:hover {{ background-color: rgba(255,255,255,0.08); }}
            """
        )
        self._drag_pos: QPoint | None = None
        self._pinned = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)

        self.title_label = QLabel("📋 Floating Kanban")
        layout.addWidget(self.title_label)
        layout.addStretch()

        self.add_col_btn = QPushButton("+ Column")
        self.add_col_btn.clicked.connect(self.add_column_requested.emit)
        layout.addWidget(self.add_col_btn)

        self.view_toggle_btn = QPushButton("⤢")
        self.view_toggle_btn.setToolTip("Switch between fullscreen and compact view")
        self.view_toggle_btn.clicked.connect(self.view_mode_toggle_requested.emit)
        layout.addWidget(self.view_toggle_btn)

        self.pin_btn = QPushButton("📌")
        self.pin_btn.setCheckable(True)
        self.pin_btn.clicked.connect(self._on_pin_clicked)
        layout.addWidget(self.pin_btn)

        close_btn = QPushButton("✕")
        close_btn.clicked.connect(self.close_requested.emit)
        layout.addWidget(close_btn)

    def set_compact_mode(self, compact: bool) -> None:
        """Concise look when minimized: shrink the title, hide the
        'add column' affordance (editing lives in fullscreen), and flip
        the toggle icon."""
        self.title_label.setText("📋" if compact else "📋 Floating Kanban")
        self.add_col_btn.setVisible(not compact)
        self.view_toggle_btn.setText("⛶" if compact else "⤢")
        self.view_toggle_btn.setToolTip("Expand to fullscreen" if compact else "Minimize to compact view")

    def _on_pin_clicked(self) -> None:
        self._pinned = self.pin_btn.isChecked()
        self.pin_toggled.emit(self._pinned)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)


# ============================================================================
# Analytics panel -- only shown in expanded/fullscreen mode
# ============================================================================
class _MiniBarChart(QWidget):
    """A tiny hand-painted horizontal bar chart (cards done vs. total per
    column). No charting library needed for a handful of columns."""

    def __init__(self, per_column: list[dict], parent=None):
        super().__init__(parent)
        self.per_column = per_column
        row_height = 22
        self.setMinimumHeight(max(row_height * len(per_column), row_height) + 8)

    def set_data(self, per_column: list[dict]) -> None:
        self.per_column = per_column
        row_height = 22
        self.setMinimumHeight(max(row_height * len(per_column), row_height) + 8)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 -- Qt naming convention
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row_height = 22
        label_width = 90
        max_bar_width = max(self.width() - label_width - 40, 20)
        max_total = max((c["total"] for c in self.per_column), default=1) or 1

        for i, col in enumerate(self.per_column):
            y = i * row_height
            painter.setPen(QColor("#cdd6f4"))
            elided = col["title"] if len(col["title"]) <= 12 else col["title"][:11] + "…"
            painter.drawText(0, y, label_width, row_height - 4, Qt.AlignmentFlag.AlignVCenter, elided)

            track_x = label_width
            bar_w_total = int(max_bar_width * (col["total"] / max_total))
            bar_w_done = int(max_bar_width * (col["done"] / max_total)) if max_total else 0

            painter.setBrush(QColor("#313244"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(track_x, y + 3, bar_w_total, row_height - 10, 4, 4)

            painter.setBrush(QColor(ACCENT_COLOR))
            painter.drawRoundedRect(track_x, y + 3, bar_w_done, row_height - 10, 4, 4)

            painter.setPen(QColor("#a6adc8"))
            painter.drawText(
                track_x + max_bar_width + 6, y, 34, row_height - 4,
                Qt.AlignmentFlag.AlignVCenter, f"{col['done']}/{col['total']}",
            )
        painter.end()


class AnalyticsPanel(QFrame):
    """Summary stats shown alongside the board in expanded/fullscreen mode:
    total/done/overdue/due-soon counts plus a per-column completion chart."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(220)
        self.setStyleSheet(
            """
            QFrame#analyticsPanel { background-color: rgba(24, 24, 37, 0.75); border-radius: 10px; }
            QLabel { color: #cdd6f4; }
            """
        )
        self.setObjectName("analyticsPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        header = QLabel("📊 Analytics")
        header.setStyleSheet("font-weight: 700; font-size: 13px;")
        layout.addWidget(header)

        self.stat_grid = QGridLayout()
        self.stat_grid.setSpacing(4)
        layout.addLayout(self.stat_grid)
        self._stat_labels: dict[str, QLabel] = {}
        for row, (key, caption) in enumerate(
            [("total", "Total cards"), ("done", "Completed"), ("overdue", "Overdue"), ("due_soon", "Due soon")]
        ):
            caption_lbl = QLabel(caption)
            caption_lbl.setStyleSheet("color: #a6adc8; font-size: 11px;")
            value_lbl = QLabel("0")
            value_lbl.setStyleSheet("font-weight: 700; font-size: 13px;")
            self.stat_grid.addWidget(caption_lbl, row, 0)
            self.stat_grid.addWidget(value_lbl, row, 1)
            self._stat_labels[key] = value_lbl

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #45475a; max-height: 1px;")
        layout.addWidget(sep)

        chart_caption = QLabel("Progress by column")
        chart_caption.setStyleSheet("color: #a6adc8; font-size: 11px;")
        layout.addWidget(chart_caption)

        self.chart = _MiniBarChart([])
        layout.addWidget(self.chart)
        layout.addStretch()

    def refresh(self, analytics: dict) -> None:
        overdue = 0
        due_soon = 0
        for row in analytics["rows"]:
            status, _ = deadline_status(row.get("deadline"), bool(row.get("is_done")))
            if status == "overdue":
                overdue += 1
            elif status == "due_soon":
                due_soon += 1

        self._stat_labels["total"].setText(str(analytics["total_cards"]))
        self._stat_labels["done"].setText(str(analytics["done_cards"]))
        self._stat_labels["overdue"].setText(str(overdue))
        self._stat_labels["due_soon"].setText(str(due_soon))
        self.chart.set_data(analytics["per_column"])


# ============================================================================
# Compact summary -- the "concise look" shown when the widget is minimized.
# Read-only: no add/edit affordances, just enough to glance at.
# ============================================================================
class CompactSummaryWidget(QFrame):
    expand_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QLabel { color: #cdd6f4; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        self.summary_label = QLabel("No columns yet")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-size: 11px; color: #a6adc8;")
        layout.addWidget(self.summary_label)

        self.next_deadline_label = QLabel("")
        self.next_deadline_label.setWordWrap(True)
        self.next_deadline_label.setStyleSheet("font-size: 11px; font-weight: 600;")
        layout.addWidget(self.next_deadline_label)
        layout.addStretch()

    def mouseDoubleClickEvent(self, event) -> None:
        self.expand_requested.emit()
        super().mouseDoubleClickEvent(event)

    def refresh(self, columns: list[dict], analytics: dict) -> None:
        if columns:
            parts = [f"{c['title']}: {sum(1 for r in analytics['rows'] if r.get('column_title') == c['title'])}" for c in columns]
            self.summary_label.setText("  •  ".join(parts))
        else:
            self.summary_label.setText("No columns yet")

        soonest = None
        soonest_dt = None
        for row in analytics["rows"]:
            if row.get("is_done"):
                continue
            dt = _parse_iso(row.get("deadline"))
            if dt is not None and (soonest_dt is None or dt < soonest_dt):
                soonest_dt = dt
                soonest = row
        if soonest is not None:
            status, label = deadline_status(soonest.get("deadline"), False)
            icon = "🔴" if status == "overdue" else ("🟡" if status == "due_soon" else "🗓")
            self.next_deadline_label.setText(f"{icon} Next: {label}")
        else:
            self.next_deadline_label.setText("No upcoming deadlines")


# ============================================================================
# The main floating board window
# ============================================================================
class KanbanWindow(QWidget):
    def __init__(self, local_store: LocalStore, board: dict):
        super().__init__()
        self.local_store = local_store
        self.board = board
        self.columns: dict[str, ColumnWidget] = {}
        self.view_mode = "expanded"  # "expanded" (fullscreen, editable) | "compact" (minimized, concise)
        self._expanded_geometry: tuple[int, int, int, int] | None = None  # remembers size/pos across mode switches

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool  # keeps it off the taskbar, like a widget
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self._build_ui()
        self.reload_from_local()

        # Deadlines don't need a DB round trip to re-color -- just re-derive
        # from "now" periodically so a card visibly turns amber/red while
        # the board sits open.
        self.deadline_timer = QTimer(self)
        self.deadline_timer.setInterval(DEADLINE_CHECK_INTERVAL_MS)
        self.deadline_timer.timeout.connect(self._refresh_deadline_badges)
        self.deadline_timer.start()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.container = QFrame()
        self.container.setStyleSheet(
            f"""
            QFrame#mainContainer {{
                background-color: {WINDOW_OPACITY_BG};
                border-radius: 12px;
            }}
            """
        )
        self.container.setObjectName("mainContainer")
        root.addWidget(self.container)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        self.header = HeaderBar()
        self.header.pin_toggled.connect(self._on_pin_toggled)
        self.header.close_requested.connect(self.hide)
        self.header.add_column_requested.connect(self._on_add_column)
        self.header.view_mode_toggle_requested.connect(self.toggle_view_mode)
        container_layout.addWidget(self.header)

        # ---- expanded (fullscreen) content: board + analytics, resizable via a splitter ----
        self.board_scroll = QScrollArea()
        self.board_scroll.setWidgetResizable(True)
        self.board_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.board_scroll.setStyleSheet("background: transparent;")
        self.board_body = QWidget()
        self.board_body.setStyleSheet("background: transparent;")
        self.board_layout = QHBoxLayout(self.board_body)
        self.board_layout.setContentsMargins(10, 10, 10, 10)
        self.board_layout.setSpacing(10)
        self.board_layout.addStretch()
        self.board_scroll.setWidget(self.board_body)

        self.analytics_panel = AnalyticsPanel()

        self.expanded_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.expanded_splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        self.expanded_splitter.addWidget(self.board_scroll)
        self.expanded_splitter.addWidget(self.analytics_panel)
        self.expanded_splitter.setStretchFactor(0, 1)
        self.expanded_splitter.setStretchFactor(1, 0)
        container_layout.addWidget(self.expanded_splitter, stretch=1)

        # ---- compact (minimized) content: concise, read-only summary ----
        self.compact_summary = CompactSummaryWidget()
        self.compact_summary.expand_requested.connect(self.set_view_mode_expanded)
        self.compact_summary.setVisible(False)
        container_layout.addWidget(self.compact_summary, stretch=1)

        # ---- resize grip, bottom-right corner, works in both view modes ----
        grip_row = QHBoxLayout()
        grip_row.addStretch()
        self.size_grip = QSizeGrip(self.container)
        self.size_grip.setStyleSheet("background: transparent;")
        grip_row.addWidget(self.size_grip, alignment=Qt.AlignmentFlag.AlignBottom)
        container_layout.addLayout(grip_row)

    # ------------------------------------------------------------------
    # View mode: fullscreen/editable <-> compact/concise
    # ------------------------------------------------------------------
    def toggle_view_mode(self) -> None:
        if self.view_mode == "expanded":
            self.set_view_mode_compact()
        else:
            self.set_view_mode_expanded()

    def set_view_mode_compact(self) -> None:
        self.view_mode = "compact"
        self._expanded_geometry = (self.x(), self.y(), self.width(), self.height())

        self.expanded_splitter.setVisible(False)
        self.compact_summary.setVisible(True)
        self.header.set_compact_mode(True)
        self.setMinimumSize(MIN_COMPACT_WIDTH, MIN_COMPACT_HEIGHT)
        self.resize(COMPACT_WIDTH, COMPACT_HEIGHT)
        self._refresh_compact_summary()

    def set_view_mode_expanded(self) -> None:
        self.view_mode = "expanded"

        self.compact_summary.setVisible(False)
        self.expanded_splitter.setVisible(True)
        self.header.set_compact_mode(False)
        self.setMinimumSize(MIN_EXPANDED_WIDTH, MIN_EXPANDED_HEIGHT)
        if self._expanded_geometry:
            x, y, w, h = self._expanded_geometry
            self.setGeometry(x, y, max(w, MIN_EXPANDED_WIDTH), max(h, MIN_EXPANDED_HEIGHT))
        else:
            self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self._refresh_analytics()

    # ------------------------------------------------------------------
    # Loading data from local cache into the UI
    # ------------------------------------------------------------------
    def reload_from_local(self) -> None:
        while self.board_layout.count() > 1:
            item = self.board_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.columns.clear()

        for column in self.local_store.get_columns(self.board["id"]):
            self._add_column_widget(column)

        self._refresh_analytics()
        self._refresh_compact_summary()

    def _add_column_widget(self, column: dict) -> ColumnWidget:
        widget = ColumnWidget(column)
        widget.card_moved.connect(self._on_card_moved)
        widget.card_add_requested.connect(self._on_add_card)
        widget.card_edit_requested.connect(self._on_edit_card)
        widget.card_delete_requested.connect(self._on_delete_card)
        widget.card_done_toggled.connect(self._on_card_done_toggled)
        widget.column_rename_requested.connect(self._on_rename_column)
        widget.column_delete_requested.connect(self._on_delete_column)
        self.board_layout.insertWidget(self.board_layout.count() - 1, widget)
        self.columns[column["id"]] = widget
        widget.set_cards(self.local_store.get_cards(column["id"]))
        return widget

    def refresh_column(self, column_id: str) -> None:
        widget = self.columns.get(column_id)
        if widget:
            widget.set_cards(self.local_store.get_cards(column_id))
        self._refresh_analytics()
        self._refresh_compact_summary()

    # ------------------------------------------------------------------
    # Analytics / compact-summary refresh (fed by the same LocalStore query)
    # ------------------------------------------------------------------
    def _refresh_analytics(self) -> None:
        if self.view_mode != "expanded":
            return
        analytics = self.local_store.get_board_analytics(self.board["id"])
        self.analytics_panel.refresh(analytics)

    def _refresh_compact_summary(self) -> None:
        if self.view_mode != "compact":
            return
        analytics = self.local_store.get_board_analytics(self.board["id"])
        columns = self.local_store.get_columns(self.board["id"])
        self.compact_summary.refresh(columns, analytics)

    def _refresh_deadline_badges(self) -> None:
        for column_widget in self.columns.values():
            for card_widget in column_widget.iter_card_widgets():
                card_widget.refresh_deadline_style()
        # Overdue/due-soon counts can shift purely with the passage of time,
        # independent of any edit, so re-derive whichever summary is visible.
        self._refresh_analytics()
        self._refresh_compact_summary()

    # ------------------------------------------------------------------
    # User actions -> local_store writes (sync_manager picks these up
    # from the sync_queue automatically)
    # ------------------------------------------------------------------
    def _on_add_column(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        title, ok = QInputDialog.getText(self, "New column", "Column title:")
        if ok and title.strip():
            position = len(self.columns)
            column = self.local_store.add_column(self.board["id"], title.strip(), position)
            self._add_column_widget(column)
            self._refresh_analytics()

    def _on_rename_column(self, column_id: str, new_title: str) -> None:
        self.local_store.rename_column(column_id, new_title)
        widget = self.columns.get(column_id)
        if widget:
            widget.title_label.setText(new_title)
        self._refresh_analytics()

    def _on_delete_column(self, column_id: str) -> None:
        self.local_store.delete_column(column_id)
        widget = self.columns.pop(column_id, None)
        if widget:
            widget.deleteLater()
        self._refresh_analytics()

    def _on_add_card(self, column_id: str) -> None:
        dialog = CardEditDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            title, desc, color, deadline_iso, is_done = dialog.values()
            if not title:
                return
            existing = self.local_store.get_cards(column_id)
            self.local_store.add_card(column_id, title, desc, color, len(existing), deadline=deadline_iso, is_done=is_done)
            self.refresh_column(column_id)

    def _on_edit_card(self, card_id: str) -> None:
        # find current card values by scanning local cache (kept simple --
        # a production build might index cards by id in memory)
        for column_id, widget in self.columns.items():
            for card in self.local_store.get_cards(column_id):
                if card["id"] == card_id:
                    dialog = CardEditDialog(
                        card["title"],
                        card.get("description") or "",
                        card.get("color_label") or DEFAULT_CARD_COLOR,
                        deadline_iso=card.get("deadline"),
                        is_done=bool(card.get("is_done")),
                        parent=self,
                    )
                    if dialog.exec() == QDialog.DialogCode.Accepted:
                        title, desc, color, deadline_iso, is_done = dialog.values()
                        self.local_store.update_card(
                            card_id,
                            title=title,
                            description=desc,
                            color_label=color,
                            deadline=deadline_iso,
                            is_done=int(is_done),
                        )
                        self.refresh_column(column_id)
                    return

    def _on_card_done_toggled(self, card_id: str, is_done: bool) -> None:
        # The checkbox on the card is the fast path for the "checklist"
        # workflow -- no need to open the full edit dialog just to tick
        # a task off.
        self.local_store.update_card(card_id, is_done=int(is_done))
        for column_id in self.columns:
            self.refresh_column(column_id)

    def _on_delete_card(self, card_id: str) -> None:
        self.local_store.delete_card(card_id)
        # refresh every column since we don't track card->column locally here
        for column_id in self.columns:
            self.refresh_column(column_id)

    def _on_card_moved(self, card_id: str, target_column_id: str, index: int) -> None:
        self.local_store.move_card(card_id, target_column_id, index)
        for column_id in self.columns:
            self.refresh_column(column_id)

    def _on_pin_toggled(self, pinned: bool) -> None:
        flags = self.windowFlags()
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

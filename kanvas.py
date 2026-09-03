#!/usr/bin/env python3
"""
Simple Kanban Board - Qt + SQLite edition, with multiple boards
===================================================================
A small desktop app that can hold several independent Kanban boards,
each with its own columns and tasks. Built with PySide6 (Qt for
Python) and backed by a local SQLite database. Works on Windows and
Linux (tested against Fedora).

Install the one dependency, then run:
    pip install PySide6
    python3 kanvas.py

The database lives at:
    Windows:  %APPDATA%\\KanbanBoard\\kanban.db
    Linux:    ~/.local/share/kanban_board/kanban.db

On first run, one board is created for you (with the usual Today /
In Progress / Blocked / Complete columns). Use "+ Board" to add more,
and the small buttons next to the board selector to rename, reorder,
or delete the current one. If you're upgrading from a version of
this app that only had a single board, your existing columns and
tasks are moved into a board called "Default" automatically.
"""

import os
import re
import sys
import uuid
import secrets
import sqlite3
import platform
import threading
from datetime import datetime

from PySide6.QtCore import Qt, QRect, QDate, QObject, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QListView, QComboBox,
    QAbstractItemView, QInputDialog, QMessageBox, QDialog, QLineEdit, QTextEdit,
    QCheckBox, QDateEdit, QMenu,
)

APP_TITLE = "Kanvas"
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
WINDOWS_APP_USER_MODEL_ID = "Kanvas.KanbanBoard"

# Seed columns created the first time a board is set up (whether that's
# the very first board on a fresh install, or a board created later with
# "+ Board"). The slugs (first element of each pair) are what's stored
# in each task's "status" column.
DEFAULT_COLUMNS = [
    ("today", "Today"),
    ("in_progress", "In Progress"),
    ("blocked", "Blocked"),
    ("complete", "Complete"),
]

DEFAULT_FIRST_BOARD_NAME = "My Board"
LEGACY_BOARD_NAME = "Default"


# ---------------------------------------------------------------------------
# Data layer (plain sqlite3, no Qt dependency, so it can be tested and
# reasoned about on its own).
# ---------------------------------------------------------------------------

def get_data_dir() -> str:
    """Return the OS-appropriate app data directory, creating it if needed."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        data_dir = os.path.join(base, "KanbanBoard")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        data_dir = os.path.join(base, "kanban_board")

    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_db_path() -> str:
    """Return the OS-appropriate path to the SQLite database file,
    creating the containing directory if needed."""
    data_dir = get_data_dir()
    return os.path.join(data_dir, "kanban.db")


def get_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug or "column"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _migrate_legacy_tasks_missing_board_id(conn: sqlite3.Connection) -> None:
    """Covers a database left mid-upgrade: "columns" already has board_id,
    but "tasks" was never given one (e.g. the process was killed between
    the two ALTER steps of an earlier run). Back-fill using the first
    board by position, since a lone-task database with no board_id at
    all can only have come from a single-board history."""
    if _table_has_column(conn, "tasks", "board_id"):
        return

    first_board = conn.execute(
        "SELECT id FROM boards ORDER BY position ASC LIMIT 1"
    ).fetchone()
    if first_board is None:
        return

    conn.execute("ALTER TABLE tasks ADD COLUMN board_id TEXT")
    conn.execute("UPDATE tasks SET board_id = ? WHERE board_id IS NULL", (first_board["id"],))
    conn.commit()


def _migrate_task_card_fields(conn: sqlite3.Connection) -> None:
    """Adds the due_date and joplin_link columns to "tasks" for databases
    created before the task card grew those fields."""
    if not _table_has_column(conn, "tasks", "due_date"):
        conn.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT NOT NULL DEFAULT ''")
    if not _table_has_column(conn, "tasks", "joplin_link"):
        conn.execute("ALTER TABLE tasks ADD COLUMN joplin_link TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _migrate_legacy_single_board_schema(conn: sqlite3.Connection) -> None:
    """If this database was created by a pre-multi-board version of this
    app, its "columns" table has no board_id column and a single-column
    primary key. Move everything into a new "Default" board rather than
    losing it. Fresh installs never hit this: init_db() creates the
    final schema directly, so "columns" already has board_id by the
    time this runs."""
    if not _table_exists(conn, "columns"):
        return
    if _table_has_column(conn, "columns", "board_id"):
        _migrate_legacy_tasks_missing_board_id(conn)
        return  # already on the current schema

    conn.execute("""
        CREATE TABLE IF NOT EXISTS boards (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            created TEXT NOT NULL
        )
    """)

    default_board_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO boards (id, name, position, created) VALUES (?, ?, 0, ?)",
        (default_board_id, LEGACY_BOARD_NAME, _now()),
    )

    # The old columns table had `status TEXT PRIMARY KEY`, which would
    # collide across boards going forward, so it needs a real rebuild
    # rather than just an ALTER TABLE ADD COLUMN.
    conn.execute("ALTER TABLE columns RENAME TO columns_legacy")
    conn.execute("""
        CREATE TABLE columns (
            board_id TEXT NOT NULL,
            status TEXT NOT NULL,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (board_id, status)
        )
    """)
    for row in conn.execute("SELECT status, name, position FROM columns_legacy").fetchall():
        conn.execute(
            "INSERT INTO columns (board_id, status, name, position) VALUES (?, ?, ?, ?)",
            (default_board_id, row["status"], row["name"], row["position"]),
        )
    conn.execute("DROP TABLE columns_legacy")

    # tasks.id was already the primary key, so it can just gain a column.
    if not _table_has_column(conn, "tasks", "board_id"):
        conn.execute("ALTER TABLE tasks ADD COLUMN board_id TEXT")
    conn.execute("UPDATE tasks SET board_id = ? WHERE board_id IS NULL", (default_board_id,))

    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS boards (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            created TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS columns (
            board_id TEXT NOT NULL,
            status TEXT NOT NULL,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (board_id, status)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            due_date TEXT NOT NULL DEFAULT '',
            joplin_link TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subtasks (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL
        )
    """)
    conn.commit()

    _migrate_legacy_single_board_schema(conn)
    _migrate_task_card_fields(conn)

    # Fresh install: no boards exist yet at all (migration only runs for
    # upgrades, so this is the true "never used before" case).
    if not get_boards(conn):
        add_board(conn, DEFAULT_FIRST_BOARD_NAME)


# -- Boards -----------------------------------------------------------------

def get_boards(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT * FROM boards ORDER BY position ASC").fetchall()
    return [dict(r) for r in rows]


def get_board(conn: sqlite3.Connection, board_id: str):
    row = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    return dict(row) if row is not None else None


def get_board_task_count(conn: sqlite3.Connection, board_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE board_id = ?", (board_id,)).fetchone()
    return row["c"]


def add_board(conn: sqlite3.Connection, name: str, seed_default_columns: bool = True) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Board name cannot be empty.")

    max_position_row = conn.execute("SELECT MAX(position) AS m FROM boards").fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    board_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO boards (id, name, position, created) VALUES (?, ?, ?, ?)",
        (board_id, name, next_position, _now()),
    )
    conn.commit()

    if seed_default_columns:
        for position, (status, col_name) in enumerate(DEFAULT_COLUMNS):
            conn.execute(
                "INSERT INTO columns (board_id, status, name, position) VALUES (?, ?, ?, ?)",
                (board_id, status, col_name, position),
            )
        conn.commit()

    return get_board(conn, board_id)


def rename_board(conn: sqlite3.Connection, board_id: str, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Board name cannot be empty.")
    conn.execute("UPDATE boards SET name = ? WHERE id = ?", (new_name, board_id))
    conn.commit()


def _compact_board_positions(conn: sqlite3.Connection) -> None:
    for position, board in enumerate(get_boards(conn)):
        if board["position"] != position:
            conn.execute("UPDATE boards SET position = ? WHERE id = ?", (position, board["id"]))
    conn.commit()


def delete_board(conn: sqlite3.Connection, board_id: str) -> None:
    """Deletes a board along with all of its columns and tasks. Callers
    that want to warn the user first should check get_board_task_count()
    before calling this."""
    boards = get_boards(conn)
    if len(boards) <= 1:
        raise ValueError("At least one board must remain.")

    conn.execute("DELETE FROM tasks WHERE board_id = ?", (board_id,))
    conn.execute("DELETE FROM columns WHERE board_id = ?", (board_id,))
    conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    conn.commit()
    _compact_board_positions(conn)


def move_board(conn: sqlite3.Connection, board_id: str, direction: int) -> None:
    """Swap a board with its neighbour. direction=-1 moves it earlier,
    +1 moves it later. No-op if already at that edge."""
    boards = get_boards(conn)
    ids = [b["id"] for b in boards]
    if board_id not in ids:
        return

    idx = ids.index(board_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(boards):
        return

    boards[idx], boards[new_idx] = boards[new_idx], boards[idx]
    for position, board in enumerate(boards):
        conn.execute("UPDATE boards SET position = ? WHERE id = ?", (position, board["id"]))
    conn.commit()


# -- Columns (always scoped to a board) --------------------------------------

def get_columns(conn: sqlite3.Connection, board_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM columns WHERE board_id = ? ORDER BY position ASC", (board_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_column(conn: sqlite3.Connection, board_id: str, status: str):
    row = conn.execute(
        "SELECT * FROM columns WHERE board_id = ? AND status = ?", (board_id, status)
    ).fetchone()
    return dict(row) if row is not None else None


def get_default_new_task_status(conn: sqlite3.Connection, board_id: str):
    columns = get_columns(conn, board_id)
    return columns[0]["status"] if columns else None


def add_column(conn: sqlite3.Connection, board_id: str, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Column name cannot be empty.")

    existing_statuses = {c["status"] for c in get_columns(conn, board_id)}
    base_slug = _slugify(name)
    slug = base_slug
    suffix = 2
    while slug in existing_statuses:
        slug = f"{base_slug}_{suffix}"
        suffix += 1

    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM columns WHERE board_id = ?", (board_id,)
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    conn.execute(
        "INSERT INTO columns (board_id, status, name, position) VALUES (?, ?, ?, ?)",
        (board_id, slug, name, next_position),
    )
    conn.commit()
    return get_column(conn, board_id, slug)


def rename_column(conn: sqlite3.Connection, board_id: str, status: str, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Column name cannot be empty.")
    conn.execute(
        "UPDATE columns SET name = ? WHERE board_id = ? AND status = ?",
        (new_name, board_id, status),
    )
    conn.commit()


def _compact_column_positions(conn: sqlite3.Connection, board_id: str) -> None:
    for position, col in enumerate(get_columns(conn, board_id)):
        if col["position"] != position:
            conn.execute(
                "UPDATE columns SET position = ? WHERE board_id = ? AND status = ?",
                (position, board_id, col["status"]),
            )
    conn.commit()


def delete_column(conn: sqlite3.Connection, board_id: str, status: str) -> None:
    columns = get_columns(conn, board_id)
    if len(columns) <= 1:
        raise ValueError("At least one column must remain on this board.")

    task_count = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE board_id = ? AND status = ?", (board_id, status)
    ).fetchone()["c"]
    if task_count > 0:
        raise ValueError(
            f"This column still has {task_count} task(s) in it. "
            "Move or delete them first, then delete the column."
        )

    conn.execute("DELETE FROM columns WHERE board_id = ? AND status = ?", (board_id, status))
    conn.commit()
    _compact_column_positions(conn, board_id)


def move_column(conn: sqlite3.Connection, board_id: str, status: str, direction: int) -> None:
    columns = get_columns(conn, board_id)
    statuses = [c["status"] for c in columns]
    if status not in statuses:
        return

    idx = statuses.index(status)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(columns):
        return

    columns[idx], columns[new_idx] = columns[new_idx], columns[idx]
    for position, col in enumerate(columns):
        conn.execute(
            "UPDATE columns SET position = ? WHERE board_id = ? AND status = ?",
            (position, board_id, col["status"]),
        )
    conn.commit()


# -- Tasks (looked up by their own id once created; add/list need board_id) --

def add_task(conn: sqlite3.Connection, board_id: str, title: str, notes: str = "", status: str = None) -> dict:
    if status is None:
        status = get_default_new_task_status(conn, board_id)
    if status is None:
        raise ValueError("This board has no columns to add a task to. Create a column first.")

    task_id = uuid.uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO tasks (id, board_id, title, notes, status, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, board_id, title, notes, status, now, now),
    )
    conn.commit()
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row is not None else None


def get_tasks_by_status(conn: sqlite3.Connection, board_id: str, status: str) -> list:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE board_id = ? AND status = ? ORDER BY updated DESC",
        (board_id, status),
    ).fetchall()
    return [dict(r) for r in rows]


def update_task(
    conn: sqlite3.Connection,
    task_id: str,
    title: str,
    notes: str,
    due_date: str = "",
    joplin_link: str = "",
) -> None:
    conn.execute(
        "UPDATE tasks SET title = ?, notes = ?, due_date = ?, joplin_link = ?, updated = ? WHERE id = ?",
        (title, notes, due_date, joplin_link, _now(), task_id),
    )
    conn.commit()


def move_task(conn: sqlite3.Connection, task_id: str, new_status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated = ? WHERE id = ?",
        (new_status, _now(), task_id),
    )
    conn.commit()


def delete_task(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
    conn.commit()


# -- Subtasks (checklist items that belong to a single task) ----------------

def get_subtasks(conn: sqlite3.Connection, task_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM subtasks WHERE task_id = ? ORDER BY position ASC", (task_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_subtask(conn: sqlite3.Connection, task_id: str, title: str) -> dict:
    title = title.strip()
    if not title:
        raise ValueError("Subtask title cannot be empty.")

    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM subtasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    subtask_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO subtasks (id, task_id, title, done, position) VALUES (?, ?, ?, 0, ?)",
        (subtask_id, task_id, title, next_position),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM subtasks WHERE id = ?", (subtask_id,)).fetchone()
    return dict(row)


def set_subtask_done(conn: sqlite3.Connection, subtask_id: str, done: bool) -> None:
    conn.execute("UPDATE subtasks SET done = ? WHERE id = ?", (1 if done else 0, subtask_id))
    conn.commit()


def delete_subtask(conn: sqlite3.Connection, subtask_id: str) -> None:
    conn.execute("DELETE FROM subtasks WHERE id = ?", (subtask_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

# Kanvas's own dark theme, applied app-wide via QApplication.setStyleSheet()
# so the look is consistent regardless of the OS's light/dark setting.
# ColumnWidget and TaskListWidget are styled by their (Python) class name,
# which Qt's stylesheet engine matches like any other type selector - this
# keeps the card look scoped to board columns without touching unrelated
# QListWidgets (e.g. the subtask checklist in TaskCardDialog).
ACCENT_COLOR = "#4F46E5"
ACCENT_COLOR_HOVER = "#5b52f0"

APP_STYLESHEET = f"""
QWidget {{
    background-color: #1b1c1e;
    color: #e8e8e8;
    font-size: 13px;
}}

ColumnWidget {{
    background-color: #232427;
    border-radius: 10px;
}}

QLabel {{
    background: transparent;
}}

QPushButton {{
    background-color: #2c2e33;
    color: #e8e8e8;
    border: 1px solid #3a3d42;
    border-radius: 6px;
    padding: 5px 10px;
}}
QPushButton:hover {{
    background-color: #34363b;
}}
QPushButton:pressed {{
    background-color: #26282c;
}}
QPushButton:disabled {{
    color: #6b6b6b;
}}

QPushButton[accent="true"] {{
    background-color: {ACCENT_COLOR};
    border: 1px solid {ACCENT_COLOR};
    color: #ffffff;
    font-weight: bold;
}}
QPushButton[accent="true"]:hover {{
    background-color: {ACCENT_COLOR_HOVER};
}}

QPushButton[compact="true"] {{
    padding: 2px 0px;
}}

TaskListWidget {{
    background: transparent;
    border: none;
}}
TaskListWidget::item {{
    background-color: #2c2e33;
    border: 1px solid #3a3d42;
    border-radius: 8px;
    padding: 10px;
    margin: 6px 2px;
}}
TaskListWidget::item:hover {{
    background-color: #34363b;
}}
TaskListWidget::item:selected {{
    background-color: #2e2f45;
    border: 1px solid {ACCENT_COLOR};
}}

QLineEdit, QTextEdit, QComboBox, QDateEdit {{
    background-color: #2c2e33;
    border: 1px solid #3a3d42;
    border-radius: 6px;
    padding: 4px 6px;
    color: #e8e8e8;
}}
QComboBox QAbstractItemView {{
    background-color: #2c2e33;
    color: #e8e8e8;
    selection-background-color: {ACCENT_COLOR};
}}
"""


class TaskCardDialog(QDialog):
    """Full card view for a single task: title, status/column, due date,
    Joplin note link, notes, a subtask checklist, and the created/updated
    timestamps, all in one dialog rather than the separate title-then-notes
    prompts this replaced.

    Title/status/due-date/Joplin-link/notes are only committed if the user
    clicks Save (standard form semantics), but subtask add/check/delete
    write straight through to the database as they happen — a checklist
    that could be "cancelled" would be surprising, and it avoids having to
    diff and reconcile a whole second collection on save."""

    def __init__(self, conn: sqlite3.Connection, task: dict, columns: list, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.task_id = task["id"]
        self.setWindowTitle(task["title"])
        self.resize(440, 620)
        self.delete_requested = False

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(task["title"])
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Status"))
        self.status_combo = QComboBox()
        for col in columns:
            self.status_combo.addItem(col["name"], col["status"])
        current_idx = next(
            (i for i, col in enumerate(columns) if col["status"] == task["status"]), 0
        )
        self.status_combo.setCurrentIndex(current_idx)
        layout.addWidget(self.status_combo)

        layout.addWidget(QLabel("Due Date"))
        due_row = QHBoxLayout()
        self.due_date_check = QCheckBox("Set")
        due_row.addWidget(self.due_date_check)
        self.due_date_edit = QDateEdit()
        self.due_date_edit.setCalendarPopup(True)
        self.due_date_edit.setDisplayFormat("yyyy-MM-dd")
        existing_due_date = QDate.fromString(task.get("due_date") or "", "yyyy-MM-dd")
        self.due_date_check.setChecked(existing_due_date.isValid())
        self.due_date_edit.setDate(existing_due_date if existing_due_date.isValid() else QDate.currentDate())
        self.due_date_edit.setEnabled(existing_due_date.isValid())
        self.due_date_check.toggled.connect(self.due_date_edit.setEnabled)
        due_row.addWidget(self.due_date_edit, stretch=1)
        layout.addLayout(due_row)

        layout.addWidget(QLabel("Joplin Note Link"))
        self.joplin_link_edit = QLineEdit(task.get("joplin_link") or "")
        self.joplin_link_edit.setPlaceholderText("joplin://... or https://...")
        layout.addWidget(self.joplin_link_edit)

        layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(task.get("notes") or "")
        layout.addWidget(self.notes_edit, stretch=1)

        layout.addWidget(QLabel("Subtasks"))
        self.subtasks_list = QListWidget()
        self.subtasks_list.itemChanged.connect(self._on_subtask_item_changed)
        layout.addWidget(self.subtasks_list, stretch=1)

        subtask_add_row = QHBoxLayout()
        self.new_subtask_edit = QLineEdit()
        self.new_subtask_edit.setPlaceholderText("New subtask...")
        self.new_subtask_edit.returnPressed.connect(self._add_subtask)
        subtask_add_row.addWidget(self.new_subtask_edit)
        add_subtask_btn = QPushButton("Add")
        add_subtask_btn.clicked.connect(self._add_subtask)
        subtask_add_row.addWidget(add_subtask_btn)
        delete_subtask_btn = QPushButton("Delete")
        delete_subtask_btn.clicked.connect(self._delete_selected_subtask)
        subtask_add_row.addWidget(delete_subtask_btn)
        layout.addLayout(subtask_add_row)

        self._refresh_subtasks()

        meta_label = QLabel(f"Created {task['created']}    ·    Updated {task['updated']}")
        meta_label.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(meta_label)

        btn_row = QHBoxLayout()
        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet("color: #b00000;")
        delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setProperty("accent", True)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    # -- subtasks (write straight to the database, see class docstring) --

    def _refresh_subtasks(self) -> None:
        self.subtasks_list.blockSignals(True)
        self.subtasks_list.clear()
        for sub in get_subtasks(self.conn, self.task_id):
            item = QListWidgetItem(sub["title"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if sub["done"] else Qt.Unchecked)
            item.setData(Qt.UserRole, sub["id"])
            self.subtasks_list.addItem(item)
        self.subtasks_list.blockSignals(False)

    def _on_subtask_item_changed(self, item: QListWidgetItem) -> None:
        subtask_id = item.data(Qt.UserRole)
        set_subtask_done(self.conn, subtask_id, item.checkState() == Qt.Checked)

    def _add_subtask(self) -> None:
        title = self.new_subtask_edit.text().strip()
        if not title:
            return
        add_subtask(self.conn, self.task_id, title)
        self.new_subtask_edit.clear()
        self._refresh_subtasks()

    def _delete_selected_subtask(self) -> None:
        item = self.subtasks_list.currentItem()
        if item is None:
            return
        delete_subtask(self.conn, item.data(Qt.UserRole))
        self._refresh_subtasks()

    # -- title/status/due-date/link/notes (only committed on Save) -------

    def _on_delete(self) -> None:
        self.delete_requested = True
        self.reject()

    def result_values(self) -> dict:
        due_date = self.due_date_edit.date().toString("yyyy-MM-dd") if self.due_date_check.isChecked() else ""
        return {
            "title": self.title_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "status": self.status_combo.currentData(),
            "due_date": due_date,
            "joplin_link": self.joplin_link_edit.text().strip(),
        }


class NewTaskDialog(QDialog):
    """Single-form task creation dialog, styled and laid out like
    TaskCardDialog's edit form (title/status/due date/Joplin link/notes)
    instead of the sequence of plain input-box prompts this replaced."""

    def __init__(self, columns: list, default_status: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Task")
        self.resize(420, 420)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit()
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Status"))
        self.status_combo = QComboBox()
        for col in columns:
            self.status_combo.addItem(col["name"], col["status"])
        default_idx = next(
            (i for i, col in enumerate(columns) if col["status"] == default_status), 0
        )
        self.status_combo.setCurrentIndex(default_idx)
        layout.addWidget(self.status_combo)

        layout.addWidget(QLabel("Due Date"))
        due_row = QHBoxLayout()
        self.due_date_check = QCheckBox("Set")
        due_row.addWidget(self.due_date_check)
        self.due_date_edit = QDateEdit()
        self.due_date_edit.setCalendarPopup(True)
        self.due_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.due_date_edit.setDate(QDate.currentDate())
        self.due_date_edit.setEnabled(False)
        self.due_date_check.toggled.connect(self.due_date_edit.setEnabled)
        due_row.addWidget(self.due_date_edit, stretch=1)
        layout.addLayout(due_row)

        layout.addWidget(QLabel("Joplin Note Link"))
        self.joplin_link_edit = QLineEdit()
        self.joplin_link_edit.setPlaceholderText("joplin://... or https://...")
        layout.addWidget(self.joplin_link_edit)

        layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        layout.addWidget(self.notes_edit, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        add_btn = QPushButton("Add Task")
        add_btn.setProperty("accent", True)
        add_btn.setDefault(True)
        add_btn.clicked.connect(self.accept)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

        self.title_edit.setFocus()

    def result_values(self) -> dict:
        due_date = self.due_date_edit.date().toString("yyyy-MM-dd") if self.due_date_check.isChecked() else ""
        return {
            "title": self.title_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "status": self.status_combo.currentData(),
            "due_date": due_date,
            "joplin_link": self.joplin_link_edit.text().strip(),
        }


class QuickAddDialog(QDialog):
    """Popped up by the global Ctrl+Space shortcut, from anywhere, so it
    needs its own board picker (unlike NewTaskDialog, which always targets
    whichever board is currently open). Tasks are written straight to the
    database as they're added rather than being held for a bulk save, so
    "Create multiple" can add several without losing what's already been
    committed if the dialog is dismissed midway."""

    def __init__(self, conn: sqlite3.Connection, boards: list, current_board_id: str, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.added_board_ids = set()
        self.setWindowTitle("Quick Add Task")
        self.resize(420, 460)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Board"))
        self.board_combo = QComboBox()
        for b in boards:
            self.board_combo.addItem(b["name"], b["id"])
        default_idx = next((i for i, b in enumerate(boards) if b["id"] == current_board_id), 0)
        self.board_combo.setCurrentIndex(default_idx)
        self.board_combo.currentIndexChanged.connect(self._reload_statuses)
        layout.addWidget(self.board_combo)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit()
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Status"))
        self.status_combo = QComboBox()
        layout.addWidget(self.status_combo)

        layout.addWidget(QLabel("Due Date"))
        due_row = QHBoxLayout()
        self.due_date_check = QCheckBox("Set")
        due_row.addWidget(self.due_date_check)
        self.due_date_edit = QDateEdit()
        self.due_date_edit.setCalendarPopup(True)
        self.due_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.due_date_edit.setDate(QDate.currentDate())
        self.due_date_edit.setEnabled(False)
        self.due_date_check.toggled.connect(self.due_date_edit.setEnabled)
        due_row.addWidget(self.due_date_edit, stretch=1)
        layout.addLayout(due_row)

        layout.addWidget(QLabel("Joplin Note Link"))
        self.joplin_link_edit = QLineEdit()
        self.joplin_link_edit.setPlaceholderText("joplin://... or https://...")
        layout.addWidget(self.joplin_link_edit)

        layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        layout.addWidget(self.notes_edit, stretch=1)

        btn_row = QHBoxLayout()
        self.multiple_check = QCheckBox("Create multiple")
        self.multiple_check.setToolTip("Keep this dialog open to add another task after each Add")
        btn_row.addWidget(self.multiple_check)
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        add_btn = QPushButton("Add Task")
        add_btn.setProperty("accent", True)
        add_btn.setDefault(True)
        add_btn.clicked.connect(self._on_add)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

        self._reload_statuses()
        self.title_edit.setFocus()

    def _reload_statuses(self) -> None:
        board_id = self.board_combo.currentData()
        columns = get_columns(self.conn, board_id)
        default_status = get_default_new_task_status(self.conn, board_id)
        self.status_combo.clear()
        for col in columns:
            self.status_combo.addItem(col["name"], col["status"])
        default_idx = next((i for i, col in enumerate(columns) if col["status"] == default_status), 0)
        if columns:
            self.status_combo.setCurrentIndex(default_idx)

    def _on_add(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return
        board_id = self.board_combo.currentData()
        status = self.status_combo.currentData()
        if not status:
            QMessageBox.information(self, "No columns", "That board has no columns to add a task to.")
            return

        notes = self.notes_edit.toPlainText().strip()
        due_date = self.due_date_edit.date().toString("yyyy-MM-dd") if self.due_date_check.isChecked() else ""
        joplin_link = self.joplin_link_edit.text().strip()

        task = add_task(self.conn, board_id, title, notes, status)
        if due_date or joplin_link:
            update_task(self.conn, task["id"], title, notes, due_date, joplin_link)
        self.added_board_ids.add(board_id)

        if self.multiple_check.isChecked():
            self.title_edit.clear()
            self.notes_edit.clear()
            self.joplin_link_edit.clear()
            self.due_date_check.setChecked(False)
            self.title_edit.setFocus()
        else:
            self.accept()


class TaskListWidget(QListWidget):
    """A QListWidget that accepts drops only from another TaskListWidget on
    the SAME board (columns belonging to a different board are never shown
    at the same time, but this guards against any stray drag anyway), and
    asks the board to persist the move rather than letting Qt shuffle items
    around on its own."""

    def __init__(self, status, board, parent=None):
        super().__init__(parent)
        self.status = status
        self.board = board
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setWordWrap(True)
        self.setResizeMode(QListView.Adjust)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.itemDoubleClicked.connect(self._on_double_click)

    def _on_double_click(self, item):
        task_id = item.data(Qt.UserRole)
        self.board.edit_task(task_id)

    def dragEnterEvent(self, event):
        source = event.source()
        if isinstance(source, TaskListWidget) and source is not self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        source = event.source()
        if isinstance(source, TaskListWidget) and source is not self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        source = event.source()
        if not isinstance(source, TaskListWidget) or source is self:
            event.ignore()
            return

        item = source.currentItem()
        if item is None:
            event.ignore()
            return

        task_id = item.data(Qt.UserRole)
        event.setDropAction(Qt.MoveAction)
        event.accept()
        self.board.handle_move(task_id, self.status)


class ColumnWidget(QWidget):
    """One board column: a header (with buttons to rename/reorder/delete
    the column itself) and the task list. Tasks are edited, moved, and
    deleted via the task card (double-click a card to open it) or by
    dragging a card to another column."""

    def __init__(self, column: dict, board, parent=None):
        super().__init__(parent)
        self.status = column["status"]
        self.name = column["name"]
        self.board = board
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        header_row = QHBoxLayout()
        self.header_label = QLabel(self.name)
        self.header_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        header_row.addWidget(self.header_label)
        header_row.addStretch()

        left_btn = QPushButton("<")
        left_btn.setFixedWidth(26)
        left_btn.setProperty("compact", True)
        left_btn.setToolTip("Move this column left")
        left_btn.clicked.connect(lambda: self.board.move_column_ui(self.status, -1))
        header_row.addWidget(left_btn)

        right_btn = QPushButton(">")
        right_btn.setFixedWidth(26)
        right_btn.setProperty("compact", True)
        right_btn.setToolTip("Move this column right")
        right_btn.clicked.connect(lambda: self.board.move_column_ui(self.status, 1))
        header_row.addWidget(right_btn)

        rename_btn = QPushButton("R")
        rename_btn.setFixedWidth(26)
        rename_btn.setProperty("compact", True)
        rename_btn.setToolTip("Rename this column")
        rename_btn.clicked.connect(lambda: self.board.rename_column_ui(self.status))
        header_row.addWidget(rename_btn)

        delete_btn = QPushButton("X")
        delete_btn.setFixedWidth(26)
        delete_btn.setProperty("compact", True)
        delete_btn.setToolTip("Delete this column")
        delete_btn.setStyleSheet("color: #b00000;")
        delete_btn.clicked.connect(lambda: self.board.delete_column_ui(self.status))
        header_row.addWidget(delete_btn)

        self._edit_buttons = [left_btn, right_btn, rename_btn, delete_btn]
        self.set_edit_controls_visible(False)

        layout.addLayout(header_row)

        self.list_widget = TaskListWidget(self.status, board)
        layout.addWidget(self.list_widget)

    def set_count(self, count: int) -> None:
        self.header_label.setText(f"{self.name} ({count})")

    def set_edit_controls_visible(self, visible: bool) -> None:
        for btn in self._edit_buttons:
            btn.setVisible(visible)


class BoardSidePanel(QWidget):
    """Slide-out panel opened by the hamburger button. It's positioned as
    an absolute overlay on top of KanbanBoard (not placed in a layout),
    since it needs to slide in over the existing content rather than
    push it aside."""

    PANEL_WIDTH = 240

    def __init__(self, board, parent=None):
        super().__init__(parent)
        self.board = board
        self.setFixedWidth(self.PANEL_WIDTH)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "BoardSidePanel { background-color: #202225; border-right: 1px solid #35373b; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(16, 14, 8, 14)
        title = QLabel("Boards")
        title.setStyleSheet("font-weight: bold; font-size: 15px; color: #f0f0f0;")
        header_row.addWidget(title)
        header_row.addStretch()
        close_btn = QPushButton("X")
        close_btn.setFixedWidth(26)
        close_btn.setProperty("compact", True)
        close_btn.setFlat(True)
        close_btn.setStyleSheet("color: #cccccc;")
        close_btn.clicked.connect(self.board.close_board_panel)
        header_row.addWidget(close_btn)
        layout.addLayout(header_row)

        my_boards_label = QLabel("MY BOARDS")
        my_boards_label.setStyleSheet(
            "color: #9a9a9a; font-weight: bold; font-size: 11px; padding: 10px 16px 4px 16px;"
        )
        layout.addWidget(my_boards_label)

        self.boards_list_layout = QVBoxLayout()
        self.boards_list_layout.setContentsMargins(0, 0, 0, 0)
        self.boards_list_layout.setSpacing(0)
        layout.addLayout(self.boards_list_layout)

        projects_label = QLabel("PROJECTS")
        projects_label.setStyleSheet(
            "color: #9a9a9a; font-weight: bold; font-size: 11px; padding: 16px 16px 4px 16px;"
        )
        layout.addWidget(projects_label)

        no_projects_label = QLabel("No projects yet")
        no_projects_label.setStyleSheet("color: #6e6e6e; padding: 4px 16px;")
        layout.addWidget(no_projects_label)

        layout.addStretch()

    def refresh(self, boards, current_board_id) -> None:
        while self.boards_list_layout.count():
            item = self.boards_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for b in boards:
            is_current = b["id"] == current_board_id

            row_widget = QWidget()
            row_widget.setObjectName("boardRow")
            row_widget.setAttribute(Qt.WA_StyledBackground, True)
            row_widget.setStyleSheet(
                "QWidget#boardRow { background-color: transparent; border-radius: 8px; }"
                "QWidget#boardRow:hover { background-color: #2c2e33; }"
            )
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(4, 2, 4, 2)
            row.setSpacing(0)

            weight = "bold" if is_current else "normal"
            text_color = "#8b93ff" if is_current else "#e8e8e8"
            flat_btn_qss = (
                "QPushButton {{ background: transparent; border: none; {extra} }}"
                "QPushButton:hover {{ background: transparent; {hover_extra} }}"
                "QPushButton:pressed {{ background: transparent; }}"
            )

            select_btn = QPushButton(("✓  " if is_current else "     ") + b["name"])
            select_btn.setFlat(True)
            select_btn.setStyleSheet(
                flat_btn_qss.format(
                    extra=f"text-align: left; padding: 8px 4px 8px 12px; font-weight: {weight}; color: {text_color};",
                    hover_extra="",
                )
            )
            select_btn.clicked.connect(lambda checked=False, board_id=b["id"]: self.board.select_board_from_panel(board_id))
            row.addWidget(select_btn, stretch=1)

            edit_btn = QPushButton("⋮")
            edit_btn.setFixedWidth(24)
            edit_btn.setStyleSheet(
                flat_btn_qss.format(extra="color: #9a9a9a;", hover_extra="color: #e8e8e8;")
            )
            edit_btn.setToolTip("Edit board")

            def make_menu(checked=False, board_id=b["id"], anchor_btn=edit_btn):
                menu = QMenu(anchor_btn)
                move_up = menu.addAction("Move Up")
                move_down = menu.addAction("Move Down")
                rename = menu.addAction("Rename")
                menu.addSeparator()
                delete = menu.addAction("Delete")

                move_up.triggered.connect(lambda: self.board.move_board_ui(board_id, -1))
                move_down.triggered.connect(lambda: self.board.move_board_ui(board_id, 1))
                rename.triggered.connect(lambda: self.board.rename_board_ui(board_id))
                delete.triggered.connect(lambda: self.board.delete_board_ui(board_id))

                menu.exec(anchor_btn.mapToGlobal(anchor_btn.rect().bottomLeft()))

            edit_btn.clicked.connect(make_menu)
            row.addWidget(edit_btn)

            self.boards_list_layout.addWidget(row_widget)

        if not boards:
            empty = QLabel("No boards yet")
            empty.setStyleSheet("color: #6e6e6e; padding: 4px 16px;")
            self.boards_list_layout.addWidget(empty)

        add_board_btn = QPushButton("+ Add Board")
        add_board_btn.setFlat(True)
        add_board_btn.setStyleSheet("text-align: left; padding: 8px 16px; color: #8b93ff; border: none;")
        add_board_btn.clicked.connect(self.board.add_board_ui)
        self.boards_list_layout.addWidget(add_board_btn)


class KanbanBoard(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.current_board_id = None
        self.columns = {}          # status -> ColumnWidget, for the current board
        self._columns_cache = []   # last-fetched column dicts for the current board
        self._board_panel_open = False
        self._board_panel_anim = None
        self._quick_add_dialog = None

        outer = QVBoxLayout(self)

        toolbar = QHBoxLayout()

        self.board_menu_btn = QPushButton("☰")
        self.board_menu_btn.setFixedWidth(36)
        self.board_menu_btn.setToolTip("Switch board, or add/rename/reorder/delete boards")
        self.board_menu_btn.clicked.connect(self.toggle_board_panel)
        toolbar.addWidget(self.board_menu_btn)

        add_task_btn = QPushButton("+ New Task")
        add_task_btn.setProperty("accent", True)
        add_task_btn.clicked.connect(self.add_task)
        toolbar.addWidget(add_task_btn)

        toolbar.addStretch()

        self.add_col_btn = QPushButton("+ Column")
        self.add_col_btn.clicked.connect(self.add_column_ui)
        self.add_col_btn.hide()
        toolbar.addWidget(self.add_col_btn)

        self.edit_board_btn = QPushButton("Edit Board")
        self.edit_board_btn.setCheckable(True)
        self.edit_board_btn.setToolTip("Show or hide column move/rename/delete controls")
        self.edit_board_btn.toggled.connect(self._on_edit_board_toggled)
        toolbar.addWidget(self.edit_board_btn)
        outer.addLayout(toolbar)

        self.columns_layout = QHBoxLayout()
        self.columns_layout.setSpacing(12)
        outer.addLayout(self.columns_layout)

        self.board_panel_overlay = QWidget(self)
        self.board_panel_overlay.setStyleSheet("background-color: rgba(0, 0, 0, 90);")
        self.board_panel_overlay.mousePressEvent = lambda event: self.close_board_panel()
        self.board_panel_overlay.hide()

        self.board_panel = BoardSidePanel(self, parent=self)
        self.board_panel.hide()

        self.rebuild_boards_selector()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._board_panel_open:
            self.board_panel_overlay.setGeometry(0, 0, self.width(), self.height())
            self.board_panel.setGeometry(0, 0, BoardSidePanel.PANEL_WIDTH, self.height())

    # -- side panel (hamburger menu replacement) -------------------------

    def toggle_board_panel(self) -> None:
        if self._board_panel_open:
            self.close_board_panel()
        else:
            self.open_board_panel()

    def open_board_panel(self) -> None:
        self.board_panel.refresh(self._boards_cache, self.current_board_id)

        panel_width = BoardSidePanel.PANEL_WIDTH

        self.board_panel_overlay.setGeometry(0, 0, self.width(), self.height())
        self.board_panel_overlay.show()
        self.board_panel_overlay.raise_()

        self.board_panel.setGeometry(-panel_width, 0, panel_width, self.height())
        self.board_panel.show()
        self.board_panel.raise_()

        self._board_panel_anim = QPropertyAnimation(self.board_panel, b"geometry")
        self._board_panel_anim.setDuration(180)
        self._board_panel_anim.setStartValue(QRect(-panel_width, 0, panel_width, self.height()))
        self._board_panel_anim.setEndValue(QRect(0, 0, panel_width, self.height()))
        self._board_panel_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._board_panel_anim.start()

        self._board_panel_open = True

    def close_board_panel(self) -> None:
        if not self._board_panel_open:
            return

        panel_width = BoardSidePanel.PANEL_WIDTH

        self._board_panel_anim = QPropertyAnimation(self.board_panel, b"geometry")
        self._board_panel_anim.setDuration(160)
        self._board_panel_anim.setStartValue(self.board_panel.geometry())
        self._board_panel_anim.setEndValue(QRect(-panel_width, 0, panel_width, self.height()))
        self._board_panel_anim.setEasingCurve(QEasingCurve.InCubic)
        self._board_panel_anim.finished.connect(self.board_panel.hide)
        self._board_panel_anim.start()

        self.board_panel_overlay.hide()
        self._board_panel_open = False

    def select_board_from_panel(self, board_id: str) -> None:
        self._select_board(board_id)
        self.close_board_panel()

    # -- column-status helpers used by ColumnWidget --------------------

    def column_name(self, status):
        for col in self._columns_cache:
            if col["status"] == status:
                return col["name"]
        return status

    # -- board selector / switching --------------------------------------

    def _current_board_name(self) -> str:
        for b in self._boards_cache:
            if b["id"] == self.current_board_id:
                return b["name"]
        return ""

    def _select_board(self, board_id: str) -> None:
        if board_id and board_id != self.current_board_id:
            self.current_board_id = board_id
            self._update_board_button_label()
            self.rebuild_columns()

    def _update_board_button_label(self) -> None:
        current_name = self._current_board_name()
        self.board_menu_btn.setToolTip(
            f"Switch board (current: {current_name})" if current_name else "Switch board, or add/rename/reorder/delete boards"
        )

    def rebuild_boards_selector(self, preferred_board_id=None):
        boards = get_boards(self.conn)
        self._boards_cache = boards

        valid_ids = [b["id"] for b in boards]
        if preferred_board_id in valid_ids:
            target_id = preferred_board_id
        elif self.current_board_id in valid_ids:
            target_id = self.current_board_id
        elif boards:
            target_id = boards[0]["id"]
        else:
            target_id = None

        self.current_board_id = target_id

        self._update_board_button_label()
        self.rebuild_columns()

    # -- structural rebuild (columns added/removed/reordered/renamed) --

    def rebuild_columns(self):
        while self.columns_layout.count():
            item = self.columns_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.columns = {}
        self._columns_cache = get_columns(self.conn, self.current_board_id) if self.current_board_id else []
        for col in self._columns_cache:
            widget = ColumnWidget(col, self)
            widget.set_edit_controls_visible(self.edit_board_btn.isChecked())
            self.columns[col["status"]] = widget
            self.columns_layout.addWidget(widget)

        self.refresh()

    def _on_edit_board_toggled(self, checked: bool) -> None:
        self.edit_board_btn.setText("Done Editing" if checked else "Edit Board")
        self.add_col_btn.setVisible(checked)
        for col_widget in self.columns.values():
            col_widget.set_edit_controls_visible(checked)

    # -- lightweight refresh (task list contents only) ------------------

    def refresh(self) -> None:
        for status, col_widget in self.columns.items():
            col_widget.list_widget.clear()
            tasks = get_tasks_by_status(self.conn, self.current_board_id, status)
            for task in tasks:
                subtasks = get_subtasks(self.conn, task["id"])

                meta_parts = []
                if task.get("due_date"):
                    meta_parts.append(f"Due {task['due_date']}")
                if subtasks:
                    done_count = sum(1 for s in subtasks if s["done"])
                    meta_parts.append(f"{done_count}/{len(subtasks)} done")
                if task.get("joplin_link"):
                    meta_parts.append("Joplin")

                card_text = task["title"]
                if meta_parts:
                    card_text += "\n" + "   ".join(meta_parts)

                item = QListWidgetItem(card_text)
                item.setData(Qt.UserRole, task["id"])

                tooltip_lines = []
                if task.get("due_date"):
                    tooltip_lines.append(f"Due: {task['due_date']}")
                if task.get("notes"):
                    tooltip_lines.append(task["notes"])
                if task.get("joplin_link"):
                    tooltip_lines.append(f"Joplin: {task['joplin_link']}")
                if tooltip_lines:
                    item.setToolTip("\n".join(tooltip_lines))

                col_widget.list_widget.addItem(item)
            col_widget.set_count(len(tasks))

    # -- task operations ------------------------------------------------

    def add_task(self) -> None:
        if not self._columns_cache:
            QMessageBox.information(self, "No columns", "Add a column first.")
            return

        default_status = get_default_new_task_status(self.conn, self.current_board_id)
        dialog = NewTaskDialog(self._columns_cache, default_status, self)
        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.result_values()
        if not values["title"]:
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return

        task = add_task(self.conn, self.current_board_id, values["title"], values["notes"], values["status"])
        if values["due_date"] or values["joplin_link"]:
            update_task(
                self.conn, task["id"], values["title"], values["notes"],
                values["due_date"], values["joplin_link"],
            )
        self.refresh()

    def edit_task(self, task_id: str) -> None:
        task = get_task(self.conn, task_id)
        if not task:
            return

        dialog = TaskCardDialog(self.conn, task, self._columns_cache, self)
        result = dialog.exec()

        if dialog.delete_requested:
            self.delete_task_ui(task_id)
            return
        if result != QDialog.Accepted:
            return

        values = dialog.result_values()
        if not values["title"]:
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return

        update_task(
            self.conn, task_id, values["title"], values["notes"],
            values["due_date"], values["joplin_link"],
        )
        if values["status"] and values["status"] != task["status"]:
            move_task(self.conn, task_id, values["status"])
        self.refresh()

    def handle_move(self, task_id: str, new_status: str) -> None:
        task = get_task(self.conn, task_id)
        if not task or task["status"] == new_status:
            return
        move_task(self.conn, task_id, new_status)
        self.refresh()

    def delete_task_ui(self, task_id: str) -> None:
        task = get_task(self.conn, task_id)
        if not task:
            return
        reply = QMessageBox.question(
            self, "Delete task", f'Delete "{task["title"]}"?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            delete_task(self.conn, task_id)
            self.refresh()

    # -- column operations ------------------------------------------------

    def add_column_ui(self) -> None:
        name, ok = QInputDialog.getText(self, "New Column", "Column name:")
        if not ok or not name.strip():
            return
        try:
            add_column(self.conn, self.current_board_id, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not add column", str(e))
            return
        self.rebuild_columns()

    def rename_column_ui(self, status: str) -> None:
        current_name = self.column_name(status)
        name, ok = QInputDialog.getText(self, "Rename Column", "Column name:", text=current_name)
        if not ok or not name.strip():
            return
        try:
            rename_column(self.conn, self.current_board_id, status, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename column", str(e))
            return
        self.rebuild_columns()

    def delete_column_ui(self, status: str) -> None:
        name = self.column_name(status)
        reply = QMessageBox.question(
            self, "Delete column", f'Delete the "{name}" column?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            delete_column(self.conn, self.current_board_id, status)
        except ValueError as e:
            QMessageBox.warning(self, "Could not delete column", str(e))
            return
        self.rebuild_columns()

    def move_column_ui(self, status: str, direction: int) -> None:
        move_column(self.conn, self.current_board_id, status, direction)
        self.rebuild_columns()

    # -- board operations ------------------------------------------------

    def _sync_board_panel(self) -> None:
        self.board_panel.refresh(self._boards_cache, self.current_board_id)

    def add_board_ui(self) -> None:
        name, ok = QInputDialog.getText(self, "New Board", "Board name:")
        if not ok or not name.strip():
            return
        try:
            new_board = add_board(self.conn, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not add board", str(e))
            return
        self.rebuild_boards_selector(preferred_board_id=new_board["id"])
        self._sync_board_panel()

    def rename_board_ui(self, board_id: str) -> None:
        board = get_board(self.conn, board_id)
        if not board:
            return
        name, ok = QInputDialog.getText(self, "Rename Board", "Board name:", text=board["name"])
        if not ok or not name.strip():
            return
        try:
            rename_board(self.conn, board_id, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename board", str(e))
            return
        self.rebuild_boards_selector(preferred_board_id=self.current_board_id)
        self._sync_board_panel()

    def delete_board_ui(self, board_id: str) -> None:
        board = get_board(self.conn, board_id)
        if not board:
            return
        task_count = get_board_task_count(self.conn, board_id)
        message = f'Delete the board "{board["name"]}"?'
        if task_count:
            message += f" This will permanently delete {task_count} task(s) on it."
        reply = QMessageBox.question(self, "Delete board", message, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            delete_board(self.conn, board_id)
        except ValueError as e:
            QMessageBox.warning(self, "Could not delete board", str(e))
            return
        self.rebuild_boards_selector(preferred_board_id=self.current_board_id)
        self._sync_board_panel()

    def move_board_ui(self, board_id: str, direction: int) -> None:
        move_board(self.conn, board_id, direction)
        self.rebuild_boards_selector(preferred_board_id=self.current_board_id)
        self._sync_board_panel()

    # -- global quick-add shortcut (Ctrl+Space) ---------------------------

    def show_quick_add_dialog(self) -> None:
        """Slot for GlobalShortcutBridge.triggered. May be invoked while
        the window is minimized or unfocused, so it brings the window to
        the front itself rather than assuming it's already visible."""
        if self._quick_add_dialog is not None:
            self._quick_add_dialog.raise_()
            self._quick_add_dialog.activateWindow()
            return

        boards = get_boards(self.conn)
        if not boards:
            return

        top = self.window()
        top.show()
        top.raise_()
        top.activateWindow()

        dialog = QuickAddDialog(self.conn, boards, self.current_board_id, self)
        self._quick_add_dialog = dialog
        dialog.exec()
        self._quick_add_dialog = None

        if self.current_board_id in dialog.added_board_ids:
            self.refresh()


# ---------------------------------------------------------------------------
# Global "quick add" hotkey (Ctrl+Space) - works even while Kanvas isn't the
# foreground window. There's no cross-platform way to grab a global hotkey,
# so this is two separate best-effort backends; if neither can set up
# (unsupported desktop, missing libraries, key already taken) Kanvas just
# runs without the shortcut instead of failing to start.
# ---------------------------------------------------------------------------

QUICK_ADD_SHORTCUT_ID = "quick-add-task"
QUICK_ADD_SHORTCUT_DESCRIPTION = "Quick-add a Kanvas task"


class GlobalShortcutBridge(QObject):
    """The platform backends run on a background thread and must never
    touch widgets directly, so they only ever call emit() here. Since this
    QObject is created on and lives on the GUI thread, PySide queues the
    delivery of `triggered` onto the GUI thread's event loop automatically,
    even though emit() itself is called from the background thread."""
    triggered = Signal()


def _start_linux_global_shortcut(bridge: GlobalShortcutBridge) -> threading.Thread:
    """Registers Ctrl+Space via the XDG Desktop Portal GlobalShortcuts
    interface - the only sanctioned way to get a global hotkey under
    Wayland (raw-input libraries like pynput/keyboard don't work there).
    The first time this runs for a given app, the compositor may open its
    own shortcut-settings UI for the user to confirm/assign the key; once
    assigned it's remembered for future launches (BindShortcuts then comes
    back with a non-empty trigger_description and ConfigureShortcuts is
    skipped). Runs entirely on a background thread with its own GLib main
    loop, since dbus-python's signal delivery needs a running main loop."""
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    def worker():
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        loop = GLib.MainLoop()
        sender_token = bus.get_unique_name()[1:].replace(".", "_")

        portal = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
        shortcuts_iface = dbus.Interface(portal, "org.freedesktop.portal.GlobalShortcuts")

        def request_path(token):
            return f"/org/freedesktop/portal/desktop/request/{sender_token}/{token}"

        def on_activated(session_handle, shortcut_id, timestamp, options):
            if str(shortcut_id) == QUICK_ADD_SHORTCUT_ID:
                bridge.triggered.emit()

        shortcuts_iface.connect_to_signal("Activated", on_activated)

        def on_configure_response(response, results):
            pass

        def on_bind_response(response, results):
            if response != 0:
                return
            for shortcut_id, info in results.get("shortcuts") or []:
                already_assigned = bool(str(info.get("trigger_description", "")))
                if str(shortcut_id) == QUICK_ADD_SHORTCUT_ID and not already_assigned:
                    configure_token = f"configure_{secrets.token_hex(4)}"
                    bus.add_signal_receiver(
                        on_configure_response,
                        signal_name="Response",
                        dbus_interface="org.freedesktop.portal.Request",
                        path=request_path(configure_token),
                    )
                    shortcuts_iface.ConfigureShortcuts(
                        session_handle, "",
                        dbus.Dictionary({dbus.String("handle_token"): dbus.String(configure_token)}, signature="sv"),
                    )

        session_handle = None

        def on_session_response(response, results):
            nonlocal session_handle
            if response != 0:
                return
            session_handle = str(results["session_handle"])

            shortcuts = dbus.Array([
                dbus.Struct((
                    dbus.String(QUICK_ADD_SHORTCUT_ID),
                    dbus.Dictionary(
                        {dbus.String("description"): dbus.String(QUICK_ADD_SHORTCUT_DESCRIPTION)}, signature="sv"
                    ),
                ), signature="sa{sv}")
            ], signature="(sa{sv})")

            bind_token = f"bind_{secrets.token_hex(4)}"
            bus.add_signal_receiver(
                on_bind_response,
                signal_name="Response",
                dbus_interface="org.freedesktop.portal.Request",
                path=request_path(bind_token),
            )
            shortcuts_iface.BindShortcuts(
                session_handle, shortcuts, "",
                dbus.Dictionary({dbus.String("handle_token"): dbus.String(bind_token)}, signature="sv"),
            )

        create_token = f"create_{secrets.token_hex(4)}"
        bus.add_signal_receiver(
            on_session_response,
            signal_name="Response",
            dbus_interface="org.freedesktop.portal.Request",
            path=request_path(create_token),
        )
        shortcuts_iface.CreateSession(dbus.Dictionary({
            dbus.String("handle_token"): dbus.String(create_token),
            dbus.String("session_handle_token"): dbus.String(f"session_{secrets.token_hex(4)}"),
        }, signature="sv"))

        loop.run()

    thread = threading.Thread(target=worker, daemon=True, name="kanvas-global-shortcut")
    thread.start()
    return thread


def _start_windows_global_shortcut(bridge: GlobalShortcutBridge) -> threading.Thread:
    """Registers Ctrl+Space as a systemwide hotkey via the Win32
    RegisterHotKey API. Windows has no Wayland-style sandboxing around
    this, so it's just a thread-message loop and no user confirmation
    step is needed. RegisterHotKey/GetMessage must run on the same
    thread, so the whole thing lives in one background-thread worker."""
    import ctypes
    from ctypes import wintypes

    MOD_CONTROL = 0x0002
    VK_SPACE = 0x20
    WM_HOTKEY = 0x0312
    HOTKEY_ID = 1

    def worker():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL, VK_SPACE):
            return
        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    bridge.triggered.emit()
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    thread = threading.Thread(target=worker, daemon=True, name="kanvas-global-shortcut")
    thread.start()
    return thread


def start_global_shortcut(bridge: GlobalShortcutBridge):
    """Best-effort setup; failures are logged and swallowed rather than
    crashing the app; Kanvas is fully usable without the shortcut."""
    try:
        if platform.system() == "Windows":
            return _start_windows_global_shortcut(bridge)
        else:
            return _start_linux_global_shortcut(bridge)
    except Exception as e:
        print(f"Quick-add global shortcut unavailable: {e}", file=sys.stderr)
        return None


def _load_app_icon() -> QIcon:
    """Built from the PNG sizes in assets/ rather than the SVG alone: some
    PySide6/Windows installs don't ship the SVG icon-engine plugin, which
    leaves QIcon(svg_path) silently null and the app un-iconed everywhere
    it's shown (title bar, taskbar, alt-tab). PNGs always work since they
    go through the built-in image format plugins."""
    icon = QIcon()
    for size in (16, 32, 64, 128, 256, 512):
        png_path = os.path.join(ASSETS_DIR, f"kanvas_logo_{size}.png")
        if os.path.exists(png_path):
            icon.addFile(png_path)
    if icon.isNull():
        svg_path = os.path.join(ASSETS_DIR, "kanvas_logo.svg")
        if os.path.exists(svg_path):
            icon.addFile(svg_path)
    return icon


def _set_windows_app_user_model_id() -> None:
    """Without this, Windows' taskbar identifies a `python kanvas.py`
    process by python.exe's own AppUserModelID, so it shows (and groups
    windows under) Python's icon instead of the one set via
    setWindowIcon()."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_USER_MODEL_ID)
    except Exception:
        pass


def main():
    if platform.system() == "Windows":
        _set_windows_app_user_model_id()

    db_path = get_db_path()
    conn = get_connection(db_path)
    init_db(conn)

    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)
    app_icon = _load_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    window = QMainWindow()
    window.setWindowTitle(APP_TITLE)
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    board = KanbanBoard(conn)
    window.setCentralWidget(board)
    window.resize(1150, 640)
    window.show()

    board.shortcut_bridge = GlobalShortcutBridge()
    board.shortcut_bridge.triggered.connect(board.show_quick_add_dialog)
    board.shortcut_thread = start_global_shortcut(board.shortcut_bridge)

    exit_code = app.exec()
    conn.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

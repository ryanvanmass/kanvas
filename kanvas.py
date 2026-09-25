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
from datetime import datetime, date, timedelta, time as dt_time

from PySide6.QtCore import Qt, QRect, QPoint, QDate, QTime, QDateTime, QTimer, QObject, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QIcon, QAction, QFont, QColor, QCursor, QPainter, QPen, QBrush, QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QListView, QComboBox,
    QAbstractItemView, QInputDialog, QMessageBox, QDialog, QLineEdit, QTextEdit,
    QCheckBox, QDateEdit, QSpinBox, QMenu, QToolButton, QStackedWidget,
    QDateTimeEdit, QTimeEdit, QRadioButton, QButtonGroup, QSystemTrayIcon,
    QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea, QGridLayout, QSizePolicy,
    QStyledItemDelegate, QTabWidget,
)

APP_TITLE = "Kanvas"
# PyInstaller extracts bundled data files (see --add-data in the README's
# build steps) next to sys._MEIPASS, not next to this script's own path -
# __file__ inside a frozen exe points into the bootloader's internals, so
# resolving assets/ from it would silently find nothing.
_BASE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(_BASE_DIR, "assets")
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
# Projects - a second, parallel feature (see init_db's project_* tables
# below). Fully isolated from the Boards feature above: no shared tables,
# no shared UI state, no shared code paths beyond generic app scaffolding.
# ---------------------------------------------------------------------------

# Seed columns for a newly created project board (Main Board or sub-board).
# Unlike DEFAULT_COLUMNS, project_columns.id is a real primary key rather
# than a slug derived from the name, so only names are needed here.
PROJECT_DEFAULT_COLUMNS = ["Today", "In Progress", "Blocked", "Complete"]

# Soft warning threshold for sub-board nesting depth (Main Board = depth 1).
# Nothing blocks creation past this depth - it's just a nudge.
PROJECT_BOARD_DEPTH_WARNING_THRESHOLD = 10

# Gantt bars and Calendar chips are colored by column (swimlane) so the
# "grouped by column" structure reads visually in both views. Cycles once
# there are more columns than colors - fine for a handful of columns.
CALENDAR_CHIP_COLORS = ["#4F46E5", "#0891b2", "#059669", "#d97706", "#db2777", "#7c3aed"]


def project_column_color(column_position: int) -> str:
    return CALENDAR_CHIP_COLORS[column_position % len(CALENDAR_CHIP_COLORS)]


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


def _migrate_task_completion_fields(conn: sqlite3.Connection) -> None:
    """Adds the completed/completed_at columns to "tasks" for databases
    created before task completion was tracked independently of column."""
    if not _table_has_column(conn, "tasks", "completed"):
        conn.execute("ALTER TABLE tasks ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
    if not _table_has_column(conn, "tasks", "completed_at"):
        conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT NOT NULL DEFAULT ''")
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
            joplin_link TEXT NOT NULL DEFAULT '',
            completed INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT NOT NULL DEFAULT ''
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_templates (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            due_offset_days INTEGER,
            joplin_link TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS template_subtasks (
            id TEXT PRIMARY KEY,
            template_id TEXT NOT NULL,
            title TEXT NOT NULL,
            position INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS automation_rules (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            rule_type TEXT NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,

            schedule_kind TEXT NOT NULL,
            schedule_time TEXT NOT NULL DEFAULT '09:00',
            schedule_weekdays TEXT NOT NULL DEFAULT '',
            schedule_interval_days INTEGER,
            schedule_datetime TEXT,
            next_run TEXT,
            last_run TEXT,

            template_id TEXT,
            task_title TEXT NOT NULL DEFAULT '',
            task_notes TEXT NOT NULL DEFAULT '',
            task_status TEXT,
            task_joplin_link TEXT NOT NULL DEFAULT '',
            task_due_offset_days INTEGER,

            from_status TEXT,
            to_status TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS automation_rule_subtasks (
            id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL,
            title TEXT NOT NULL,
            position INTEGER NOT NULL
        )
    """)

    # -- Projects (see the "Projects" comment block above DEFAULT_FIRST_BOARD_NAME) --
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            main_board_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_boards (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            parent_task_id TEXT UNIQUE,
            name TEXT NOT NULL,
            last_view TEXT NOT NULL DEFAULT 'kanban',
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_columns (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            name TEXT NOT NULL,
            position INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_tasks (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            column_id TEXT NOT NULL,
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            start_date TEXT,
            due_date TEXT,
            completed INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL,
            link TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_subtasks (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL
        )
    """)
    # project_documents is created now (so a later phase lands without a
    # migration) but gets no CRUD or UI yet - nothing writes to it in this
    # phase. project_activity_log is fully wired up (Phase 3).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_documents (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            path_or_url TEXT NOT NULL,
            label TEXT NOT NULL,
            notes TEXT,
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_activity_log (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            board_id TEXT,
            task_id TEXT,
            task_title_snapshot TEXT NOT NULL DEFAULT '',
            board_path_snapshot TEXT NOT NULL DEFAULT '',
            action_type TEXT NOT NULL,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()

    _migrate_legacy_single_board_schema(conn)
    _migrate_task_card_fields(conn)
    _migrate_task_completion_fields(conn)

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
    conn.execute(
        "DELETE FROM template_subtasks WHERE template_id IN "
        "(SELECT id FROM task_templates WHERE board_id = ?)",
        (board_id,),
    )
    conn.execute("DELETE FROM task_templates WHERE board_id = ?", (board_id,))
    conn.execute(
        "DELETE FROM automation_rule_subtasks WHERE rule_id IN "
        "(SELECT id FROM automation_rules WHERE board_id = ?)",
        (board_id,),
    )
    conn.execute("DELETE FROM automation_rules WHERE board_id = ?", (board_id,))
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


def set_task_completed(conn: sqlite3.Connection, task_id: str, completed: bool) -> None:
    """Completion is tracked independently of column - a completed task
    stays wherever it is, it's just hidden from the board by default (see
    KanbanBoard's "Show Completed" toggle)."""
    now = _now()
    conn.execute(
        "UPDATE tasks SET completed = ?, completed_at = ?, updated = ? WHERE id = ?",
        (1 if completed else 0, now if completed else "", now, task_id),
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


# -- Task templates (per-board presets that prefill the New Task dialog) ----

def get_templates(conn: sqlite3.Connection, board_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM task_templates WHERE board_id = ? ORDER BY position ASC", (board_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_template(conn: sqlite3.Connection, template_id: str):
    row = conn.execute("SELECT * FROM task_templates WHERE id = ?", (template_id,)).fetchone()
    return dict(row) if row is not None else None


def get_template_subtasks(conn: sqlite3.Connection, template_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM template_subtasks WHERE template_id = ? ORDER BY position ASC", (template_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _replace_template_subtasks(conn: sqlite3.Connection, template_id: str, subtask_titles: list) -> None:
    conn.execute("DELETE FROM template_subtasks WHERE template_id = ?", (template_id,))
    for position, title in enumerate(subtask_titles):
        conn.execute(
            "INSERT INTO template_subtasks (id, template_id, title, position) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, template_id, title, position),
        )


def add_template(
    conn: sqlite3.Connection,
    board_id: str,
    title: str,
    notes: str,
    status: str,
    due_offset_days,
    joplin_link: str,
    subtask_titles: list,
) -> dict:
    title = title.strip()
    if not title:
        raise ValueError("Template title cannot be empty.")

    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM task_templates WHERE board_id = ?", (board_id,)
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    template_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO task_templates "
        "(id, board_id, position, title, notes, status, due_offset_days, joplin_link) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (template_id, board_id, next_position, title, notes, status, due_offset_days, joplin_link),
    )
    _replace_template_subtasks(conn, template_id, subtask_titles)
    conn.commit()
    return get_template(conn, template_id)


def update_template(
    conn: sqlite3.Connection,
    template_id: str,
    title: str,
    notes: str,
    status: str,
    due_offset_days,
    joplin_link: str,
    subtask_titles: list,
) -> None:
    title = title.strip()
    if not title:
        raise ValueError("Template title cannot be empty.")

    conn.execute(
        "UPDATE task_templates SET title = ?, notes = ?, status = ?, due_offset_days = ?, joplin_link = ? "
        "WHERE id = ?",
        (title, notes, status, due_offset_days, joplin_link, template_id),
    )
    _replace_template_subtasks(conn, template_id, subtask_titles)
    conn.commit()


def _compact_template_positions(conn: sqlite3.Connection, board_id: str) -> None:
    for position, tpl in enumerate(get_templates(conn, board_id)):
        if tpl["position"] != position:
            conn.execute("UPDATE task_templates SET position = ? WHERE id = ?", (position, tpl["id"]))
    conn.commit()


def delete_template(conn: sqlite3.Connection, template_id: str) -> None:
    template = get_template(conn, template_id)
    if template is None:
        return
    conn.execute("DELETE FROM template_subtasks WHERE template_id = ?", (template_id,))
    conn.execute("DELETE FROM task_templates WHERE id = ?", (template_id,))
    conn.commit()
    _compact_template_positions(conn, template["board_id"])


def move_template(conn: sqlite3.Connection, board_id: str, template_id: str, direction: int) -> None:
    templates = get_templates(conn, board_id)
    ids = [t["id"] for t in templates]
    if template_id not in ids:
        return

    idx = ids.index(template_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(templates):
        return

    templates[idx], templates[new_idx] = templates[new_idx], templates[idx]
    for position, tpl in enumerate(templates):
        conn.execute("UPDATE task_templates SET position = ? WHERE id = ?", (position, tpl["id"]))
    conn.commit()


# -- Task automation (per-board scheduled rules) -----------------------------
#
# Kanvas has no background service, so a rule's `next_run` is only ever
# checked while the app is open (a timer in KanbanBoard, plus once at
# startup) - see run_due_automation_rules(). This means a rule "catches
# up" at most once per missed occurrence, not once per occurrence that
# would have fired while the app was closed.

def _compute_next_run(
    schedule_kind: str,
    schedule_time: str,
    schedule_weekdays: str,
    schedule_interval_days,
    schedule_datetime,
    after: datetime,
):
    """Next ISO datetime string this schedule should fire strictly after
    `after`, or None if there's nothing left to schedule (e.g. 'once')."""
    if schedule_kind == "once":
        return schedule_datetime

    if schedule_kind == "hourly":
        # Reuses schedule_interval_days as an hour count (see
        # ScheduleEditorWidget.get_values()) rather than a schema column
        # of its own. Always lands on the hour mark rather than N hours
        # from whatever minute `after` happens to be.
        interval_hours = schedule_interval_days or 1
        candidate = after.replace(minute=0, second=0, microsecond=0) + timedelta(hours=interval_hours)
        return candidate.isoformat(timespec="seconds")

    hh, mm = (int(part) for part in schedule_time.split(":"))

    if schedule_kind == "daily":
        candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate.isoformat(timespec="seconds")

    if schedule_kind == "weekly":
        weekdays = sorted(int(d) for d in schedule_weekdays.split(",") if d != "")
        if not weekdays:
            return None
        for offset in range(8):
            candidate_date = (after + timedelta(days=offset)).date()
            if candidate_date.weekday() in weekdays:
                candidate = datetime.combine(candidate_date, dt_time(hh, mm))
                if candidate > after:
                    return candidate.isoformat(timespec="seconds")
        return None

    if schedule_kind == "interval":
        interval_days = schedule_interval_days or 1
        candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=interval_days)
        return candidate.isoformat(timespec="seconds")

    return None


def get_automation_rules(conn: sqlite3.Connection, board_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM automation_rules WHERE board_id = ? ORDER BY name ASC", (board_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_automation_rule(conn: sqlite3.Connection, rule_id: str):
    row = conn.execute("SELECT * FROM automation_rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row is not None else None


def get_automation_rule_subtasks(conn: sqlite3.Connection, rule_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM automation_rule_subtasks WHERE rule_id = ? ORDER BY position ASC", (rule_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _replace_automation_rule_subtasks(conn: sqlite3.Connection, rule_id: str, subtask_titles: list) -> None:
    conn.execute("DELETE FROM automation_rule_subtasks WHERE rule_id = ?", (rule_id,))
    for position, title in enumerate(subtask_titles):
        conn.execute(
            "INSERT INTO automation_rule_subtasks (id, rule_id, title, position) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, rule_id, title, position),
        )


def _insert_automation_rule(
    conn: sqlite3.Connection, rule_id: str, board_id: str, rule_type: str, name: str, enabled: bool,
    schedule_kind: str, schedule_time: str, schedule_weekdays: str, schedule_interval_days, schedule_datetime,
    next_run, template_id=None, task_title="", task_notes="", task_status=None, task_joplin_link="",
    task_due_offset_days=None, from_status=None, to_status=None,
) -> None:
    conn.execute(
        "INSERT INTO automation_rules "
        "(id, board_id, rule_type, name, enabled, schedule_kind, schedule_time, schedule_weekdays, "
        "schedule_interval_days, schedule_datetime, next_run, last_run, "
        "template_id, task_title, task_notes, task_status, task_joplin_link, task_due_offset_days, "
        "from_status, to_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rule_id, board_id, rule_type, name, 1 if enabled else 0, schedule_kind, schedule_time,
         schedule_weekdays, schedule_interval_days, schedule_datetime, next_run,
         template_id, task_title, task_notes, task_status, task_joplin_link, task_due_offset_days,
         from_status, to_status),
    )


def add_create_task_rule(
    conn: sqlite3.Connection, board_id: str, name: str, enabled: bool,
    schedule_kind: str, schedule_time: str, schedule_weekdays: str, schedule_interval_days, schedule_datetime,
    template_id, task_title: str, task_notes: str, task_status, task_joplin_link: str, task_due_offset_days,
    subtask_titles: list,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Rule name cannot be empty.")

    rule_id = uuid.uuid4().hex
    next_run = _compute_next_run(
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime,
        after=datetime.now(),
    )
    _insert_automation_rule(
        conn, rule_id, board_id, "create_task", name, enabled,
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime, next_run,
        template_id=template_id, task_title=task_title, task_notes=task_notes, task_status=task_status,
        task_joplin_link=task_joplin_link, task_due_offset_days=task_due_offset_days,
    )
    _replace_automation_rule_subtasks(conn, rule_id, subtask_titles)
    conn.commit()
    return get_automation_rule(conn, rule_id)


def add_move_task_rule(
    conn: sqlite3.Connection, board_id: str, name: str, enabled: bool,
    schedule_kind: str, schedule_time: str, schedule_weekdays: str, schedule_interval_days, schedule_datetime,
    from_status: str, to_status: str,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Rule name cannot be empty.")
    if from_status == to_status:
        raise ValueError("The \"from\" and \"to\" columns must be different.")

    rule_id = uuid.uuid4().hex
    next_run = _compute_next_run(
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime,
        after=datetime.now(),
    )
    _insert_automation_rule(
        conn, rule_id, board_id, "move_task", name, enabled,
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime, next_run,
        from_status=from_status, to_status=to_status,
    )
    conn.commit()
    return get_automation_rule(conn, rule_id)


def add_complete_task_rule(
    conn: sqlite3.Connection, board_id: str, name: str, enabled: bool,
    schedule_kind: str, schedule_time: str, schedule_weekdays: str, schedule_interval_days, schedule_datetime,
    from_status: str,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Rule name cannot be empty.")

    rule_id = uuid.uuid4().hex
    next_run = _compute_next_run(
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime,
        after=datetime.now(),
    )
    _insert_automation_rule(
        conn, rule_id, board_id, "complete_task", name, enabled,
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime, next_run,
        from_status=from_status,
    )
    conn.commit()
    return get_automation_rule(conn, rule_id)


def update_complete_task_rule(
    conn: sqlite3.Connection, rule_id: str, name: str, enabled: bool,
    schedule_kind: str, schedule_time: str, schedule_weekdays: str, schedule_interval_days, schedule_datetime,
    from_status: str,
) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Rule name cannot be empty.")

    next_run = _compute_next_run(
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime,
        after=datetime.now(),
    )
    conn.execute(
        "UPDATE automation_rules SET name = ?, enabled = ?, schedule_kind = ?, schedule_time = ?, "
        "schedule_weekdays = ?, schedule_interval_days = ?, schedule_datetime = ?, next_run = ?, "
        "from_status = ? WHERE id = ?",
        (name, 1 if enabled else 0, schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days,
         schedule_datetime, next_run, from_status, rule_id),
    )
    conn.commit()


def update_create_task_rule(
    conn: sqlite3.Connection, rule_id: str, name: str, enabled: bool,
    schedule_kind: str, schedule_time: str, schedule_weekdays: str, schedule_interval_days, schedule_datetime,
    template_id, task_title: str, task_notes: str, task_status, task_joplin_link: str, task_due_offset_days,
    subtask_titles: list,
) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Rule name cannot be empty.")

    next_run = _compute_next_run(
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime,
        after=datetime.now(),
    )
    conn.execute(
        "UPDATE automation_rules SET name = ?, enabled = ?, schedule_kind = ?, schedule_time = ?, "
        "schedule_weekdays = ?, schedule_interval_days = ?, schedule_datetime = ?, next_run = ?, "
        "template_id = ?, task_title = ?, task_notes = ?, task_status = ?, task_joplin_link = ?, "
        "task_due_offset_days = ? WHERE id = ?",
        (name, 1 if enabled else 0, schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days,
         schedule_datetime, next_run, template_id, task_title, task_notes, task_status, task_joplin_link,
         task_due_offset_days, rule_id),
    )
    _replace_automation_rule_subtasks(conn, rule_id, subtask_titles)
    conn.commit()


def update_move_task_rule(
    conn: sqlite3.Connection, rule_id: str, name: str, enabled: bool,
    schedule_kind: str, schedule_time: str, schedule_weekdays: str, schedule_interval_days, schedule_datetime,
    from_status: str, to_status: str,
) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Rule name cannot be empty.")
    if from_status == to_status:
        raise ValueError("The \"from\" and \"to\" columns must be different.")

    next_run = _compute_next_run(
        schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days, schedule_datetime,
        after=datetime.now(),
    )
    conn.execute(
        "UPDATE automation_rules SET name = ?, enabled = ?, schedule_kind = ?, schedule_time = ?, "
        "schedule_weekdays = ?, schedule_interval_days = ?, schedule_datetime = ?, next_run = ?, "
        "from_status = ?, to_status = ? WHERE id = ?",
        (name, 1 if enabled else 0, schedule_kind, schedule_time, schedule_weekdays, schedule_interval_days,
         schedule_datetime, next_run, from_status, to_status, rule_id),
    )
    conn.commit()


def set_automation_rule_enabled(conn: sqlite3.Connection, rule_id: str, enabled: bool) -> None:
    conn.execute("UPDATE automation_rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, rule_id))
    conn.commit()


def delete_automation_rule(conn: sqlite3.Connection, rule_id: str) -> None:
    conn.execute("DELETE FROM automation_rule_subtasks WHERE rule_id = ?", (rule_id,))
    conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
    conn.commit()


def get_due_automation_rules(conn: sqlite3.Connection, now: datetime = None) -> list:
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT * FROM automation_rules WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? "
        "ORDER BY next_run ASC",
        (now.isoformat(timespec="seconds"),),
    ).fetchall()
    return [dict(r) for r in rows]


def _run_create_task_rule(conn: sqlite3.Connection, rule: dict) -> None:
    if rule["template_id"]:
        template = get_template(conn, rule["template_id"])
        if template is None:
            return  # template was deleted since the rule was created; skip this run
        title = template["title"]
        notes = template["notes"]
        status = template["status"]
        joplin_link = template["joplin_link"]
        due_offset_days = template["due_offset_days"]
        subtask_titles = [s["title"] for s in get_template_subtasks(conn, template["id"])]
    else:
        title = rule["task_title"]
        notes = rule["task_notes"]
        status = rule["task_status"]
        joplin_link = rule["task_joplin_link"]
        due_offset_days = rule["task_due_offset_days"]
        subtask_titles = [s["title"] for s in get_automation_rule_subtasks(conn, rule["id"])]

    if not title:
        return

    board_columns = get_columns(conn, rule["board_id"])
    valid_statuses = {c["status"] for c in board_columns}
    if status not in valid_statuses:
        if not board_columns:
            return
        status = board_columns[0]["status"]

    due_date = ""
    if due_offset_days is not None:
        due_date = (date.today() + timedelta(days=due_offset_days)).strftime("%Y-%m-%d")

    task = add_task(conn, rule["board_id"], title, notes, status)
    if due_date or joplin_link:
        update_task(conn, task["id"], title, notes, due_date, joplin_link)
    for subtask_title in subtask_titles:
        add_subtask(conn, task["id"], subtask_title)


def _run_move_task_rule(conn: sqlite3.Connection, rule: dict) -> None:
    from_status = rule["from_status"]
    to_status = rule["to_status"]
    if not from_status or not to_status:
        return
    board_statuses = {c["status"] for c in get_columns(conn, rule["board_id"])}
    if from_status not in board_statuses or to_status not in board_statuses:
        return  # a referenced column was deleted since the rule was created; skip this run
    for task in get_tasks_by_status(conn, rule["board_id"], from_status):
        move_task(conn, task["id"], to_status)


def _run_complete_task_rule(conn: sqlite3.Connection, rule: dict) -> None:
    from_status = rule["from_status"]
    if not from_status:
        return
    board_statuses = {c["status"] for c in get_columns(conn, rule["board_id"])}
    if from_status not in board_statuses:
        return  # the referenced column was deleted since the rule was created; skip this run
    for task in get_tasks_by_status(conn, rule["board_id"], from_status):
        if not task["completed"]:
            set_task_completed(conn, task["id"], True)


def run_automation_rule(conn: sqlite3.Connection, rule: dict, now: datetime = None) -> None:
    now = now or datetime.now()
    if rule["rule_type"] == "create_task":
        _run_create_task_rule(conn, rule)
    elif rule["rule_type"] == "move_task":
        _run_move_task_rule(conn, rule)
    elif rule["rule_type"] == "complete_task":
        _run_complete_task_rule(conn, rule)

    if rule["schedule_kind"] == "once":
        conn.execute(
            "UPDATE automation_rules SET last_run = ?, next_run = NULL, enabled = 0 WHERE id = ?",
            (now.isoformat(timespec="seconds"), rule["id"]),
        )
    else:
        next_run = _compute_next_run(
            rule["schedule_kind"], rule["schedule_time"], rule["schedule_weekdays"],
            rule["schedule_interval_days"], rule["schedule_datetime"], after=now,
        )
        conn.execute(
            "UPDATE automation_rules SET last_run = ?, next_run = ? WHERE id = ?",
            (now.isoformat(timespec="seconds"), next_run, rule["id"]),
        )
    conn.commit()


def run_due_automation_rules(conn: sqlite3.Connection, now: datetime = None) -> set:
    now = now or datetime.now()
    affected_board_ids = set()
    for rule in get_due_automation_rules(conn, now):
        run_automation_rule(conn, rule, now)
        affected_board_ids.add(rule["board_id"])
    return affected_board_ids


def get_board_report(conn: sqlite3.Connection, board_id: str) -> dict:
    columns = get_columns(conn, board_id)
    column_counts = []
    total_tasks = 0
    overdue_count = 0
    today_str = date.today().strftime("%Y-%m-%d")
    recent_count = 0
    recent_cutoff = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    subtasks_total = 0
    subtasks_done = 0

    for col in columns:
        tasks = get_tasks_by_status(conn, board_id, col["status"])
        column_counts.append({"name": col["name"], "status": col["status"], "count": len(tasks)})
        total_tasks += len(tasks)
        for task in tasks:
            if task["due_date"] and task["due_date"] < today_str:
                overdue_count += 1
            if task["created"] >= recent_cutoff:
                recent_count += 1
            for sub in get_subtasks(conn, task["id"]):
                subtasks_total += 1
                if sub["done"]:
                    subtasks_done += 1

    return {
        "total_tasks": total_tasks,
        "column_counts": column_counts,
        "overdue_count": overdue_count,
        "recent_count": recent_count,
        "subtasks_total": subtasks_total,
        "subtasks_done": subtasks_done,
    }


# ---------------------------------------------------------------------------
# Projects (fully separate from Boards above - see the module-level comment
# near PROJECT_DEFAULT_COLUMNS). A Project has exactly one Main Board;
# any task on any Projects board can spawn one Sub-board of its own, to
# unlimited depth. Tasks never move between boards once created, and a
# sub-board's parent is always a task on some existing board, so the board
# tree can only grow downward - no cycle-guard is needed anywhere below.
# ---------------------------------------------------------------------------

# -- Projects -----------------------------------------------------------

def get_projects(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT * FROM projects ORDER BY position ASC").fetchall()
    return [dict(r) for r in rows]


def get_project(conn: sqlite3.Connection, project_id: str):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row is not None else None


def add_project(conn: sqlite3.Connection, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Project name cannot be empty.")

    max_position_row = conn.execute("SELECT MAX(position) AS m FROM projects").fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    project_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO projects (id, name, position, created_at, main_board_id) VALUES (?, ?, ?, ?, NULL)",
        (project_id, name, next_position, _now()),
    )
    conn.commit()

    main_board = add_project_board(conn, project_id, None, name)
    conn.execute("UPDATE projects SET main_board_id = ? WHERE id = ?", (main_board["id"], project_id))
    conn.commit()

    return get_project(conn, project_id)


def rename_project(conn: sqlite3.Connection, project_id: str, new_name: str) -> None:
    """Renames only the project itself - its Main Board keeps whatever name
    it already has. project_boards.name only defaults to the project name
    at creation time (see add_project_board), it isn't kept in sync."""
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Project name cannot be empty.")
    conn.execute("UPDATE projects SET name = ? WHERE id = ?", (new_name, project_id))
    conn.commit()


def _compact_project_positions(conn: sqlite3.Connection) -> None:
    for position, p in enumerate(get_projects(conn)):
        if p["position"] != position:
            conn.execute("UPDATE projects SET position = ? WHERE id = ?", (position, p["id"]))
    conn.commit()


def move_project(conn: sqlite3.Connection, project_id: str, direction: int) -> None:
    projects = get_projects(conn)
    ids = [p["id"] for p in projects]
    if project_id not in ids:
        return

    idx = ids.index(project_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(projects):
        return

    projects[idx], projects[new_idx] = projects[new_idx], projects[idx]
    for position, p in enumerate(projects):
        conn.execute("UPDATE projects SET position = ? WHERE id = ?", (position, p["id"]))
    conn.commit()


def delete_project(conn: sqlite3.Connection, project_id: str) -> None:
    """Deletes a project along with its entire board tree (every board,
    column, task and subtask reachable from the Main Board) and its
    Document Library. Deliberately does NOT touch project_activity_log -
    even though nothing writes to it yet in this phase, log entries are
    meant to be a permanent audit trail (see the Activity Log's future
    permanence requirement) and should survive a project delete once
    that feature is built."""
    project = get_project(conn, project_id)
    if project is not None and project.get("main_board_id"):
        delete_project_board(conn, project["main_board_id"])

    conn.execute("DELETE FROM project_documents WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    _compact_project_positions(conn)


# -- Project boards (Main Board or Sub-board - same shape, same tables) --

def get_project_board(conn: sqlite3.Connection, board_id: str):
    row = conn.execute("SELECT * FROM project_boards WHERE id = ?", (board_id,)).fetchone()
    return dict(row) if row is not None else None


def get_subboard_for_task(conn: sqlite3.Connection, task_id: str):
    """A task owns at most one sub-board, enforced by the UNIQUE constraint
    on project_boards.parent_task_id."""
    row = conn.execute(
        "SELECT * FROM project_boards WHERE parent_task_id = ?", (task_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def add_project_board(
    conn: sqlite3.Connection, project_id: str, parent_task_id, name: str,
    seed_default_columns: bool = True,
) -> dict:
    """Low-level board creation - parent_task_id=None makes a Main Board,
    otherwise a sub-board owned by that task. Kept separate from
    add_subboard_for_task() (which resolves project_id/default name from
    the task) so a future whole-project clone/template operation can call
    this directly without duplicating the seeding logic."""
    name = name.strip() or "Untitled Board"

    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM project_boards WHERE project_id = ?", (project_id,)
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    board_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO project_boards (id, project_id, parent_task_id, name, last_view, position, created_at) "
        "VALUES (?, ?, ?, ?, 'kanban', ?, ?)",
        (board_id, project_id, parent_task_id, name, next_position, _now()),
    )
    conn.commit()

    if seed_default_columns:
        for position, col_name in enumerate(PROJECT_DEFAULT_COLUMNS):
            conn.execute(
                "INSERT INTO project_columns (id, board_id, name, position) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, board_id, col_name, position),
            )
        conn.commit()

    return get_project_board(conn, board_id)


def add_subboard_for_task(conn: sqlite3.Connection, parent_task_id: str) -> dict:
    """Higher-level entry point used by the UI: resolves the owning
    project and a default name (the parent task's title) from the task,
    then delegates to add_project_board(). Raises if the task already
    owns a sub-board - callers should check get_subboard_for_task() first
    if they want to distinguish "already exists" from "just created"."""
    if get_subboard_for_task(conn, parent_task_id) is not None:
        raise ValueError("This task already has a sub-board.")

    task = get_project_task(conn, parent_task_id)
    if task is None:
        raise ValueError("That task no longer exists.")
    parent_board = get_project_board(conn, task["board_id"])
    if parent_board is None:
        raise ValueError("That task's board no longer exists.")

    subboard = add_project_board(conn, parent_board["project_id"], parent_task_id, task["title"])
    _log_task_event(conn, task, "subboard_created", f'Created sub-board "{subboard["name"]}"')
    return subboard


def set_board_last_view(conn: sqlite3.Connection, board_id: str, view_name: str) -> None:
    conn.execute("UPDATE project_boards SET last_view = ? WHERE id = ?", (view_name, board_id))
    conn.commit()


def get_board_ancestry(conn: sqlite3.Connection, board_id: str) -> list:
    """Main Board first, board_id last. Walks upward: each board's
    parent_task_id names the task that spawned it, and that task's
    board_id names the parent board - repeat until parent_task_id is
    NULL (the Main Board)."""
    chain = []
    current = get_project_board(conn, board_id)
    while current is not None:
        chain.append(current)
        if current["parent_task_id"] is None:
            break
        parent_task = get_project_task(conn, current["parent_task_id"])
        current = get_project_board(conn, parent_task["board_id"]) if parent_task else None
    chain.reverse()
    return chain


def get_board_breadcrumb(conn: sqlite3.Connection, board_id: str) -> list:
    """Like get_board_ancestry(), but returns display-ready
    {"label", "board_id"} dicts. The first label is the *project's* name
    (not the Main Board's own name, in case they've diverged via
    rename_project) - matches the spec's example "Project Name > Task A"."""
    ancestry = get_board_ancestry(conn, board_id)
    if not ancestry:
        return []

    main_board = ancestry[0]
    project = get_project(conn, main_board["project_id"])
    project_name = project["name"] if project else main_board["name"]

    crumbs = [{"label": project_name, "board_id": main_board["id"]}]
    for board in ancestry[1:]:
        crumbs.append({"label": board["name"], "board_id": board["id"]})
    return crumbs


def get_board_subtree_ids(conn: sqlite3.Connection, root_board_id: str) -> list:
    """Every board id in the subtree rooted at root_board_id (itself
    included), walked breadth-first. This is the one shared "walk this
    subtree" helper that depth checks, breadcrumb badges, and every
    cascade-delete confirmation below all build on - iterative rather
    than recursive since the depth guardrail is a soft warning, not an
    enforced limit, so a pathological tree could exceed Python's default
    recursion limit if walked recursively."""
    result = [root_board_id]
    frontier = [root_board_id]
    while frontier:
        placeholders = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"SELECT pb.id FROM project_boards pb "
            f"JOIN project_tasks pt ON pb.parent_task_id = pt.id "
            f"WHERE pt.board_id IN ({placeholders})",
            frontier,
        ).fetchall()
        frontier = [r["id"] for r in rows]
        result.extend(frontier)
    return result


def get_subtree_counts(conn: sqlite3.Connection, root_board_id: str) -> dict:
    """Used by cascade-delete confirmations to state how much a delete
    would take out: every board (root included) and every task across
    that whole subtree."""
    board_ids = get_board_subtree_ids(conn, root_board_id)
    placeholders = ",".join("?" for _ in board_ids)
    task_count = conn.execute(
        f"SELECT COUNT(*) AS c FROM project_tasks WHERE board_id IN ({placeholders})", board_ids
    ).fetchone()["c"]
    return {"board_count": len(board_ids), "task_count": task_count}


def get_subtree_task_progress(conn: sqlite3.Connection, root_board_id: str) -> tuple:
    """(done, total) task counts across a board's whole subtree - powers
    the recursive "this task's sub-board is N/M done" badge."""
    board_ids = get_board_subtree_ids(conn, root_board_id)
    placeholders = ",".join("?" for _ in board_ids)
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM project_tasks WHERE board_id IN ({placeholders})", board_ids
    ).fetchone()["c"]
    done = conn.execute(
        f"SELECT COUNT(*) AS c FROM project_tasks WHERE board_id IN ({placeholders}) AND completed = 1",
        board_ids,
    ).fetchone()["c"]
    return done, total


def delete_project_board(conn: sqlite3.Connection, board_id: str) -> None:
    """Deletes a board along with its columns/tasks/subtasks, and
    (recursively, via get_board_subtree_ids) any sub-boards spawned from
    tasks on it. project_activity_log entries are NOT part of this
    cascade - see the docstring on delete_project() above."""
    board_ids = get_board_subtree_ids(conn, board_id)
    placeholders = ",".join("?" for _ in board_ids)

    conn.execute(
        f"DELETE FROM project_subtasks WHERE task_id IN "
        f"(SELECT id FROM project_tasks WHERE board_id IN ({placeholders}))",
        board_ids,
    )
    conn.execute(f"DELETE FROM project_tasks WHERE board_id IN ({placeholders})", board_ids)
    conn.execute(f"DELETE FROM project_columns WHERE board_id IN ({placeholders})", board_ids)
    conn.execute(f"DELETE FROM project_boards WHERE id IN ({placeholders})", board_ids)
    conn.commit()


# -- Project columns (always scoped to a project board) ------------------

def get_project_columns(conn: sqlite3.Connection, board_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM project_columns WHERE board_id = ? ORDER BY position ASC", (board_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_project_column(conn: sqlite3.Connection, column_id: str):
    row = conn.execute("SELECT * FROM project_columns WHERE id = ?", (column_id,)).fetchone()
    return dict(row) if row is not None else None


def get_default_new_project_task_column(conn: sqlite3.Connection, board_id: str):
    columns = get_project_columns(conn, board_id)
    return columns[0]["id"] if columns else None


def add_project_column(conn: sqlite3.Connection, board_id: str, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Column name cannot be empty.")

    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM project_columns WHERE board_id = ?", (board_id,)
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    column_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO project_columns (id, board_id, name, position) VALUES (?, ?, ?, ?)",
        (column_id, board_id, name, next_position),
    )
    conn.commit()
    return get_project_column(conn, column_id)


def rename_project_column(conn: sqlite3.Connection, column_id: str, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Column name cannot be empty.")
    conn.execute("UPDATE project_columns SET name = ? WHERE id = ?", (new_name, column_id))
    conn.commit()


def _compact_project_column_positions(conn: sqlite3.Connection, board_id: str) -> None:
    for position, col in enumerate(get_project_columns(conn, board_id)):
        if col["position"] != position:
            conn.execute("UPDATE project_columns SET position = ? WHERE id = ?", (position, col["id"]))
    conn.commit()


def delete_project_column(conn: sqlite3.Connection, board_id: str, column_id: str) -> None:
    columns = get_project_columns(conn, board_id)
    if len(columns) <= 1:
        raise ValueError("At least one column must remain on this board.")

    task_count = conn.execute(
        "SELECT COUNT(*) AS c FROM project_tasks WHERE board_id = ? AND column_id = ?",
        (board_id, column_id),
    ).fetchone()["c"]
    if task_count > 0:
        raise ValueError(
            f"This column still has {task_count} task(s) in it. "
            "Move or delete them first, then delete the column."
        )

    conn.execute("DELETE FROM project_columns WHERE id = ?", (column_id,))
    conn.commit()
    _compact_project_column_positions(conn, board_id)


def move_project_column(conn: sqlite3.Connection, board_id: str, column_id: str, direction: int) -> None:
    columns = get_project_columns(conn, board_id)
    ids = [c["id"] for c in columns]
    if column_id not in ids:
        return

    idx = ids.index(column_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(columns):
        return

    columns[idx], columns[new_idx] = columns[new_idx], columns[idx]
    for position, col in enumerate(columns):
        conn.execute("UPDATE project_columns SET position = ? WHERE id = ?", (position, col["id"]))
    conn.commit()


# -- Project tasks (fixed to the board they were created on) -------------

def add_project_task(
    conn: sqlite3.Connection, board_id: str, column_id: str, title: str,
    notes: str = "", start_date: str = "", due_date: str = "", link: str = "",
) -> dict:
    title = title.strip()
    if not title:
        raise ValueError("Task title cannot be empty.")

    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM project_tasks WHERE board_id = ? AND column_id = ?",
        (board_id, column_id),
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    task_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO project_tasks "
        "(id, board_id, column_id, title, notes, start_date, due_date, completed, position, link, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (task_id, board_id, column_id, title, notes, start_date or None, due_date or None,
         next_position, link, _now()),
    )
    conn.commit()
    task = get_project_task(conn, task_id)
    _log_task_event(conn, task, "created", f'Created "{title}"')
    return task


def get_project_task(conn: sqlite3.Connection, task_id: str):
    row = conn.execute("SELECT * FROM project_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row is not None else None


def get_project_tasks_for_column(conn: sqlite3.Connection, board_id: str, column_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM project_tasks WHERE board_id = ? AND column_id = ? ORDER BY position ASC",
        (board_id, column_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_project_tasks_for_board(conn: sqlite3.Connection, board_id: str) -> list:
    """Flat, all columns - used by the List view."""
    rows = conn.execute(
        "SELECT * FROM project_tasks WHERE board_id = ? ORDER BY position ASC", (board_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_missing_dates_tasks(conn: sqlite3.Connection, board_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM project_tasks WHERE board_id = ? "
        "AND (start_date IS NULL OR start_date = '' OR due_date IS NULL OR due_date = '') "
        "ORDER BY position ASC",
        (board_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_scheduled_project_tasks(conn: sqlite3.Connection, board_id: str) -> list:
    """Complement of get_missing_dates_tasks() - tasks with BOTH start_date
    and due_date set. Feeds the Gantt chart's bars; anything missing
    either date falls into the Unscheduled tray via
    get_missing_dates_tasks() itself, not a re-implementation here."""
    rows = conn.execute(
        "SELECT * FROM project_tasks WHERE board_id = ? "
        "AND start_date IS NOT NULL AND start_date != '' "
        "AND due_date IS NOT NULL AND due_date != '' "
        "ORDER BY start_date ASC, position ASC",
        (board_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_calendar_project_tasks(conn: sqlite3.Connection, board_id: str) -> list:
    """Tasks with due_date set (start_date optional) - feeds the Calendar
    view. A due-only task renders as a single-day chip on due_date; a
    task with both dates renders as a spanning chip. A task missing
    due_date entirely never appears on the calendar. Deliberately
    overlaps with get_scheduled_project_tasks()/get_missing_dates_tasks()
    - each view is answering a different question ("can I draw a bar?"
    vs. "does it have a day to sit on?")."""
    rows = conn.execute(
        "SELECT * FROM project_tasks WHERE board_id = ? "
        "AND due_date IS NOT NULL AND due_date != '' "
        "ORDER BY due_date ASC, position ASC",
        (board_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_due_date(conn: sqlite3.Connection, board_id: str, exclude_task_id: str = None):
    """The most recent due_date set among this board's tasks (lexicographic
    MAX works since dates are stored as zero-padded yyyy-MM-dd strings),
    or None if none have one. Used to default a new task's start date to
    right where the last one left off, instead of always defaulting to
    today - see default_project_task_start_date()."""
    query = (
        "SELECT MAX(due_date) AS d FROM project_tasks WHERE board_id = ? "
        "AND due_date IS NOT NULL AND due_date != ''"
    )
    params = [board_id]
    if exclude_task_id:
        query += " AND id != ?"
        params.append(exclude_task_id)
    row = conn.execute(query, params).fetchone()
    return row["d"] if row and row["d"] else None


def build_project_task_meta_parts(conn: sqlite3.Connection, task: dict) -> list:
    """Shared meta-line builder - dates/subtask progress/sub-board
    badge/link - reused by ProjectKanbanWidget's card text and by the
    Gantt/Calendar views' tooltips, so this logic exists exactly once."""
    meta_parts = []
    if task.get("start_date") and task.get("due_date"):
        meta_parts.append(f"{task['start_date']} → {task['due_date']}")
    elif task.get("due_date"):
        meta_parts.append(f"Due {task['due_date']}")
    elif task.get("start_date"):
        meta_parts.append(f"Starts {task['start_date']}")
    else:
        meta_parts.append("◇ no dates set")

    subtasks = get_project_subtasks(conn, task["id"])
    if subtasks:
        done_count = sum(1 for s in subtasks if s["done"])
        meta_parts.append(f"{done_count}/{len(subtasks)} done")

    subboard = get_subboard_for_task(conn, task["id"])
    if subboard is not None:
        done, total = get_subtree_task_progress(conn, subboard["id"])
        meta_parts.append(f"⧉ {done}/{total}")

    if task.get("link"):
        meta_parts.append("Link")

    return meta_parts


def update_project_task(
    conn: sqlite3.Connection, task_id: str, title: str, notes: str,
    start_date: str, due_date: str, link: str,
) -> None:
    title = title.strip()
    if not title:
        raise ValueError("Task title cannot be empty.")

    old_task = get_project_task(conn, task_id)
    conn.execute(
        "UPDATE project_tasks SET title = ?, notes = ?, start_date = ?, due_date = ?, link = ? WHERE id = ?",
        (title, notes, start_date or None, due_date or None, link, task_id),
    )
    conn.commit()

    if old_task is None:
        return
    new_task = get_project_task(conn, task_id)
    # One log entry per changed field, per the issue's §9.1 ("a single
    # edit that changes both title and due date produces two entries").
    for field_name in ("title", "notes", "start_date", "due_date", "link"):
        old_value = old_task.get(field_name) or ""
        new_value = new_task.get(field_name) or ""
        if old_value == new_value:
            continue
        label = _ACTIVITY_FIELD_LABELS[field_name]
        description = f'Changed {label} from "{old_value or "(empty)"}" to "{new_value or "(empty)"}"'
        _log_task_event(
            conn, new_task, "field_changed", description,
            field_name=field_name, old_value=old_value, new_value=new_value,
        )


def set_project_task_completed(conn: sqlite3.Connection, task_id: str, completed: bool) -> None:
    task = get_project_task(conn, task_id)
    if task is None or bool(task["completed"]) == completed:
        return
    conn.execute(
        "UPDATE project_tasks SET completed = ? WHERE id = ?", (1 if completed else 0, task_id)
    )
    conn.commit()
    action_type = "completed" if completed else "uncompleted"
    description = "Marked complete" if completed else "Marked incomplete"
    _log_task_event(conn, task, action_type, description)


def move_project_task(conn: sqlite3.Connection, task_id: str, new_column_id: str) -> None:
    task = get_project_task(conn, task_id)
    if task is None:
        return
    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM project_tasks WHERE board_id = ? AND column_id = ?",
        (task["board_id"], new_column_id),
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0
    conn.execute(
        "UPDATE project_tasks SET column_id = ?, position = ? WHERE id = ?",
        (new_column_id, next_position, task_id),
    )
    conn.commit()

    old_column = get_project_column(conn, task["column_id"])
    new_column = get_project_column(conn, new_column_id)
    old_name = old_column["name"] if old_column else "?"
    new_name = new_column["name"] if new_column else "?"
    _log_task_event(
        conn, task, "moved", f'Moved from "{old_name}" to "{new_name}"',
        old_value=old_name, new_value=new_name,
    )


def delete_project_task(conn: sqlite3.Connection, task_id: str) -> None:
    """If this task owns a sub-board, that whole subtree is deleted first
    (see delete_project_board). Callers that want to confirm this with the
    user first should check get_subboard_for_task() before calling this -
    see ProjectsHub.confirm_and_delete_task(). Logged before the actual
    delete so the entry can still resolve the task's board/breadcrumb;
    cascaded deletions of a sub-board's own tasks are NOT individually
    logged - only the directly-deleted task is."""
    task = get_project_task(conn, task_id)
    if task is not None:
        _log_task_event(conn, task, "deleted", f'Deleted "{task["title"]}"')

    subboard = get_subboard_for_task(conn, task_id)
    if subboard is not None:
        delete_project_board(conn, subboard["id"])
    conn.execute("DELETE FROM project_subtasks WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM project_tasks WHERE id = ?", (task_id,))
    conn.commit()


# -- Project subtasks (checklist items on a single project task) ---------

def get_project_subtasks(conn: sqlite3.Connection, task_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM project_subtasks WHERE task_id = ? ORDER BY position ASC", (task_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_project_subtask(conn: sqlite3.Connection, task_id: str, title: str) -> dict:
    title = title.strip()
    if not title:
        raise ValueError("Subtask title cannot be empty.")

    max_position_row = conn.execute(
        "SELECT MAX(position) AS m FROM project_subtasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    next_position = (max_position_row["m"] + 1) if max_position_row["m"] is not None else 0

    subtask_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO project_subtasks (id, task_id, title, done, position) VALUES (?, ?, ?, 0, ?)",
        (subtask_id, task_id, title, next_position),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM project_subtasks WHERE id = ?", (subtask_id,)).fetchone()

    task = get_project_task(conn, task_id)
    if task is not None:
        _log_task_event(conn, task, "subtask_added", f'Added subtask "{title}"')
    return dict(row)


def get_project_subtask(conn: sqlite3.Connection, subtask_id: str):
    row = conn.execute("SELECT * FROM project_subtasks WHERE id = ?", (subtask_id,)).fetchone()
    return dict(row) if row is not None else None


def set_project_subtask_done(conn: sqlite3.Connection, subtask_id: str, done: bool) -> None:
    subtask = get_project_subtask(conn, subtask_id)
    if subtask is None or bool(subtask["done"]) == done:
        return
    conn.execute("UPDATE project_subtasks SET done = ? WHERE id = ?", (1 if done else 0, subtask_id))
    conn.commit()

    task = get_project_task(conn, subtask["task_id"])
    if task is not None:
        verb = "Checked" if done else "Unchecked"
        _log_task_event(
            conn, task, "subtask_done", f'{verb} subtask "{subtask["title"]}"',
            old_value="0" if done else "1", new_value="1" if done else "0",
        )


def delete_project_subtask(conn: sqlite3.Connection, subtask_id: str) -> None:
    subtask = get_project_subtask(conn, subtask_id)
    conn.execute("DELETE FROM project_subtasks WHERE id = ?", (subtask_id,))
    conn.commit()

    if subtask is not None:
        task = get_project_task(conn, subtask["task_id"])
        if task is not None:
            _log_task_event(conn, task, "subtask_removed", f'Removed subtask "{subtask["title"]}"')


def get_project_task_count(conn: sqlite3.Connection, project_id: str) -> int:
    """Total tasks across a whole project's board tree - used by the
    delete-project confirmation, mirroring get_board_task_count()."""
    project = get_project(conn, project_id)
    if project is None or not project.get("main_board_id"):
        return 0
    return get_subtree_counts(conn, project["main_board_id"])["task_count"]


# -- Activity Log (permanent audit trail of task-lifecycle events) -------
#
# project_id/board_id/task_id here are plain text references, NOT
# cascading foreign keys - when a task or board is deleted, its log
# entries are left untouched (see delete_project_task/delete_project_board
# above, which never touch this table), remaining readable via
# task_title_snapshot/board_path_snapshot, captured at the moment of the
# event rather than looked up live. Board/column-level structural events
# and Document Library changes are deliberately never logged - scope is
# task lifecycle only.

ACTIVITY_ACTION_TYPES = [
    ("created", "Created"),
    ("field_changed", "Edited"),
    ("moved", "Moved"),
    ("completed", "Completed"),
    ("uncompleted", "Uncompleted"),
    ("subtask_added", "Subtask added"),
    ("subtask_done", "Subtask checked/unchecked"),
    ("subtask_removed", "Subtask removed"),
    ("subboard_created", "Sub-board created"),
    ("deleted", "Deleted"),
]

_ACTIVITY_FIELD_LABELS = {
    "title": "Title",
    "notes": "Notes",
    "start_date": "Start date",
    "due_date": "Due date",
    "link": "Link",
}


def add_activity_log_entry(
    conn: sqlite3.Connection, project_id: str, board_id, task_id,
    task_title_snapshot: str, board_path_snapshot: str, action_type: str,
    field_name: str = None, old_value: str = None, new_value: str = None,
    description: str = "",
) -> None:
    """Low-level insert - prefer _log_task_event() below for anything
    task-related, since it resolves project_id/board_path_snapshot for
    you rather than requiring every call site to do it themselves."""
    conn.execute(
        "INSERT INTO project_activity_log "
        "(id, project_id, board_id, task_id, task_title_snapshot, board_path_snapshot, "
        "action_type, field_name, old_value, new_value, description, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex, project_id, board_id, task_id, task_title_snapshot,
            board_path_snapshot, action_type, field_name, old_value, new_value,
            description, _now(),
        ),
    )
    conn.commit()


def _log_task_event(
    conn: sqlite3.Connection, task: dict, action_type: str, description: str,
    field_name: str = None, old_value: str = None, new_value: str = None,
) -> None:
    """Resolves project_id/board_path_snapshot from the task's board so
    every instrumentation call site below only has to name what
    happened, not re-derive where it happened."""
    board = get_project_board(conn, task["board_id"])
    if board is None:
        return
    board_path = " › ".join(c["label"] for c in get_board_breadcrumb(conn, task["board_id"]))
    add_activity_log_entry(
        conn, board["project_id"], task["board_id"], task["id"],
        task["title"], board_path, action_type,
        field_name=field_name, old_value=old_value, new_value=new_value,
        description=description,
    )


def get_activity_log_entries(
    conn: sqlite3.Connection, project_id: str, search_text: str = "",
    action_types: list = None, board_path_filter: str = "",
    date_from: str = "", date_to: str = "",
) -> list:
    """Project-wide Activity Log screen's query - newest first, with the
    search/action-type/board/date filters from the issue's §9.4."""
    query = "SELECT * FROM project_activity_log WHERE project_id = ?"
    params = [project_id]

    if search_text.strip():
        query += " AND (description LIKE ? OR task_title_snapshot LIKE ?)"
        like = f"%{search_text.strip()}%"
        params.extend([like, like])

    if action_types:
        placeholders = ",".join("?" for _ in action_types)
        query += f" AND action_type IN ({placeholders})"
        params.extend(action_types)

    if board_path_filter:
        query += " AND board_path_snapshot = ?"
        params.append(board_path_filter)

    if date_from:
        query += " AND created_at >= ?"
        params.append(date_from)
    if date_to:
        # created_at is a full ISO timestamp - "<= date_to 23:59:59"
        # keeps the end date inclusive rather than excluding same-day events.
        query += " AND created_at <= ?"
        params.append(f"{date_to}T23:59:59")

    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_activity_log_entries_for_task(conn: sqlite3.Connection, task_id: str) -> list:
    """Per-task History tab - newest first, no filters (only usable
    while the task still exists; deleted tasks' history only remains
    visible in the project-wide log)."""
    rows = conn.execute(
        "SELECT * FROM project_activity_log WHERE task_id = ? ORDER BY created_at DESC", (task_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_activity_log_board_paths(conn: sqlite3.Connection, project_id: str) -> list:
    """Distinct board_path_snapshot values logged for this project, sorted -
    populates the Project Activity Log screen's board filter. Reads the
    snapshots themselves rather than the live board tree, so it still lists
    a since-deleted board's path as long as history remains under it."""
    rows = conn.execute(
        "SELECT DISTINCT board_path_snapshot FROM project_activity_log "
        "WHERE project_id = ? AND board_path_snapshot != '' ORDER BY board_path_snapshot",
        (project_id,),
    ).fetchall()
    return [r["board_path_snapshot"] for r in rows]


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

QToolButton {{
    background-color: #2c2e33;
    color: #e8e8e8;
    border: 1px solid #3a3d42;
    border-radius: 6px;
    padding: 5px 28px 5px 10px;
}}
QToolButton:hover {{
    background-color: #34363b;
}}
QToolButton:pressed {{
    background-color: #26282c;
}}
QToolButton[accent="true"] {{
    background-color: {ACCENT_COLOR};
    border: 1px solid {ACCENT_COLOR};
    color: #ffffff;
    font-weight: bold;
}}
QToolButton[accent="true"]:hover {{
    background-color: {ACCENT_COLOR_HOVER};
}}
QToolButton::menu-button {{
    border: none;
    width: 22px;
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

        self.completed_check = QCheckBox("Completed")
        self.completed_check.setChecked(bool(task.get("completed")))
        self.completed_check.setToolTip(
            "Hides this task from the board by default (independent of column) - "
            "see the \"Show Completed\" toggle to review it again."
        )
        layout.addWidget(self.completed_check)

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
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_save(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return
        self.accept()

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
            "completed": self.completed_check.isChecked(),
        }


class NewTaskDialog(QDialog):
    """Single-form task creation dialog, styled and laid out like
    TaskCardDialog's edit form (title/status/due date/Joplin link/notes)
    instead of the sequence of plain input-box prompts this replaced."""

    def __init__(self, columns: list, default_status: str, parent=None, prefill: dict = None):
        super().__init__(parent)
        self.setWindowTitle("New Task")
        self.resize(420, 420)
        prefill = prefill or {}

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(prefill.get("title", ""))
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Status"))
        self.status_combo = QComboBox()
        for col in columns:
            self.status_combo.addItem(col["name"], col["status"])
        target_status = prefill.get("status") or default_status
        default_idx = next(
            (i for i, col in enumerate(columns) if col["status"] == target_status), 0
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
        prefill_due_date = QDate.fromString(prefill.get("due_date") or "", "yyyy-MM-dd")
        self.due_date_check.setChecked(prefill_due_date.isValid())
        self.due_date_edit.setDate(prefill_due_date if prefill_due_date.isValid() else QDate.currentDate())
        self.due_date_edit.setEnabled(prefill_due_date.isValid())
        self.due_date_check.toggled.connect(self.due_date_edit.setEnabled)
        due_row.addWidget(self.due_date_edit, stretch=1)
        layout.addLayout(due_row)

        layout.addWidget(QLabel("Joplin Note Link"))
        self.joplin_link_edit = QLineEdit(prefill.get("joplin_link", ""))
        self.joplin_link_edit.setPlaceholderText("joplin://... or https://...")
        layout.addWidget(self.joplin_link_edit)

        layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(prefill.get("notes", ""))
        layout.addWidget(self.notes_edit, stretch=1)

        btn_row = QHBoxLayout()
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

        self.title_edit.setFocus()

    def _on_add(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return
        self.accept()

    def result_values(self) -> dict:
        due_date = self.due_date_edit.date().toString("yyyy-MM-dd") if self.due_date_check.isChecked() else ""
        return {
            "title": self.title_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "status": self.status_combo.currentData(),
            "due_date": due_date,
            "joplin_link": self.joplin_link_edit.text().strip(),
        }


class TemplateEditorDialog(QDialog):
    """Create/edit form for a single task template. Unlike TaskCardDialog,
    the subtask checklist here is staged in memory (self._subtask_titles)
    and only written to the database when the whole template is saved -
    a template is a definition object, not a live task, so there's no
    "already in progress" checklist state that would make a Cancel feel
    surprising."""

    def __init__(self, columns: list, template: dict = None, subtask_titles: list = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Template" if template else "New Template")
        self.resize(420, 520)
        self._subtask_titles = list(subtask_titles or [])

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(template["title"] if template else "")
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Column"))
        self.status_combo = QComboBox()
        for col in columns:
            self.status_combo.addItem(col["name"], col["status"])
        target_status = template["status"] if template else None
        default_idx = next(
            (i for i, col in enumerate(columns) if col["status"] == target_status), 0
        )
        self.status_combo.setCurrentIndex(default_idx)
        layout.addWidget(self.status_combo)

        layout.addWidget(QLabel("Due Date"))
        due_row = QHBoxLayout()
        existing_offset = template.get("due_offset_days") if template else None
        self.due_date_check = QCheckBox("Set")
        self.due_date_check.setChecked(existing_offset is not None)
        due_row.addWidget(self.due_date_check)
        self.due_offset_spin = QSpinBox()
        self.due_offset_spin.setRange(0, 3650)
        self.due_offset_spin.setSuffix(" day(s) from creation")
        self.due_offset_spin.setValue(existing_offset if existing_offset is not None else 0)
        self.due_offset_spin.setEnabled(existing_offset is not None)
        self.due_date_check.toggled.connect(self.due_offset_spin.setEnabled)
        due_row.addWidget(self.due_offset_spin, stretch=1)
        layout.addLayout(due_row)

        layout.addWidget(QLabel("Joplin Note Link"))
        self.joplin_link_edit = QLineEdit(template.get("joplin_link", "") if template else "")
        self.joplin_link_edit.setPlaceholderText("joplin://... or https://...")
        layout.addWidget(self.joplin_link_edit)

        layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(template.get("notes", "") if template else "")
        layout.addWidget(self.notes_edit, stretch=1)

        layout.addWidget(QLabel("Subtasks"))
        self.subtasks_list = QListWidget()
        layout.addWidget(self.subtasks_list, stretch=1)
        self._refresh_subtasks_list()

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

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setProperty("accent", True)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        self.title_edit.setFocus()

    def _on_save(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Title required", "Template title cannot be empty.")
            return
        self.accept()

    def _refresh_subtasks_list(self) -> None:
        self.subtasks_list.clear()
        for title in self._subtask_titles:
            self.subtasks_list.addItem(QListWidgetItem(title))

    def _add_subtask(self) -> None:
        title = self.new_subtask_edit.text().strip()
        if not title:
            return
        self._subtask_titles.append(title)
        self.new_subtask_edit.clear()
        self._refresh_subtasks_list()

    def _delete_selected_subtask(self) -> None:
        row = self.subtasks_list.currentRow()
        if row < 0:
            return
        del self._subtask_titles[row]
        self._refresh_subtasks_list()

    def result_values(self) -> dict:
        due_offset_days = self.due_offset_spin.value() if self.due_date_check.isChecked() else None
        return {
            "title": self.title_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "status": self.status_combo.currentData(),
            "due_offset_days": due_offset_days,
            "joplin_link": self.joplin_link_edit.text().strip(),
            "subtask_titles": list(self._subtask_titles),
        }


class ManageTemplatesDialog(QDialog):
    """Lists a board's task templates with add/edit/delete/reorder
    controls, mirroring the board-management pattern (rename/delete via
    dialogs, immediate writes rather than a staged save)."""

    def __init__(self, conn: sqlite3.Connection, board_id: str, columns: list, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.board_id = board_id
        self.columns = columns
        self.setWindowTitle("Manage Templates")
        self.resize(380, 420)

        layout = QVBoxLayout(self)

        self.templates_list = QListWidget()
        self.templates_list.itemDoubleClicked.connect(lambda _item: self._edit_selected())
        layout.addWidget(self.templates_list, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add")
        add_btn.clicked.connect(self._add_template)
        btn_row.addWidget(add_btn)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_selected)
        btn_row.addWidget(edit_btn)
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(lambda: self._move_selected(-1))
        btn_row.addWidget(up_btn)
        down_btn = QPushButton("Down")
        down_btn.clicked.connect(lambda: self._move_selected(1))
        btn_row.addWidget(down_btn)
        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet("color: #b00000;")
        delete_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        self.refresh()

    def refresh(self) -> None:
        self.templates_list.clear()
        for tpl in get_templates(self.conn, self.board_id):
            item = QListWidgetItem(tpl["title"])
            item.setData(Qt.UserRole, tpl["id"])
            self.templates_list.addItem(item)

    def _selected_template_id(self):
        item = self.templates_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _add_template(self) -> None:
        dialog = TemplateEditorDialog(self.columns, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values()
        try:
            add_template(
                self.conn, self.board_id, values["title"], values["notes"], values["status"],
                values["due_offset_days"], values["joplin_link"], values["subtask_titles"],
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not add template", str(e))
            return
        self.refresh()

    def _edit_selected(self) -> None:
        template_id = self._selected_template_id()
        if template_id is None:
            return
        template = get_template(self.conn, template_id)
        if template is None:
            return
        subtask_titles = [s["title"] for s in get_template_subtasks(self.conn, template_id)]
        dialog = TemplateEditorDialog(self.columns, template=template, subtask_titles=subtask_titles, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values()
        try:
            update_template(
                self.conn, template_id, values["title"], values["notes"], values["status"],
                values["due_offset_days"], values["joplin_link"], values["subtask_titles"],
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not update template", str(e))
            return
        self.refresh()

    def _delete_selected(self) -> None:
        template_id = self._selected_template_id()
        if template_id is None:
            return
        template = get_template(self.conn, template_id)
        if template is None:
            return
        reply = QMessageBox.question(
            self, "Delete template", f'Delete the "{template["title"]}" template?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        delete_template(self.conn, template_id)
        self.refresh()

    def _move_selected(self, direction: int) -> None:
        template_id = self._selected_template_id()
        if template_id is None:
            return
        move_template(self.conn, self.board_id, template_id, direction)
        self.refresh()
        for i in range(self.templates_list.count()):
            if self.templates_list.item(i).data(Qt.UserRole) == template_id:
                self.templates_list.setCurrentRow(i)
                break


class ScheduleEditorWidget(QWidget):
    """Reusable "Repeat" sub-form shared by CreateTaskRuleDialog and
    MoveTaskRuleDialog: a kind selector driving a QStackedWidget with the
    kind-specific controls. get_values()/set_values() round-trip exactly
    the fields _compute_next_run() consumes."""

    KIND_LABELS = [
        ("once", "Once"),
        ("hourly", "Hourly"),
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("interval", "Every N days"),
    ]
    WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.kind_combo = QComboBox()
        for slug, label in self.KIND_LABELS:
            self.kind_combo.addItem(label, slug)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        layout.addWidget(self.kind_combo)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        once_page = QWidget()
        once_layout = QVBoxLayout(once_page)
        once_layout.setContentsMargins(0, 0, 0, 0)
        once_layout.addWidget(QLabel("Date and time"))
        self.once_edit = QDateTimeEdit()
        self.once_edit.setCalendarPopup(True)
        self.once_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.once_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        once_layout.addWidget(self.once_edit)
        self.stack.addWidget(once_page)

        hourly_page = QWidget()
        hourly_layout = QVBoxLayout(hourly_page)
        hourly_layout.setContentsMargins(0, 0, 0, 0)
        hourly_layout.addWidget(QLabel("Every"))
        self.hourly_spin = QSpinBox()
        self.hourly_spin.setRange(1, 720)
        self.hourly_spin.setSuffix(" hour(s)")
        self.hourly_spin.setValue(1)
        hourly_layout.addWidget(self.hourly_spin)
        hourly_layout.addStretch()
        self.stack.addWidget(hourly_page)

        daily_page = QWidget()
        daily_layout = QVBoxLayout(daily_page)
        daily_layout.setContentsMargins(0, 0, 0, 0)
        daily_layout.addWidget(QLabel("Time of day"))
        self.daily_time_edit = QTimeEdit()
        self.daily_time_edit.setDisplayFormat("HH:mm")
        self.daily_time_edit.setTime(QTime(9, 0))
        daily_layout.addWidget(self.daily_time_edit)
        self.stack.addWidget(daily_page)

        weekly_page = QWidget()
        weekly_layout = QVBoxLayout(weekly_page)
        weekly_layout.setContentsMargins(0, 0, 0, 0)
        weekly_layout.addWidget(QLabel("Days"))
        weekday_row = QHBoxLayout()
        self.weekday_checks = []
        for label in self.WEEKDAY_LABELS:
            cb = QCheckBox(label)
            weekday_row.addWidget(cb)
            self.weekday_checks.append(cb)
        weekly_layout.addLayout(weekday_row)
        weekly_layout.addWidget(QLabel("Time of day"))
        self.weekly_time_edit = QTimeEdit()
        self.weekly_time_edit.setDisplayFormat("HH:mm")
        self.weekly_time_edit.setTime(QTime(9, 0))
        weekly_layout.addWidget(self.weekly_time_edit)
        self.stack.addWidget(weekly_page)

        interval_page = QWidget()
        interval_layout = QVBoxLayout(interval_page)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.addWidget(QLabel("Every"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 3650)
        self.interval_spin.setSuffix(" day(s)")
        self.interval_spin.setValue(1)
        interval_layout.addWidget(self.interval_spin)
        interval_layout.addWidget(QLabel("Time of day"))
        self.interval_time_edit = QTimeEdit()
        self.interval_time_edit.setDisplayFormat("HH:mm")
        self.interval_time_edit.setTime(QTime(9, 0))
        interval_layout.addWidget(self.interval_time_edit)
        self.stack.addWidget(interval_page)

        self.kind_combo.setCurrentIndex(2)  # default to Daily

    def _on_kind_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)

    def get_values(self) -> dict:
        kind = self.kind_combo.currentData()
        weekdays = ",".join(str(i) for i, cb in enumerate(self.weekday_checks) if cb.isChecked())
        time_edit = {
            "daily": self.daily_time_edit,
            "weekly": self.weekly_time_edit,
            "interval": self.interval_time_edit,
        }.get(kind, self.daily_time_edit)
        # "hourly" reuses the same interval_days column as an hour count
        # rather than adding a schema column - the two kinds are mutually
        # exclusive per rule, so there's no ambiguity in what it means.
        interval_amount = None
        if kind == "hourly":
            interval_amount = self.hourly_spin.value()
        elif kind == "interval":
            interval_amount = self.interval_spin.value()
        return {
            "schedule_kind": kind,
            "schedule_time": time_edit.time().toString("HH:mm"),
            "schedule_weekdays": weekdays,
            "schedule_interval_days": interval_amount,
            "schedule_datetime": (
                self.once_edit.dateTime().toString("yyyy-MM-ddTHH:mm:ss") if kind == "once" else None
            ),
        }

    def set_values(self, rule: dict) -> None:
        kind_index = next(
            (i for i, (slug, _) in enumerate(self.KIND_LABELS) if slug == rule["schedule_kind"]), 2
        )
        self.kind_combo.setCurrentIndex(kind_index)

        hh, mm = 9, 0
        if rule.get("schedule_time"):
            hh, mm = (int(p) for p in rule["schedule_time"].split(":"))
        qtime = QTime(hh, mm)
        self.daily_time_edit.setTime(qtime)
        self.weekly_time_edit.setTime(qtime)
        self.interval_time_edit.setTime(qtime)

        weekdays = set()
        if rule.get("schedule_weekdays"):
            weekdays = {int(d) for d in rule["schedule_weekdays"].split(",") if d != ""}
        for i, cb in enumerate(self.weekday_checks):
            cb.setChecked(i in weekdays)

        if rule.get("schedule_interval_days"):
            self.interval_spin.setValue(rule["schedule_interval_days"])
            self.hourly_spin.setValue(rule["schedule_interval_days"])

        if rule.get("schedule_datetime"):
            qdt = QDateTime.fromString(rule["schedule_datetime"], "yyyy-MM-ddTHH:mm:ss")
            if qdt.isValid():
                self.once_edit.setDateTime(qdt)


class CreateTaskRuleDialog(QDialog):
    """Add/edit form for a "create task" automation rule. The task's
    fields come from either an existing Template (kept live - editing the
    template afterwards changes what future runs create) or fields
    entered directly on the rule, toggled by a radio-button pair; the
    custom-fields form mirrors TemplateEditorDialog's, subtasks staged
    the same way with no DB writes until Save."""

    def __init__(self, columns: list, templates: list, rule: dict = None, subtask_titles: list = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Create-Task Rule" if rule else "New Create-Task Rule")
        self.resize(440, 660)
        self._subtask_titles = list(subtask_titles or [])
        rule_is_custom = rule is not None and not rule.get("template_id")

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Rule Name"))
        self.name_edit = QLineEdit(rule["name"] if rule else "")
        layout.addWidget(self.name_edit)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked((rule["enabled"] == 1) if rule else True)
        layout.addWidget(self.enabled_check)

        layout.addWidget(QLabel("Schedule"))
        self.schedule_editor = ScheduleEditorWidget()
        if rule:
            self.schedule_editor.set_values(rule)
        layout.addWidget(self.schedule_editor)

        layout.addWidget(QLabel("Task Source"))
        source_row = QHBoxLayout()
        self.template_radio = QRadioButton("Use a Template")
        self.custom_radio = QRadioButton("Custom fields")
        source_group = QButtonGroup(self)
        source_group.addButton(self.template_radio)
        source_group.addButton(self.custom_radio)
        source_row.addWidget(self.template_radio)
        source_row.addWidget(self.custom_radio)
        layout.addLayout(source_row)

        self.source_stack = QStackedWidget()
        layout.addWidget(self.source_stack, stretch=1)

        template_page = QWidget()
        template_layout = QVBoxLayout(template_page)
        template_layout.setContentsMargins(0, 0, 0, 0)
        template_layout.addWidget(QLabel("Template"))
        self.template_combo = QComboBox()
        for tpl in templates:
            self.template_combo.addItem(tpl["title"], tpl["id"])
        template_layout.addWidget(self.template_combo)
        template_layout.addStretch()
        self.source_stack.addWidget(template_page)

        custom_page = QWidget()
        custom_layout = QVBoxLayout(custom_page)
        custom_layout.setContentsMargins(0, 0, 0, 0)

        custom_layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(rule["task_title"] if rule_is_custom else "")
        custom_layout.addWidget(self.title_edit)

        custom_layout.addWidget(QLabel("Column"))
        self.status_combo = QComboBox()
        for col in columns:
            self.status_combo.addItem(col["name"], col["status"])
        target_status = rule["task_status"] if rule_is_custom else None
        default_idx = next((i for i, col in enumerate(columns) if col["status"] == target_status), 0)
        self.status_combo.setCurrentIndex(default_idx)
        custom_layout.addWidget(self.status_combo)

        custom_layout.addWidget(QLabel("Due Date"))
        due_row = QHBoxLayout()
        existing_offset = rule.get("task_due_offset_days") if rule_is_custom else None
        self.due_date_check = QCheckBox("Set")
        self.due_date_check.setChecked(existing_offset is not None)
        due_row.addWidget(self.due_date_check)
        self.due_offset_spin = QSpinBox()
        self.due_offset_spin.setRange(0, 3650)
        self.due_offset_spin.setSuffix(" day(s) from creation")
        self.due_offset_spin.setValue(existing_offset if existing_offset is not None else 0)
        self.due_offset_spin.setEnabled(existing_offset is not None)
        self.due_date_check.toggled.connect(self.due_offset_spin.setEnabled)
        due_row.addWidget(self.due_offset_spin, stretch=1)
        custom_layout.addLayout(due_row)

        custom_layout.addWidget(QLabel("Joplin Note Link"))
        self.joplin_link_edit = QLineEdit(rule["task_joplin_link"] if rule_is_custom else "")
        self.joplin_link_edit.setPlaceholderText("joplin://... or https://...")
        custom_layout.addWidget(self.joplin_link_edit)

        custom_layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(rule["task_notes"] if rule_is_custom else "")
        custom_layout.addWidget(self.notes_edit, stretch=1)

        custom_layout.addWidget(QLabel("Subtasks"))
        self.subtasks_list = QListWidget()
        custom_layout.addWidget(self.subtasks_list, stretch=1)
        self._refresh_subtasks_list()

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
        custom_layout.addLayout(subtask_add_row)

        self.source_stack.addWidget(custom_page)

        self.template_radio.toggled.connect(
            lambda checked: self.source_stack.setCurrentIndex(0) if checked else None
        )
        self.custom_radio.toggled.connect(
            lambda checked: self.source_stack.setCurrentIndex(1) if checked else None
        )

        use_template = bool(rule.get("template_id")) if rule else bool(templates)
        if not templates:
            self.template_radio.setEnabled(False)
            use_template = False
        if use_template:
            self.template_radio.setChecked(True)
            if rule and rule.get("template_id"):
                tpl_idx = next(
                    (i for i in range(self.template_combo.count())
                     if self.template_combo.itemData(i) == rule["template_id"]), 0
                )
                self.template_combo.setCurrentIndex(tpl_idx)
        else:
            self.custom_radio.setChecked(True)
        self.source_stack.setCurrentIndex(0 if use_template else 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setProperty("accent", True)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_save(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Name required", "Rule name cannot be empty.")
            return
        if self.custom_radio.isChecked() and not self.title_edit.text().strip():
            QMessageBox.warning(self, "Title required", "Enter a task title, or switch to using a Template.")
            return
        self.accept()

    def _refresh_subtasks_list(self) -> None:
        self.subtasks_list.clear()
        for title in self._subtask_titles:
            self.subtasks_list.addItem(QListWidgetItem(title))

    def _add_subtask(self) -> None:
        title = self.new_subtask_edit.text().strip()
        if not title:
            return
        self._subtask_titles.append(title)
        self.new_subtask_edit.clear()
        self._refresh_subtasks_list()

    def _delete_selected_subtask(self) -> None:
        row = self.subtasks_list.currentRow()
        if row < 0:
            return
        del self._subtask_titles[row]
        self._refresh_subtasks_list()

    def result_values(self) -> dict:
        schedule = self.schedule_editor.get_values()
        values = {
            "name": self.name_edit.text().strip(),
            "enabled": self.enabled_check.isChecked(),
            **schedule,
        }
        if self.template_radio.isChecked():
            values.update({
                "template_id": self.template_combo.currentData(),
                "task_title": "",
                "task_notes": "",
                "task_status": None,
                "task_joplin_link": "",
                "task_due_offset_days": None,
                "subtask_titles": [],
            })
        else:
            due_offset_days = self.due_offset_spin.value() if self.due_date_check.isChecked() else None
            values.update({
                "template_id": None,
                "task_title": self.title_edit.text().strip(),
                "task_notes": self.notes_edit.toPlainText().strip(),
                "task_status": self.status_combo.currentData(),
                "task_joplin_link": self.joplin_link_edit.text().strip(),
                "task_due_offset_days": due_offset_days,
                "subtask_titles": list(self._subtask_titles),
            })
        return values


class MoveTaskRuleDialog(QDialog):
    """Add/edit form for a "move tasks" automation rule: at the scheduled
    time, every task currently in the "from" column moves to the "to"
    column."""

    def __init__(self, columns: list, rule: dict = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Move-Task Rule" if rule else "New Move-Task Rule")
        self.resize(400, 440)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Rule Name"))
        self.name_edit = QLineEdit(rule["name"] if rule else "")
        layout.addWidget(self.name_edit)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked((rule["enabled"] == 1) if rule else True)
        layout.addWidget(self.enabled_check)

        layout.addWidget(QLabel("Schedule"))
        self.schedule_editor = ScheduleEditorWidget()
        if rule:
            self.schedule_editor.set_values(rule)
        layout.addWidget(self.schedule_editor)

        layout.addWidget(QLabel("Move tasks from"))
        self.from_combo = QComboBox()
        for col in columns:
            self.from_combo.addItem(col["name"], col["status"])
        layout.addWidget(self.from_combo)

        layout.addWidget(QLabel("to"))
        self.to_combo = QComboBox()
        for col in columns:
            self.to_combo.addItem(col["name"], col["status"])
        layout.addWidget(self.to_combo)

        if rule:
            from_idx = next((i for i, col in enumerate(columns) if col["status"] == rule["from_status"]), 0)
            to_idx = next((i for i, col in enumerate(columns) if col["status"] == rule["to_status"]), 0)
            self.from_combo.setCurrentIndex(from_idx)
            self.to_combo.setCurrentIndex(to_idx)
        elif len(columns) > 1:
            self.to_combo.setCurrentIndex(1)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setProperty("accent", True)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_save(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Name required", "Rule name cannot be empty.")
            return
        if self.from_combo.currentData() == self.to_combo.currentData():
            QMessageBox.warning(self, "Invalid columns", 'The "from" and "to" columns must be different.')
            return
        self.accept()

    def result_values(self) -> dict:
        schedule = self.schedule_editor.get_values()
        return {
            "name": self.name_edit.text().strip(),
            "enabled": self.enabled_check.isChecked(),
            "from_status": self.from_combo.currentData(),
            "to_status": self.to_combo.currentData(),
            **schedule,
        }


class CompleteTaskRuleDialog(QDialog):
    """Add/edit form for a "complete tasks" automation rule: at the
    scheduled time, every task currently in the chosen column is marked
    complete (they stay in that column - completion is independent of
    column, see set_task_completed())."""

    def __init__(self, columns: list, rule: dict = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Complete-Task Rule" if rule else "New Complete-Task Rule")
        self.resize(400, 380)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Rule Name"))
        self.name_edit = QLineEdit(rule["name"] if rule else "")
        layout.addWidget(self.name_edit)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked((rule["enabled"] == 1) if rule else True)
        layout.addWidget(self.enabled_check)

        layout.addWidget(QLabel("Schedule"))
        self.schedule_editor = ScheduleEditorWidget()
        if rule:
            self.schedule_editor.set_values(rule)
        layout.addWidget(self.schedule_editor)

        layout.addWidget(QLabel("Complete tasks in"))
        self.from_combo = QComboBox()
        for col in columns:
            self.from_combo.addItem(col["name"], col["status"])
        layout.addWidget(self.from_combo)

        if rule:
            from_idx = next((i for i, col in enumerate(columns) if col["status"] == rule["from_status"]), 0)
            self.from_combo.setCurrentIndex(from_idx)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setProperty("accent", True)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_save(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Name required", "Rule name cannot be empty.")
            return
        self.accept()

    def result_values(self) -> dict:
        schedule = self.schedule_editor.get_values()
        return {
            "name": self.name_edit.text().strip(),
            "enabled": self.enabled_check.isChecked(),
            "from_status": self.from_combo.currentData(),
            **schedule,
        }


def _describe_schedule(rule: dict) -> str:
    kind = rule["schedule_kind"]
    if kind == "once":
        return f"Once at {rule['schedule_datetime'] or '?'}"
    if kind == "hourly":
        return f"Every {rule['schedule_interval_days']} hour(s)"
    if kind == "daily":
        return f"Daily at {rule['schedule_time']}"
    if kind == "weekly":
        names = ScheduleEditorWidget.WEEKDAY_LABELS
        days = [names[int(d)] for d in rule["schedule_weekdays"].split(",") if d != ""]
        return f"Weekly on {', '.join(days) or '?'} at {rule['schedule_time']}"
    if kind == "interval":
        return f"Every {rule['schedule_interval_days']} day(s) at {rule['schedule_time']}"
    return kind


class ManageAutomationsDialog(QDialog):
    """Lists a board's automation rules (both create-task and move-task)
    with add/edit/delete/enable-toggle controls, mirroring
    ManageTemplatesDialog's immediate-write pattern."""

    def __init__(self, conn: sqlite3.Connection, board_id: str, columns: list, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.board_id = board_id
        self.columns = columns
        self.setWindowTitle("Manage Automation")
        self.resize(460, 480)

        layout = QVBoxLayout(self)

        self.rules_list = QListWidget()
        self.rules_list.itemChanged.connect(self._on_item_changed)
        self.rules_list.itemDoubleClicked.connect(lambda _item: self._edit_selected())
        layout.addWidget(self.rules_list, stretch=1)

        add_row = QHBoxLayout()
        add_create_btn = QPushButton("+ Create Task Rule")
        add_create_btn.clicked.connect(self._add_create_task_rule)
        add_row.addWidget(add_create_btn)
        add_move_btn = QPushButton("+ Move Tasks Rule")
        add_move_btn.clicked.connect(self._add_move_task_rule)
        add_row.addWidget(add_move_btn)
        add_complete_btn = QPushButton("+ Complete Task Rule")
        add_complete_btn.clicked.connect(self._add_complete_task_rule)
        add_row.addWidget(add_complete_btn)
        layout.addLayout(add_row)

        btn_row = QHBoxLayout()
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_selected)
        btn_row.addWidget(edit_btn)
        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet("color: #b00000;")
        delete_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self) -> None:
        self.rules_list.blockSignals(True)
        self.rules_list.clear()
        type_labels = {
            "create_task": "Create Task",
            "move_task": "Move Tasks",
            "complete_task": "Complete Tasks",
        }
        for rule in get_automation_rules(self.conn, self.board_id):
            type_label = type_labels.get(rule["rule_type"], rule["rule_type"])
            text = f'{rule["name"]}  ·  {type_label}  ·  {_describe_schedule(rule)}'
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if rule["enabled"] else Qt.Unchecked)
            item.setData(Qt.UserRole, rule["id"])
            self.rules_list.addItem(item)
        self.rules_list.blockSignals(False)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        rule_id = item.data(Qt.UserRole)
        set_automation_rule_enabled(self.conn, rule_id, item.checkState() == Qt.Checked)

    def _selected_rule_id(self):
        item = self.rules_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _current_templates(self) -> list:
        return get_templates(self.conn, self.board_id)

    def _add_create_task_rule(self) -> None:
        dialog = CreateTaskRuleDialog(self.columns, self._current_templates(), parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values()
        try:
            add_create_task_rule(
                self.conn, self.board_id, values["name"], values["enabled"],
                values["schedule_kind"], values["schedule_time"], values["schedule_weekdays"],
                values["schedule_interval_days"], values["schedule_datetime"],
                values["template_id"], values["task_title"], values["task_notes"], values["task_status"],
                values["task_joplin_link"], values["task_due_offset_days"], values["subtask_titles"],
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not add rule", str(e))
            return
        self.refresh()

    def _add_move_task_rule(self) -> None:
        if len(self.columns) < 2:
            QMessageBox.information(
                self, "Not enough columns", "This board needs at least two columns to move tasks between."
            )
            return
        dialog = MoveTaskRuleDialog(self.columns, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values()
        try:
            add_move_task_rule(
                self.conn, self.board_id, values["name"], values["enabled"],
                values["schedule_kind"], values["schedule_time"], values["schedule_weekdays"],
                values["schedule_interval_days"], values["schedule_datetime"],
                values["from_status"], values["to_status"],
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not add rule", str(e))
            return
        self.refresh()

    def _add_complete_task_rule(self) -> None:
        dialog = CompleteTaskRuleDialog(self.columns, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values()
        try:
            add_complete_task_rule(
                self.conn, self.board_id, values["name"], values["enabled"],
                values["schedule_kind"], values["schedule_time"], values["schedule_weekdays"],
                values["schedule_interval_days"], values["schedule_datetime"],
                values["from_status"],
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not add rule", str(e))
            return
        self.refresh()

    def _edit_selected(self) -> None:
        rule_id = self._selected_rule_id()
        if rule_id is None:
            return
        rule = get_automation_rule(self.conn, rule_id)
        if rule is None:
            return

        if rule["rule_type"] == "create_task":
            subtask_titles = [s["title"] for s in get_automation_rule_subtasks(self.conn, rule_id)]
            dialog = CreateTaskRuleDialog(
                self.columns, self._current_templates(),
                rule=rule, subtask_titles=subtask_titles, parent=self,
            )
            if dialog.exec() != QDialog.Accepted:
                return
            values = dialog.result_values()
            try:
                update_create_task_rule(
                    self.conn, rule_id, values["name"], values["enabled"],
                    values["schedule_kind"], values["schedule_time"], values["schedule_weekdays"],
                    values["schedule_interval_days"], values["schedule_datetime"],
                    values["template_id"], values["task_title"], values["task_notes"], values["task_status"],
                    values["task_joplin_link"], values["task_due_offset_days"], values["subtask_titles"],
                )
            except ValueError as e:
                QMessageBox.warning(self, "Could not update rule", str(e))
                return
        elif rule["rule_type"] == "move_task":
            dialog = MoveTaskRuleDialog(self.columns, rule=rule, parent=self)
            if dialog.exec() != QDialog.Accepted:
                return
            values = dialog.result_values()
            try:
                update_move_task_rule(
                    self.conn, rule_id, values["name"], values["enabled"],
                    values["schedule_kind"], values["schedule_time"], values["schedule_weekdays"],
                    values["schedule_interval_days"], values["schedule_datetime"],
                    values["from_status"], values["to_status"],
                )
            except ValueError as e:
                QMessageBox.warning(self, "Could not update rule", str(e))
                return
        else:
            dialog = CompleteTaskRuleDialog(self.columns, rule=rule, parent=self)
            if dialog.exec() != QDialog.Accepted:
                return
            values = dialog.result_values()
            try:
                update_complete_task_rule(
                    self.conn, rule_id, values["name"], values["enabled"],
                    values["schedule_kind"], values["schedule_time"], values["schedule_weekdays"],
                    values["schedule_interval_days"], values["schedule_datetime"],
                    values["from_status"],
                )
            except ValueError as e:
                QMessageBox.warning(self, "Could not update rule", str(e))
                return

        self.refresh()

    def _delete_selected(self) -> None:
        rule_id = self._selected_rule_id()
        if rule_id is None:
            return
        rule = get_automation_rule(self.conn, rule_id)
        if rule is None:
            return
        reply = QMessageBox.question(
            self, "Delete rule", f'Delete the "{rule["name"]}" rule?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        delete_automation_rule(self.conn, rule_id)
        self.refresh()


class BoardReportDialog(QDialog):
    """Read-only per-board stat sheet. No export yet - more report types
    and formats are expected to build on this first pass."""

    def __init__(self, conn: sqlite3.Connection, board_id: str, board_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Report — {board_name}")
        self.resize(360, 420)

        report = get_board_report(conn, board_id)

        layout = QVBoxLayout(self)

        total_label = QLabel(f"Total tasks: {report['total_tasks']}")
        total_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(total_label)

        layout.addWidget(QLabel("By column:"))
        for col in report["column_counts"]:
            layout.addWidget(QLabel(f'    {col["name"]}: {col["count"]}'))

        spacer = QLabel("")
        layout.addWidget(spacer)

        overdue_label = QLabel(f"Overdue tasks: {report['overdue_count']}")
        if report["overdue_count"]:
            overdue_label.setStyleSheet("color: #e05252;")
        layout.addWidget(overdue_label)

        layout.addWidget(QLabel(f"Created in the last 7 days: {report['recent_count']}"))

        if report["subtasks_total"]:
            pct = round(100 * report["subtasks_done"] / report["subtasks_total"])
            layout.addWidget(QLabel(
                f"Subtasks done: {report['subtasks_done']}/{report['subtasks_total']} ({pct}%)"
            ))
        else:
            layout.addWidget(QLabel("Subtasks done: —"))

        layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)


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
    around on its own.

    Reused as-is by both the Boards feature (KanbanBoard) and the Projects
    feature (ProjectKanbanWidget) - each "board" controller sets a
    BOARD_KIND class attribute ("boards"/"projects"), and _same_kind()
    below refuses drops across that boundary. Nothing about that crossing
    can normally happen (the two features are never both visible at once,
    since main() only ever shows one page of its QStackedWidget), but the
    check is cheap and directly serves the Projects issue's isolation
    requirement, so it's here as defense in depth."""

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
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.itemChanged.connect(self._on_item_changed)

    def _on_double_click(self, item):
        task_id = item.data(Qt.UserRole)
        self.board.edit_task(task_id)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        task_id = item.data(Qt.UserRole)
        self.board.handle_set_completed(task_id, item.checkState() == Qt.Checked)

    def _on_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        self.setCurrentItem(item)
        task_id = item.data(Qt.UserRole)
        self.board.show_move_task_menu(task_id, self.status, self.mapToGlobal(pos))

    def _same_kind(self, source) -> bool:
        return (
            isinstance(source, TaskListWidget)
            and source is not self
            and getattr(source.board, "BOARD_KIND", None) == getattr(self.board, "BOARD_KIND", None)
        )

    def dragEnterEvent(self, event):
        source = event.source()
        if self._same_kind(source):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        source = event.source()
        if self._same_kind(source):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        source = event.source()
        if not self._same_kind(source):
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

        add_task_btn = QPushButton("+")
        add_task_btn.setFixedWidth(26)
        add_task_btn.setProperty("compact", True)
        add_task_btn.setToolTip("Add a task to this column")
        add_task_btn.clicked.connect(lambda: self.board.add_task(target_status=self.status))
        header_row.addWidget(add_task_btn)

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

        self.projects_list_layout = QVBoxLayout()
        self.projects_list_layout.setContentsMargins(0, 0, 0, 0)
        self.projects_list_layout.setSpacing(0)
        layout.addLayout(self.projects_list_layout)

        layout.addStretch()

    def refresh(self, boards, current_board_id, projects) -> None:
        while self.boards_list_layout.count():
            item = self.boards_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # hide() first: takeAt() stops the layout from managing
                # the widget, but doesn't hide it - left alone, it stays
                # visibly painted at its last position until deleteLater()
                # actually runs, which isn't guaranteed before the next
                # repaint (seen as literal ghost duplicate cells in the
                # Calendar view under fast navigation).
                widget.hide()
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

        self._refresh_projects(projects)

    def _refresh_projects(self, projects) -> None:
        """Same row styling as the boards list above, but there's no
        "current project" concept to highlight - selecting a project
        switches the whole app view (Boards <-> Projects) rather than
        selecting within this panel like a board does."""
        while self.projects_list_layout.count():
            item = self.projects_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # hide() first: takeAt() stops the layout from managing
                # the widget, but doesn't hide it - left alone, it stays
                # visibly painted at its last position until deleteLater()
                # actually runs, which isn't guaranteed before the next
                # repaint (seen as literal ghost duplicate cells in the
                # Calendar view under fast navigation).
                widget.hide()
                widget.deleteLater()

        flat_btn_qss = (
            "QPushButton {{ background: transparent; border: none; {extra} }}"
            "QPushButton:hover {{ background: transparent; {hover_extra} }}"
            "QPushButton:pressed {{ background: transparent; }}"
        )

        for p in projects:
            row_widget = QWidget()
            row_widget.setObjectName("projectRow")
            row_widget.setAttribute(Qt.WA_StyledBackground, True)
            row_widget.setStyleSheet(
                "QWidget#projectRow { background-color: transparent; border-radius: 8px; }"
                "QWidget#projectRow:hover { background-color: #2c2e33; }"
            )
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(4, 2, 4, 2)
            row.setSpacing(0)

            select_btn = QPushButton("     " + p["name"])
            select_btn.setFlat(True)
            select_btn.setStyleSheet(
                flat_btn_qss.format(
                    extra="text-align: left; padding: 8px 4px 8px 12px; color: #e8e8e8;",
                    hover_extra="",
                )
            )
            select_btn.clicked.connect(
                lambda checked=False, project_id=p["id"]: self.board.select_project_from_panel(project_id)
            )
            row.addWidget(select_btn, stretch=1)

            edit_btn = QPushButton("⋮")
            edit_btn.setFixedWidth(24)
            edit_btn.setStyleSheet(
                flat_btn_qss.format(extra="color: #9a9a9a;", hover_extra="color: #e8e8e8;")
            )
            edit_btn.setToolTip("Edit project")

            def make_menu(checked=False, project_id=p["id"], anchor_btn=edit_btn):
                menu = QMenu(anchor_btn)
                move_up = menu.addAction("Move Up")
                move_down = menu.addAction("Move Down")
                rename = menu.addAction("Rename")
                menu.addSeparator()
                delete = menu.addAction("Delete")

                move_up.triggered.connect(lambda: self.board.move_project_ui(project_id, -1))
                move_down.triggered.connect(lambda: self.board.move_project_ui(project_id, 1))
                rename.triggered.connect(lambda: self.board.rename_project_ui(project_id))
                delete.triggered.connect(lambda: self.board.delete_project_ui(project_id))

                menu.exec(anchor_btn.mapToGlobal(anchor_btn.rect().bottomLeft()))

            edit_btn.clicked.connect(make_menu)
            row.addWidget(edit_btn)

            self.projects_list_layout.addWidget(row_widget)

        if not projects:
            empty = QLabel("No projects yet")
            empty.setStyleSheet("color: #6e6e6e; padding: 4px 16px;")
            self.projects_list_layout.addWidget(empty)

        add_project_btn = QPushButton("+ Add Project")
        add_project_btn.setFlat(True)
        add_project_btn.setStyleSheet("text-align: left; padding: 8px 16px; color: #8b93ff; border: none;")
        add_project_btn.clicked.connect(self.board.add_project_ui)
        self.projects_list_layout.addWidget(add_project_btn)


class KanbanBoard(QWidget):
    # Used by TaskListWidget's drag-and-drop guard to keep Boards and
    # Projects cards from being dragged into each other's columns, even
    # though both features reuse the same ColumnWidget/TaskListWidget
    # classes - see ProjectKanbanWidget.BOARD_KIND.
    BOARD_KIND = "boards"

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.current_board_id = None
        self.columns = {}          # status -> ColumnWidget, for the current board
        self._columns_cache = []   # last-fetched column dicts for the current board
        self._board_panel_open = False
        self._board_panel_anim = None
        self._quick_add_dialog = None
        self.show_completed = False
        self._projects_cache = get_projects(self.conn)
        self._open_project_callback = None

        outer = QVBoxLayout(self)

        toolbar = QHBoxLayout()

        self.board_menu_btn = QPushButton("☰")
        self.board_menu_btn.setFixedWidth(36)
        self.board_menu_btn.setToolTip("Switch board, or add/rename/reorder/delete boards")
        self.board_menu_btn.clicked.connect(self.toggle_board_panel)
        toolbar.addWidget(self.board_menu_btn)

        self.add_task_btn = QToolButton()
        self.add_task_btn.setText("+ New Task")
        self.add_task_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.add_task_btn.setProperty("accent", True)
        self.add_task_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.add_task_btn.clicked.connect(lambda: self.add_task())
        self.new_task_menu = QMenu(self.add_task_btn)
        self.add_task_btn.setMenu(self.new_task_menu)
        toolbar.addWidget(self.add_task_btn)

        self.automation_btn = QPushButton("Automation")
        self.automation_btn.setToolTip("Manage scheduled task-creation and column-move rules for this board")
        self.automation_btn.clicked.connect(self.manage_automations_ui)
        toolbar.addWidget(self.automation_btn)

        self.report_btn = QPushButton("Report")
        self.report_btn.setToolTip("View a summary report for this board")
        self.report_btn.clicked.connect(self.show_report_ui)
        toolbar.addWidget(self.report_btn)

        toolbar.addStretch()

        self.add_col_btn = QPushButton("+ Column")
        self.add_col_btn.clicked.connect(self.add_column_ui)
        self.add_col_btn.hide()
        toolbar.addWidget(self.add_col_btn)

        self.show_completed_btn = QPushButton("Show Completed")
        self.show_completed_btn.setCheckable(True)
        self.show_completed_btn.setToolTip(
            "Completed tasks are hidden from their column by default - toggle this to review them"
        )
        self.show_completed_btn.toggled.connect(self._on_show_completed_toggled)
        toolbar.addWidget(self.show_completed_btn)

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

        # Automation rules only ever run while Kanvas is open (no background
        # service) - this timer plus one immediate check below is the whole
        # execution model. See the "Task automation" section in the data
        # layer for how a rule catches up after being closed past its
        # scheduled time.
        self._automation_timer = QTimer(self)
        self._automation_timer.timeout.connect(self._run_due_automations)
        self._automation_timer.start(60_000)
        self._run_due_automations()

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
        self.rebuild_projects_cache()
        self.board_panel.refresh(self._boards_cache, self.current_board_id, self._projects_cache)

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

    # -- Projects entry point (kept decoupled from ProjectsHub itself -
    # the callback is injected by main(), so KanbanBoard never has to
    # import or know about Projects UI classes) -------------------------

    def set_open_project_callback(self, callback) -> None:
        self._open_project_callback = callback

    def select_project_from_panel(self, project_id: str) -> None:
        if self._open_project_callback is not None:
            self._open_project_callback(project_id)
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
                # hide() first: takeAt() stops the layout from managing
                # the widget, but doesn't hide it - left alone, it stays
                # visibly painted at its last position until deleteLater()
                # actually runs, which isn't guaranteed before the next
                # repaint (seen as literal ghost duplicate cells in the
                # Calendar view under fast navigation).
                widget.hide()
                widget.deleteLater()

        self.columns = {}
        self._columns_cache = get_columns(self.conn, self.current_board_id) if self.current_board_id else []
        for col in self._columns_cache:
            widget = ColumnWidget(col, self)
            widget.set_edit_controls_visible(self.edit_board_btn.isChecked())
            self.columns[col["status"]] = widget
            self.columns_layout.addWidget(widget)

        self._rebuild_new_task_menu()
        self.refresh()

    def _on_edit_board_toggled(self, checked: bool) -> None:
        self.edit_board_btn.setText("Done Editing" if checked else "Edit Board")
        self.add_col_btn.setVisible(checked)
        for col_widget in self.columns.values():
            col_widget.set_edit_controls_visible(checked)

    def _on_show_completed_toggled(self, checked: bool) -> None:
        self.show_completed = checked
        self.refresh()

    # -- lightweight refresh (task list contents only) ------------------

    def refresh(self) -> None:
        for status, col_widget in self.columns.items():
            list_widget = col_widget.list_widget
            list_widget.blockSignals(True)
            list_widget.clear()

            all_tasks = get_tasks_by_status(self.conn, self.current_board_id, status)
            tasks = all_tasks if self.show_completed else [t for t in all_tasks if not t["completed"]]

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
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if task["completed"] else Qt.Unchecked)
                if task["completed"]:
                    font = item.font()
                    font.setStrikeOut(True)
                    item.setFont(font)
                    item.setForeground(QColor("#767676"))

                tooltip_lines = []
                if task.get("due_date"):
                    tooltip_lines.append(f"Due: {task['due_date']}")
                if task.get("notes"):
                    tooltip_lines.append(task["notes"])
                if task.get("joplin_link"):
                    tooltip_lines.append(f"Joplin: {task['joplin_link']}")
                if tooltip_lines:
                    item.setToolTip("\n".join(tooltip_lines))

                list_widget.addItem(item)

            list_widget.blockSignals(False)
            col_widget.set_count(len(tasks))

    # -- task operations ------------------------------------------------

    def add_task(self, template: dict = None, target_status: str = None) -> None:
        if not self._columns_cache:
            QMessageBox.information(self, "No columns", "Add a column first.")
            return

        default_status = target_status or get_default_new_task_status(self.conn, self.current_board_id)

        prefill = None
        template_subtask_titles = []
        if template is not None:
            due_date = ""
            if template.get("due_offset_days") is not None:
                due_date = (date.today() + timedelta(days=template["due_offset_days"])).strftime("%Y-%m-%d")
            prefill = {
                "title": template["title"],
                "notes": template["notes"],
                "status": template["status"],
                "due_date": due_date,
                "joplin_link": template["joplin_link"],
            }
            template_subtask_titles = [s["title"] for s in get_template_subtasks(self.conn, template["id"])]

        dialog = NewTaskDialog(self._columns_cache, default_status, self, prefill=prefill)
        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.result_values()
        task = add_task(self.conn, self.current_board_id, values["title"], values["notes"], values["status"])
        if values["due_date"] or values["joplin_link"]:
            update_task(
                self.conn, task["id"], values["title"], values["notes"],
                values["due_date"], values["joplin_link"],
            )
        for subtask_title in template_subtask_titles:
            add_subtask(self.conn, task["id"], subtask_title)
        self.refresh()

    # -- templates (per-board presets that prefill the New Task dialog) --

    def _rebuild_new_task_menu(self) -> None:
        self.new_task_menu.clear()
        templates = get_templates(self.conn, self.current_board_id) if self.current_board_id else []
        if not templates:
            empty_action = self.new_task_menu.addAction("No templates yet")
            empty_action.setEnabled(False)
        else:
            for tpl in templates:
                action = QAction(tpl["title"], self.new_task_menu)
                action.triggered.connect(lambda checked=False, t=tpl: self.add_task(template=t))
                self.new_task_menu.addAction(action)
        self.new_task_menu.addSeparator()
        manage_action = self.new_task_menu.addAction("Manage Templates…")
        manage_action.triggered.connect(self.manage_templates_ui)

    def manage_templates_ui(self) -> None:
        dialog = ManageTemplatesDialog(self.conn, self.current_board_id, self._columns_cache, self)
        dialog.exec()
        self._rebuild_new_task_menu()

    # -- automation (scheduled task-creation / column-move rules) --------

    def manage_automations_ui(self) -> None:
        if not self._columns_cache:
            QMessageBox.information(self, "No columns", "Add a column first.")
            return
        dialog = ManageAutomationsDialog(self.conn, self.current_board_id, self._columns_cache, self)
        dialog.exec()

    def show_report_ui(self) -> None:
        if not self.current_board_id:
            return
        dialog = BoardReportDialog(self.conn, self.current_board_id, self._current_board_name(), self)
        dialog.exec()

    def _run_due_automations(self) -> None:
        affected_board_ids = run_due_automation_rules(self.conn)
        if self.current_board_id in affected_board_ids:
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
        update_task(
            self.conn, task_id, values["title"], values["notes"],
            values["due_date"], values["joplin_link"],
        )
        if values["status"] and values["status"] != task["status"]:
            move_task(self.conn, task_id, values["status"])
        if values["completed"] != bool(task["completed"]):
            set_task_completed(self.conn, task_id, values["completed"])
        self.refresh()

    def handle_move(self, task_id: str, new_status: str) -> None:
        task = get_task(self.conn, task_id)
        if not task or task["status"] == new_status:
            return
        move_task(self.conn, task_id, new_status)
        self.refresh()

    def handle_set_completed(self, task_id: str, completed: bool) -> None:
        set_task_completed(self.conn, task_id, completed)
        # Deferred: this fires from inside the card's own checkbox-toggle
        # signal, and refresh() rebuilds (clears) that same list widget -
        # doing that synchronously would modify the list mid-signal.
        QTimer.singleShot(0, self.refresh)

    def show_move_task_menu(self, task_id: str, current_status: str, global_pos) -> None:
        menu = QMenu(self)
        other_columns = [col for col in self._columns_cache if col["status"] != current_status]
        if not other_columns:
            no_columns_action = menu.addAction("No other columns")
            no_columns_action.setEnabled(False)
        else:
            for col in other_columns:
                action = menu.addAction(f"Move to {col['name']}")
                action.triggered.connect(lambda checked=False, s=col["status"]: self.handle_move(task_id, s))
        menu.exec(global_pos)

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
        self.board_panel.refresh(self._boards_cache, self.current_board_id, self._projects_cache)

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

    # -- project operations (Projects side-panel section) ----------------
    # Mirrors the board operations above 1:1 against the project_*
    # data layer - the only difference is that "selecting" a project
    # switches the whole app view (see select_project_from_panel) rather
    # than swapping content within this same widget.

    def rebuild_projects_cache(self) -> None:
        self._projects_cache = get_projects(self.conn)

    def add_project_ui(self) -> None:
        name, ok = QInputDialog.getText(self, "New Project", "Project name:")
        if not ok or not name.strip():
            return
        try:
            project = add_project(self.conn, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not add project", str(e))
            return
        self.rebuild_projects_cache()
        self._sync_board_panel()
        self.select_project_from_panel(project["id"])

    def rename_project_ui(self, project_id: str) -> None:
        project = get_project(self.conn, project_id)
        if not project:
            return
        name, ok = QInputDialog.getText(self, "Rename Project", "Project name:", text=project["name"])
        if not ok or not name.strip():
            return
        try:
            rename_project(self.conn, project_id, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename project", str(e))
            return
        self.rebuild_projects_cache()
        self._sync_board_panel()

    def delete_project_ui(self, project_id: str) -> None:
        project = get_project(self.conn, project_id)
        if not project:
            return
        task_count = get_project_task_count(self.conn, project_id)
        message = f'Delete the project "{project["name"]}"? This deletes its Main Board and every sub-board below it.'
        if task_count:
            message += f" This will permanently delete {task_count} task(s) across all of its boards."
        reply = QMessageBox.question(self, "Delete project", message, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        delete_project(self.conn, project_id)
        self.rebuild_projects_cache()
        self._sync_board_panel()

    def move_project_ui(self, project_id: str, direction: int) -> None:
        move_project(self.conn, project_id, direction)
        self.rebuild_projects_cache()
        self._sync_board_panel()

    # -- global quick-add shortcut (Ctrl+Space) ---------------------------

    def show_quick_add_dialog(self) -> None:
        """Slot for GlobalShortcutBridge.triggered and the tray menu's
        Quick Add action. Deliberately independent of the main board
        window's visibility or position - the popup shows on its own,
        wherever the cursor currently is, whether the board is open,
        minimized, or hidden in the system tray. This sidesteps the
        window-restore-to-wrong-display issue entirely (#9) rather than
        chasing it: there's no window to un-minimize/reposition here."""
        if self._quick_add_dialog is not None:
            self._quick_add_dialog.raise_()
            self._quick_add_dialog.activateWindow()
            return

        boards = get_boards(self.conn)
        if not boards:
            return

        dialog = QuickAddDialog(self.conn, boards, self.current_board_id, self)
        self._quick_add_dialog = dialog
        _move_to_cursor_screen(dialog)
        dialog.raise_()
        dialog.activateWindow()
        dialog.exec()
        self._quick_add_dialog = None

        if self.current_board_id in dialog.added_board_ids:
            self.refresh()


# ---------------------------------------------------------------------------
# Projects UI. Everything below is new, and self-contained: it only reaches
# into the code above for the generic app scaffolding this file already
# shares everywhere (Qt widgets, APP_STYLESHEET, ColumnWidget/TaskListWidget
# reused via duck-typing - see ProjectKanbanWidget). No class here holds a
# reference to KanbanBoard or any of its state, and vice versa.
# ---------------------------------------------------------------------------

def default_project_task_start_date(conn: sqlite3.Connection, board_id: str, exclude_task_id: str = None) -> QDate:
    """Suggested default for a new/unset start date: right where the
    last already-scheduled task on this board leaves off (its due_date),
    so tasks chain sequentially instead of every new task defaulting to
    today. Falls back to today when nothing on the board has a due_date
    yet. QDate-returning (unlike the plain-string data layer functions
    it wraps), so it lives here in the UI section rather than above."""
    latest_due = get_latest_due_date(conn, board_id, exclude_task_id=exclude_task_id)
    parsed = QDate.fromString(latest_due, "yyyy-MM-dd") if latest_due else None
    return parsed if parsed and parsed.isValid() else QDate.currentDate()


def default_project_task_due_date(start_date: QDate) -> QDate:
    """Suggested default due date: the day after start. Pure QDate math -
    unlike default_project_task_start_date, doesn't need to look at the
    board's other tasks."""
    return start_date.addDays(1)


class BreadcrumbBar(QWidget):
    """The "Project Name > Task A > Task A.2" navigation strip at the top
    of a Projects board view. Truncates the middle into a "..." popup menu
    (rather than just eliding text) when the full path doesn't fit, so
    every ancestor board stays reachable even when collapsed."""

    crumb_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._crumbs = []
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(2)

    def set_crumbs(self, crumbs: list) -> None:
        self._crumbs = crumbs
        self._rebuild()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rebuild()

    def _rebuild(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # hide() first: takeAt() stops the layout from managing
                # the widget, but doesn't hide it - left alone, it stays
                # visibly painted at its last position until deleteLater()
                # actually runs, which isn't guaranteed before the next
                # repaint (seen as literal ghost duplicate cells in the
                # Calendar view under fast navigation).
                widget.hide()
                widget.deleteLater()

        crumbs = self._crumbs
        if not crumbs:
            return

        metrics = self.fontMetrics()
        CHROME_PER_CRUMB = 40  # rough padding/separator allowance per segment
        total_width = sum(metrics.horizontalAdvance(c["label"]) + CHROME_PER_CRUMB for c in crumbs)

        display = crumbs
        hidden = []
        if total_width > max(self.width(), 1) and len(crumbs) > 2:
            hidden = crumbs[1:-1]
            display = [crumbs[0], None, crumbs[-1]]

        current = crumbs[-1]
        for node in display:
            if node is None:
                ellipsis_btn = QToolButton()
                ellipsis_btn.setText("…")
                ellipsis_btn.setAutoRaise(True)
                ellipsis_btn.setStyleSheet(
                    "QToolButton { color: #8a8a8a; border: none; background: transparent; }"
                )
                menu = QMenu(ellipsis_btn)
                for hidden_crumb in hidden:
                    action = menu.addAction(hidden_crumb["label"])
                    action.triggered.connect(
                        lambda checked=False, bid=hidden_crumb["board_id"]: self.crumb_clicked.emit(bid)
                    )
                ellipsis_btn.setMenu(menu)
                ellipsis_btn.setPopupMode(QToolButton.InstantPopup)
                self._row.addWidget(ellipsis_btn)
                self._add_separator()
                continue

            is_current = node is current
            btn = QPushButton(node["label"])
            btn.setFlat(True)
            if is_current:
                btn.setEnabled(False)
                btn.setStyleSheet(
                    "QPushButton { font-weight: bold; color: #e8e8e8; border: none; "
                    "background: transparent; text-align: left; padding: 2px; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { color: #8b93ff; border: none; background: transparent; "
                    "text-align: left; padding: 2px; }"
                )
                btn.clicked.connect(lambda checked=False, bid=node["board_id"]: self.crumb_clicked.emit(bid))
            self._row.addWidget(btn)
            if not is_current:
                self._add_separator()

        self._row.addStretch()

    def _add_separator(self) -> None:
        sep = QLabel("›")
        sep.setStyleSheet("color: #6e6e6e; padding: 0 2px;")
        self._row.addWidget(sep)


class NewProjectTaskDialog(QDialog):
    """Task-creation dialog for a Projects board - same shape as
    NewTaskDialog, plus a second date (start_date, for the Gantt/Calendar
    views a later phase adds) and "link" instead of "joplin_link"."""

    def __init__(self, columns: list, default_column_id, parent=None, default_start_date: QDate = None):
        super().__init__(parent)
        self.setWindowTitle("New Task")
        self.resize(420, 460)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit()
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Column"))
        self.column_combo = QComboBox()
        for col in columns:
            self.column_combo.addItem(col["name"], col["id"])
        default_idx = next((i for i, c in enumerate(columns) if c["id"] == default_column_id), 0)
        self.column_combo.setCurrentIndex(default_idx)
        layout.addWidget(self.column_combo)

        layout.addWidget(QLabel("Start Date"))
        start_default = default_start_date or QDate.currentDate()
        self.start_date_check, self.start_date_edit = self._build_date_row(layout, start_default)

        layout.addWidget(QLabel("Due Date"))
        self.due_date_check, self.due_date_edit = self._build_date_row(
            layout, default_project_task_due_date(start_default)
        )

        layout.addWidget(QLabel("Link"))
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("https://...")
        layout.addWidget(self.link_edit)

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
        add_btn.clicked.connect(self._on_add)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

        self.title_edit.setFocus()

        # Due tracks Start (start+1) until the user actually edits Due
        # themselves - see _sync_due_date_default's docstring.
        self._due_date_user_edited = False
        self.start_date_edit.dateChanged.connect(self._sync_due_date_default)
        self.start_date_check.toggled.connect(self._sync_due_date_default)
        self.due_date_edit.dateChanged.connect(self._mark_due_date_edited)

    def _sync_due_date_default(self, *_args) -> None:
        """Keeps Due tracking start+1 as Start changes, but only until the
        user has actually edited Due themselves - checked via a dirty
        flag set by _mark_due_date_edited, which blockSignals here is
        careful not to trigger on this method's own programmatic update."""
        if self._due_date_user_edited:
            return
        self.due_date_edit.blockSignals(True)
        self.due_date_edit.setDate(default_project_task_due_date(self.start_date_edit.date()))
        self.due_date_edit.blockSignals(False)

    def _mark_due_date_edited(self, *_args) -> None:
        self._due_date_user_edited = True

    @staticmethod
    def _build_date_row(layout, default_date: QDate):
        row = QHBoxLayout()
        check = QCheckBox("Set")
        row.addWidget(check)
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd")
        edit.setDate(default_date)
        edit.setEnabled(False)
        check.toggled.connect(edit.setEnabled)
        row.addWidget(edit, stretch=1)
        layout.addLayout(row)
        return check, edit

    def _on_add(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return
        self.accept()

    def result_values(self) -> dict:
        return {
            "title": self.title_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "column_id": self.column_combo.currentData(),
            "start_date": self.start_date_edit.date().toString("yyyy-MM-dd") if self.start_date_check.isChecked() else "",
            "due_date": self.due_date_edit.date().toString("yyyy-MM-dd") if self.due_date_check.isChecked() else "",
            "link": self.link_edit.text().strip(),
        }


class BulkAddProjectTasksDialog(QDialog):
    """Quickly add several bare tasks (title only, no dates/notes/link)
    to one column at once - type or paste one title per line. For
    scheduling them, use the inline Start/Due editing in the List/
    Missing Dates tables afterward rather than the full New Task dialog
    per task."""

    def __init__(self, columns: list, default_column_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Multiple Tasks")
        self.resize(420, 420)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Column"))
        self.column_combo = QComboBox()
        for col in columns:
            self.column_combo.addItem(col["name"], col["id"])
        default_idx = next((i for i, c in enumerate(columns) if c["id"] == default_column_id), 0)
        self.column_combo.setCurrentIndex(default_idx)
        layout.addWidget(self.column_combo)

        layout.addWidget(QLabel("Task Titles"))
        self.titles_edit = QTextEdit()
        self.titles_edit.setPlaceholderText("One task per line...")
        layout.addWidget(self.titles_edit, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        add_btn = QPushButton("Add Tasks")
        add_btn.setProperty("accent", True)
        add_btn.setDefault(True)
        add_btn.clicked.connect(self._on_add)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

        self.titles_edit.setFocus()

    def _titles(self) -> list:
        return [line.strip() for line in self.titles_edit.toPlainText().splitlines() if line.strip()]

    def _on_add(self) -> None:
        if not self._titles():
            QMessageBox.warning(self, "Titles required", "Enter at least one task title.")
            return
        self.accept()

    def result_values(self) -> dict:
        return {
            "column_id": self.column_combo.currentData(),
            "titles": self._titles(),
        }


class ProjectTaskCardDialog(QDialog):
    """Full card view for a Projects task - same shape/rationale as
    TaskCardDialog (subtasks write straight through, everything else only
    applied on Save), plus a start date and a Create/Open Sub-board
    action. That action sets subboard_action_requested and accepts the
    dialog (not a separate reject path) so any field edits made before
    clicking it are still saved by the caller."""

    def __init__(self, conn: sqlite3.Connection, task: dict, columns: list, subboard, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.task_id = task["id"]
        self.setWindowTitle(task["title"])
        self.resize(440, 700)
        self.delete_requested = False
        self.subboard_action_requested = False

        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, stretch=1)

        details_tab = QWidget()
        details_layout = QVBoxLayout(details_tab)

        details_layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(task["title"])
        details_layout.addWidget(self.title_edit)

        details_layout.addWidget(QLabel("Column"))
        self.column_combo = QComboBox()
        for col in columns:
            self.column_combo.addItem(col["name"], col["id"])
        current_idx = next((i for i, c in enumerate(columns) if c["id"] == task["column_id"]), 0)
        self.column_combo.setCurrentIndex(current_idx)
        details_layout.addWidget(self.column_combo)

        self.completed_check = QCheckBox("Completed")
        self.completed_check.setChecked(bool(task.get("completed")))
        details_layout.addWidget(self.completed_check)

        details_layout.addWidget(QLabel("Start Date"))
        default_start = default_project_task_start_date(conn, task["board_id"], exclude_task_id=task["id"])
        self.start_date_check, self.start_date_edit = self._build_date_row(
            details_layout, task.get("start_date"), default_start
        )

        details_layout.addWidget(QLabel("Due Date"))
        self.due_date_check, self.due_date_edit = self._build_date_row(
            details_layout, task.get("due_date"), default_project_task_due_date(self.start_date_edit.date())
        )

        # Due tracks Start (start+1) until manually edited - but an
        # already-saved due_date counts as "already edited" so it's never
        # silently overwritten by a later Start change.
        self._due_date_user_edited = self.due_date_check.isChecked()
        self.start_date_edit.dateChanged.connect(self._sync_due_date_default)
        self.start_date_check.toggled.connect(self._sync_due_date_default)
        self.due_date_edit.dateChanged.connect(self._mark_due_date_edited)

        details_layout.addWidget(QLabel("Link"))
        self.link_edit = QLineEdit(task.get("link") or "")
        self.link_edit.setPlaceholderText("https://...")
        details_layout.addWidget(self.link_edit)

        details_layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(task.get("notes") or "")
        details_layout.addWidget(self.notes_edit, stretch=1)

        details_layout.addWidget(QLabel("Subtasks"))
        self.subtasks_list = QListWidget()
        self.subtasks_list.itemChanged.connect(self._on_subtask_item_changed)
        details_layout.addWidget(self.subtasks_list, stretch=1)

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
        details_layout.addLayout(subtask_add_row)
        self._refresh_subtasks()

        subboard_row = QHBoxLayout()
        if subboard is not None:
            done, total = get_subtree_task_progress(self.conn, subboard["id"])
            subboard_btn = QPushButton(f"Open Sub-board (⧉ {done}/{total})")
        else:
            subboard_btn = QPushButton("Create Sub-board")
        subboard_btn.clicked.connect(self._on_subboard_action)
        subboard_row.addWidget(subboard_btn)
        details_layout.addLayout(subboard_row)

        meta_label = QLabel(f"Created {task['created_at']}")
        meta_label.setStyleSheet("color: #888888; font-size: 11px;")
        details_layout.addWidget(meta_label)

        self.tabs.addTab(details_tab, "Details")

        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(3)
        self.history_table.setHorizontalHeaderLabels(["Date", "Action", "Description"])
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        action_labels = dict(ACTIVITY_ACTION_TYPES)
        history_entries = get_activity_log_entries_for_task(conn, task["id"])
        self.history_table.setRowCount(len(history_entries))
        for row, entry in enumerate(history_entries):
            self.history_table.setItem(row, 0, QTableWidgetItem(entry["created_at"]))
            self.history_table.setItem(
                row, 1, QTableWidgetItem(action_labels.get(entry["action_type"], entry["action_type"]))
            )
            self.history_table.setItem(row, 2, QTableWidgetItem(entry["description"]))
        history_layout.addWidget(self.history_table)
        self.tabs.addTab(history_tab, "History")

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
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def _build_date_row(layout, existing_value, default_date: QDate):
        row = QHBoxLayout()
        check = QCheckBox("Set")
        row.addWidget(check)
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd")
        existing = QDate.fromString(existing_value or "", "yyyy-MM-dd")
        check.setChecked(existing.isValid())
        edit.setDate(existing if existing.isValid() else default_date)
        edit.setEnabled(existing.isValid())
        check.toggled.connect(edit.setEnabled)
        row.addWidget(edit, stretch=1)
        layout.addLayout(row)
        return check, edit

    def _sync_due_date_default(self, *_args) -> None:
        """See NewProjectTaskDialog's identical method - kept as a
        separate copy since these are two unrelated QDialog subclasses,
        not a shared base."""
        if self._due_date_user_edited:
            return
        self.due_date_edit.blockSignals(True)
        self.due_date_edit.setDate(default_project_task_due_date(self.start_date_edit.date()))
        self.due_date_edit.blockSignals(False)

    def _mark_due_date_edited(self, *_args) -> None:
        self._due_date_user_edited = True

    def _on_save(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return
        self.accept()

    def _on_delete(self) -> None:
        self.delete_requested = True
        self.reject()

    def _on_subboard_action(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Title required", "Task title cannot be empty.")
            return
        self.subboard_action_requested = True
        self.accept()

    # -- subtasks (write straight to the database, same rationale as
    # TaskCardDialog's own subtask handling) -----------------------------

    def _refresh_subtasks(self) -> None:
        self.subtasks_list.blockSignals(True)
        self.subtasks_list.clear()
        for sub in get_project_subtasks(self.conn, self.task_id):
            item = QListWidgetItem(sub["title"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if sub["done"] else Qt.Unchecked)
            item.setData(Qt.UserRole, sub["id"])
            self.subtasks_list.addItem(item)
        self.subtasks_list.blockSignals(False)

    def _on_subtask_item_changed(self, item: QListWidgetItem) -> None:
        set_project_subtask_done(self.conn, item.data(Qt.UserRole), item.checkState() == Qt.Checked)

    def _add_subtask(self) -> None:
        title = self.new_subtask_edit.text().strip()
        if not title:
            return
        add_project_subtask(self.conn, self.task_id, title)
        self.new_subtask_edit.clear()
        self._refresh_subtasks()

    def _delete_selected_subtask(self) -> None:
        item = self.subtasks_list.currentItem()
        if item is None:
            return
        delete_project_subtask(self.conn, item.data(Qt.UserRole))
        self._refresh_subtasks()

    def result_values(self) -> dict:
        return {
            "title": self.title_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "column_id": self.column_combo.currentData(),
            "start_date": self.start_date_edit.date().toString("yyyy-MM-dd") if self.start_date_check.isChecked() else "",
            "due_date": self.due_date_edit.date().toString("yyyy-MM-dd") if self.due_date_check.isChecked() else "",
            "link": self.link_edit.text().strip(),
            "completed": self.completed_check.isChecked(),
        }


class ProjectKanbanWidget(QWidget):
    """The Kanban page of a Projects board view. Reuses ColumnWidget and
    TaskListWidget unmodified (see their docstrings above) by duck-typing
    the callback methods they expect on "board", fed project_columns rows
    reshaped as {"status": column_id, "name": ...} - status here is just
    an opaque identity, not a real slug."""

    # See KanbanBoard.BOARD_KIND.
    BOARD_KIND = "projects"

    def __init__(self, conn: sqlite3.Connection, hub, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.hub = hub
        self.board_id = None
        self.columns = {}          # project_column id -> ColumnWidget
        self._columns_cache = []
        self._edit_mode = False
        self.show_completed = False

        self.columns_layout = QHBoxLayout(self)
        self.columns_layout.setSpacing(12)

    # -- board load / structural rebuild ---------------------------------

    def load_board(self, board_id: str) -> None:
        self.board_id = board_id
        self.rebuild_columns()

    def rebuild_columns(self) -> None:
        while self.columns_layout.count():
            item = self.columns_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # hide() first: takeAt() stops the layout from managing
                # the widget, but doesn't hide it - left alone, it stays
                # visibly painted at its last position until deleteLater()
                # actually runs, which isn't guaranteed before the next
                # repaint (seen as literal ghost duplicate cells in the
                # Calendar view under fast navigation).
                widget.hide()
                widget.deleteLater()

        self.columns = {}
        self._columns_cache = get_project_columns(self.conn, self.board_id) if self.board_id else []
        for col in self._columns_cache:
            adapted = {"status": col["id"], "name": col["name"]}
            widget = ColumnWidget(adapted, self)
            widget.set_edit_controls_visible(self._edit_mode)
            self.columns[col["id"]] = widget
            self.columns_layout.addWidget(widget)

        self.refresh()

    def set_edit_mode(self, enabled: bool) -> None:
        self._edit_mode = enabled
        for widget in self.columns.values():
            widget.set_edit_controls_visible(enabled)

    def set_show_completed(self, enabled: bool) -> None:
        self.show_completed = enabled
        self.refresh()

    # -- lightweight refresh (task list contents only) -------------------

    def refresh(self) -> None:
        for column_id, col_widget in self.columns.items():
            list_widget = col_widget.list_widget
            list_widget.blockSignals(True)
            list_widget.clear()

            all_tasks = get_project_tasks_for_column(self.conn, self.board_id, column_id)
            tasks = all_tasks if self.show_completed else [t for t in all_tasks if not t["completed"]]

            for task in tasks:
                item = QListWidgetItem(self._task_card_text(task))
                item.setData(Qt.UserRole, task["id"])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if task["completed"] else Qt.Unchecked)
                if task["completed"]:
                    font = item.font()
                    font.setStrikeOut(True)
                    item.setFont(font)
                    item.setForeground(QColor("#767676"))
                list_widget.addItem(item)

            list_widget.blockSignals(False)
            col_widget.set_count(len(tasks))

    def _task_card_text(self, task: dict) -> str:
        meta_parts = build_project_task_meta_parts(self.conn, task)
        text = task["title"]
        if meta_parts:
            text += "\n" + "   ".join(meta_parts)
        return text

    # -- task operations (called by ColumnWidget/TaskListWidget) ---------

    def add_task(self, target_status=None) -> None:
        if not self._columns_cache:
            QMessageBox.information(self, "No columns", "Add a column first.")
            return
        default_column_id = target_status or get_default_new_project_task_column(self.conn, self.board_id)
        default_start = default_project_task_start_date(self.conn, self.board_id)
        dialog = NewProjectTaskDialog(
            self._columns_cache, default_column_id, self, default_start_date=default_start
        )
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values()
        add_project_task(
            self.conn, self.board_id, values["column_id"], values["title"], values["notes"],
            values["start_date"], values["due_date"], values["link"],
        )
        self.hub.refresh_all_views()

    def add_multiple_tasks_ui(self) -> None:
        if not self._columns_cache:
            QMessageBox.information(self, "No columns", "Add a column first.")
            return
        default_column_id = get_default_new_project_task_column(self.conn, self.board_id)
        dialog = BulkAddProjectTasksDialog(self._columns_cache, default_column_id, self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values()
        for title in values["titles"]:
            add_project_task(self.conn, self.board_id, values["column_id"], title)
        self.hub.refresh_all_views()

    def edit_task(self, task_id: str) -> None:
        task = get_project_task(self.conn, task_id)
        if not task:
            return

        subboard = get_subboard_for_task(self.conn, task_id)
        dialog = ProjectTaskCardDialog(self.conn, task, self._columns_cache, subboard, self)
        result = dialog.exec()

        if dialog.delete_requested:
            self.hub.confirm_and_delete_task(task_id)
            return

        if result != QDialog.Accepted:
            return

        values = dialog.result_values()
        update_project_task(
            self.conn, task_id, values["title"], values["notes"],
            values["start_date"], values["due_date"], values["link"],
        )
        if values["column_id"] and values["column_id"] != task["column_id"]:
            move_project_task(self.conn, task_id, values["column_id"])
        if values["completed"] != bool(task["completed"]):
            set_project_task_completed(self.conn, task_id, values["completed"])

        if dialog.subboard_action_requested:
            self.hub.open_or_create_subboard(task_id)
            return

        self.hub.refresh_all_views()

    def handle_move(self, task_id: str, new_column_id: str) -> None:
        task = get_project_task(self.conn, task_id)
        if not task or task["column_id"] == new_column_id:
            return
        move_project_task(self.conn, task_id, new_column_id)
        self.hub.refresh_all_views()

    def handle_set_completed(self, task_id: str, completed: bool) -> None:
        set_project_task_completed(self.conn, task_id, completed)
        # Deferred for the same reason as KanbanBoard.handle_set_completed:
        # this fires from inside the card's own checkbox-toggle signal, and
        # refresh() rebuilds (clears) that same list widget.
        QTimer.singleShot(0, self.hub.refresh_all_views)

    def show_move_task_menu(self, task_id: str, current_column_id: str, global_pos) -> None:
        menu = QMenu(self)

        subboard = get_subboard_for_task(self.conn, task_id)
        if subboard is not None:
            done, total = get_subtree_task_progress(self.conn, subboard["id"])
            open_action = menu.addAction(f"Open Sub-board (⧉ {done}/{total})")
            open_action.triggered.connect(lambda: self.hub.navigate_to_board(subboard["id"]))
        else:
            create_action = menu.addAction("Create Sub-board")
            create_action.triggered.connect(lambda: self.hub.open_or_create_subboard(task_id))
        delete_action = menu.addAction("Delete Task")
        delete_action.triggered.connect(lambda: self.hub.confirm_and_delete_task(task_id))
        menu.addSeparator()

        other_columns = [c for c in self._columns_cache if c["id"] != current_column_id]
        if not other_columns:
            no_columns_action = menu.addAction("No other columns")
            no_columns_action.setEnabled(False)
        else:
            for col in other_columns:
                action = menu.addAction(f"Move to {col['name']}")
                action.triggered.connect(lambda checked=False, cid=col["id"]: self.handle_move(task_id, cid))
        menu.exec(global_pos)

    # -- column operations -------------------------------------------------

    def column_name(self, column_id: str) -> str:
        for col in self._columns_cache:
            if col["id"] == column_id:
                return col["name"]
        return ""

    def add_column_ui(self) -> None:
        name, ok = QInputDialog.getText(self, "New Column", "Column name:")
        if not ok or not name.strip():
            return
        try:
            add_project_column(self.conn, self.board_id, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not add column", str(e))
            return
        self.rebuild_columns()

    def rename_column_ui(self, column_id: str) -> None:
        current_name = self.column_name(column_id)
        name, ok = QInputDialog.getText(self, "Rename Column", "Column name:", text=current_name)
        if not ok or not name.strip():
            return
        try:
            rename_project_column(self.conn, column_id, name.strip())
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename column", str(e))
            return
        self.rebuild_columns()

    def delete_column_ui(self, column_id: str) -> None:
        name = self.column_name(column_id)
        reply = QMessageBox.question(
            self, "Delete column", f'Delete the "{name}" column?', QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            delete_project_column(self.conn, self.board_id, column_id)
        except ValueError as e:
            QMessageBox.warning(self, "Could not delete column", str(e))
            return
        self.rebuild_columns()

    def move_column_ui(self, column_id: str, direction: int) -> None:
        move_project_column(self.conn, self.board_id, column_id, direction)
        self.rebuild_columns()


class _TableDateDelegate(QStyledItemDelegate):
    """Inline date editor for the List/Missing Dates tables' Start/Due
    columns - a QDateEdit with a calendar popup, matching the date
    fields used everywhere else in the app (TaskCardDialog and friends).
    Editing only ever produces a valid date; clearing a date back to
    unset still goes through the task dialog's "Set" checkbox.

    Needs a reference back to the owning view (rather than just conn) to
    resolve the current board_id and to apply column-specific defaults:
    Start defaults to right after the last due date on the board; Due
    defaults to the day after this row's own Start value (falling back
    through Start's own default when Start is also empty)."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view

    def createEditor(self, parent, option, index):
        editor = QDateEdit(parent)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("yyyy-MM-dd")
        return editor

    def setEditorData(self, editor, index) -> None:
        existing = QDate.fromString(index.data(Qt.EditRole) or "", "yyyy-MM-dd")
        if existing.isValid():
            editor.setDate(existing)
        elif index.column() == self.view.COLUMN_START and self.view.board_id:
            editor.setDate(default_project_task_start_date(self.view.conn, self.view.board_id))
        elif index.column() == self.view.COLUMN_DUE:
            start_index = index.sibling(index.row(), self.view.COLUMN_START)
            start_value = QDate.fromString(start_index.data(Qt.EditRole) or "", "yyyy-MM-dd")
            if not start_value.isValid() and self.view.board_id:
                start_value = default_project_task_start_date(self.view.conn, self.view.board_id)
            editor.setDate(
                default_project_task_due_date(start_value) if start_value.isValid() else QDate.currentDate()
            )
        else:
            editor.setDate(QDate.currentDate())

    def setModelData(self, editor, model, index) -> None:
        model.setData(index, editor.date().toString("yyyy-MM-dd"), Qt.EditRole)

    def updateEditorGeometry(self, editor, option, index) -> None:
        editor.setGeometry(option.rect)


class _ProjectTaskTableView(QWidget):
    """Shared QTableWidget wrapper backing both the List and Missing Dates
    views - same columns, same sort/double-click behavior, only the task
    query differs (see ProjectListView/ProjectMissingDatesView below).
    Start/Due are editable in place (via _TableDateDelegate); every other
    column stays read-only, opened through the full task dialog instead."""

    COLUMN_START = 2
    COLUMN_DUE = 3

    def __init__(self, conn: sqlite3.Connection, hub, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.hub = hub
        self.board_id = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Title", "Column", "Start", "Due", "Completed"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        self._date_delegate = _TableDateDelegate(self, self.table)
        self.table.setItemDelegateForColumn(self.COLUMN_START, self._date_delegate)
        self.table.setItemDelegateForColumn(self.COLUMN_DUE, self._date_delegate)
        self.table.itemDoubleClicked.connect(self._on_row_double_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)

    def load_board(self, board_id: str) -> None:
        self.board_id = board_id
        self.refresh()

    def _fetch_tasks(self) -> list:
        raise NotImplementedError

    def refresh(self) -> None:
        if not self.board_id:
            self.table.setRowCount(0)
            return

        col_name_by_id = {c["id"]: c["name"] for c in get_project_columns(self.conn, self.board_id)}
        tasks = self._fetch_tasks()

        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            title_item = QTableWidgetItem(task["title"])
            title_item.setData(Qt.UserRole, task["id"])
            title_item.setFlags(title_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, title_item)

            column_item = QTableWidgetItem(col_name_by_id.get(task["column_id"], ""))
            column_item.setFlags(column_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 1, column_item)

            self.table.setItem(row, self.COLUMN_START, QTableWidgetItem(task.get("start_date") or ""))
            self.table.setItem(row, self.COLUMN_DUE, QTableWidgetItem(task.get("due_date") or ""))

            completed_item = QTableWidgetItem("Yes" if task["completed"] else "")
            completed_item.setFlags(completed_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 4, completed_item)
        self.table.blockSignals(False)
        self.table.setSortingEnabled(True)

    def _on_row_double_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() in (self.COLUMN_START, self.COLUMN_DUE):
            return  # edited in place instead of opening the task dialog
        table = item.tableWidget()
        task_id = table.item(item.row(), 0).data(Qt.UserRole)
        self.hub.kanban_widget.edit_task(task_id)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() not in (self.COLUMN_START, self.COLUMN_DUE):
            return

        title_item = self.table.item(item.row(), 0)
        task_id = title_item.data(Qt.UserRole) if title_item else None
        task = get_project_task(self.conn, task_id) if task_id else None
        if task is None:
            return

        new_value = item.text().strip()
        start_date = new_value if item.column() == self.COLUMN_START else (task.get("start_date") or "")
        due_date = new_value if item.column() == self.COLUMN_DUE else (task.get("due_date") or "")
        update_project_task(self.conn, task_id, task["title"], task["notes"], start_date, due_date, task["link"])
        self.hub.refresh_all_views()


class ProjectListView(_ProjectTaskTableView):
    """All tasks on the current board, any column."""

    def _fetch_tasks(self) -> list:
        return get_project_tasks_for_board(self.conn, self.board_id)


class ProjectMissingDatesView(_ProjectTaskTableView):
    """Same shape as ProjectListView, filtered to tasks missing a start
    date, due date, or both - everything Gantt/Calendar would otherwise
    drop into "Unscheduled" once those views exist."""

    def _fetch_tasks(self) -> list:
        return get_missing_dates_tasks(self.conn, self.board_id)


class ProjectGanttChart(QWidget):
    """The painted canvas half of the Gantt view: one horizontal bar per
    scheduled task (both start_date and due_date set), all tasks shown
    together in one flat, chronologically-ordered list rather than
    grouped by column - bars are still tinted by column (see
    project_column_color) so status stays visible at a glance without a
    separate swimlane per column. No dependency arrows - project_tasks
    has no dependency field, matching the issue's "No dependency arrows
    in v1".

    This is the only custom-painted widget in the app; everywhere else
    is built from standard Qt widgets/layouts. A free date axis with
    variable-width bars doesn't map cleanly onto a layout of child
    widgets the way the Calendar's day grid does (see
    ProjectCalendarView), so painting is the natural fit here."""

    DAY_WIDTH = 24
    ROW_HEIGHT = 32
    ROW_VPAD = 6
    DATE_RULER_HEIGHT = 24
    LEFT_MARGIN = 12
    PADDING_DAYS = 3
    TODAY_MARKER_COLOR = "#f2545b"

    task_clicked = Signal(str)

    def __init__(self, conn: sqlite3.Connection, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.board_id = None
        self._tasks = []
        self._column_position_by_id = {}
        self._range_start = None
        self._range_end = None
        self._task_rects = []  # [(QRect, task_id), ...] - rebuilt each paint
        self.setFixedSize(1, 1)

    def load_board(self, board_id: str) -> None:
        self.board_id = board_id
        self.refresh()

    def refresh(self) -> None:
        self._recompute_layout()
        self.update()

    def has_scheduled_tasks(self) -> bool:
        return self._range_start is not None

    def _recompute_layout(self) -> None:
        columns = get_project_columns(self.conn, self.board_id) if self.board_id else []
        self._column_position_by_id = {c["id"]: c["position"] for c in columns}
        scheduled = get_scheduled_project_tasks(self.conn, self.board_id) if self.board_id else []

        if not scheduled:
            self._range_start = None
            self._range_end = None
            self._tasks = []
            self.setFixedSize(1, 1)
            return

        starts = [QDate.fromString(t["start_date"], "yyyy-MM-dd") for t in scheduled]
        dues = [QDate.fromString(t["due_date"], "yyyy-MM-dd") for t in scheduled]
        self._range_start = min(starts).addDays(-self.PADDING_DAYS)
        self._range_end = max(dues).addDays(self.PADDING_DAYS)
        self._tasks = scheduled  # flat - already ORDER BY start_date, position

        total_days = self._range_start.daysTo(self._range_end) + 1
        width = self.LEFT_MARGIN * 2 + total_days * self.DAY_WIDTH
        height = self.DATE_RULER_HEIGHT + len(self._tasks) * self.ROW_HEIGHT
        self.setFixedSize(max(width, 1), max(height, 1))

    def _x_for_date(self, d: QDate) -> int:
        return self.LEFT_MARGIN + self._range_start.daysTo(d) * self.DAY_WIDTH

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        self._task_rects = []

        if self._range_start is None:
            painter.end()
            return

        # Today marker's label rect, computed up front so the date-ruler
        # loop below can skip any tick label that would collide with it
        # (rather than painting one over the other and losing either).
        today = QDate.currentDate()
        today_x = None
        today_label_rect = None
        if self._range_start <= today <= self._range_end:
            today_x = self._x_for_date(today) + self.DAY_WIDTH // 2
            label_metrics = painter.fontMetrics()
            today_label_rect = label_metrics.boundingRect("Today").adjusted(-2, -1, 2, 1)
            today_label_rect.moveTopLeft(QPoint(today_x + 3, 2))

        # Date ruler: a tick + short label every 7 days across the range,
        # skipping any label that would collide with the Today label.
        painter.setPen(QColor("#9a9a9a"))
        d = self._range_start
        while d <= self._range_end:
            if self._range_start.daysTo(d) % 7 == 0:
                x = self._x_for_date(d)
                tick_label_rect = QRect(x + 2, 0, painter.fontMetrics().horizontalAdvance(d.toString("MMM d")), self.DATE_RULER_HEIGHT)
                if today_label_rect is None or not tick_label_rect.intersects(today_label_rect):
                    painter.drawText(x + 2, self.DATE_RULER_HEIGHT - 8, d.toString("MMM d"))
                painter.drawLine(x, self.DATE_RULER_HEIGHT - 4, x, self.height())
            d = d.addDays(1)

        y = self.DATE_RULER_HEIGHT
        for i, task in enumerate(self._tasks):
            start = QDate.fromString(task["start_date"], "yyyy-MM-dd")
            due = QDate.fromString(task["due_date"], "yyyy-MM-dd")
            x1 = self._x_for_date(start)
            x2 = self._x_for_date(due) + self.DAY_WIDTH
            bar_y = y + i * self.ROW_HEIGHT + self.ROW_VPAD // 2
            bar_h = self.ROW_HEIGHT - self.ROW_VPAD
            rect = QRect(x1, bar_y, max(x2 - x1, 4), bar_h)
            bar_color = QColor(project_column_color(self._column_position_by_id.get(task["column_id"], 0)))
            painter.fillRect(rect, bar_color)
            painter.setPen(QColor("#ffffff"))
            text_rect = rect.adjusted(4, 0, -4, 0)
            elided_title = painter.fontMetrics().elidedText(task["title"], Qt.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignVCenter, elided_title)
            self._task_rects.append((rect, task["id"]))

        if today_x is not None:
            painter.setPen(QPen(QColor(self.TODAY_MARKER_COLOR), 1, Qt.DashLine))
            painter.drawLine(today_x, 0, today_x, self.height())

            # Opaque backdrop behind the label too, as a second line of
            # defense on top of the ruler already yielding its own label
            # above - e.g. if a task bar's text were ever tall enough to
            # reach this band.
            painter.fillRect(today_label_rect, QColor("#1b1c1e"))
            painter.setPen(QColor(self.TODAY_MARKER_COLOR))
            painter.drawText(today_label_rect, Qt.AlignCenter, "Today")

        painter.end()

    def mousePressEvent(self, event) -> None:
        pos = event.position().toPoint()
        for rect, task_id in self._task_rects:
            if rect.contains(pos):
                self.task_clicked.emit(task_id)
                return
        super().mousePressEvent(event)


class ProjectGanttView(QWidget):
    """The Gantt page ProjectsHub holds: a scrollable painted chart on
    top, and the exact same Missing Dates table - reused via
    composition, not reimplemented - as an "Unscheduled" tray below it,
    per the issue's "this tray is the same underlying query as the
    Missing Dates view, scoped to the board.\""""

    UNSCHEDULED_TRAY_HEIGHT = 180

    def __init__(self, conn: sqlite3.Connection, hub, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.hub = hub

        outer = QVBoxLayout(self)

        self.chart = ProjectGanttChart(conn)
        self.chart.task_clicked.connect(lambda task_id: self.hub.kanban_widget.edit_task(task_id))

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.chart)
        self.scroll_area.setWidgetResizable(False)
        outer.addWidget(self.scroll_area, stretch=1)

        self.empty_label = QLabel("No scheduled tasks yet")
        self.empty_label.setStyleSheet("color: #6e6e6e; padding: 16px;")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()
        outer.addWidget(self.empty_label, stretch=1)

        unscheduled_label = QLabel("UNSCHEDULED")
        unscheduled_label.setStyleSheet(
            "color: #9a9a9a; font-weight: bold; font-size: 11px; padding: 8px 0 2px 0;"
        )
        outer.addWidget(unscheduled_label)

        self.unscheduled = ProjectMissingDatesView(conn, hub)
        self.unscheduled.setFixedHeight(self.UNSCHEDULED_TRAY_HEIGHT)
        outer.addWidget(self.unscheduled)

    def load_board(self, board_id: str) -> None:
        self.chart.load_board(board_id)
        self.unscheduled.load_board(board_id)
        self._update_empty_state()

    def refresh(self) -> None:
        self.chart.refresh()
        self.unscheduled.refresh()
        self._update_empty_state()

    def _update_empty_state(self) -> None:
        has_scheduled = self.chart.has_scheduled_tasks()
        self.scroll_area.setVisible(has_scheduled)
        self.empty_label.setVisible(not has_scheduled)


class ProjectCalendarView(QWidget):
    """Month/week calendar grid. Unlike Gantt's free date axis, days are
    naturally grid-shaped: a real QGridLayout gives multi-day-chip
    spanning via colSpan and QPushButton chips get clicking for free,
    matching this app's existing "clear and rebuild a layout" idiom
    (see ProjectKanbanWidget.rebuild_columns) rather than introducing a
    second painted widget where a grid already fits."""

    MODE_MONTH = "month"
    MODE_WEEK = "week"
    WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    CALENDAR_MAX_LANES = 3
    DAY_CELL_MIN_WIDTH = 90
    CHIP_HEIGHT = 20

    def __init__(self, conn: sqlite3.Connection, hub, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.hub = hub
        self.board_id = None
        self.mode = self.MODE_MONTH
        self.anchor = QDate.currentDate()

        outer = QVBoxLayout(self)

        header_row = QHBoxLayout()
        prev_btn = QPushButton("◀")
        prev_btn.setFixedWidth(32)
        prev_btn.clicked.connect(self._go_previous)
        header_row.addWidget(prev_btn)

        today_btn = QPushButton("Today")
        today_btn.clicked.connect(self._go_today)
        header_row.addWidget(today_btn)

        next_btn = QPushButton("▶")
        next_btn.setFixedWidth(32)
        next_btn.clicked.connect(self._go_next)
        header_row.addWidget(next_btn)

        self.range_label = QLabel()
        self.range_label.setAlignment(Qt.AlignCenter)
        self.range_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        header_row.addWidget(self.range_label, stretch=1)

        month_btn = QPushButton("Month")
        month_btn.setCheckable(True)
        month_btn.setChecked(True)
        week_btn = QPushButton("Week")
        week_btn.setCheckable(True)
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        mode_group.addButton(month_btn)
        mode_group.addButton(week_btn)
        month_btn.clicked.connect(lambda: self._set_mode(self.MODE_MONTH))
        week_btn.clicked.connect(lambda: self._set_mode(self.MODE_WEEK))
        header_row.addWidget(month_btn)
        header_row.addWidget(week_btn)

        outer.addLayout(header_row)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        self.grid_container = QWidget()
        self.grid = QGridLayout(self.grid_container)
        self.grid.setSpacing(2)
        scroll_area.setWidget(self.grid_container)
        outer.addWidget(scroll_area, stretch=1)

        for col, label_text in enumerate(self.WEEKDAY_LABELS):
            header_label = QLabel(label_text)
            header_label.setAlignment(Qt.AlignCenter)
            header_label.setStyleSheet(
                "color: #9a9a9a; font-weight: bold; font-size: 11px; padding: 4px;"
            )
            self.grid.addWidget(header_label, 0, col)
        for col in range(7):
            self.grid.setColumnMinimumWidth(col, self.DAY_CELL_MIN_WIDTH)
            self.grid.setColumnStretch(col, 1)

    # -- board load / navigation -----------------------------------------

    def load_board(self, board_id: str) -> None:
        self.board_id = board_id
        self.mode = self.MODE_MONTH
        self.anchor = QDate.currentDate()
        self._rebuild()

    def refresh(self) -> None:
        self._rebuild()

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self._rebuild()

    def _go_previous(self) -> None:
        self.anchor = self.anchor.addMonths(-1) if self.mode == self.MODE_MONTH else self.anchor.addDays(-7)
        self._rebuild()

    def _go_next(self) -> None:
        self.anchor = self.anchor.addMonths(1) if self.mode == self.MODE_MONTH else self.anchor.addDays(7)
        self._rebuild()

    def _go_today(self) -> None:
        self.anchor = QDate.currentDate()
        self._rebuild()

    # -- grid rebuild ------------------------------------------------------

    def _visible_weeks(self) -> list:
        """Returns a list of weeks, each a list of 7 QDate (Mon..Sun)."""
        if self.mode == self.MODE_WEEK:
            week_start = self.anchor.addDays(-(self.anchor.dayOfWeek() - 1))
            return [[week_start.addDays(i) for i in range(7)]]

        first_of_month = QDate(self.anchor.year(), self.anchor.month(), 1)
        grid_start = first_of_month.addDays(-(first_of_month.dayOfWeek() - 1))
        last_of_month = first_of_month.addDays(first_of_month.daysInMonth() - 1)
        weeks = []
        week_start = grid_start
        while week_start <= last_of_month:
            weeks.append([week_start.addDays(i) for i in range(7)])
            week_start = week_start.addDays(7)
        return weeks

    def _rebuild(self) -> None:
        self._clear_grid_rows()

        if self.mode == self.MODE_MONTH:
            self.range_label.setText(self.anchor.toString("MMMM yyyy"))
        else:
            week_start = self.anchor.addDays(-(self.anchor.dayOfWeek() - 1))
            self.range_label.setText(
                f"{week_start.toString('MMM d')} - {week_start.addDays(6).toString('MMM d, yyyy')}"
            )

        if not self.board_id:
            return

        weeks = self._visible_weeks()
        tasks = get_calendar_project_tasks(self.conn, self.board_id)
        task_ranges = {t["id"]: self._effective_range(t) for t in tasks}
        current_month = self.anchor.month() if self.mode == self.MODE_MONTH else None

        grid_row = 1
        for week in weeks:
            grid_row = self._build_week_row(week, tasks, task_ranges, grid_row, current_month)

    def _clear_grid_rows(self) -> None:
        # The first 7 items (row 0's weekday headers) are added once in
        # __init__ and never removed - everything after them is rebuilt
        # from scratch on every navigation/mode change.
        while self.grid.count() > 7:
            item = self.grid.takeAt(self.grid.count() - 1)
            widget = item.widget()
            if widget is not None:
                # hide() first: takeAt() stops the layout from managing
                # the widget, but doesn't hide it - left alone, it stays
                # visibly painted at its last position until deleteLater()
                # actually runs, which isn't guaranteed before the next
                # repaint (seen as literal ghost duplicate cells in the
                # Calendar view under fast navigation).
                widget.hide()
                widget.deleteLater()

    def _build_week_row(self, week: list, tasks: list, task_ranges: dict, grid_row: int, current_month) -> int:
        week_start, week_end = week[0], week[6]

        day_number_row = grid_row
        for col, day in enumerate(week):
            in_month = current_month is None or day.month() == current_month
            is_today = day == QDate.currentDate()
            label = QLabel(str(day.day()))
            color = "#e8e8e8" if in_month else "#5a5a5a"
            weight = "bold" if is_today else "normal"
            bg = f"background-color: {ACCENT_COLOR}; border-radius: 4px;" if is_today else ""
            label.setStyleSheet(f"color: {color}; font-weight: {weight}; padding: 2px 4px; {bg}")
            self.grid.addWidget(label, day_number_row, col)

        # -- lane assignment (greedy "first available lane", per week-row) --
        overlapping = [
            t for t in tasks
            if task_ranges[t["id"]][0] <= week_end and task_ranges[t["id"]][1] >= week_start
        ]
        overlapping.sort(
            key=lambda t: (
                task_ranges[t["id"]][0],
                -task_ranges[t["id"]][0].daysTo(task_ranges[t["id"]][1]),
                t["title"],
            )
        )

        lane_end_dates = []
        task_lane = {}
        for t in overlapping:
            eff_start, eff_end = task_ranges[t["id"]]
            placed = False
            for lane_idx, lane_end in enumerate(lane_end_dates):
                if lane_end < eff_start:
                    lane_end_dates[lane_idx] = eff_end
                    task_lane[t["id"]] = lane_idx
                    placed = True
                    break
            if not placed:
                lane_end_dates.append(eff_end)
                task_lane[t["id"]] = len(lane_end_dates) - 1

        visible_lane_count = min(len(lane_end_dates), self.CALENDAR_MAX_LANES)
        for t in overlapping:
            lane_idx = task_lane[t["id"]]
            if lane_idx >= visible_lane_count:
                continue

            eff_start, eff_end = task_ranges[t["id"]]
            seg_start = max(eff_start, week_start)
            seg_end = min(eff_end, week_end)
            col = week.index(seg_start)
            col_span = seg_start.daysTo(seg_end) + 1

            label_text = t["title"]
            if eff_start < seg_start:
                label_text = "◂ " + label_text
            if eff_end > seg_end:
                label_text = label_text + " ▸"

            chip = QPushButton(label_text)
            chip.setFixedHeight(self.CHIP_HEIGHT)
            chip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            chip.setToolTip("\n".join(build_project_task_meta_parts(self.conn, t)))
            fill = project_column_color(self._column_position(t["column_id"]))
            chip.setStyleSheet(
                f"QPushButton {{ background-color: {fill}; color: white; border: none; "
                f"border-radius: 4px; text-align: left; padding: 1px 6px; font-size: 11px; }}"
                f"QPushButton:hover {{ background-color: {fill}; }}"
            )
            chip.clicked.connect(lambda checked=False, tid=t["id"]: self.hub.kanban_widget.edit_task(tid))
            self.grid.addWidget(chip, grid_row + 1 + lane_idx, col, 1, col_span)

        overflow_row = grid_row + 1 + visible_lane_count
        for col, day in enumerate(week):
            hidden_titles = [
                t["title"] for t in overlapping
                if task_lane[t["id"]] >= self.CALENDAR_MAX_LANES
                and task_ranges[t["id"]][0] <= day <= task_ranges[t["id"]][1]
            ]
            if hidden_titles:
                overflow_label = QLabel(f"+{len(hidden_titles)} more")
                overflow_label.setStyleSheet("color: #9a9a9a; font-size: 10px; padding: 1px 4px;")
                overflow_label.setToolTip("\n".join(hidden_titles))
                self.grid.addWidget(overflow_label, overflow_row, col)

        return overflow_row + 1

    def _effective_range(self, task: dict) -> tuple:
        """(start, due) - falls back to due for both ends when start_date
        isn't set, giving a due-only task a single-day span."""
        due = QDate.fromString(task["due_date"], "yyyy-MM-dd")
        start = QDate.fromString(task.get("start_date") or "", "yyyy-MM-dd")
        if not start.isValid():
            start = due
        return start, due

    def _column_position(self, column_id: str) -> int:
        column = get_project_column(self.conn, column_id)
        return column["position"] if column else 0


class ProjectActivityLogView(QWidget):
    """Project-wide Activity Log screen (issue §9.3/§9.4) - separate from
    the per-board view switcher since a project's history spans every
    board under it, not just the one currently open. Filters run entirely
    against the permanent project_activity_log rows, so an entry keeps
    showing up here (under its captured snapshot) even after the task or
    board it names has since been deleted."""

    ACTION_FILTER_ALL = "__all__"
    BOARD_FILTER_ALL = "__all__"

    def __init__(self, conn: sqlite3.Connection, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = None

        outer = QVBoxLayout(self)

        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search description or task title...")
        self.search_edit.textChanged.connect(self.refresh)
        filter_row.addWidget(self.search_edit, stretch=1)

        self.action_combo = QComboBox()
        self.action_combo.addItem("All Actions", self.ACTION_FILTER_ALL)
        for action_type, label in ACTIVITY_ACTION_TYPES:
            self.action_combo.addItem(label, action_type)
        self.action_combo.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.action_combo)

        self.board_combo = QComboBox()
        self.board_combo.addItem("All Boards", self.BOARD_FILTER_ALL)
        self.board_combo.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.board_combo)
        outer.addLayout(filter_row)

        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("From"))
        self.date_from_check = QCheckBox()
        date_row.addWidget(self.date_from_check)
        self.date_from_edit = QDateEdit(QDate.currentDate())
        self.date_from_edit.setCalendarPopup(True)
        self.date_from_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_from_edit.setEnabled(False)
        self.date_from_check.toggled.connect(self.date_from_edit.setEnabled)
        self.date_from_check.toggled.connect(self.refresh)
        self.date_from_edit.dateChanged.connect(self.refresh)
        date_row.addWidget(self.date_from_edit)

        date_row.addWidget(QLabel("To"))
        self.date_to_check = QCheckBox()
        date_row.addWidget(self.date_to_check)
        self.date_to_edit = QDateEdit(QDate.currentDate())
        self.date_to_edit.setCalendarPopup(True)
        self.date_to_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_to_edit.setEnabled(False)
        self.date_to_check.toggled.connect(self.date_to_edit.setEnabled)
        self.date_to_check.toggled.connect(self.refresh)
        self.date_to_edit.dateChanged.connect(self.refresh)
        date_row.addWidget(self.date_to_edit)

        clear_btn = QPushButton("Clear Filters")
        clear_btn.clicked.connect(self.clear_filters)
        date_row.addWidget(clear_btn)
        date_row.addStretch()
        outer.addLayout(date_row)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Date", "Board", "Task", "Action", "Description"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        outer.addWidget(self.table, stretch=1)

    def load_project(self, project_id: str) -> None:
        self.project_id = project_id
        self._reload_board_filter_options()
        self.refresh()

    def clear_filters(self) -> None:
        self.search_edit.blockSignals(True)
        self.action_combo.blockSignals(True)
        self.board_combo.blockSignals(True)
        self.date_from_check.blockSignals(True)
        self.date_to_check.blockSignals(True)
        self.search_edit.clear()
        self.action_combo.setCurrentIndex(0)
        self.board_combo.setCurrentIndex(0)
        self.date_from_check.setChecked(False)
        self.date_to_check.setChecked(False)
        self.date_from_edit.setEnabled(False)
        self.date_to_edit.setEnabled(False)
        self.search_edit.blockSignals(False)
        self.action_combo.blockSignals(False)
        self.board_combo.blockSignals(False)
        self.date_from_check.blockSignals(False)
        self.date_to_check.blockSignals(False)
        self.refresh()

    def _reload_board_filter_options(self) -> None:
        current = self.board_combo.currentData()
        self.board_combo.blockSignals(True)
        self.board_combo.clear()
        self.board_combo.addItem("All Boards", self.BOARD_FILTER_ALL)
        for board_path in get_activity_log_board_paths(self.conn, self.project_id):
            self.board_combo.addItem(board_path, board_path)
        restore_idx = self.board_combo.findData(current) if current else 0
        self.board_combo.setCurrentIndex(restore_idx if restore_idx >= 0 else 0)
        self.board_combo.blockSignals(False)

    def refresh(self) -> None:
        if not self.project_id:
            self.table.setRowCount(0)
            return

        action_filter = self.action_combo.currentData()
        action_types = [action_filter] if action_filter and action_filter != self.ACTION_FILTER_ALL else None
        board_filter = self.board_combo.currentData()
        board_path = board_filter if board_filter and board_filter != self.BOARD_FILTER_ALL else ""
        date_from = self.date_from_edit.date().toString("yyyy-MM-dd") if self.date_from_check.isChecked() else ""
        date_to = self.date_to_edit.date().toString("yyyy-MM-dd") if self.date_to_check.isChecked() else ""

        entries = get_activity_log_entries(
            self.conn, self.project_id, search_text=self.search_edit.text(),
            action_types=action_types, board_path_filter=board_path,
            date_from=date_from, date_to=date_to,
        )

        action_labels = dict(ACTIVITY_ACTION_TYPES)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(entry["created_at"]))
            self.table.setItem(row, 1, QTableWidgetItem(entry["board_path_snapshot"]))
            self.table.setItem(row, 2, QTableWidgetItem(entry["task_title_snapshot"]))
            self.table.setItem(row, 3, QTableWidgetItem(action_labels.get(entry["action_type"], entry["action_type"])))
            self.table.setItem(row, 4, QTableWidgetItem(entry["description"]))


class ProjectsHub(QWidget):
    """The Projects page of the app's central QStackedWidget (see main()).
    Owns all Projects UI state - current project/board, the breadcrumb,
    and the three view widgets - entirely separately from KanbanBoard."""

    VIEW_KANBAN = "kanban"
    VIEW_LIST = "list"
    VIEW_MISSING_DATES = "missing_dates"
    VIEW_GANTT = "gantt"
    VIEW_CALENDAR = "calendar"
    VIEWS = [
        (VIEW_KANBAN, "Kanban"), (VIEW_LIST, "List"), (VIEW_MISSING_DATES, "Missing Dates"),
        (VIEW_GANTT, "Gantt"), (VIEW_CALENDAR, "Calendar"),
    ]

    def __init__(self, conn: sqlite3.Connection, on_back_to_boards, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.on_back_to_boards = on_back_to_boards
        self.current_project_id = None
        self.current_board_id = None

        outer = QVBoxLayout(self)

        self.board_page = QWidget()
        board_page_layout = QVBoxLayout(self.board_page)
        board_page_layout.setContentsMargins(0, 0, 0, 0)

        toolbar = QHBoxLayout()

        back_btn = QPushButton("← Boards")
        back_btn.setToolTip("Back to the Boards feature")
        back_btn.clicked.connect(self.on_back_to_boards)
        toolbar.addWidget(back_btn)

        self.breadcrumb = BreadcrumbBar()
        self.breadcrumb.crumb_clicked.connect(self.navigate_to_board)
        toolbar.addWidget(self.breadcrumb, stretch=1)

        self.add_task_btn = QToolButton()
        self.add_task_btn.setText("+ New Task")
        self.add_task_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.add_task_btn.setProperty("accent", True)
        self.add_task_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.add_task_btn.clicked.connect(lambda: self.kanban_widget.add_task())
        self.new_task_menu = QMenu(self.add_task_btn)
        add_multiple_action = self.new_task_menu.addAction("Add Multiple…")
        add_multiple_action.triggered.connect(lambda: self.kanban_widget.add_multiple_tasks_ui())
        self.add_task_btn.setMenu(self.new_task_menu)
        toolbar.addWidget(self.add_task_btn)

        self.add_col_btn = QPushButton("+ Column")
        self.add_col_btn.clicked.connect(lambda: self.kanban_widget.add_column_ui())
        self.add_col_btn.hide()
        toolbar.addWidget(self.add_col_btn)

        self.show_completed_btn = QPushButton("Show Completed")
        self.show_completed_btn.setCheckable(True)
        self.show_completed_btn.toggled.connect(self._on_show_completed_toggled)
        toolbar.addWidget(self.show_completed_btn)

        self.edit_board_btn = QPushButton("Edit Board")
        self.edit_board_btn.setCheckable(True)
        self.edit_board_btn.setToolTip("Show or hide column move/rename/delete controls")
        self.edit_board_btn.toggled.connect(self._on_edit_board_toggled)
        toolbar.addWidget(self.edit_board_btn)

        self.delete_board_btn = QPushButton("Delete Sub-board")
        self.delete_board_btn.setStyleSheet("color: #b00000;")
        self.delete_board_btn.setToolTip(
            "Delete this sub-board and everything below it (a Main Board can't be "
            "deleted independently of its project - delete the project instead)"
        )
        self.delete_board_btn.clicked.connect(self._on_delete_board_clicked)
        self.delete_board_btn.hide()
        toolbar.addWidget(self.delete_board_btn)

        self.activity_log_btn = QPushButton("Activity Log")
        self.activity_log_btn.setToolTip("View the project-wide task history log")
        self.activity_log_btn.clicked.connect(self.show_activity_log)
        toolbar.addWidget(self.activity_log_btn)

        board_page_layout.addLayout(toolbar)

        view_row = QHBoxLayout()
        self.view_buttons = {}
        view_group = QButtonGroup(self)
        view_group.setExclusive(True)
        for key, label in self.VIEWS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, k=key: self.set_view(k))
            view_group.addButton(btn)
            view_row.addWidget(btn)
            self.view_buttons[key] = btn
        view_row.addStretch()
        board_page_layout.addLayout(view_row)

        self.content_stack = QStackedWidget()
        board_page_layout.addWidget(self.content_stack, stretch=1)

        self.kanban_widget = ProjectKanbanWidget(conn, self)
        self.list_view = ProjectListView(conn, self)
        self.missing_dates_view = ProjectMissingDatesView(conn, self)
        self.gantt_view = ProjectGanttView(conn, self)
        self.calendar_view = ProjectCalendarView(conn, self)
        self.content_stack.addWidget(self.kanban_widget)        # index 0 - VIEW_KANBAN
        self.content_stack.addWidget(self.list_view)             # index 1 - VIEW_LIST
        self.content_stack.addWidget(self.missing_dates_view)    # index 2 - VIEW_MISSING_DATES
        self.content_stack.addWidget(self.gantt_view)            # index 3 - VIEW_GANTT
        self.content_stack.addWidget(self.calendar_view)         # index 4 - VIEW_CALENDAR

        self.activity_log_page = QWidget()
        activity_log_page_layout = QVBoxLayout(self.activity_log_page)
        activity_log_page_layout.setContentsMargins(0, 0, 0, 0)
        log_back_row = QHBoxLayout()
        log_back_btn = QPushButton("← Back to Board")
        log_back_btn.clicked.connect(self.show_board_page)
        log_back_row.addWidget(log_back_btn)
        log_back_row.addStretch()
        activity_log_page_layout.addLayout(log_back_row)
        self.activity_log_view = ProjectActivityLogView(conn, self)
        activity_log_page_layout.addWidget(self.activity_log_view, stretch=1)

        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.board_page)          # index 0
        self.page_stack.addWidget(self.activity_log_page)    # index 1
        outer.addWidget(self.page_stack, stretch=1)

    # -- navigation --------------------------------------------------------

    def load_project(self, project_id: str) -> None:
        if self.current_project_id == project_id and self.current_board_id:
            return
        project = get_project(self.conn, project_id)
        if project is None or not project.get("main_board_id"):
            return
        self.navigate_to_board(project["main_board_id"])

    def navigate_to_board(self, board_id: str) -> None:
        board = get_project_board(self.conn, board_id)
        if board is None:
            return

        self.current_board_id = board_id
        self.current_project_id = board["project_id"]

        self.breadcrumb.set_crumbs(get_board_breadcrumb(self.conn, board_id))

        self.edit_board_btn.blockSignals(True)
        self.edit_board_btn.setChecked(False)
        self.edit_board_btn.blockSignals(False)
        self.add_col_btn.hide()
        self.kanban_widget.set_edit_mode(False)

        self.delete_board_btn.setVisible(board["parent_task_id"] is not None)

        self.kanban_widget.load_board(board_id)
        self.list_view.load_board(board_id)
        self.missing_dates_view.load_board(board_id)
        self.gantt_view.load_board(board_id)
        self.calendar_view.load_board(board_id)

        valid_views = {key for key, _ in self.VIEWS}
        last_view = board.get("last_view") if board.get("last_view") in valid_views else self.VIEW_KANBAN
        self.set_view(last_view, persist=False)

    def refresh_all_views(self) -> None:
        self.kanban_widget.refresh()
        self.list_view.refresh()
        self.missing_dates_view.refresh()
        self.gantt_view.refresh()
        self.calendar_view.refresh()
        if self.page_stack.currentWidget() is self.activity_log_page:
            self.activity_log_view.refresh()

    def show_activity_log(self) -> None:
        if not self.current_project_id:
            return
        self.activity_log_view.load_project(self.current_project_id)
        self.page_stack.setCurrentWidget(self.activity_log_page)

    def show_board_page(self) -> None:
        self.page_stack.setCurrentWidget(self.board_page)

    def set_view(self, view_key: str, persist: bool = True) -> None:
        index = [key for key, _ in self.VIEWS].index(view_key)
        self.content_stack.setCurrentIndex(index)
        btn = self.view_buttons.get(view_key)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)
        if persist and self.current_board_id:
            set_board_last_view(self.conn, self.current_board_id, view_key)

    # -- toolbar toggles -----------------------------------------------

    def _on_edit_board_toggled(self, checked: bool) -> None:
        self.edit_board_btn.setText("Done Editing" if checked else "Edit Board")
        self.add_col_btn.setVisible(checked)
        self.kanban_widget.set_edit_mode(checked)

    def _on_show_completed_toggled(self, checked: bool) -> None:
        self.kanban_widget.set_show_completed(checked)
        # The List/Missing Dates tables always show every task regardless -
        # their own "Completed" column already surfaces the status, so a
        # separate filter there would just be one more control to learn.

    # -- sub-board create/navigate/delete --------------------------------

    def open_or_create_subboard(self, task_id: str) -> None:
        subboard = get_subboard_for_task(self.conn, task_id)
        if subboard is None:
            new_depth = len(get_board_ancestry(self.conn, self.current_board_id)) + 1
            if new_depth >= PROJECT_BOARD_DEPTH_WARNING_THRESHOLD:
                reply = QMessageBox.question(
                    self, "Deep nesting",
                    f"This board would be {new_depth} levels deep - consider whether this "
                    "task should live on its own project instead.\n\nCreate the sub-board anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            try:
                subboard = add_subboard_for_task(self.conn, task_id)
            except ValueError as e:
                QMessageBox.warning(self, "Could not create sub-board", str(e))
                return
        self.navigate_to_board(subboard["id"])

    def confirm_and_delete_task(self, task_id: str) -> None:
        task = get_project_task(self.conn, task_id)
        if task is None:
            return

        subboard = get_subboard_for_task(self.conn, task_id)
        if subboard is not None:
            counts = get_subtree_counts(self.conn, subboard["id"])
            box = QMessageBox(self)
            box.setWindowTitle("Delete task and everything below it")
            box.setText(
                f'"{task["title"]}" owns a sub-board containing {counts["board_count"]} board(s) '
                f'and {counts["task_count"]} task(s) in total. Deleting this task permanently '
                "deletes everything below it too."
            )
            delete_btn = box.addButton("Delete everything below this", QMessageBox.DestructiveRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not delete_btn:
                return
        else:
            reply = QMessageBox.question(
                self, "Delete task", f'Delete "{task["title"]}"?', QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        delete_project_task(self.conn, task_id)
        self.refresh_all_views()

    def _on_delete_board_clicked(self) -> None:
        if self.current_board_id:
            self.confirm_and_delete_board(self.current_board_id)

    def confirm_and_delete_board(self, board_id: str) -> None:
        board = get_project_board(self.conn, board_id)
        if board is None or board["parent_task_id"] is None:
            return  # Main Board can't be deleted independently of its project

        counts = get_subtree_counts(self.conn, board_id)
        box = QMessageBox(self)
        box.setWindowTitle("Delete sub-board and everything below it")
        box.setText(
            f'Delete "{board["name"]}"? It contains {counts["board_count"]} board(s) and '
            f'{counts["task_count"]} task(s) in total, all of which will be permanently deleted.'
        )
        delete_btn = box.addButton("Delete everything below this", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not delete_btn:
            return

        ancestry = get_board_ancestry(self.conn, board_id)
        parent_board_id = ancestry[-2]["id"] if len(ancestry) >= 2 else None
        delete_project_board(self.conn, board_id)
        if parent_board_id:
            self.navigate_to_board(parent_board_id)


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


def _move_to_cursor_screen(widget) -> None:
    """Centers `widget` (clamped to fit) on whichever screen currently
    has the mouse cursor, rather than trusting the OS to restore it
    somewhere sensible on its own - see issue #9."""
    screen = QApplication.screenAt(QCursor.pos())
    if screen is None:
        return
    target_geo = screen.availableGeometry()
    size = widget.size()
    x = target_geo.x() + (target_geo.width() - size.width()) // 2
    y = target_geo.y() + (target_geo.height() - size.height()) // 2
    # Clamp in case the widget is larger than the target screen (e.g. a
    # big window landing on a small/low-res monitor) - otherwise
    # centering alone can push it partly off-screen.
    x = max(target_geo.x(), min(x, target_geo.x() + target_geo.width() - size.width()))
    y = max(target_geo.y(), min(y, target_geo.y() + target_geo.height() - size.height()))
    widget.move(x, y)


class MainWindow(QMainWindow):
    """The board window. Closing it (the X button) hides it to the
    system tray instead of quitting, so Kanvas keeps running in the
    background for the global quick-add hotkey - only the tray menu's
    Quit actually exits."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._quitting = False

    def closeEvent(self, event) -> None:
        if self._quitting or not QSystemTrayIcon.isSystemTrayAvailable():
            event.accept()
        else:
            event.ignore()
            self.hide()

    def quit_app(self) -> None:
        self._quitting = True
        QApplication.instance().quit()

    def show_and_raise(self) -> None:
        _move_to_cursor_screen(self)
        self.show()
        self.raise_()
        self.activateWindow()


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

    window = MainWindow()
    window.setWindowTitle(APP_TITLE)
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)

    central_stack = QStackedWidget()
    window.setCentralWidget(central_stack)

    board = KanbanBoard(conn)
    projects_hub = ProjectsHub(conn, on_back_to_boards=lambda: central_stack.setCurrentWidget(board))
    board.set_open_project_callback(
        lambda project_id: (projects_hub.load_project(project_id), central_stack.setCurrentWidget(projects_hub))
    )
    central_stack.addWidget(board)
    central_stack.addWidget(projects_hub)
    central_stack.setCurrentWidget(board)

    window.resize(1150, 640)
    window.show()

    board.shortcut_bridge = GlobalShortcutBridge()
    board.shortcut_bridge.triggered.connect(board.show_quick_add_dialog)
    board.shortcut_thread = start_global_shortcut(board.shortcut_bridge)

    tray_icon = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray_icon = QSystemTrayIcon(app_icon if not app_icon.isNull() else QIcon(), app)
        tray_icon.setToolTip(APP_TITLE)

        tray_menu = QMenu()
        open_action = tray_menu.addAction("Open Kanvas")
        open_action.triggered.connect(window.show_and_raise)
        quick_add_action = tray_menu.addAction("Quick Add Task")
        quick_add_action.triggered.connect(board.show_quick_add_dialog)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Quit Kanvas")
        quit_action.triggered.connect(window.quit_app)
        tray_icon.setContextMenu(tray_menu)

        def _on_tray_activated(reason):
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
                window.show_and_raise()
        tray_icon.activated.connect(_on_tray_activated)

        tray_icon.show()
        # Only skip quit-on-close when there's actually a tray icon to
        # fall back to - otherwise closing the window would leave the
        # process running with no visible UI and no way to quit it.
        app.setQuitOnLastWindowClosed(False)

    exit_code = app.exec()
    conn.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

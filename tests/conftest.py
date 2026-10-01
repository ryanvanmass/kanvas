import os
import sqlite3

# Must be set before Qt is imported anywhere - lets the GUI smoke tests run
# without a display (CI, containers).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import kanvas


@pytest.fixture
def conn():
    """A fresh in-memory database with the real schema (init_db), so every
    test starts from the same state as a brand-new install."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    kanvas.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def board_id(conn):
    return kanvas.get_boards(conn)[0]["id"]


@pytest.fixture
def project(conn):
    """A project plus its Main Board id and that board's first column id."""
    proj = kanvas.add_project(conn, "Test Project")
    main_board_id = kanvas.get_project(conn, proj["id"])["main_board_id"]
    column_id = kanvas.get_project_columns(conn, main_board_id)[0]["id"]
    return {"id": proj["id"], "board_id": main_board_id, "column_id": column_id}


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app

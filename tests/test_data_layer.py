import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kanvas as k  # noqa: E402


@pytest.fixture
def conn():
    c = k.get_connection(":memory:")
    k.init_db(c)
    return c


def _board(conn, name):
    return k.add_board(conn, name)


def _first_board(conn):
    return k.get_boards(conn)[0]


def test_migration_adds_cancel_columns(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, board_id TEXT NOT NULL, title TEXT NOT NULL,"
                " notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created TEXT NOT NULL,"
                " updated TEXT NOT NULL, due_date TEXT NOT NULL DEFAULT '', joplin_link TEXT NOT NULL DEFAULT '',"
                " completed INTEGER NOT NULL DEFAULT 0, completed_at TEXT NOT NULL DEFAULT '')")
    raw.execute("INSERT INTO tasks VALUES ('t1','b1','Old','', 'today','x','x','','',0,'')")
    raw.commit()
    raw.close()
    c = k.get_connection(path)
    k.init_db(c)
    t = k.get_task(c, "t1")
    assert t["title"] == "Old" and t["cancelled"] == 0 and t["cancelled_note"] == ""
    k.init_db(c)  # idempotent


def test_tags_crud_and_assignment(conn):
    b = _first_board(conn)
    t = k.add_task(conn, b["id"], "A")
    tag = k.add_tag(conn, "urgent")
    assert k.add_tag(conn, "URGENT")["id"] == tag["id"]
    k.set_task_tags(conn, t["id"], [tag["id"]])
    assert [x["name"] for x in k.get_tasks_by_status(conn, b["id"], t["status"])[0]["tags"]] == ["urgent"]
    with pytest.raises(ValueError):
        k.add_tag(conn, "  ")
    k.delete_task(conn, t["id"])
    assert conn.execute("SELECT COUNT(*) c FROM task_tags").fetchone()["c"] == 0
    t2 = k.add_task(conn, b["id"], "B")
    k.set_task_tags(conn, t2["id"], [tag["id"]])
    k.delete_tag(conn, tag["id"])
    assert k.get_task_tags(conn, t2["id"]) == []


def test_cancel_and_reopen(conn):
    b = _first_board(conn)
    t = k.add_task(conn, b["id"], "A")
    k.set_task_cancelled(conn, t["id"], True, " no longer needed ")
    got = k.get_task(conn, t["id"])
    assert got["cancelled"] == 1 and got["cancelled_note"] == "no longer needed" and got["cancelled_at"]
    k.set_task_cancelled(conn, t["id"], False)
    got = k.get_task(conn, t["id"])
    assert got["cancelled"] == 0 and got["cancelled_note"] == ""
    actions = [e["action_type"] for e in k.get_board_activity_log_entries(conn, b["id"])]
    assert "cancelled" in actions and "uncancelled" in actions


def test_move_task_to_board(conn):
    b1 = _first_board(conn)
    b2 = _board(conn, "Other")
    t = k.add_task(conn, b1["id"], "A")
    k.add_subtask(conn, t["id"], "sub")
    tag = k.add_tag(conn, "x")
    k.set_task_tags(conn, t["id"], [tag["id"]])
    k.move_task_to_board(conn, t["id"], b2["id"])
    got = k.get_task(conn, t["id"])
    assert got["board_id"] == b2["id"]
    assert got["status"] in {c["status"] for c in k.get_columns(conn, b2["id"])}
    assert len(k.get_subtasks(conn, t["id"])) == 1
    assert len(k.get_task_tags(conn, t["id"])) == 1
    for bid in (b1["id"], b2["id"]):
        assert any(e["action_type"] == "board_moved" for e in k.get_board_activity_log_entries(conn, bid))
    with pytest.raises(ValueError):
        k.move_task_to_board(conn, t["id"], b2["id"])
    with pytest.raises(ValueError):
        k.move_task_to_board(conn, t["id"], "nope")


def test_project_cancel_and_tags(conn):
    p = k.add_project(conn, "P")
    board = k.get_project_board(conn, p["main_board_id"])
    col = k.get_project_columns(conn, board["id"])[0]
    t = k.add_project_task(conn, board["id"], col["id"], "T", due_date="2026-01-05")
    k.set_project_task_cancelled(conn, t["id"], True, "dropped")
    assert k.get_project_task(conn, t["id"])["cancelled"] == 1
    tag = k.add_tag(conn, "pt")
    k.set_task_tags(conn, t["id"], [tag["id"]], "project")
    assert k.get_project_tasks_for_board(conn, board["id"])[0]["tags"][0]["name"] == "pt"
    k.delete_project_task(conn, t["id"])
    assert conn.execute("SELECT COUNT(*) c FROM project_task_tags").fetchone()["c"] == 0


def test_global_calendar(conn):
    b = _first_board(conn)
    k.add_task(conn, b["id"], "dated", due_date="2026-03-02")
    k.add_task(conn, b["id"], "undated")
    cancelled = k.add_task(conn, b["id"], "gone", due_date="2026-03-03")
    k.set_task_cancelled(conn, cancelled["id"], True)
    p = k.add_project(conn, "P")
    board = k.get_project_board(conn, p["main_board_id"])
    col = k.get_project_columns(conn, board["id"])[0]
    k.add_project_task(conn, board["id"], col["id"], "span", start_date="2026-03-01", due_date="2026-03-04")
    k.add_project_task(conn, board["id"], col["id"], "nodate")
    items = k.get_global_calendar_items(conn)
    assert [i["title"] for i in items] == ["span", "dated"]
    assert items[0]["kind"] == "project" and items[0]["start"] == "2026-03-01"
    assert len(k.get_global_calendar_items(conn, include_cancelled=True)) == 3

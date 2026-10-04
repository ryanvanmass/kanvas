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


ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly@test
DTSTAMP:20260101T000000Z
DTSTART:20260105T090000
DTEND:20260105T100000
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:trip@test
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260110
DTEND;VALUE=DATE:20260113
SUMMARY:Trip
LOCATION:Paris
END:VEVENT
BEGIN:VEVENT
UID:off@test
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260120
STATUS:CANCELLED
SUMMARY:Called off
END:VEVENT
END:VCALENDAR
"""


def test_subscription_crud_and_url_normalising(conn):
    sub = k.add_calendar_subscription(conn, " Work ", "webcal://example.com/a.ics")
    assert sub["url"] == "https://example.com/a.ics" and sub["name"] == "Work"
    with pytest.raises(ValueError):
        k.add_calendar_subscription(conn, "x", "ftp://nope")
    with pytest.raises(ValueError):
        k.add_calendar_subscription(conn, "", "https://example.com/a.ics")
    k.set_calendar_fetch_result(conn, sub["id"], ICS)
    k.set_calendar_fetch_result(conn, sub["id"], error="offline")
    got = k.get_calendar_subscription(conn, sub["id"])
    assert got["ics_text"] == ICS and got["last_error"] == "offline"  # last good feed kept
    k.update_calendar_subscription(conn, sub["id"], "Work", "https://example.com/b.ics", sub["color"], True)
    assert k.get_calendar_subscription(conn, sub["id"])["ics_text"] == ""  # new URL drops cache
    k.delete_calendar_subscription(conn, sub["id"])
    assert k.get_calendar_subscriptions(conn) == []


def test_external_items_expand_recurrence_and_all_day(conn):
    from datetime import date
    sub = k.add_calendar_subscription(conn, "Work", "https://example.com/a.ics")
    k.set_calendar_fetch_result(conn, sub["id"], ICS)
    items = k.get_external_calendar_items(conn, date(2026, 1, 1), date(2026, 1, 31))
    standups = [i for i in items if i["title"] == "Standup"]
    assert [i["start"] for i in standups] == ["2026-01-05", "2026-01-12", "2026-01-19"]
    assert standups[0]["time"] == "09:00–10:00"
    trip = next(i for i in items if i["title"] == "Trip")
    assert (trip["start"], trip["end"]) == ("2026-01-10", "2026-01-12")  # DTEND exclusive
    assert trip["location"] == "Paris" and trip["kind"] == "external"
    assert next(i for i in items if i["title"] == "Called off")["cancelled"]
    # range filtering
    assert [i["title"] for i in k.get_external_calendar_items(conn, date(2026, 1, 11), date(2026, 1, 11))] == ["Trip"]
    # disabled subscriptions vanish
    k.update_calendar_subscription(conn, sub["id"], "Work", sub["url"], sub["color"], False)
    assert k.get_external_calendar_items(conn, date(2026, 1, 1), date(2026, 1, 31)) == []


def test_bad_feed_is_skipped_and_flagged(conn):
    from datetime import date
    sub = k.add_calendar_subscription(conn, "Bad", "https://example.com/a.ics")
    k.set_calendar_fetch_result(conn, sub["id"], "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:garbage\nEND:VEVENT\nEND:VCALENDAR")
    assert k.get_external_calendar_items(conn, date(2026, 1, 1), date(2026, 1, 31)) == []


def test_fetch_ics_text_over_http(tmp_path):
    import functools, http.server, threading
    (tmp_path / "ok.ics").write_text(ICS)
    (tmp_path / "html.ics").write_text("<html>nope</html>")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert "BEGIN:VCALENDAR" in k.fetch_ics_text(f"{base}/ok.ics")
        with pytest.raises(ValueError, match="iCalendar"):
            k.fetch_ics_text(f"{base}/html.ics")
        with pytest.raises(ValueError, match="404"):
            k.fetch_ics_text(f"{base}/missing.ics")
    finally:
        server.shutdown()
    with pytest.raises(ValueError, match="Could not reach"):
        k.fetch_ics_text("http://127.0.0.1:1/x.ics", timeout=2)

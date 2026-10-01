import pytest

import kanvas


def bodies(notes):
    return [n["body"] for n in notes]


def test_initial_notes_param_becomes_first_entry_and_is_not_logged(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A", notes="  first  ")
    assert bodies(kanvas.get_task_notes(conn, task["id"])) == ["first"]
    assert "note_added" not in [e["action_type"] for e in kanvas.get_board_activity_log_entries(conn, board_id)]


def test_blank_initial_notes_creates_no_entry(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A", notes="   ")
    assert kanvas.get_task_notes(conn, task["id"]) == []


def test_notes_are_newest_first_even_within_the_same_second(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    for body in ("one", "two", "three"):
        kanvas.add_task_note(conn, task["id"], body)
    assert bodies(kanvas.get_task_notes(conn, task["id"])) == ["three", "two", "one"]


def test_edit_marks_note_edited_and_logs(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    note = kanvas.add_task_note(conn, task["id"], "old")
    assert note["edited_at"] == ""
    kanvas.update_task_note(conn, note["id"], "new")
    (stored,) = kanvas.get_task_notes(conn, task["id"])
    assert stored["body"] == "new" and stored["edited_at"] != ""
    assert "note_edited" in [e["action_type"] for e in kanvas.get_board_activity_log_entries(conn, board_id)]


def test_saving_an_unchanged_note_does_not_mark_it_edited(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    note = kanvas.add_task_note(conn, task["id"], "same")
    kanvas.update_task_note(conn, note["id"], "same")
    assert kanvas.get_task_notes(conn, task["id"])[0]["edited_at"] == ""


def test_empty_note_is_rejected(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    with pytest.raises(ValueError):
        kanvas.add_task_note(conn, task["id"], "  ")
    note = kanvas.add_task_note(conn, task["id"], "x")
    with pytest.raises(ValueError):
        kanvas.update_task_note(conn, note["id"], "")


def test_delete_note_and_cascade_on_task_delete(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    note = kanvas.add_task_note(conn, task["id"], "x")
    kanvas.add_task_note(conn, task["id"], "y")
    kanvas.delete_task_note(conn, note["id"])
    assert bodies(kanvas.get_task_notes(conn, task["id"])) == ["y"]
    kanvas.delete_task(conn, task["id"])
    assert kanvas.get_task_notes(conn, task["id"]) == []


def test_long_note_snippet_is_truncated_in_the_log(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    kanvas.add_task_note(conn, task["id"], "word " * 100)
    (entry,) = kanvas.get_board_activity_log_entries(conn, board_id, action_types=["note_added"])
    assert len(entry["description"]) < 100 and "…" in entry["description"]


def test_legacy_notes_migrate_into_timeline_once(conn, board_id):
    status = kanvas.get_columns(conn, board_id)[0]["status"]
    conn.execute(
        "INSERT INTO tasks (id, board_id, title, notes, status, created, updated) "
        "VALUES ('legacy', ?, 'Old', 'legacy text', ?, '2025-01-02T03:04:05', '2025-01-02T03:04:05')",
        (board_id, status),
    )
    conn.commit()
    kanvas.init_db(conn)
    kanvas.init_db(conn)  # idempotent
    (note,) = kanvas.get_task_notes(conn, "legacy")
    assert note["body"] == "legacy text"
    assert note["created_at"] == "2025-01-02T03:04:05"
    assert conn.execute("SELECT notes FROM tasks WHERE id = 'legacy'").fetchone()[0] == ""


def test_project_legacy_notes_migrate_too(conn, project):
    conn.execute(
        "INSERT INTO project_tasks (id, board_id, column_id, title, notes, position, created_at) "
        "VALUES ('legacy', ?, ?, 'Old', 'legacy text', 0, '2024-05-05T01:02:03')",
        (project["board_id"], project["column_id"]),
    )
    conn.commit()
    kanvas.init_db(conn)
    (note,) = kanvas.get_project_task_notes(conn, "legacy")
    assert (note["body"], note["created_at"]) == ("legacy text", "2024-05-05T01:02:03")

import kanvas


def action_types(conn, board_id, **kwargs):
    entries = kanvas.get_board_activity_log_entries(conn, board_id, **kwargs)
    return [e["action_type"] for e in entries]


def test_fresh_install_has_default_board_and_columns(conn, board_id):
    assert kanvas.get_columns(conn, board_id)


def test_add_task_logs_created_once_even_with_due_date_and_link(conn, board_id):
    kanvas.add_task(conn, board_id, "A", due_date="2026-01-01", joplin_link="https://x")
    assert action_types(conn, board_id) == ["created"]


def test_update_task_logs_one_entry_per_changed_field(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    kanvas.update_task(conn, task["id"], "B", "2026-02-02", "")
    entries = kanvas.get_board_activity_log_entries(conn, board_id, action_types=["field_changed"])
    assert sorted(e["field_name"] for e in entries) == ["due_date", "title"]


def test_update_task_with_no_changes_logs_nothing(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    kanvas.update_task(conn, task["id"], "A", "", "")
    assert action_types(conn, board_id) == ["created"]


def test_move_logs_column_names(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    columns = kanvas.get_columns(conn, board_id)
    kanvas.move_task(conn, task["id"], columns[1]["status"])
    (entry,) = kanvas.get_board_activity_log_entries(conn, board_id, action_types=["moved"])
    assert entry["old_value"] == columns[0]["name"]
    assert entry["new_value"] == columns[1]["name"]


def test_moving_to_same_column_logs_nothing(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    kanvas.move_task(conn, task["id"], task["status"])
    assert "moved" not in action_types(conn, board_id)


def test_completion_logs_only_real_transitions(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    kanvas.set_task_completed(conn, task["id"], True)
    kanvas.set_task_completed(conn, task["id"], True)
    kanvas.set_task_completed(conn, task["id"], False)
    types = action_types(conn, board_id)
    assert types.count("completed") == 1
    assert types.count("uncompleted") == 1


def test_subtask_events_are_logged(conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    sub = kanvas.add_subtask(conn, task["id"], "s")
    kanvas.set_subtask_done(conn, sub["id"], True)
    kanvas.set_subtask_done(conn, sub["id"], True)  # no-op
    kanvas.delete_subtask(conn, sub["id"])
    types = action_types(conn, board_id)
    assert types.count("subtask_added") == 1
    assert types.count("subtask_done") == 1
    assert types.count("subtask_removed") == 1


def test_log_filters(conn, board_id):
    task = kanvas.add_task(conn, board_id, "Alpha")
    kanvas.add_task(conn, board_id, "Beta")
    kanvas.set_task_completed(conn, task["id"], True)
    assert len(kanvas.get_board_activity_log_entries(conn, board_id, search_text="alpha")) == 2
    assert action_types(conn, board_id, action_types=["completed"]) == ["completed"]
    assert kanvas.get_board_activity_log_entries(conn, board_id, date_from="2999-01-01") == []
    assert kanvas.get_board_activity_log_entries(conn, board_id, date_to="2000-01-01") == []


def test_deleted_task_entries_survive_with_snapshot_title(conn, board_id):
    task = kanvas.add_task(conn, board_id, "Gone")
    kanvas.delete_task(conn, task["id"])
    entries = kanvas.get_board_activity_log_entries(conn, board_id)
    assert {e["task_title_snapshot"] for e in entries} == {"Gone"}
    assert "deleted" in [e["action_type"] for e in entries]


def test_per_task_history_is_scoped_to_that_task(conn, board_id):
    a = kanvas.add_task(conn, board_id, "A")
    b = kanvas.add_task(conn, board_id, "B")
    kanvas.add_task_note(conn, a["id"], "hi")
    assert [e["action_type"] for e in kanvas.get_board_activity_log_entries_for_task(conn, a["id"])] == [
        "note_added", "created",
    ]
    assert len(kanvas.get_board_activity_log_entries_for_task(conn, b["id"])) == 1


def test_delete_board_removes_its_log_and_notes(conn):
    other = kanvas.add_board(conn, "Other")["id"]
    task = kanvas.add_task(conn, other, "T", notes="first")
    kanvas.delete_board(conn, other)
    assert conn.execute("SELECT COUNT(*) FROM board_activity_log WHERE board_id = ?", (other,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_notes WHERE task_id = ?", (task["id"],)).fetchone()[0] == 0

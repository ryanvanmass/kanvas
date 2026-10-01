import kanvas


def log_types(conn, project_id, **kwargs):
    return [e["action_type"] for e in kanvas.get_activity_log_entries(conn, project_id, **kwargs)]


def test_project_task_lifecycle_is_logged(conn, project):
    task = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "T")
    kanvas.update_project_task(conn, task["id"], "T2", "", "", "")
    kanvas.set_project_task_completed(conn, task["id"], True)
    types = log_types(conn, project["id"])
    assert {"created", "field_changed", "completed"} <= set(types)


def test_project_update_does_not_log_notes_as_a_field(conn, project):
    task = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "T")
    kanvas.update_project_task(conn, task["id"], "T", "", "", "")
    assert "field_changed" not in log_types(conn, project["id"])


def test_project_notes_crud_and_logging(conn, project):
    task = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "T", notes="first")
    note = kanvas.add_project_task_note(conn, task["id"], "second")
    kanvas.update_project_task_note(conn, note["id"], "second!")
    assert [n["body"] for n in kanvas.get_project_task_notes(conn, task["id"])] == ["second!", "first"]
    kanvas.delete_project_task_note(conn, note["id"])
    types = log_types(conn, project["id"])
    assert {"note_added", "note_edited", "note_deleted"} <= set(types)


def test_deleting_project_task_removes_its_notes(conn, project):
    task = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "T", notes="n")
    kanvas.delete_project_task(conn, task["id"])
    assert kanvas.get_project_task_notes(conn, task["id"]) == []


def test_deleting_project_board_removes_subtree_notes(conn, project):
    parent = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "Parent")
    sub = kanvas.add_subboard_for_task(conn, parent["id"])
    sub_col = kanvas.get_project_columns(conn, sub["id"])[0]["id"]
    child = kanvas.add_project_task(conn, sub["id"], sub_col, "Child", notes="deep")
    kanvas.delete_project_board(conn, sub["id"])
    assert kanvas.get_project_task_notes(conn, child["id"]) == []


def test_duplicate_project_copies_note_timeline_in_order(conn, project):
    task = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "T", notes="first")
    kanvas.add_project_task_note(conn, task["id"], "second")
    clone = kanvas.duplicate_project(conn, project["id"])
    clone_board = kanvas.get_project(conn, clone["id"])["main_board_id"]
    (cloned_task,) = kanvas.get_project_tasks_for_board(conn, clone_board)
    notes = kanvas.get_project_task_notes(conn, cloned_task["id"])
    assert [n["body"] for n in notes] == ["second", "first"]  # newest first, same order as the source


def test_log_entries_survive_task_deletion(conn, project):
    task = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "Gone")
    kanvas.delete_project_task(conn, task["id"])
    entries = kanvas.get_activity_log_entries(conn, project["id"])
    assert {e["task_title_snapshot"] for e in entries} == {"Gone"}

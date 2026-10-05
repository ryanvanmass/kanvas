"""Offscreen smoke tests: build the dialogs this project's recent features
touch and drive their main interactions. These catch wiring mistakes
(missing attributes, broken signal hookups, layout construction errors)
that data-layer tests can't - they don't judge how anything looks."""
import kanvas
from PySide6.QtWidgets import QLabel, QTextEdit


def make_timeline(conn, task_id):
    return kanvas.NotesTimelineWidget(
        lambda: kanvas.get_task_notes(conn, task_id),
        lambda body: kanvas.add_task_note(conn, task_id, body),
        lambda note_id, body: kanvas.update_task_note(conn, note_id, body),
        lambda note_id: kanvas.delete_task_note(conn, note_id),
    )


def test_timeline_posts_notes_and_clears_composer(qapp, conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    widget = make_timeline(conn, task["id"])
    widget.composer.setPlainText("**bold** note")
    widget._post_note()
    assert widget.composer.toPlainText() == ""
    assert [n["body"] for n in kanvas.get_task_notes(conn, task["id"])] == ["**bold** note"]


def test_timeline_ignores_blank_post(qapp, conn, board_id):
    task = kanvas.add_task(conn, board_id, "A")
    widget = make_timeline(conn, task["id"])
    widget.composer.setPlainText("   ")
    widget._post_note()
    assert kanvas.get_task_notes(conn, task["id"]) == []


def test_timeline_renders_markdown_and_edit_flow(qapp, conn, board_id):
    task = kanvas.add_task(conn, board_id, "A", notes="hello")
    widget = make_timeline(conn, task["id"])
    labels = widget._entries_host.findChildren(QLabel)
    assert any("hello" in label.text() for label in labels)

    note_id = kanvas.get_task_notes(conn, task["id"])[0]["id"]
    widget._start_edit(note_id)
    editor = widget._entries_host.findChild(QTextEdit)
    editor.setPlainText("changed")
    widget._save_edit(note_id, editor)
    assert kanvas.get_task_notes(conn, task["id"])[0]["body"] == "changed"
    assert widget._editing_note_id is None


def test_standard_task_card_has_tabs_and_history(qapp, conn, board_id):
    task = kanvas.add_task(conn, board_id, "A", notes="n")
    columns = kanvas.get_columns(conn, board_id)
    dialog = kanvas.TaskCardDialog(conn, kanvas.get_task(conn, task["id"]), columns)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == ["Details", "Notes", "History"]
    assert dialog.history_table.rowCount() == 1  # "created"
    assert "notes" not in dialog.result_values()

    kanvas.add_subtask(conn, task["id"], "s")
    dialog.tabs.setCurrentIndex(0)
    dialog.tabs.setCurrentIndex(2)  # reloads on open
    assert dialog.history_table.rowCount() == 2


def test_project_task_card_has_notes_tab(qapp, conn, project):
    task = kanvas.add_project_task(conn, project["board_id"], project["column_id"], "T", notes="n")
    columns = kanvas.get_project_columns(conn, project["board_id"])
    dialog = kanvas.ProjectTaskCardDialog(conn, kanvas.get_project_task(conn, task["id"]), columns, None)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == ["Details", "Notes", "History"]
    assert "notes" not in dialog.result_values()


def test_board_activity_log_dialog_filters(qapp, conn, board_id):
    kanvas.add_task(conn, board_id, "Alpha")
    kanvas.add_task(conn, board_id, "Beta")
    dialog = kanvas.BoardActivityLogDialog(conn, board_id, "Board")
    assert dialog.table.rowCount() == 2
    dialog.search_edit.setText("alpha")
    assert dialog.table.rowCount() == 1


def test_kanban_board_builds_and_exposes_activity_log_button(qapp, conn):
    board = kanvas.KanbanBoard(conn)
    assert board.activity_log_btn.text() == "Activity Log"


def test_new_project_task_dialog_returns_initial_notes(qapp, conn, project):
    columns = kanvas.get_project_columns(conn, project["board_id"])
    dialog = kanvas.NewProjectTaskDialog(columns, columns[0]["id"])
    dialog.title_edit.setText("T")
    dialog.notes_edit.setPlainText("seed note")
    values = dialog.result_values()
    assert values["notes"] == "seed note"
    task = kanvas.add_project_task(
        conn, project["board_id"], values["column_id"], values["title"], values["notes"],
    )
    assert [n["body"] for n in kanvas.get_project_task_notes(conn, task["id"])] == ["seed note"]


def test_every_dialog_result_matches_what_its_caller_expects(qapp, conn, board_id, project):
    """Regression for result_values() drifting from the code that consumes
    it: the creation dialogs keep "notes" (the initial timeline entry), the
    task cards don't (notes live in the Notes tab)."""
    columns = kanvas.get_columns(conn, board_id)
    new_task = kanvas.NewTaskDialog(columns, columns[0]["status"])
    assert "notes" in new_task.result_values()

    task = kanvas.add_task(conn, board_id, "A")
    card = kanvas.TaskCardDialog(conn, kanvas.get_task(conn, task["id"]), columns)
    assert "notes" not in card.result_values()


def test_markdown_renders_formatting_and_recolours_links(qapp):
    html = kanvas._markdown_to_html("**bold** [site](https://example.com)")
    assert "font-weight" in html and "https://example.com" in html
    assert "#0000ff" not in html and "#8AB4F8" in html

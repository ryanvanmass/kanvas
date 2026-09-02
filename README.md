<p align="center">
  <img src="assets/kanvas_logo_128.png" alt="Kanvas logo" width="96">
</p>

<h1 align="center">Kanvas</h1>

<p align="center">A small desktop Kanban board app built with PySide6 (Qt for Python) and SQLite.</p>

## Features

- Multiple independent boards, each with its own columns and tasks
- Drag-and-drop tasks between columns
- Add, rename, reorder, and delete columns (behind an "Edit Board" mode)
- Add, rename, reorder, and delete boards from the side panel (hamburger menu)
- Tasks support notes, an optional due date, subtasks, and an optional link (e.g. a Joplin note)
- Data is stored locally in a SQLite database — no account or internet connection required

## Requirements

- Python 3.9+
- [PySide6](https://pypi.org/project/PySide6/)
- Windows or Linux (tested on Fedora)

## Installation

```bash
pip install PySide6
```

## Usage

```bash
python3 kanvas.py
```

On first run, a board named "My Board" is created automatically with four starter columns: Today, In Progress, Blocked, and Complete.

## Data storage

Kanvas keeps its SQLite database outside the project folder, so your tasks persist across updates:

| OS      | Location                                  |
|---------|--------------------------------------------|
| Linux   | `~/.local/share/kanban_board/kanban.db`    |
| Windows | `%APPDATA%\KanbanBoard\kanban.db`          |

## License

No license has been chosen yet. All rights reserved by default until one is added.

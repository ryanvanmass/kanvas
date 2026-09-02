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

## Building your own package

There's no CI/release pipeline yet — these are manual, local build steps using [PyInstaller](https://pyinstaller.org/).

```bash
pip install pyinstaller
```

### Windows (.exe)

```bash
pyinstaller --onefile --windowed --name kanvas kanvas.py
```

The executable is written to `dist/kanvas.exe`.

### Linux (.deb / .rpm)

Linux packages are easiest to build with [fpm](https://github.com/jordansissel/fpm), which wraps a folder of files into a native package.

```bash
# 1. Freeze the app into a folder
pyinstaller --onedir --windowed --name kanvas kanvas.py

# 2. Install fpm (needs Ruby)
sudo dnf install ruby ruby-devel gcc make rpm-build   # Fedora
gem install --no-document fpm

# 3. Package dist/kanvas/ as a .deb or .rpm
fpm -s dir -t deb -n kanvas -v 1.0.0 --prefix /opt/kanvas -C dist/kanvas .
fpm -s dir -t rpm -n kanvas -v 1.0.0 --prefix /opt/kanvas -C dist/kanvas .
```

This produces `kanvas_1.0.0_amd64.deb` and `kanvas-1.0.0.x86_64.rpm` in the current directory, installing the app to `/opt/kanvas`. Add a symlink into `/usr/local/bin` (fpm's `--after-install` hook can do this) if you want a `kanvas` command on the PATH.

## License

No license has been chosen yet. All rights reserved by default until one is added.

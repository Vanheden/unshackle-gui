# Unshackle GUI

A graphical interface for [Unshackle](https://github.com/unshackle-dl/unshackle) — modular movie, TV, and music archival software. This program was purely done with AI just out of curiosity.

## Features

- Full access to all `unshackle dl` options through a scrollable form
- Live colored terminal output (via Windows ConPTY + pyte VT100 emulator)
- Download queue — add multiple items and run them sequentially
- Built-in config editor for `unshackle.yaml`
- Command preview before running
- Standalone `.exe` build via PyInstaller

## Requirements

- [Unshackle](https://github.com/unshackle-dl/unshackle) installed and available in `PATH`
- Python 3.10–3.12

### PATH setup (required for the standalone .exe)

The standalone `.exe` locates Unshackle by searching `PATH`. Add Unshackle's virtual-environment Scripts folder to your user PATH:

```
D:\unshackle\.venv\Scripts
```

Replace `D:\unshackle` with wherever you installed Unshackle. After adding it, restart Windows (or open a new session) for the change to take effect.

> If you run the GUI from source via `uv run python gui.py`, PATH is set up automatically — no manual step needed.

## Run from source

```bash
pip install customtkinter pywinpty pyte
python gui.py
```

Or with uv (recommended if you use Unshackle's own environment):

```bash
uv pip install customtkinter pywinpty pyte
uv run python gui.py
```

## Build standalone .exe

Double-click **`build.bat`** — it installs dependencies and produces `dist\unshackle-gui.exe`.

> **Note:** The `.exe` bundles only the GUI. Unshackle itself (plus `ffmpeg`, `mp4decrypt`, etc.) must still be installed and available in `PATH`.

## Tabs

| Tab | Description |
|-----|-------------|
| **Download** | All `dl` options + live console on the right |
| **Queue** | Schedule multiple downloads to run in order |
| **Console** | Full-page terminal output |
| **Config** | Load, edit and save `unshackle.yaml` |

## Educational Purpose Only

## License

This software is licensed under the terms of the GNU General Public License, Version 3.0.
You can find a copy of the license in the [LICENSE](LICENSE) file in the root folder.

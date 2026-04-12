#!/usr/bin/env python3
"""Unshackle GUI — Graphical interface for the Unshackle download tool."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

# ── winpty (ConPTY) — optional but required for live rich output ───────────────
try:
    from winpty import PtyProcess as _PtyProcess  # type: ignore
    HAS_WINPTY = True
except Exception:
    HAS_WINPTY = False

# ── Theme ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── Constants ──────────────────────────────────────────────────────────────────

def _discover_services() -> list[str]:
    """
    Auto-detect installed Unshackle services by scanning the services/ directory.

    Priority:
      1. unshackle Python package — works for any pip/uv install regardless
         of where the package lives (site-packages, dev editable, etc.)
      2. importlib: locate the package without importing it (faster, no side
         effects) — covers frozen / unusual installs
      3. Relative paths from gui.py — covers running directly next to the repo
      4. Relative to the unshackle executable in PATH
      5. Hard-coded fallback list
    """
    import shutil

    _SKIP = {"__pycache__", "EXAMPLE"}

    def _scan(p: Path) -> list[str] | None:
        if not p.is_dir():
            return None
        names = sorted(
            d.name for d in p.iterdir()
            if d.is_dir() and d.name not in _SKIP and not d.name.startswith(".")
        )
        return names or None

    # 1. Ask the installed unshackle package directly — most reliable
    try:
        from unshackle.core.config import config as _cfg  # type: ignore
        for svc_dir in _cfg.directories.services:
            result = _scan(Path(svc_dir))
            if result:
                return result
    except Exception:
        pass

    # 2. Locate the package via importlib without fully importing it
    try:
        import importlib.util
        spec = importlib.util.find_spec("unshackle")
        if spec and spec.submodule_search_locations:
            for loc in spec.submodule_search_locations:
                result = _scan(Path(loc) / "services")
                if result:
                    return result
    except Exception:
        pass

    # 3. Relative paths from gui.py
    gui_dir = Path(__file__).resolve().parent
    for rel in (
        "unshackle/services",
        "../unshackle/services",
        "../../unshackle/services",
    ):
        result = _scan(gui_dir / rel)
        if result is not None:
            return result

    # 4. Relative to the unshackle executable found in PATH
    exe = shutil.which("unshackle")
    if exe:
        exe_dir = Path(exe).resolve().parent
        for rel in ("../unshackle/services", "../../unshackle/services"):
            result = _scan(exe_dir / rel)
            if result is not None:
                return result

    # 5. Hard-coded fallback — at least something shows up
    return [
        "ABMA", "ADN", "AMZN", "ATV", "CR", "DSMART", "DSNP",
        "EXXEN", "GLBO", "HIDI", "HMAX", "HPLA", "HULUJP", "ITUNES",
        "KCW", "KNPY", "MUBI", "NF", "NPO", "PLTV", "PMTP", "SKST",
        "SVTP", "TOD", "UNEXT", "UNXT", "UPLAY", "VIDO", "VIKI", "VRT", "iQ",
    ]


SERVICES = _discover_services()
VIDEO_CODECS = ["H.264", "H.265", "VP9", "AV1", "VC-1", "VP8"]
AUDIO_CODECS = ["AAC", "DD", "DD+", "FLAC", "DTS", "OPUS", "ALAC"]
COLOR_RANGES = ["SDR", "HLG", "HDR10", "HDR10+", "DV", "HYBRID"]
QUALITIES    = ["2160p", "1080p", "720p", "480p", "360p", "240p"]
SUB_FORMATS  = ["", "SRT", "ASS", "TTML", "VTT", "STPP", "WVTT"]

# ── pyte VT100 renderer ───────────────────────────────────────────────────────
try:
    import pyte as _pyte
    HAS_PYTE = True
except ImportError:
    HAS_PYTE = False

# ── ANSI writer (pipe fallback, no pyte) ──────────────────────────────────────
# Catppuccin Mocha palette — matches unshackle's own theme
_FG16: dict[int, str] = {
    30: "#45475a", 31: "#f38ba8", 32: "#a6e3a1", 33: "#f9e2af",
    34: "#89b4fa", 35: "#f5c2e7", 36: "#94e2d5", 37: "#cdd6f4",
    90: "#585b70", 91: "#f38ba8", 92: "#a6e3a1", 93: "#f9e2af",
    94: "#89b4fa", 95: "#f5c2e7", 96: "#94e2d5", 97: "#cdd6f4",
}

# Matches CSI escape sequences like \x1b[1;32m or \x1b[2K
_ANSI_RE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


def _256color(n: int) -> str:
    if n < 16:
        return list(_FG16.values())[n % 16]
    if n < 232:
        n -= 16
        b, n = n % 6, n // 6
        g, r = n % 6, n // 6
        return f"#{r*51:02x}{g*51:02x}{b*51:02x}"
    v = (n - 232) * 10 + 8
    return f"#{v:02x}{v:02x}{v:02x}"


class AnsiWriter:
    """Writes ANSI-escaped text to a CTkTextbox with full color support."""

    def __init__(self, box: ctk.CTkTextbox) -> None:
        self._box = box
        self._tk  = box._textbox          # underlying tk.Text
        self._tags: list[str] = []
        self._known_tags: set[str] = set()
        self._setup_base_tags()

    def _setup_base_tags(self) -> None:
        tk = self._tk
        tk.tag_configure("bold",      font=("Consolas", 13, "bold"))
        tk.tag_configure("italic",    font=("Consolas", 13, "bold italic"))
        tk.tag_configure("underline", underline=True)
        tk.tag_configure("dim",       foreground="#585b70")
        for code, color in _FG16.items():
            tag = f"fg{code}"
            tk.tag_configure(tag, foreground=color)
            self._known_tags.add(tag)

    def _color_tag(self, color: str) -> str:
        tag = f"c{color.lstrip('#')}"
        if tag not in self._known_tags:
            self._tk.tag_configure(tag, foreground=color)
            self._known_tags.add(tag)
        return tag

    def _apply_sgr(self, params_str: str) -> None:
        params = [int(p) if p else 0 for p in params_str.split(";")]
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self._tags.clear()
            elif p == 1:
                if "bold"      not in self._tags: self._tags.append("bold")
            elif p == 2:
                if "dim"       not in self._tags: self._tags.append("dim")
            elif p == 3:
                if "italic"    not in self._tags: self._tags.append("italic")
            elif p == 4:
                if "underline" not in self._tags: self._tags.append("underline")
            elif p in (22, 23, 24):
                self._tags = [t for t in self._tags
                               if t not in ("bold", "dim", "italic", "underline")]
            elif p == 39:
                self._tags = [t for t in self._tags
                               if not (t.startswith("fg") or t.startswith("c"))]
            elif (30 <= p <= 37) or (90 <= p <= 97):
                self._tags = [t for t in self._tags
                               if not (t.startswith("fg") or t.startswith("c"))]
                self._tags.append(f"fg{p}")
            elif p == 38 and i + 1 < len(params):
                self._tags = [t for t in self._tags
                               if not (t.startswith("fg") or t.startswith("c"))]
                nxt = params[i + 1]
                if nxt == 5 and i + 2 < len(params):
                    self._tags.append(self._color_tag(_256color(params[i + 2])))
                    i += 2
                elif nxt == 2 and i + 4 < len(params):
                    r, g, b = params[i+2], params[i+3], params[i+4]
                    self._tags.append(self._color_tag(f"#{r:02x}{g:02x}{b:02x}"))
                    i += 4
            i += 1

    # ── Public API ─────────────────────────────────────────────────────────────

    def write(self, text: str) -> None:
        """Parse ANSI codes and write colored text. Must be called on main thread."""
        self._box.configure(state="normal")
        pos = 0
        for m in _ANSI_RE.finditer(text):
            if m.start() > pos:
                self._write_plain(text[pos:m.start()])
            pos = m.end()
            params, cmd = m.group(1), m.group(2)
            if cmd == "m":
                self._apply_sgr(params or "0")
            elif cmd in ("A", "F"):            # cursor up
                self._cursor_up(int(params) if params else 1)
            elif cmd == "K":                   # erase line
                p = int(params) if params else 0
                if p == 2:   self._erase_line()
                elif p == 0: self._erase_to_eol()
            # other CSI codes (cursor move, etc.) are silently ignored

        if pos < len(text):
            self._write_plain(text[pos:])

        self._tk.see("end")
        self._box.configure(state="disabled")

    def _write_plain(self, text: str) -> None:
        parts = text.split("\r")
        self._insert(parts[0])
        for part in parts[1:]:
            self._go_to_bol()
            self._overwrite(part)

    def _insert(self, text: str) -> None:
        if text:
            self._tk.insert("end", text, tuple(self._tags))

    def _overwrite(self, text: str) -> None:
        if not text:
            return
        cur  = self._tk.index("insert")
        col = cur.split(".")[1]
        avail = int(self._tk.index(f"{cur} lineend").split(".")[1]) - int(col)
        if avail > 0:
            end = self._tk.index(f"{cur}+{min(len(text), avail)}c")
            self._tk.delete(cur, end)
        self._tk.insert(cur, text, tuple(self._tags))

    def _go_to_bol(self) -> None:
        line = self._tk.index("insert").split(".")[0]
        self._tk.mark_set("insert", f"{line}.0")

    def _erase_line(self) -> None:
        line = self._tk.index("insert").split(".")[0]
        self._tk.delete(f"{line}.0", f"{line}.end")
        self._tk.mark_set("insert", f"{line}.0")

    def _erase_to_eol(self) -> None:
        cur = self._tk.index("insert")
        self._tk.delete(cur, f"{cur} lineend")

    def _cursor_up(self, n: int) -> None:
        cur  = self._tk.index("insert")
        line = max(1, int(cur.split(".")[0]) - n)
        col  = cur.split(".")[1]
        self._tk.mark_set("insert", f"{line}.{col}")

    def clear(self) -> None:
        self._box.configure(state="normal")
        self._tk.delete("1.0", "end")
        self._box.configure(state="disabled")
        self._tags.clear()


# ── PtyRenderer — pyte-based VT100 emulator ───────────────────────────────────

# Catppuccin Mocha: maps pyte colour names → hex
_PYTE_COLORS: dict[str, str] = {
    "default":       "#cdd6f4",
    # standard (pyte names, no spaces/underscores for brights)
    "black":         "#45475a",  "red":           "#f38ba8",
    "green":         "#a6e3a1",  "yellow":        "#f9e2af",
    "blue":          "#89b4fa",  "magenta":       "#f5c2e7",
    "cyan":          "#94e2d5",  "white":         "#cdd6f4",
    # bright — pyte uses "brightX" (no separator)
    "brightblack":   "#585b70",  "brightred":     "#f38ba8",
    "brightgreen":   "#a6e3a1",  "brightyellow":  "#f9e2af",
    "brightblue":    "#89b4fa",  "brightmagenta": "#f5c2e7",
    "brightcyan":    "#94e2d5",  "brightwhite":   "#cdd6f4",
    # underscore variants (older pyte / other sources)
    "bright_black":  "#585b70",  "bright_red":    "#f38ba8",
    "bright_green":  "#a6e3a1",  "bright_yellow": "#f9e2af",
    "bright_blue":   "#89b4fa",  "bright_magenta":"#f5c2e7",
    "bright_cyan":   "#94e2d5",  "bright_white":  "#cdd6f4",
}


class PtyRenderer:
    """
    Feeds PTY bytes into a pyte VT100 screen and periodically renders
    the resulting screen buffer (with colours) to one or more CTkTextboxes.
    """

    def __init__(self, *boxes: ctk.CTkTextbox, cols: int = 220, rows: int = 50) -> None:
        self._boxes = boxes
        self._tks   = [b._textbox for b in boxes]
        self._lock  = threading.Lock()
        self._known_tags: set[str] = set()
        # Per-textbox cache of the last rendered rows — used for incremental updates.
        self._last_rows: list[list[list[tuple[str, str, bool]]]] = [[] for _ in boxes]

        if HAS_PYTE:
            self._screen = _pyte.HistoryScreen(cols, rows, history=2000)
            self._stream = _pyte.ByteStream(self._screen)
        else:
            self._screen = None
            self._stream = None

        for tk in self._tks:
            self._setup_tags(tk)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _setup_tags(self, tk: "tk.Text") -> None:  # type: ignore[name-defined]
        normal_font = ctk.CTkFont(family="Consolas", size=13, weight="bold")
        bold_font   = ctk.CTkFont(family="Consolas", size=13, weight="bold")
        for name, color in _PYTE_COLORS.items():
            for bold in (False, True):
                tag = f"pt_p{name}_{'B' if bold else 'n'}"
                tk.tag_configure(tag, foreground=color,
                                 font=bold_font if bold else normal_font)
                self._known_tags.add(tag)

    def _color_hex(self, name: str) -> str:
        if not name or name == "default":
            return _PYTE_COLORS["default"]
        if name.startswith("#"):
            return name
        # pyte stores truecolor (SGR 38;2) and 256-color (SGR 38;5) as bare
        # 6-char hex strings without the leading '#'
        if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
            return f"#{name}"
        return _PYTE_COLORS.get(name, _PYTE_COLORS["default"])

    def _ensure_tag(self, tk: "tk.Text", fg: str, bold: bool) -> str:  # type: ignore[name-defined]
        # Fast path: named colours were pre-registered in _setup_tags
        prebuilt = f"pt_p{fg}_{'B' if bold else 'n'}"
        if prebuilt in self._known_tags:
            return prebuilt

        # Normalise bare hex (pyte gives "rrggbb", we want "#rrggbb") so we
        # never create two separate tags for the same colour.
        if len(fg) == 6 and all(c in "0123456789abcdefABCDEF" for c in fg):
            fg = f"#{fg}"

        safe = fg.replace("#", "x")
        tag  = f"pt_{safe}_{'B' if bold else 'n'}"
        if tag not in self._known_tags:
            color = self._color_hex(fg)
            font  = ctk.CTkFont(family="Consolas", size=13, weight="bold")
            for t in self._tks:
                t.tag_configure(tag, foreground=color, font=font)
            self._known_tags.add(tag)
        return tag

    # ── public API ────────────────────────────────────────────────────────────

    def feed(self, data: bytes | str) -> None:
        if self._stream is None:
            return
        raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        with self._lock:
            self._stream.feed(raw)

    def _write_row(self, tk: "tk.Text", cells: list[tuple[str, str, bool]],  # type: ignore[name-defined]
                   pos: str) -> None:
        """Insert a single row's cells at *pos* (Tk index or 'end').
        Consecutive cells with the same style are merged into one insert call."""
        i = 0
        while i < len(cells):
            char, fg, bold = cells[i]
            tag  = self._ensure_tag(tk, fg, bold)
            text = char
            i += 1
            while i < len(cells) and cells[i][1] == fg and cells[i][2] == bold:
                text += cells[i][0]
                i += 1
            tk.insert(pos, text, (tag,))

    def render(self) -> None:
        """Render pyte screen (+ scrollback history) to all textboxes.
        Uses incremental line-by-line updates so the widget is never blanked —
        eliminates flicker during long output. Auto-scrolls only when already
        at the bottom; preserves scroll position otherwise."""
        if self._screen is None:
            return

        with self._lock:
            screen = self._screen

            def _snapshot_row(row_dict) -> list[tuple[str, str, bool]]:
                cells: list[tuple[str, str, bool]] = []
                for c in range(screen.columns):
                    ch = row_dict.get(c)
                    cells.append((ch.data, ch.fg, ch.bold) if ch
                                  else (" ", "default", False))
                while cells and cells[-1][0] in (" ", "\x00"):
                    cells.pop()
                return cells

            # ── history rows (HistoryScreen.history.top) ──────────────────────
            rows_data: list[list[tuple[str, str, bool]]] = []
            if hasattr(screen, "history"):
                for hist_row in screen.history.top:
                    rows_data.append(_snapshot_row(hist_row))

            # ── current visible rows ───────────────────────────────────────────
            last_row = -1
            for r in range(screen.lines):
                if any(c.data.strip() for c in screen.buffer[r].values()):
                    last_row = r
            for r in range(last_row + 1):
                rows_data.append(_snapshot_row(screen.buffer[r]))

        if not rows_data:
            return

        # ── incremental update for every textbox ───────────────────────────────
        for box_idx, (box, tk) in enumerate(zip(self._boxes, self._tks)):
            last     = self._last_rows[box_idx]
            new_len  = len(rows_data)
            old_len  = len(last)

            # Fast path: nothing changed since last render
            if rows_data == last:
                continue

            at_bottom = tk.yview()[1] >= 0.99
            box.configure(state="normal")

            for i, cells in enumerate(rows_data):
                if i < old_len:
                    # Row already exists in the widget — skip if unchanged
                    if cells == last[i]:
                        continue
                    # Update in-place: clear line content then re-insert.
                    # The trailing \n is NOT deleted so the widget never goes blank.
                    line = i + 1
                    tk.delete(f"{line}.0", f"{line}.end")
                    if cells:
                        tk.mark_set("insert", f"{line}.0")
                        self._write_row(tk, cells, "insert")
                else:
                    # Append new row at the end
                    if i > 0 or old_len > 0:
                        tk.insert("end", "\n")
                    if cells:
                        self._write_row(tk, cells, "end")

            # Remove any trailing rows that no longer exist
            if old_len > new_len:
                tk.delete(f"{new_len}.end", "end")

            if at_bottom:
                tk.see("end")
            box.configure(state="disabled")

            # Store a copy for next-cycle comparison
            self._last_rows[box_idx] = [row[:] for row in rows_data]

    def clear(self) -> None:
        if self._screen is not None:
            with self._lock:
                self._screen.reset()
        self._last_rows = [[] for _ in self._boxes]
        for box, tk in zip(self._boxes, self._tks):
            box.configure(state="normal")
            tk.delete("1.0", "end")
            box.configure(state="disabled")


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_unshackle() -> list[str]:
    """Return the command to invoke unshackle in the current Python environment."""
    import shutil

    # 1. Look in PATH first (works when running via `uv run python gui.py`)
    found = shutil.which("unshackle")
    if found:
        return [found]

    # 2. Not frozen — look next to the current Python exe (normal venv)
    exe = Path(sys.executable)
    if not getattr(sys, "frozen", False):
        for name in ("unshackle", "unshackle.exe"):
            candidate = exe.parent / name
            if candidate.is_file():
                return [str(candidate)]
        return [str(exe), "-m", "unshackle"]

    # 3. Frozen (PyInstaller) — GUI exe is typically in dist/, project root is
    #    one level up. Find unshackle.exe directly in the project's .venv.
    gui_dir = exe.parent
    for project_dir in (gui_dir.parent, gui_dir):
        for venv_scripts in (
            project_dir / ".venv" / "Scripts",
            project_dir / ".venv" / "bin",
        ):
            for name in ("unshackle", "unshackle.exe"):
                candidate = venv_scripts / name
                if candidate.is_file():
                    return [str(candidate)]

    # 4. Fall back to uv run with explicit project directory
    uv = shutil.which("uv")
    for uv_candidate in filter(None, [
        uv,
        str(Path.home() / ".cargo" / "bin" / "uv.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "uv" / "bin" / "uv.exe"),
    ]):
        if Path(uv_candidate).is_file():
            for project_dir in (gui_dir.parent, gui_dir):
                if (project_dir / "pyproject.toml").is_file():
                    return [uv_candidate, "run", "--project", str(project_dir), "unshackle"]
            return [uv_candidate, "run", "unshackle"]

    # 5. Last resort
    return ["unshackle"]


def _section(parent: ctk.CTkScrollableFrame, text: str) -> None:
    ctk.CTkLabel(
        parent, text=text,
        font=ctk.CTkFont(size=13, weight="bold"),
        anchor="w", text_color=("#1565c0", "#4da6ff"),
    ).pack(fill="x", padx=8, pady=(14, 2))
    ctk.CTkFrame(parent, height=1, fg_color=("#1565c0", "#335577")).pack(
        fill="x", padx=8, pady=(0, 4))


def _row(parent: ctk.CTkScrollableFrame) -> ctk.CTkFrame:
    f = ctk.CTkFrame(parent, fg_color="transparent")
    f.pack(fill="x", padx=8, pady=3)
    return f


def _lbl(parent: ctk.CTkFrame, text: str, width: int = 150) -> None:
    ctk.CTkLabel(parent, text=text, width=width, anchor="w").pack(side="left")


def _entry(parent: ctk.CTkFrame, width: int = 160,
           placeholder: str = "") -> ctk.CTkEntry:
    e = ctk.CTkEntry(parent, width=width, placeholder_text=placeholder)
    e.pack(side="left", padx=(0, 10))
    return e


def _combo(parent: ctk.CTkFrame, values: list[str],
           width: int = 160) -> ctk.CTkComboBox:
    c = ctk.CTkComboBox(parent, values=values, width=width)
    c.pack(side="left", padx=(0, 10))
    return c


def _check(parent: ctk.CTkFrame, text: str,
           var: ctk.BooleanVar, width: int = 130) -> None:
    ctk.CTkCheckBox(parent, text=text, variable=var, width=width).pack(
        side="left", padx=(0, 6))


# ── Queue item ─────────────────────────────────────────────────────────────────

class QueueItem:
    def __init__(self, cmd: list[str], label: str) -> None:
        self.cmd    = cmd
        self.label  = label
        self.status = "Pending"


# ── Main application ───────────────────────────────────────────────────────────

class UnshackleGUI(ctk.CTk):

    def __init__(self) -> None:
        super().__init__()
        self.title("Unshackle GUI")
        self.geometry("1300x860")
        self.minsize(960, 640)

        self._dl_queue: list[QueueItem] = []
        self._queue_lock   = threading.Lock()
        self._out_queue: queue.Queue[str] = queue.Queue()
        self._active_proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._active_pty: "_PtyProcess | None" = None
        self._pty_active   = False          # True while PTY process is running
        self._queue_rows: list[ctk.CTkFrame] = []
        self._config_path: Path | None = None
        if getattr(sys, "frozen", False):
            self._settings_path = Path(sys.executable).parent / "gui_settings.json"
        else:
            self._settings_path = Path(__file__).resolve().parent / "gui_settings.json"
        # Created after UI is built (textboxes must exist first)
        self._pty_renderer: PtyRenderer | None = None
        self._ansi_inline:  AnsiWriter  | None = None
        self._ansi_console: AnsiWriter  | None = None

        self._build_ui()
        self._poll_output()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_settings()

    # ─────────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)

        self._tabs = ctk.CTkTabview(self, anchor="nw")
        self._tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 0))

        for name in ("Download", "Queue", "Console", "Config"):
            self._tabs.add(name)

        self._build_download_tab()
        self._build_queue_tab()
        self._build_console_tab()
        self._build_config_tab()

        # ── Status bar ────────────────────────────────────────────────────────
        status_bar = ctk.CTkFrame(self, height=28, fg_color=("gray85", "gray17"))
        status_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(3, 8))
        status_bar.grid_propagate(False)
        self._status_label = ctk.CTkLabel(
            status_bar, text="Idle",
            text_color=("#666666", "#888888"),
            font=ctk.CTkFont(size=12),
            anchor="w",
        )
        self._status_label.pack(side="left", padx=10, pady=4)

    # ── Download tab ──────────────────────────────────────────────────────────

    def _build_download_tab(self) -> None:
        import tkinter as _tk
        tab = self._tabs.tab("Download")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        # Draggable paned window — user can resize left/right panels
        paned = _tk.PanedWindow(
            tab, orient="horizontal",
            sashwidth=6, sashpad=0, sashrelief="flat",
            bg="#2b2b2b", handlesize=0,
            opaqueresize=False,
        )
        paned.grid(row=0, column=0, sticky="nsew")

        # Left: plain frame wrapper → scrollable form inside
        left = _tk.Frame(paned, bg="#2b2b2b")
        paned.add(left, minsize=260, width=390, stretch="never")
        form = ctk.CTkScrollableFrame(left, label_text="Options")
        form.pack(fill="both", expand=True)
        form.grid_columnconfigure(0, weight=1)
        self._build_form(form)

        # Right: plain frame wrapper → CTK frame inside
        right_wrap = _tk.Frame(paned, bg="#2b2b2b")
        paned.add(right_wrap, minsize=200, stretch="always")
        right = ctk.CTkFrame(right_wrap)
        right.pack(fill="both", expand=True)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._inline_console = ctk.CTkTextbox(
            right, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            spacing2=2)
        self._inline_console.grid(row=0, column=0, sticky="nsew",
                                   padx=8, pady=(8, 4))
        self._ansi_inline = AnsiWriter(self._inline_console)
        # PtyRenderer is created after both textboxes exist — see _build_console_tab

        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))

        ctk.CTkButton(bar, text="▶  Download Now", width=155,
                      command=self._download_now).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="＋  Add to Queue", width=155,
                      fg_color="#2d6a2d", hover_color="#3a8a3a",
                      command=self._add_to_queue).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="⚡  Preview Command", width=155,
                      fg_color="#4a4a4a", hover_color="#5a5a5a",
                      command=self._preview_command).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="🗑  Clear Temp", width=120,
                      fg_color=("#c62828", "#b71c1c"),
                      hover_color=("#b71c1c", "#7f0000"),
                      command=self._clear_temp).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="🗑 Clear", width=80,
                      fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=lambda: self._clear_box(self._ansi_inline)
                      ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(bar, text="■  Stop", width=80,
                      fg_color="#7a1010", hover_color="#9a1515",
                      command=self._stop_process).pack(side="right")

    # ── Form (all dl options) ─────────────────────────────────────────────────

    def _build_form(self, f: ctk.CTkScrollableFrame) -> None:

        # ── Service & Title ───────────────────────────────────────────────────
        _section(f, "Service & Title")

        r = _row(f)
        _lbl(r, "Service")
        self._service_var = ctk.StringVar(value="NF")
        ctk.CTkComboBox(r, values=SERVICES, width=160,
                        variable=self._service_var).pack(side="left", padx=(0, 10))

        r = _row(f)
        _lbl(r, "Title / URL")
        self._title_entry = _entry(r, width=310,
                                    placeholder="URL, title ID, or search query")

        r = _row(f)
        _lbl(r, "Profile")
        self._profile_combo = ctk.CTkComboBox(r, values=["default"], width=160)
        self._profile_combo.pack(side="left", padx=(0, 6))
        ctk.CTkButton(r, text="↺", width=32, height=28,
                      fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=self._refresh_profiles).pack(side="left")

        # ── Quality ───────────────────────────────────────────────────────────
        _section(f, "Quality")

        self._quality_vars: dict[str, ctk.BooleanVar] = {}
        for i, q in enumerate(QUALITIES):
            if i % 3 == 0:
                r = _row(f)
                _lbl(r, "Resolution(s)" if i == 0 else "")
            v = ctk.BooleanVar()
            self._quality_vars[q] = v
            ctk.CTkCheckBox(r, text=q, variable=v, width=68,
                            checkbox_width=16, checkbox_height=16).pack(side="left", padx=(0, 2))

        r = _row(f)
        _lbl(r, "Video Bitrate (kbps)")
        self._vbitrate_entry = _entry(r, width=90)
        _lbl(r, "Range", width=50)
        self._vbitrate_range_entry = _entry(r, width=110,
                                             placeholder="e.g. 6000-8000")

        r = _row(f)
        _lbl(r, "Audio Bitrate (kbps)")
        self._abitrate_entry = _entry(r, width=90)
        _lbl(r, "Range", width=50)
        self._abitrate_range_entry = _entry(r, width=110,
                                             placeholder="e.g. 128-256")

        r = _row(f)
        _lbl(r, "Audio Channels")
        self._channels_entry = _entry(r, width=90, placeholder="e.g. 5.1")

        # ── Video Codec ───────────────────────────────────────────────────────
        _section(f, "Video Codec")
        self._vcodec_vars: dict[str, ctk.BooleanVar] = {}
        for chunk in (VIDEO_CODECS[:3], VIDEO_CODECS[3:]):
            r = _row(f)
            for c in chunk:
                v = ctk.BooleanVar()
                self._vcodec_vars[c] = v
                ctk.CTkCheckBox(r, text=c, variable=v, width=88).pack(side="left")
        r = _row(f)
        self._vcodec_plain_var = ctk.BooleanVar()
        ctk.CTkCheckBox(r, text="Plain text format  (H.264→H264, H.265→H265, VC-1→VC1)",
                        variable=self._vcodec_plain_var, width=340).pack(side="left")

        # ── Audio Codec ───────────────────────────────────────────────────────
        _section(f, "Audio Codec")
        self._acodec_vars: dict[str, ctk.BooleanVar] = {}
        for chunk in (AUDIO_CODECS[:4], AUDIO_CODECS[4:]):
            r = _row(f)
            for c in chunk:
                v = ctk.BooleanVar()
                self._acodec_vars[c] = v
                ctk.CTkCheckBox(r, text=c, variable=v, width=82).pack(side="left")

        # ── Color Range ───────────────────────────────────────────────────────
        _section(f, "Color Range")
        self._range_vars: dict[str, ctk.BooleanVar] = {}
        for chunk in (COLOR_RANGES[:3], COLOR_RANGES[3:]):
            r = _row(f)
            for rng in chunk:
                v = ctk.BooleanVar(value=(rng == "SDR"))
                self._range_vars[rng] = v
                ctk.CTkCheckBox(r, text=rng, variable=v, width=88).pack(side="left")

        # ── Languages ─────────────────────────────────────────────────────────
        _section(f, "Languages")

        r = _row(f)
        _lbl(r, "Language (video+audio)")
        self._lang_entry = _entry(r, width=190,
                                   placeholder="orig  or  orig,en,de")

        r = _row(f)
        _lbl(r, "Audio Language")
        self._alang_entry = _entry(r, width=190,
                                    placeholder="overrides --lang for audio")

        r = _row(f)
        _lbl(r, "Video Language")
        self._vlang_entry = _entry(r, width=190,
                                    placeholder="only if different from audio")

        r = _row(f)
        _lbl(r, "Subtitle Language")
        self._slang_entry = _entry(r, width=190, placeholder="all")
        self._slang_entry.insert(0, "all")

        r = _row(f)
        _lbl(r, "Require Subtitles")
        self._require_subs_entry = _entry(r, width=190, placeholder="e.g. en,de")

        r = _row(f)
        self._forced_subs_var = ctk.BooleanVar()
        self._exact_lang_var  = ctk.BooleanVar()
        _check(r, "Forced Subtitles",  self._forced_subs_var, width=150)
        _check(r, "Exact Language Match", self._exact_lang_var, width=170)

        # ── Episode Selection ─────────────────────────────────────────────────
        _section(f, "Episode Selection")

        r = _row(f)
        _lbl(r, "Wanted Episodes")
        self._wanted_entry = _entry(r, width=230,
                                     placeholder="S01-S05,S07  or  S01E01-S02E03")

        r = _row(f)
        self._latest_ep_var     = ctk.BooleanVar()
        self._select_titles_var = ctk.BooleanVar()
        _check(r, "Latest Episode Only",         self._latest_ep_var,     width=170)
        _check(r, "Select Titles Interactively", self._select_titles_var, width=190)

        # ── Track Selection ───────────────────────────────────────────────────
        _section(f, "Track Selection — Include Only")

        r = _row(f)
        self._video_only_var    = ctk.BooleanVar()
        self._audio_only_var    = ctk.BooleanVar()
        self._subs_only_var     = ctk.BooleanVar()
        self._chapters_only_var = ctk.BooleanVar()
        _check(r, "Video Only",    self._video_only_var,    width=110)
        _check(r, "Audio Only",    self._audio_only_var,    width=110)
        _check(r, "Subs Only",     self._subs_only_var,     width=110)
        _check(r, "Chapters Only", self._chapters_only_var, width=120)

        _section(f, "Track Selection — Exclude")

        r = _row(f)
        self._no_video_var    = ctk.BooleanVar()
        self._no_audio_var    = ctk.BooleanVar()
        self._no_subs_var     = ctk.BooleanVar()
        self._no_chapters_var = ctk.BooleanVar()
        _check(r, "No Video",    self._no_video_var,    width=110)
        _check(r, "No Audio",    self._no_audio_var,    width=110)
        _check(r, "No Subtitles",self._no_subs_var,     width=110)
        _check(r, "No Chapters", self._no_chapters_var, width=120)

        r = _row(f)
        self._audio_desc_var  = ctk.BooleanVar()
        self._no_atmos_var    = ctk.BooleanVar()
        self._split_audio_var = ctk.BooleanVar()
        _check(r, "Audio Description", self._audio_desc_var,  width=150)
        _check(r, "No Atmos",          self._no_atmos_var,    width=110)
        _check(r, "Split Audio",       self._split_audio_var, width=110)

        # ── Subtitle Format ───────────────────────────────────────────────────
        _section(f, "Subtitle Output Format")

        r = _row(f)
        _lbl(r, "Convert subtitles to")
        self._sub_format_combo = _combo(r, values=SUB_FORMATS, width=120)

        # ── Metadata ──────────────────────────────────────────────────────────
        _section(f, "Metadata & Tagging")

        r = _row(f)
        _lbl(r, "Group Tag")
        self._tag_entry = _entry(r, width=120)
        _lbl(r, "TMDB ID", width=70)
        self._tmdb_entry = _entry(r, width=100, placeholder="integer")

        r = _row(f)
        _lbl(r, "IMDB ID")
        self._imdb_entry = _entry(r, width=120, placeholder="tt1234567")
        _lbl(r, "AnimeAPI ID", width=90)
        self._animeapi_entry = _entry(r, width=150,
                                       placeholder="mal:12345 or anilist:98765")

        r = _row(f)
        self._repack_var = ctk.BooleanVar()
        self._enrich_var = ctk.BooleanVar()
        _check(r, "REPACK",          self._repack_var, width=90)
        _check(r, "Enrich Metadata", self._enrich_var, width=150)

        # ── Output & Muxing ───────────────────────────────────────────────────
        _section(f, "Output & Muxing")

        r = _row(f)
        _lbl(r, "Output Directory")
        self._output_entry = ctk.CTkEntry(r, width=200,
                                           placeholder_text="default from config")
        self._output_entry.pack(side="left", padx=(0, 6))
        ctk.CTkButton(r, text="Browse…", width=80,
                      command=self._browse_output).pack(side="left")


        r = _row(f)
        self._no_mux_var    = ctk.BooleanVar()
        self._no_folder_var = ctk.BooleanVar()
        self._no_source_var = ctk.BooleanVar()
        _check(r, "No Mux",    self._no_mux_var,    width=100)
        _check(r, "No Folder", self._no_folder_var, width=110)
        _check(r, "No Source", self._no_source_var, width=110)

        # ── Performance ───────────────────────────────────────────────────────
        _section(f, "Download Performance")

        r = _row(f)
        _lbl(r, "Concurrent Tracks")
        self._downloads_entry = _entry(r, width=70, placeholder="1")
        _lbl(r, "Workers / Track", width=110)
        self._workers_entry = _entry(r, width=70, placeholder="auto")

        r = _row(f)
        _lbl(r, "Slow Delay (seconds)")
        self._slow_entry = _entry(r, width=110, placeholder="e.g. 60-120 (min 20s)")

        # ── Quality Behaviour ─────────────────────────────────────────────────
        _section(f, "Quality Selection Behaviour")

        r = _row(f)
        self._worst_var          = ctk.BooleanVar()
        self._best_available_var = ctk.BooleanVar()
        _check(r, "Worst (lowest bitrate within -q)",  self._worst_var,          width=230)
        _check(r, "Best Available (fallback quality)", self._best_available_var, width=230)

        # ── Keys & DRM ────────────────────────────────────────────────────────
        _section(f, "Keys & DRM")

        r = _row(f)
        self._cdm_only_var    = ctk.BooleanVar()
        self._vaults_only_var = ctk.BooleanVar()
        _check(r, "CDM Only",    self._cdm_only_var,    width=120)
        _check(r, "Vaults Only", self._vaults_only_var, width=120)

        r = _row(f)
        self._skip_dl_var = ctk.BooleanVar()
        self._export_var  = ctk.BooleanVar()
        _check(r, "Skip Download (keys only)", self._skip_dl_var, width=190)
        _check(r, "Export Keys + Info",        self._export_var,  width=160)

        # ── Listing & Debug ───────────────────────────────────────────────────
        _section(f, "Listing & Debug")

        r = _row(f)
        self._list_var        = ctk.BooleanVar()
        self._list_titles_var = ctk.BooleanVar()
        self._debug_var       = ctk.BooleanVar()
        _check(r, "List All",     self._list_var,        width=120)
        _check(r, "List Titles",  self._list_titles_var, width=120)
        _check(r, "Debug Mode",   self._debug_var,       width=120)

        # ── Cache ─────────────────────────────────────────────────────────────
        _section(f, "Cache")

        r = _row(f)
        self._no_cache_var    = ctk.BooleanVar()
        self._reset_cache_var = ctk.BooleanVar()
        _check(r, "No Cache",    self._no_cache_var,    width=120)
        _check(r, "Reset Cache", self._reset_cache_var, width=120)

        # ── Proxy ─────────────────────────────────────────────────────────────
        _section(f, "Proxy")

        r = _row(f)
        _lbl(r, "Proxy URI / Country")
        self._proxy_entry = _entry(r, width=210,
                                    placeholder="socks5://host:port  or  US")

        r = _row(f)
        self._no_proxy_var = ctk.BooleanVar()
        _check(r, "Force Disable All Proxy", self._no_proxy_var, width=200)

        # ── Remote Server ─────────────────────────────────────────────────────
        _section(f, "Remote Server")

        r = _row(f)
        self._remote_var = ctk.BooleanVar()
        _check(r, "Use Remote Server", self._remote_var, width=160)

        r = _row(f)
        _lbl(r, "Server Name")
        self._server_entry = _entry(r, width=160,
                                     placeholder="name from config")

        # ── bottom padding ────────────────────────────────────────────────────
        ctk.CTkLabel(f, text="").pack()

    # ── Queue tab ─────────────────────────────────────────────────────────────

    def _build_queue_tab(self) -> None:
        tab = self._tabs.tab("Queue")
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        self._queue_frame = ctk.CTkScrollableFrame(tab, label_text="Download Queue")
        self._queue_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self._queue_frame.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew")

        ctk.CTkButton(bar, text="▶  Run Queue", width=140,
                      command=self._run_queue).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="🗑  Clear Queue", width=140,
                      fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=self._clear_queue).pack(side="left")

    # ── Console tab ───────────────────────────────────────────────────────────

    def _build_console_tab(self) -> None:
        tab = self._tabs.tab("Console")
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        self._console = ctk.CTkTextbox(
            tab, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            spacing2=2)
        self._console.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self._ansi_console  = AnsiWriter(self._console)
        # Both textboxes now exist — build the shared PTY renderer
        self._pty_renderer  = PtyRenderer(self._inline_console, self._console,
                                          cols=220, rows=3000)

        # Forward keyboard input to the PTY process when active
        for box in (self._inline_console, self._console):
            box._textbox.bind("<Key>", self._on_console_key, add=True)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew")
        ctk.CTkButton(bar, text="🗑  Clear Console", width=140,
                      fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=lambda: self._clear_box(self._ansi_console)
                      ).pack(side="left")

    # ── Config tab ────────────────────────────────────────────────────────────

    def _build_config_tab(self) -> None:
        tab = self._tabs.tab("Config")
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        self._config_editor = ctk.CTkTextbox(
            tab, font=ctk.CTkFont(family="Consolas", size=12))
        self._config_editor.grid(row=0, column=0, sticky="nsew", pady=(0, 6))

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew")
        ctk.CTkButton(bar, text="📂  Load Config", width=140,
                      command=self._load_config).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="💾  Save Config", width=140,
                      command=self._save_config).pack(side="left")

        self._try_autoload_config()

    # ─────────────────────────────────────────────────────────────────────────
    # Command builder
    # ─────────────────────────────────────────────────────────────────────────

    def _build_command(self) -> list[str]:
        service = self._service_var.get().strip()
        title   = self._title_entry.get().strip()
        if not service:
            raise ValueError("Please select a service.")
        if not title:
            raise ValueError("Please enter a Title ID or URL.")

        cmd = find_unshackle()

        if self._debug_var.get():
            cmd += ["--debug"]

        cmd += ["dl"]

        # ── Profile ───────────────────────────────────────────────────────────
        p = self._profile_combo.get().strip()
        if p and p != "default":
            cmd += ["--profile", p]

        # ── Quality ───────────────────────────────────────────────────────────
        qlist = [q for q, v in self._quality_vars.items() if v.get()]
        if qlist:
            cmd += ["--quality", ",".join(qlist)]

        # ── Codecs ────────────────────────────────────────────────────────────
        vcodecs = [c for c, v in self._vcodec_vars.items() if v.get()]
        if vcodecs:
            cmd += ["--vcodec", ",".join(vcodecs)]

        acodecs = [c for c, v in self._acodec_vars.items() if v.get()]
        if acodecs:
            cmd += ["--acodec", ",".join(acodecs)]

        # ── Bitrates ──────────────────────────────────────────────────────────
        if vb := self._vbitrate_entry.get().strip():
            cmd += ["--vbitrate", vb]
        if ab := self._abitrate_entry.get().strip():
            cmd += ["--abitrate", ab]
        if vbr := self._vbitrate_range_entry.get().strip():
            cmd += ["--vbitrate-range", vbr]
        if abr := self._abitrate_range_entry.get().strip():
            cmd += ["--abitrate-range", abr]
        if ch := self._channels_entry.get().strip():
            cmd += ["--channels", ch]

        # ── Color Range ───────────────────────────────────────────────────────
        _range_cli = {"SDR": "sdr", "HLG": "hlg", "HDR10": "hdr10",
                      "HDR10+": "hdr10p", "DV": "dv", "HYBRID": "hybrid"}
        ranges = [_range_cli.get(r, r.lower()) for r, v in self._range_vars.items() if v.get()]
        if ranges and ranges != ["sdr"]:       # sdr is the default; skip if only SDR
            cmd += ["--range", ",".join(ranges)]

        # ── Languages ─────────────────────────────────────────────────────────
        if lang := self._lang_entry.get().strip():
            cmd += ["--lang", lang]
        if al := self._alang_entry.get().strip():
            cmd += ["--a-lang", al]
        if vl := self._vlang_entry.get().strip():
            cmd += ["--v-lang", vl]
        sl = self._slang_entry.get().strip()
        if sl and sl != "all":
            cmd += ["--s-lang", sl]
        if rs := self._require_subs_entry.get().strip():
            cmd += ["--require-subs", rs]
        if self._forced_subs_var.get():
            cmd += ["--forced-subs"]
        if self._exact_lang_var.get():
            cmd += ["--exact-lang"]

        # ── Episodes ──────────────────────────────────────────────────────────
        if w := self._wanted_entry.get().strip():
            cmd += ["--wanted", w]
        if self._latest_ep_var.get():
            cmd += ["--latest-episode"]
        if self._select_titles_var.get():
            cmd += ["--select-titles"]

        # ── Track selection flags ─────────────────────────────────────────────
        if self._video_only_var.get():    cmd += ["--video-only"]
        if self._audio_only_var.get():    cmd += ["--audio-only"]
        if self._subs_only_var.get():     cmd += ["--subs-only"]
        if self._chapters_only_var.get(): cmd += ["--chapters-only"]
        if self._no_video_var.get():      cmd += ["--no-video"]
        if self._no_audio_var.get():      cmd += ["--no-audio"]
        if self._no_subs_var.get():       cmd += ["--no-subs"]
        if self._no_chapters_var.get():   cmd += ["--no-chapters"]
        if self._audio_desc_var.get():    cmd += ["--audio-description"]
        if self._no_atmos_var.get():      cmd += ["--noatmos"]
        if self._split_audio_var.get():   cmd += ["--split-audio"]

        # ── Subtitle format ───────────────────────────────────────────────────
        if sf := self._sub_format_combo.get().strip():
            cmd += ["--sub-format", sf]

        # ── Metadata ──────────────────────────────────────────────────────────
        if tag := self._tag_entry.get().strip():
            cmd += ["--tag", tag]
        if tmdb := self._tmdb_entry.get().strip():
            cmd += ["--tmdb", tmdb]
        if imdb := self._imdb_entry.get().strip():
            cmd += ["--imdb", imdb]
        if animeapi := self._animeapi_entry.get().strip():
            cmd += ["--animeapi", animeapi]
        if self._repack_var.get(): cmd += ["--repack"]
        if self._enrich_var.get(): cmd += ["--enrich"]

        # ── Output ────────────────────────────────────────────────────────────
        if out := self._output_entry.get().strip():
            cmd += ["--output", out]
        if self._no_mux_var.get():    cmd += ["--no-mux"]
        if self._no_folder_var.get(): cmd += ["--no-folder"]
        if self._no_source_var.get(): cmd += ["--no-source"]

        # ── Performance ───────────────────────────────────────────────────────
        if dl := self._downloads_entry.get().strip():
            cmd += ["--downloads", dl]
        if wk := self._workers_entry.get().strip():
            cmd += ["--workers", wk]
        if slow := self._slow_entry.get().strip():
            cmd += ["--slow", slow]

        # ── Quality behaviour ─────────────────────────────────────────────────
        if self._worst_var.get():          cmd += ["--worst"]
        if self._best_available_var.get(): cmd += ["--best-available"]

        # ── Keys & DRM ────────────────────────────────────────────────────────
        if self._cdm_only_var.get():
            cmd += ["--cdm-only"]
        elif self._vaults_only_var.get():
            cmd += ["--vaults-only"]
        if self._skip_dl_var.get(): cmd += ["--skip-dl"]
        if self._export_var.get():  cmd += ["--export"]

        # ── Listing ───────────────────────────────────────────────────────────
        if self._list_var.get():        cmd += ["--list"]
        if self._list_titles_var.get(): cmd += ["--list-titles"]

        # ── Cache ─────────────────────────────────────────────────────────────
        if self._no_cache_var.get():    cmd += ["--no-cache"]
        if self._reset_cache_var.get(): cmd += ["--reset-cache"]

        # ── Proxy ─────────────────────────────────────────────────────────────
        if proxy := self._proxy_entry.get().strip():
            cmd += ["--proxy", proxy]
        if self._no_proxy_var.get(): cmd += ["--no-proxy"]

        # ── Remote ────────────────────────────────────────────────────────────
        if self._remote_var.get():
            cmd += ["--remote"]
        if srv := self._server_entry.get().strip():
            cmd += ["--server", srv]

        # ── Positional: service + title ───────────────────────────────────────
        cmd += [service, title]
        return cmd

    # ─────────────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────────────

    def _download_now(self) -> None:
        try:
            cmd = self._build_command()
        except ValueError as e:
            messagebox.showerror("Missing Input", str(e))
            return
        self._run_command(cmd)

    def _add_to_queue(self) -> None:
        try:
            cmd = self._build_command()
        except ValueError as e:
            messagebox.showerror("Missing Input", str(e))
            return
        service = self._service_var.get()
        title   = self._title_entry.get().strip()
        label   = f"{service} — {title[:70]}"
        item    = QueueItem(cmd, label)
        with self._queue_lock:
            self._dl_queue.append(item)
        self._refresh_queue_ui()
        self._ansi_inline.write(f"[Queue] Added: {label}\n")
        messagebox.showinfo("Added to Queue", f"Added to queue:\n{label}")

    def _preview_command(self) -> None:
        try:
            cmd = self._build_command()
        except ValueError as e:
            messagebox.showerror("Missing Input", str(e))
            return
        win = ctk.CTkToplevel(self)
        win.title("Command Preview")
        win.geometry("860x130")
        win.resizable(True, False)
        tb = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=12))
        tb.pack(fill="both", expand=True, padx=10, pady=10)
        tb.insert("end", " ".join(cmd))
        tb.configure(state="disabled")

    def _run_queue(self) -> None:
        with self._queue_lock:
            pending = [i for i in self._dl_queue if i.status == "Pending"]
        if not pending:
            messagebox.showinfo("Empty", "No pending items in the queue.")
            return
        self._tabs.set("Console")

        def _worker() -> None:
            for item in pending:
                item.status = "Running"
                self._refresh_queue_ui()
                self._run_command_sync(item.cmd)
                item.status = "Done"
                self._refresh_queue_ui()

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_queue(self) -> None:
        with self._queue_lock:
            self._dl_queue.clear()
        self._refresh_queue_ui()

    def _remove_queue_item(self, index: int) -> None:
        with self._queue_lock:
            if 0 <= index < len(self._dl_queue):
                self._dl_queue.pop(index)
        self._refresh_queue_ui()

    def _refresh_queue_ui(self) -> None:
        def _update() -> None:
            for row in self._queue_rows:
                row.destroy()
            self._queue_rows.clear()

            with self._queue_lock:
                items = list(self._dl_queue)

            STATUS_COLOR = {
                "Pending": "#aaaaaa",
                "Running": "#4da6ff",
                "Done":    "#4caf50",
                "Error":   "#f44336",
            }

            for i, item in enumerate(items):
                row = ctk.CTkFrame(self._queue_frame)
                row.pack(fill="x", padx=4, pady=2)
                row.grid_columnconfigure(1, weight=1)

                ctk.CTkLabel(row, text=f"  {i + 1}.", width=30, anchor="w"
                             ).grid(row=0, column=0, padx=(4, 0))
                ctk.CTkLabel(row, text=item.label, anchor="w"
                             ).grid(row=0, column=1, sticky="ew", padx=8)
                ctk.CTkLabel(row, text=item.status, width=70, anchor="e",
                             text_color=STATUS_COLOR.get(item.status, "#aaa")
                             ).grid(row=0, column=2, padx=(0, 6))
                ctk.CTkButton(
                    row, text="✕", width=28, height=24,
                    fg_color="#444", hover_color="#666",
                    command=lambda idx=i: self._remove_queue_item(idx),
                ).grid(row=0, column=3, padx=(0, 4))

                self._queue_rows.append(row)

        self.after(0, _update)

    def _browse_output(self) -> None:
        folder = filedialog.askdirectory(title="Select Output Directory")
        if folder:
            self._output_entry.delete(0, "end")
            self._output_entry.insert(0, folder)

    def _get_temp_dir(self) -> "Path | None":
        """Return the Temp directory unshackle uses for in-progress downloads."""
        root = self._project_root_from_cmd()
        if not getattr(sys, "frozen", False):
            try:
                from unshackle.core.config import config as _cfg  # type: ignore
                t = Path(_cfg.directories.temp)
                if t.is_absolute():
                    return t
                return (root / t) if root else t
            except Exception:
                pass
        if root:
            for cfg_name in ("unshackle.yaml", ".unshackle.yaml"):
                cfg_path = root / cfg_name
                if cfg_path.is_file():
                    try:
                        import re as _re
                        text = cfg_path.read_text(encoding="utf-8", errors="ignore")
                        m = _re.search(r"^\s*temp:\s*(.+)$", text, _re.MULTILINE)
                        if m:
                            val = m.group(1).strip().strip("\"'")
                            t = Path(val)
                            return (root / t) if not t.is_absolute() else t
                    except Exception:
                        pass
            return root / "Temp"
        return None

    def _clear_temp(self) -> None:
        """Delete all files in unshackle's Temp directory."""
        import shutil as _shutil
        temp_dir = self._get_temp_dir()
        if not temp_dir or not temp_dir.is_dir():
            messagebox.showinfo(
                "Clear Temp",
                f"Temp folder not found:\n{temp_dir}\n\nNothing to delete.")
            return
        files = list(temp_dir.iterdir())
        if not files:
            messagebox.showinfo("Clear Temp", f"Temp folder is already empty:\n{temp_dir}")
            return
        if not messagebox.askyesno(
            "Clear Temp",
            f"Delete {len(files)} item(s) from:\n{temp_dir}\n\nThis cannot be undone."
        ):
            return
        errors: list[str] = []
        for item in files:
            try:
                if item.is_dir():
                    _shutil.rmtree(item)
                else:
                    item.unlink()
            except Exception as exc:
                errors.append(f"{item.name}: {exc}")
        if errors:
            messagebox.showwarning("Clear Temp", "Some items could not be deleted:\n" + "\n".join(errors))
        else:
            messagebox.showinfo("Clear Temp", f"Temp folder cleared ({len(files)} item(s) deleted).")

    # ANSI escape sequences for special keys
    _KEY_MAP: dict[str, str] = {
        "Up":        "\x1b[A",
        "Down":      "\x1b[B",
        "Right":     "\x1b[C",
        "Left":      "\x1b[D",
        "Prior":     "\x1b[5~",   # Page Up
        "Next":      "\x1b[6~",   # Page Down
        "Home":      "\x1b[H",
        "End":       "\x1b[F",
        "Return":    "\r",
        "KP_Enter":  "\r",
        "BackSpace": "\x7f",
        "Tab":       "\t",
        "Escape":    "\x1b",
        "Delete":    "\x1b[3~",
        "F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS",
        "F5": "\x1b[15~", "F6": "\x1b[17~", "F7": "\x1b[18~",
        "F8": "\x1b[19~", "F9": "\x1b[20~", "F10": "\x1b[21~",
    }

    def _on_console_key(self, event: "tk.Event") -> str:  # type: ignore[name-defined]
        """Forward key presses to the active PTY process."""
        if not self._pty_active or self._active_pty is None:
            return ""  # not active — let normal handling proceed
        data = self._KEY_MAP.get(event.keysym)
        if data is None:
            if event.char and event.char != "\x00":
                data = event.char
        if data:
            try:
                self._active_pty.write(data)
            except Exception:
                pass
        return "break"  # prevent tkinter from handling the key

    def _stop_process(self) -> None:
        stopped = False
        if self._active_pty is not None:
            try:
                self._active_pty.terminate()
            except Exception:
                pass
            self._active_pty = None
            self._pty_active  = False
            stopped = True
        if self._active_proc and self._active_proc.poll() is None:
            self._active_proc.terminate()
            stopped = True
        if stopped:
            self._out_queue.put("\n[Stopped by user]\n")
            self._update_status("■ Stopped", ("#e65100", "#ff9800"))
        else:
            messagebox.showinfo("No Process", "No active download to stop.")

    # ─────────────────────────────────────────────────────────────────────────
    # Process execution
    # ─────────────────────────────────────────────────────────────────────────

    def _run_command(self, cmd: list[str]) -> None:
        busy = (HAS_WINPTY and self._active_pty is not None) or \
               (self._active_proc and self._active_proc.poll() is None)
        if busy:
            # If PTY is active the user may be in an interactive selection prompt —
            # silently ignore the click instead of showing a confusing dialog.
            if self._pty_active:
                self._inline_console._textbox.focus_set()
                return
            messagebox.showwarning(
                "Already Running",
                "A download is already running.\n"
                "Stop it first or use the Queue to schedule more.")
            return
        threading.Thread(target=self._run_command_sync,
                         args=(cmd,), daemon=True).start()

    @staticmethod
    def _unshackle_cwd(cmd: list[str]) -> str:
        """Return the working directory for running unshackle.

        If the exe is inside a .venv (e.g. D:\\proj\\.venv\\Scripts\\unshackle.exe),
        return the project root (3 levels up) so that relative paths in
        unshackle.yaml (e.g. 'directories.services: unshackle/services') resolve
        correctly regardless of where the GUI exe lives.
        """
        try:
            exe = Path(cmd[0]).resolve()
            for i, part in enumerate(exe.parts):
                if part.lower() == ".venv":
                    project_root = Path(*exe.parts[:i])
                    if project_root.is_dir():
                        return str(project_root)
        except Exception:
            pass
        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).parent)
        return str(Path(__file__).parent)

    def _run_command_sync(self, cmd: list[str]) -> None:
        interactive = self._select_titles_var.get()
        status_msg = "● Select titles in console — use keyboard" if interactive else "● Downloading…"
        self._update_status(status_msg, ("#1565c0", "#4da6ff"))
        self._out_queue.put(f"\n$ {' '.join(cmd)}\n{'─' * 60}\n")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"]       = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # FORCE_COLOR makes rich emit ANSI codes even without a real TTY.
        # We parse and render them ourselves in AnsiWriter.
        env["FORCE_COLOR"] = "1"
        env["COLORTERM"]   = "truecolor"
        # Provide a wide virtual terminal so rich doesn't wrap aggressively.
        env["COLUMNS"] = "200"
        env["LINES"]   = "200"

        # Record start time so we can find files created during this download
        import time as _time
        _do_rename  = self._vcodec_plain_var.get()
        _out_dir    = self._get_output_dir() if _do_rename else None
        _start_time = _time.time()

        if HAS_WINPTY:
            self._run_with_pty(cmd, env)
        else:
            self._run_with_pipe(cmd, env)

        # Post-download: rename codec strings in new files AND folders.
        # Rename deepest paths first so parent renames don't break child paths.
        if _do_rename and _out_dir and _out_dir.is_dir():
            candidates = [
                p for p in _out_dir.rglob("*")
                if p.stat().st_mtime >= _start_time
            ]
            for p in sorted(candidates, key=lambda x: len(x.parts), reverse=True):
                self._apply_plain_codec_rename(p)

    def _run_with_pty(self, cmd: list[str], env: dict[str, str]) -> None:
        """Run via Windows ConPTY — gives rich a real terminal so Live/progress works."""
        assert self._pty_renderer is not None
        self._pty_active = True
        self._pty_renderer.clear()
        # Auto-focus the inline console so keyboard input works immediately
        self.after(0, self._inline_console._textbox.focus_set)
        try:
            pty = _PtyProcess.spawn(
                cmd,
                cwd=self._unshackle_cwd(cmd),
                env=env,
                dimensions=(200, 220),
            )
            self._active_pty = pty
            while pty.isalive():
                try:
                    chunk = pty.read(4096)
                    if chunk:
                        self._pty_renderer.feed(chunk)
                except EOFError:
                    break
            exit_code = pty.exitstatus if pty.exitstatus is not None else "?"
            # Feed a final status line through the normal text queue so it
            # appears after the PTY renderer hands over control.
            self._out_queue.put(f"\n{'─' * 60}\nFinished — exit code {exit_code}\n")
            # 0xC000013A (3221225786) = STATUS_CONTROL_C_EXIT — winpty sends
            # this when the process exits normally via PTY teardown; treat as success
            if exit_code in (0, 3221225786):
                self._update_status("✓ Done", ("#2e7d32", "#4caf50"))
            else:
                self._update_status(f"✗ Failed — exit {exit_code}", ("#b71c1c", "#ef5350"))
        except Exception as exc:
            self._out_queue.put(f"PTY error: {exc}\n")
            self._update_status("✗ PTY error", ("#b71c1c", "#ef5350"))
        finally:
            self._active_pty  = None
            self._pty_active  = False

    def _run_with_pipe(self, cmd: list[str], env: dict[str, str]) -> None:
        """Fallback: pipe-based subprocess. Live progress bars may not stream."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                env=env,
                cwd=self._unshackle_cwd(cmd),
            )
            self._active_proc = proc
            assert proc.stdout
            buf = b""
            while True:
                chunk = proc.stdout.read(512)
                if not chunk:
                    break
                buf += chunk
                # Emit on newline/CR or when buffer is large enough
                if b"\n" in buf or b"\r" in buf or len(buf) > 512:
                    self._out_queue.put(buf.decode("utf-8", errors="replace"))
                    buf = b""
            if buf:
                self._out_queue.put(buf.decode("utf-8", errors="replace"))
            proc.wait()
            rc = proc.returncode
            self._out_queue.put(f"\n{'─' * 60}\nFinished — exit code {rc}\n")
            if rc == 0:
                self._update_status(f"✓ Done — exit {rc}", ("#2e7d32", "#4caf50"))
            else:
                self._update_status(f"✗ Failed — exit {rc}", ("#b71c1c", "#ef5350"))
        except FileNotFoundError:
            self._out_queue.put(
                "ERROR: 'unshackle' was not found.\n"
                "Make sure the virtual environment is active and installed.\n")
            self._update_status("✗ unshackle not found", ("#b71c1c", "#ef5350"))
        finally:
            self._active_proc = None

    # ─────────────────────────────────────────────────────────────────────────
    # Output helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _poll_output(self) -> None:
        if self._pty_active and self._pty_renderer is not None:
            # PTY mode: render the pyte screen buffer every tick
            self._pty_renderer.render()
        else:
            # Pipe mode (or PTY finished): drain plain-text queue → AnsiWriter
            try:
                while True:
                    text = self._out_queue.get_nowait()
                    if self._ansi_console:
                        self._ansi_console.write(text)
                    if self._ansi_inline:
                        self._ansi_inline.write(text)
            except queue.Empty:
                pass
        self.after(150, self._poll_output)

    def _clear_box(self, writer: "AnsiWriter | PtyRenderer") -> None:
        writer.clear()

    # ── codec plain-text rename helpers ───────────────────────────────────────

    _PLAIN_CODEC_MAP = [
        ("H.264", "H264"),
        ("H.265", "H265"),
        ("VC-1",  "VC1"),
    ]

    def _get_output_dir(self) -> "Path | None":
        """Return the directory where unshackle writes downloaded files."""
        if out := self._output_entry.get().strip():
            return Path(out)

        root = self._project_root_from_cmd()

        # In a non-frozen process the installed package gives the real config.
        if not getattr(sys, "frozen", False):
            try:
                from unshackle.core.config import config as _cfg  # type: ignore
                dl = Path(_cfg.directories.downloads)
                if dl.is_absolute():
                    return dl
                return (root / dl) if root else dl
            except Exception:
                pass

        # Frozen EXE (or import failed): read unshackle.yaml directly.
        if root:
            for cfg_name in ("unshackle.yaml", ".unshackle.yaml"):
                cfg_path = root / cfg_name
                if cfg_path.is_file():
                    try:
                        import re as _re
                        text = cfg_path.read_text(encoding="utf-8", errors="ignore")
                        m = _re.search(r"^\s*downloads:\s*(.+)$", text, _re.MULTILINE)
                        if m:
                            dl_val = m.group(1).strip().strip("\"'")
                            dl = Path(dl_val)
                            return (root / dl) if not dl.is_absolute() else dl
                    except Exception:
                        pass
            return root / "Downloads"
        return None

    def _project_root_from_cmd(self) -> "Path | None":
        """Return the unshackle project root inferred from the exe path."""
        try:
            cmd = find_unshackle()
            exe = Path(cmd[0]).resolve()
            for i, part in enumerate(exe.parts):
                if part.lower() == ".venv":
                    root = Path(*exe.parts[:i])
                    if root.is_dir():
                        return root
        except Exception:
            pass
        return None

    def _apply_plain_codec_rename(self, path: Path) -> None:
        """Rename codec notation in a file or folder, e.g. H.264 → H264."""
        new_name = path.name
        for old, new in self._PLAIN_CODEC_MAP:
            new_name = new_name.replace(old, new)
        if new_name != path.name:
            try:
                path.rename(path.parent / new_name)
                kind = "Dir " if path.is_dir() else "File"
                self._out_queue.put(f"[Renamed {kind}] {path.name}  →  {new_name}\n")
            except Exception as exc:
                self._out_queue.put(f"[Rename failed] {path.name}: {exc}\n")

    def _update_status(self, text: str, color: str) -> None:
        """Update the status bar label. Thread-safe."""
        self.after(0, lambda: self._status_label.configure(
            text=text, text_color=color))

    # ─────────────────────────────────────────────────────────────────────────
    # Settings persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _settings_get(self) -> dict:
        return {
            "window_geometry":  self.geometry(),
            "service":          self._service_var.get(),
            "title":            self._title_entry.get(),
            "profile":          self._profile_combo.get(),
            "quality":          {k: v.get() for k, v in self._quality_vars.items()},
            "vcodec":           {k: v.get() for k, v in self._vcodec_vars.items()},
            "vcodec_plain":     self._vcodec_plain_var.get(),
            "acodec":           {k: v.get() for k, v in self._acodec_vars.items()},
            "range":            {k: v.get() for k, v in self._range_vars.items()},
            "lang":             self._lang_entry.get(),
            "alang":            self._alang_entry.get(),
            "vlang":            self._vlang_entry.get(),
            "slang":            self._slang_entry.get(),
            "require_subs":     self._require_subs_entry.get(),
            "forced_subs":      self._forced_subs_var.get(),
            "exact_lang":       self._exact_lang_var.get(),
            "wanted":           self._wanted_entry.get(),
            "latest_ep":        self._latest_ep_var.get(),
            "select_titles":    self._select_titles_var.get(),
            "video_only":       self._video_only_var.get(),
            "audio_only":       self._audio_only_var.get(),
            "subs_only":        self._subs_only_var.get(),
            "chapters_only":    self._chapters_only_var.get(),
            "no_video":         self._no_video_var.get(),
            "no_audio":         self._no_audio_var.get(),
            "no_subs":          self._no_subs_var.get(),
            "no_chapters":      self._no_chapters_var.get(),
            "audio_desc":       self._audio_desc_var.get(),
            "no_atmos":         self._no_atmos_var.get(),
            "split_audio":      self._split_audio_var.get(),
            "sub_format":       self._sub_format_combo.get(),
            "tag":              self._tag_entry.get(),
            "tmdb":             self._tmdb_entry.get(),
            "imdb":             self._imdb_entry.get(),
            "animeapi":         self._animeapi_entry.get(),
            "repack":           self._repack_var.get(),
            "enrich":           self._enrich_var.get(),
            "output":           self._output_entry.get(),
            "no_mux":           self._no_mux_var.get(),
            "no_folder":        self._no_folder_var.get(),
            "no_source":        self._no_source_var.get(),
            "downloads":        self._downloads_entry.get(),
            "workers":          self._workers_entry.get(),
            "slow":             self._slow_entry.get(),
            "vbitrate":         self._vbitrate_entry.get(),
            "abitrate":         self._abitrate_entry.get(),
            "vbitrate_range":   self._vbitrate_range_entry.get(),
            "abitrate_range":   self._abitrate_range_entry.get(),
            "channels":         self._channels_entry.get(),
            "worst":            self._worst_var.get(),
            "best_available":   self._best_available_var.get(),
            "cdm_only":         self._cdm_only_var.get(),
            "vaults_only":      self._vaults_only_var.get(),
            "skip_dl":          self._skip_dl_var.get(),
            "export":           self._export_var.get(),
            "list":             self._list_var.get(),
            "list_titles":      self._list_titles_var.get(),
            "debug":            self._debug_var.get(),
            "no_cache":         self._no_cache_var.get(),
            "reset_cache":      self._reset_cache_var.get(),
            "proxy":            self._proxy_entry.get(),
            "no_proxy":         self._no_proxy_var.get(),
            "remote":           self._remote_var.get(),
            "server":           self._server_entry.get(),
        }

    def _save_settings(self) -> None:
        try:
            self._settings_path.write_text(
                json.dumps(self._settings_get(), indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_settings(self) -> None:
        if not self._settings_path.exists():
            return
        try:
            s = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception:
            return

        if geo := s.get("window_geometry"):
            try:
                self.geometry(geo)
            except Exception:
                pass

        def _set_entry(widget, key: str) -> None:
            val = s.get(key, "")
            widget.delete(0, "end")
            widget.insert(0, str(val))

        def _set_bool(var, key: str) -> None:
            var.set(bool(s.get(key, False)))

        def _set_dict_bool(d: dict, key: str) -> None:
            stored = s.get(key, {})
            for k, var in d.items():
                if k in stored:
                    var.set(bool(stored[k]))

        if svc := s.get("service"):
            self._service_var.set(svc)
        _set_entry(self._title_entry,           "title")
        if prof := s.get("profile"):
            self._profile_combo.set(prof)
        _set_dict_bool(self._quality_vars,      "quality")
        _set_dict_bool(self._vcodec_vars,       "vcodec")
        _set_bool(self._vcodec_plain_var,       "vcodec_plain")
        _set_dict_bool(self._acodec_vars,       "acodec")
        _set_dict_bool(self._range_vars,        "range")
        _set_entry(self._lang_entry,            "lang")
        _set_entry(self._alang_entry,           "alang")
        _set_entry(self._vlang_entry,           "vlang")
        _set_entry(self._slang_entry,           "slang")
        _set_entry(self._require_subs_entry,    "require_subs")
        _set_bool(self._forced_subs_var,        "forced_subs")
        _set_bool(self._exact_lang_var,         "exact_lang")
        _set_entry(self._wanted_entry,          "wanted")
        _set_bool(self._latest_ep_var,          "latest_ep")
        _set_bool(self._select_titles_var,      "select_titles")
        _set_bool(self._video_only_var,         "video_only")
        _set_bool(self._audio_only_var,         "audio_only")
        _set_bool(self._subs_only_var,          "subs_only")
        _set_bool(self._chapters_only_var,      "chapters_only")
        _set_bool(self._no_video_var,           "no_video")
        _set_bool(self._no_audio_var,           "no_audio")
        _set_bool(self._no_subs_var,            "no_subs")
        _set_bool(self._no_chapters_var,        "no_chapters")
        _set_bool(self._audio_desc_var,         "audio_desc")
        _set_bool(self._no_atmos_var,           "no_atmos")
        _set_bool(self._split_audio_var,        "split_audio")
        if sf := s.get("sub_format"):
            self._sub_format_combo.set(sf)
        _set_entry(self._tag_entry,             "tag")
        _set_entry(self._tmdb_entry,            "tmdb")
        _set_entry(self._imdb_entry,            "imdb")
        _set_entry(self._animeapi_entry,        "animeapi")
        _set_bool(self._repack_var,             "repack")
        _set_bool(self._enrich_var,             "enrich")
        _set_entry(self._output_entry,          "output")
        _set_bool(self._no_mux_var,             "no_mux")
        _set_bool(self._no_folder_var,          "no_folder")
        _set_bool(self._no_source_var,          "no_source")
        _set_entry(self._downloads_entry,       "downloads")
        _set_entry(self._workers_entry,         "workers")
        _set_entry(self._slow_entry,            "slow")
        _set_entry(self._vbitrate_entry,        "vbitrate")
        _set_entry(self._abitrate_entry,        "abitrate")
        _set_entry(self._vbitrate_range_entry,  "vbitrate_range")
        _set_entry(self._abitrate_range_entry,  "abitrate_range")
        _set_entry(self._channels_entry,        "channels")
        _set_bool(self._worst_var,              "worst")
        _set_bool(self._best_available_var,     "best_available")
        _set_bool(self._cdm_only_var,           "cdm_only")
        _set_bool(self._vaults_only_var,        "vaults_only")
        _set_bool(self._skip_dl_var,            "skip_dl")
        _set_bool(self._export_var,             "export")
        _set_bool(self._list_var,               "list")
        _set_bool(self._list_titles_var,        "list_titles")
        _set_bool(self._debug_var,              "debug")
        _set_bool(self._no_cache_var,           "no_cache")
        _set_bool(self._reset_cache_var,        "reset_cache")
        _set_entry(self._proxy_entry,           "proxy")
        _set_bool(self._no_proxy_var,           "no_proxy")
        _set_bool(self._remote_var,             "remote")
        _set_entry(self._server_entry,          "server")

    def _on_close(self) -> None:
        self._save_settings()
        self.destroy()

    # ─────────────────────────────────────────────────────────────────────────
    # Config helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _try_autoload_config(self) -> None:
        candidates: list[Path] = []

        # Project root via .venv (works for both frozen and non-frozen)
        root = self._project_root_from_cmd()
        if root:
            candidates += [root / "unshackle.yaml", root / "unshackle" / "unshackle.yaml"]

        # Source-run fallback
        if not getattr(sys, "frozen", False):
            candidates += [
                Path(__file__).parent / "unshackle" / "unshackle.yaml",
                Path(__file__).parent / "unshackle.yaml",
            ]

        for candidate in candidates:
            if candidate.exists():
                try:
                    self._config_editor.delete("1.0", "end")
                    self._config_editor.insert(
                        "end", candidate.read_text(encoding="utf-8"))
                    self._config_path = candidate
                except Exception:
                    pass
                self._refresh_profiles()
                return

    def _refresh_profiles(self) -> None:
        """Parse the loaded config YAML for credential profile names."""
        profiles: list[str] = ["default"]
        text = self._config_editor.get("1.0", "end")
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
            if isinstance(data, dict) and isinstance(data.get("credentials"), dict):
                for service_val in data["credentials"].values():
                    if isinstance(service_val, dict):
                        for name in service_val.keys():
                            if name not in profiles:
                                profiles.append(str(name))
        except Exception:
            pass
        current = self._profile_combo.get()
        self._profile_combo.configure(values=profiles)
        if current not in profiles:
            self._profile_combo.set("default")

    def _load_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Config File",
            filetypes=[("YAML", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if path:
            p = Path(path)
            self._config_editor.delete("1.0", "end")
            self._config_editor.insert("end", p.read_text(encoding="utf-8"))
            self._config_path = p
            self._refresh_profiles()

    def _save_config(self) -> None:
        path = self._config_path
        if not path:
            raw = filedialog.asksaveasfilename(
                title="Save Config File",
                defaultextension=".yaml",
                filetypes=[("YAML", "*.yaml *.yml")],
            )
            if not raw:
                return
            path = Path(raw)
            self._config_path = path
        path.write_text(
            self._config_editor.get("1.0", "end"), encoding="utf-8")
        messagebox.showinfo("Saved", f"Config saved to:\n{path}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    app = UnshackleGUI()
    app.mainloop()


if __name__ == "__main__":
    main()

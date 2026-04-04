#!/usr/bin/env python3
"""Unshackle GUI — Graphical interface for the Unshackle download tool."""
from __future__ import annotations

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
SERVICES = [
    "ABMA", "ADN", "AMZN", "ATV", "CR", "DSMART", "DSNP",
    "EXXEN", "GLBO", "HIDI", "HMAX", "HPLA", "HULUJP", "ITUNES",
    "KCW", "KNPY", "MUBI", "NF", "NPO", "PLTV", "PMTP", "SKST",
    "SVTP", "TOD", "UNEXT", "UNXT", "UPLAY", "VIDO", "VIKI", "VRT", "iQ",
]
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
        tk.tag_configure("bold",      font=("Consolas", 12, "bold"))
        tk.tag_configure("italic",    font=("Consolas", 12, "italic"))
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
    "default":        "#cdd6f4",
    "black":          "#45475a",  "red":            "#f38ba8",
    "green":          "#a6e3a1",  "yellow":         "#f9e2af",
    "blue":           "#89b4fa",  "magenta":        "#f5c2e7",
    "cyan":           "#94e2d5",  "white":          "#cdd6f4",
    "bright_black":   "#585b70",  "bright_red":     "#f38ba8",
    "bright_green":   "#a6e3a1",  "bright_yellow":  "#f9e2af",
    "bright_blue":    "#89b4fa",  "bright_magenta": "#f5c2e7",
    "bright_cyan":    "#94e2d5",  "bright_white":   "#cdd6f4",
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

        if HAS_PYTE:
            self._screen = _pyte.Screen(cols, rows)
            self._stream = _pyte.ByteStream(self._screen)
        else:
            self._screen = None
            self._stream = None

        for tk in self._tks:
            self._setup_tags(tk)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _setup_tags(self, tk: "tk.Text") -> None:  # type: ignore[name-defined]
        for name, color in _PYTE_COLORS.items():
            tag = f"p_{name}"
            tk.tag_configure(tag, foreground=color)
            self._known_tags.add(tag)

    def _color_hex(self, name: str) -> str:
        if not name or name == "default":
            return _PYTE_COLORS["default"]
        if name.startswith("#"):
            return name
        return _PYTE_COLORS.get(name, _PYTE_COLORS["default"])

    def _ensure_tag(self, tk: "tk.Text", fg: str, bold: bool) -> str:  # type: ignore[name-defined]
        safe = fg.replace("#", "x")
        tag  = f"pt_{safe}_{'B' if bold else 'n'}"
        if tag not in self._known_tags:
            color = self._color_hex(fg)
            font  = ctk.CTkFont(family="Consolas", size=12,
                                 weight="bold" if bold else "normal")
            tk.tag_configure(tag, foreground=color, font=font)
            self._known_tags.add(tag)
        return tag

    # ── public API ────────────────────────────────────────────────────────────

    def feed(self, data: bytes | str) -> None:
        if self._stream is None:
            return
        raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        with self._lock:
            self._stream.feed(raw)

    def render(self) -> None:
        """Render pyte screen to all textboxes. Must be called from main thread."""
        if self._screen is None:
            return

        with self._lock:
            screen = self._screen

            # Find the last row that has visible content
            last_row = -1
            for r in range(screen.lines):
                if any(c.data.strip() for c in screen.buffer[r].values()):
                    last_row = r

            if last_row < 0:
                return

            # Snapshot row data (char, fg, bold) — strip trailing spaces per row
            rows_data: list[list[tuple[str, str, bool]]] = []
            for r in range(last_row + 1):
                row = screen.buffer[r]
                cells: list[tuple[str, str, bool]] = []
                for c in range(screen.columns):
                    ch = row.get(c)
                    if ch:
                        cells.append((ch.data, ch.fg, ch.bold))
                    else:
                        cells.append((" ", "default", False))
                # strip trailing blanks
                while cells and cells[-1][0] in (" ", "\x00"):
                    cells.pop()
                rows_data.append(cells)

        # ── write to every textbox ─────────────────────────────────────────────
        for box, tk in zip(self._boxes, self._tks):
            box.configure(state="normal")
            tk.delete("1.0", "end")

            for row_idx, cells in enumerate(rows_data):
                if row_idx:
                    tk.insert("end", "\n")
                if not cells:
                    continue
                i = 0
                while i < len(cells):
                    char, fg, bold = cells[i]
                    tag = self._ensure_tag(tk, fg, bold)
                    text = char
                    i += 1
                    # merge consecutive cells with same style
                    while i < len(cells) and cells[i][1] == fg and cells[i][2] == bold:
                        text += cells[i][0]
                        i += 1
                    tk.insert("end", text, (tag,))

            tk.see("end")
            box.configure(state="disabled")

    def clear(self) -> None:
        if self._screen is not None:
            with self._lock:
                self._screen.reset()
        for box, tk in zip(self._boxes, self._tks):
            box.configure(state="normal")
            tk.delete("1.0", "end")
            box.configure(state="disabled")


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_unshackle() -> list[str]:
    """Return the command to invoke unshackle in the current Python environment."""
    import shutil

    # 1. Look in PATH first (works when installed via pip/uv)
    found = shutil.which("unshackle")
    if found:
        return [found]

    # 2. Look next to the current executable — works in a normal venv
    #    but NOT when frozen by PyInstaller (sys.executable == the gui .exe)
    exe = Path(sys.executable)
    if not getattr(sys, "frozen", False):          # not a PyInstaller bundle
        for name in ("unshackle", "unshackle.exe"):
            candidate = exe.parent / name
            if candidate.exists():
                return [str(candidate)]
        # Last resort: run as a Python module
        return [str(exe), "-m", "unshackle"]

    # 3. Frozen (PyInstaller) — try common install locations relative to the .exe
    gui_dir = exe.parent
    for rel in (".", "..", "Scripts", "../Scripts"):
        for name in ("unshackle", "unshackle.exe"):
            candidate = (gui_dir / rel / name).resolve()
            if candidate.exists():
                return [str(candidate)]

    # 4. Give up — return the bare name and let the OS resolve it
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
        # Created after UI is built (textboxes must exist first)
        self._pty_renderer: PtyRenderer | None = None
        self._ansi_inline:  AnsiWriter  | None = None
        self._ansi_console: AnsiWriter  | None = None

        self._build_ui()
        self._poll_output()

    # ─────────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._tabs = ctk.CTkTabview(self, anchor="nw")
        self._tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        for name in ("Download", "Queue", "Console", "Config"):
            self._tabs.add(name)

        self._build_download_tab()
        self._build_queue_tab()
        self._build_console_tab()
        self._build_config_tab()

    # ── Download tab ──────────────────────────────────────────────────────────

    def _build_download_tab(self) -> None:
        tab = self._tabs.tab("Download")
        tab.grid_columnconfigure(0, weight=2)
        tab.grid_columnconfigure(1, weight=3)
        tab.grid_rowconfigure(0, weight=1)

        # Left: scrollable form
        form = ctk.CTkScrollableFrame(tab, label_text="Options")
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        form.grid_columnconfigure(0, weight=1)
        self._build_form(form)

        # Right: live console + action bar
        right = ctk.CTkFrame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._inline_console = ctk.CTkTextbox(
            right, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=12))
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
        self._profile_entry = _entry(r, width=160, placeholder="default")

        # ── Quality ───────────────────────────────────────────────────────────
        _section(f, "Quality")

        r = _row(f)
        _lbl(r, "Resolution(s)")
        self._quality_vars: dict[str, ctk.BooleanVar] = {}
        for q in QUALITIES:
            v = ctk.BooleanVar()
            self._quality_vars[q] = v
            ctk.CTkCheckBox(r, text=q, variable=v, width=68).pack(side="left")

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
        r = _row(f)
        self._vcodec_vars: dict[str, ctk.BooleanVar] = {}
        for c in VIDEO_CODECS:
            v = ctk.BooleanVar()
            self._vcodec_vars[c] = v
            ctk.CTkCheckBox(r, text=c, variable=v, width=72).pack(side="left")

        # ── Audio Codec ───────────────────────────────────────────────────────
        _section(f, "Audio Codec")
        r = _row(f)
        self._acodec_vars: dict[str, ctk.BooleanVar] = {}
        for c in AUDIO_CODECS:
            v = ctk.BooleanVar()
            self._acodec_vars[c] = v
            ctk.CTkCheckBox(r, text=c, variable=v, width=68).pack(side="left")

        # ── Color Range ───────────────────────────────────────────────────────
        _section(f, "Color Range")
        r = _row(f)
        self._range_vars: dict[str, ctk.BooleanVar] = {}
        for i, rng in enumerate(COLOR_RANGES):
            v = ctk.BooleanVar(value=(rng == "SDR"))
            self._range_vars[rng] = v
            ctk.CTkCheckBox(r, text=rng, variable=v, width=72).pack(side="left")

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
        _check(r, "List Tracks",  self._list_var,        width=120)
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
            font=ctk.CTkFont(family="Consolas", size=12))
        self._console.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self._ansi_console  = AnsiWriter(self._console)
        # Both textboxes now exist — build the shared PTY renderer
        self._pty_renderer  = PtyRenderer(self._inline_console, self._console)

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
        if p := self._profile_entry.get().strip():
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
        ranges = [r for r, v in self._range_vars.items() if v.get()]
        if ranges and ranges != ["SDR"]:       # SDR is the default; skip if only SDR
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
        self._tabs.set("Console")

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
        else:
            messagebox.showinfo("No Process", "No active download to stop.")

    # ─────────────────────────────────────────────────────────────────────────
    # Process execution
    # ─────────────────────────────────────────────────────────────────────────

    def _run_command(self, cmd: list[str]) -> None:
        busy = (HAS_WINPTY and self._active_pty is not None) or \
               (self._active_proc and self._active_proc.poll() is None)
        if busy:
            messagebox.showwarning(
                "Already Running",
                "A download is already running.\n"
                "Stop it first or use the Queue to schedule more.")
            return
        threading.Thread(target=self._run_command_sync,
                         args=(cmd,), daemon=True).start()

    def _run_command_sync(self, cmd: list[str]) -> None:
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
        env["LINES"]   = "50"

        if HAS_WINPTY:
            self._run_with_pty(cmd, env)
        else:
            self._run_with_pipe(cmd, env)

    def _run_with_pty(self, cmd: list[str], env: dict[str, str]) -> None:
        """Run via Windows ConPTY — gives rich a real terminal so Live/progress works."""
        assert self._pty_renderer is not None
        self._pty_active = True
        self._pty_renderer.clear()
        try:
            pty = _PtyProcess.spawn(
                cmd,
                cwd=str(Path(__file__).parent),
                env=env,
                dimensions=(50, 220),
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
        except Exception as exc:
            self._out_queue.put(f"PTY error: {exc}\n")
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
                cwd=str(Path(__file__).parent),
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
            self._out_queue.put(
                f"\n{'─' * 60}\nFinished — exit code {proc.returncode}\n")
        except FileNotFoundError:
            self._out_queue.put(
                "ERROR: 'unshackle' was not found.\n"
                "Make sure the virtual environment is active and installed.\n")
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
        self.after(100, self._poll_output)

    def _clear_box(self, writer: "AnsiWriter | PtyRenderer") -> None:
        writer.clear()

    # ─────────────────────────────────────────────────────────────────────────
    # Config helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _try_autoload_config(self) -> None:
        for candidate in [
            Path(__file__).parent / "unshackle" / "unshackle.yaml",
            Path(__file__).parent / "unshackle.yaml",
        ]:
            if candidate.exists():
                try:
                    self._config_editor.delete("1.0", "end")
                    self._config_editor.insert(
                        "end", candidate.read_text(encoding="utf-8"))
                    self._config_path = candidate
                except Exception:
                    pass
                return

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

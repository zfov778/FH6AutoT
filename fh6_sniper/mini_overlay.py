"""Compact always-on-top mini overlay for the auction sniper.

Colour scheme aligned with the main CTk panel for visual consistency.
"""
from __future__ import annotations
import ctypes
import time
import tkinter as tk

# ── palette: matched to CTk dark theme ──────────────────────────────
_BG       = "#2B2B2B"   # CTk frame background
_CARD     = "#1E1E1E"   # CTk card / entry background
_DIVIDER  = "#3B3B3B"   # CTk border colour
_ACCENT   = "#2EA043"   # CTk primary green
_ACCENT_HV = "#238636"  # CTk primary green hover
_TEXT     = "#FFFFFF"
_TEXT_DIM = "#AAAAAA"
_TEXT_FAINT = "#888888"
_AMBER    = "#D97706"
_RED      = "#DA3633"   # CTk danger red
_RED_HV   = "#C0352F"

_STOPPED_WORDS = ("idle", "stopped", "auto-stop", "lost", "could not", "crashed",
                  "已停止", "迷失", "无法恢复", "无法识别")

_WIN_W = 380


class MiniOverlay:
    """Floating HUD shown while the sniper is running.

    When *master* is provided uses Toplevel; otherwise creates its own Tk root.
    """

    def __init__(self, on_stop=None, on_start=None, on_close=None,
                 on_setting_changed=None,
                 master=None, hide_from_capture: bool = True):
        self._on_stop = on_stop
        self._on_start = on_start
        self._on_close = on_close
        self._on_setting_changed = on_setting_changed
        self._active = False
        self._started = None
        self._drag = (0, 0)
        self._destroyed = False
        self._btn_base = _RED
        self._btn_hover = _RED_HV

        if master is not None:
            self.root = tk.Toplevel(master)
        else:
            self.root = tk.Tk()

        self.root.title("FH6一键抢车")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg=_BG)
        self.root.withdraw()

        # --- variables ---
        self._status_var   = tk.StringVar(master=self.root, value="就绪")
        self._bought_var   = tk.StringVar(master=self.root, value="0")
        self._searches_var = tk.StringVar(master=self.root, value="0")
        self._fails_var    = tk.StringVar(master=self.root, value="0")
        self._time_var     = tk.StringVar(master=self.root, value="00:00")

        self._collect_var = tk.BooleanVar(master=self.root, value=True)
        self._sound_var   = tk.BooleanVar(master=self.root, value=True)
        self._toast_var   = tk.BooleanVar(master=self.root, value=True)

        self._build()
        self.root.update_idletasks()
        self.root.geometry(f"{_WIN_W}x{self.root.winfo_reqheight()}+24+24")
        self.set_capturable(not hide_from_capture)
        self._tick()

    # ------------------------------------------------------------------
    # capture exclusion
    # ------------------------------------------------------------------

    def set_capturable(self, capturable: bool) -> None:
        try:
            user32 = ctypes.windll.user32
            hwnd = self.root.winfo_id()
            parent = user32.GetParent(hwnd)
            while parent:
                hwnd = parent
                parent = user32.GetParent(hwnd)
            affinity = 0x00 if capturable else 0x11
            user32.SetWindowDisplayAffinity(hwnd, affinity)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------

    def _build(self):
        root = self.root
        PAD = 16

        # ── accent top bar ──────────────────────────────────────────
        tk.Frame(root, bg=_ACCENT, height=2).pack(fill="x")

        # ── header ───────────────────────────────────────────────────
        header = tk.Frame(root, bg=_BG)
        header.pack(fill="x", padx=PAD, pady=(14, 0))
        self._dot = tk.Label(header, text="●", bg=_BG, fg=_TEXT_DIM,
                             font=("Segoe UI", 10))
        self._dot.pack(side="left")
        brand = tk.Label(header, text="  FH6一键抢车", bg=_BG, fg=_TEXT,
                         font=("Segoe UI", 12, "bold"))
        brand.pack(side="left")
        close = tk.Label(header, text="✕", bg=_BG, fg=_TEXT_FAINT,
                         font=("Segoe UI", 12), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._do_close())
        close.bind("<Enter>", lambda _e: close.config(fg=_TEXT))
        close.bind("<Leave>", lambda _e: close.config(fg=_TEXT_FAINT))
        for w in (header, self._dot, brand):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        tk.Frame(root, bg=_DIVIDER, height=1).pack(
            fill="x", padx=PAD, pady=(12, 0))

        # ── status ──────────────────────────────────────────────────
        self._status_label = tk.Label(
            root, textvariable=self._status_var, bg=_BG, fg=_ACCENT,
            font=("Segoe UI", 13, "bold"), anchor="center", justify="center",
            wraplength=_WIN_W - 40, height=2)
        self._status_label.pack(fill="x", padx=PAD, pady=(10, 0))

        # ── stats card ──────────────────────────────────────────────
        card = tk.Frame(root, bg=_CARD, highlightthickness=1,
                        highlightbackground=_DIVIDER)
        card.pack(fill="x", padx=PAD, pady=(12, 0))
        cells = (("已抢", self._bought_var,   _ACCENT),
                 ("搜索", self._searches_var, _TEXT),
                 ("失败", self._fails_var,    _RED),
                 ("运行", self._time_var,     _TEXT))
        for i, (caption, var, color) in enumerate(cells):
            if i:
                tk.Frame(card, bg=_DIVIDER, width=1).pack(
                    side="left", fill="y", pady=12)
            cell = tk.Frame(card, bg=_CARD)
            cell.pack(side="left", expand=True, fill="both")
            tk.Label(cell, textvariable=var, bg=_CARD, fg=color,
                     font=("Segoe UI", 18, "bold")).pack(pady=(12, 0))
            tk.Label(cell, text=caption, bg=_CARD, fg=_TEXT_FAINT,
                     font=("Segoe UI", 9)).pack(pady=(2, 12))

        # ── quick toggles ───────────────────────────────────────────
        section = tk.Frame(root, bg=_BG)
        section.pack(fill="x", padx=PAD, pady=(12, 0))
        tk.Label(section, text="快捷设置", bg=_BG, fg=_TEXT_DIM,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")

        grid = tk.Frame(root, bg=_BG)
        grid.pack(fill="x", padx=PAD, pady=(4, 0))
        grid.columnconfigure(0, weight=1, uniform="tog")
        grid.columnconfigure(1, weight=1, uniform="tog")
        grid.columnconfigure(2, weight=1, uniform="tog")

        self._build_toggle(grid, 0, 0, "自动领取", self._collect_var, "collect_after_buyout")
        self._build_toggle(grid, 0, 1, "声音",     self._sound_var,   "notify_sound")
        self._build_toggle(grid, 0, 2, "系统通知", self._toast_var,   "notify_toast")

        # ── action button ───────────────────────────────────────────
        self._btn = tk.Button(
            root, text="停止抢车", font=("Segoe UI", 11, "bold"),
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            bg=_RED, fg=_TEXT, activebackground=_RED_HV,
            activeforeground=_TEXT, height=2,
            command=self._on_btn_click)
        self._btn.pack(fill="x", padx=PAD, pady=(12, 14))
        self._btn.bind("<Enter>", lambda _e: self._btn.config(bg=self._btn_hover))
        self._btn.bind("<Leave>", lambda _e: self._btn.config(bg=self._btn_base))

    def _build_toggle(self, parent, row, col, label, var, key):
        """A single toggle cell: [■] label, laid out in a grid."""
        cell = tk.Frame(parent, bg=_BG)
        cell.grid(row=row, column=col, sticky="ew", padx=2, pady=2)

        inner = tk.Frame(cell, bg=_CARD, highlightthickness=1,
                         highlightbackground=_DIVIDER)
        inner.pack(fill="x", ipady=5)

        box = tk.Frame(inner, width=18, height=18, bg=_BG,
                       highlightthickness=2, highlightbackground=_DIVIDER,
                       cursor="hand2")
        box.pack_propagate(False)
        box.pack(side="left", padx=(10, 6))

        text = tk.Label(inner, text=label, bg=_CARD, fg=_TEXT_FAINT,
                        font=("Segoe UI", 9), cursor="hand2")
        text.pack(side="left")

        def _toggle(_e=None):
            var.set(not var.get())

        def _render(*_a):
            if var.get():
                box.config(bg=_ACCENT, highlightbackground=_ACCENT)
                text.config(fg=_TEXT)
            else:
                box.config(bg=_BG, highlightbackground=_DIVIDER)
                text.config(fg=_TEXT_FAINT)
            if self._on_setting_changed:
                self._on_setting_changed(key, var.get())

        for w in (inner, box, text):
            w.bind("<Button-1>", _toggle)
        var.trace_add("write", _render)
        _render()

    # ------------------------------------------------------------------
    # settings sync
    # ------------------------------------------------------------------

    def sync_settings(self, collect: bool, sound: bool, toast: bool):
        cb = self._on_setting_changed
        self._on_setting_changed = None
        self._collect_var.set(bool(collect))
        self._sound_var.set(bool(sound))
        self._toast_var.set(bool(toast))
        self._on_setting_changed = cb

    # ------------------------------------------------------------------
    # drag
    # ------------------------------------------------------------------

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        self.root.geometry(
            f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    # ------------------------------------------------------------------
    # status / stats helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _state_of(text: str) -> str:
        low = text.lower()
        if "paused" in low or "暂停" in text:
            return "paused"
        if any(w in low for w in _STOPPED_WORDS):
            return "stopped"
        return "running"

    def _on_btn_click(self):
        """Button click: stop when running, start when stopped."""
        if self._active:
            if self._on_stop:
                self._on_stop()
        else:
            if self._on_start:
                self._on_start()
            else:
                self._do_close()

    def _do_close(self):
        """Close button: hide overlay and restore main window."""
        self.hide()
        if self._on_close:
            self._on_close()

    def _apply_status(self, text: str):
        self._status_var.set(text)
        state = self._state_of(text)
        color_map = {"running": _ACCENT, "paused": _AMBER, "stopped": _TEXT_FAINT}
        color = color_map[state]
        self._dot.config(fg=color)
        self._status_label.config(fg=color)
        if state == "running" and self._started is None:
            self._started = time.monotonic()
            self._time_var.set("00:00")
        self._active = (state != "stopped")
        if state == "stopped":
            self._btn_base = _ACCENT
            self._btn_hover = _ACCENT_HV
            self._btn.config(text="开始抢车", bg=_ACCENT, fg=_TEXT,
                             activebackground=_ACCENT_HV, activeforeground=_TEXT,
                             command=self._on_btn_click)
        else:
            self._btn_base = _RED
            self._btn_hover = _RED_HV
            self._btn.config(text="停止抢车", bg=_RED, fg=_TEXT,
                             activebackground=_RED_HV, activeforeground=_TEXT,
                             command=self._on_btn_click)

    def _apply_stats(self, searches: int, bought: int, fails: int):
        self._searches_var.set(str(searches))
        self._bought_var.set(str(bought))
        self._fails_var.set(str(fails))

    def _tick(self):
        if self._destroyed:
            return
        if self._active and self._started is not None:
            m, s = divmod(int(time.monotonic() - self._started), 60)
            self._time_var.set(f"{m:02d}:{s:02d}")
        try:
            self.root.after(1000, self._tick)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def show(self):
        try:
            self.root.deiconify()
        except Exception:
            pass

    def force_show(self):
        """Re-show via Win32 after parent is iconified (which hides owned
        Toplevels on Windows).  SW_SHOWNOACTIVATE = 4."""
        try:
            self.root.deiconify()
            hwnd = self.root.winfo_id()
            ctypes.windll.user32.ShowWindow(hwnd, 4)
        except Exception:
            pass

    def hide(self):
        try:
            self.root.withdraw()
        except Exception:
            pass

    def set_status(self, text: str):
        try:
            self.root.after(0, self._apply_status, text)
        except RuntimeError:
            pass

    def set_stats(self, searches: int, bought: int, fails: int):
        try:
            self.root.after(0, self._apply_stats, searches, bought, fails)
        except RuntimeError:
            pass

    def destroy(self):
        self._destroyed = True
        try:
            self.root.destroy()
        except Exception:
            pass

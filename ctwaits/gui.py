"""tkinter desktop app for Ontario wait times (Windows 10 and 11).

Run from the project root with ``python -m ctwaits.gui``; the PyInstaller
build turns this file into ``CTWaits.exe``.
"""
from __future__ import annotations

import json
import math
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import date
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

try:
	from ctwaits import core
except ImportError:  # run as a bare script from inside the package folder
	import core  # type: ignore

APP_TITLE = "Ontario Wait Times"
POLL_MS = 100
P90_LABEL = "90th percentile wait time (days)"
RESULTS_HINT = "Search results. Click a column header to sort. Click a site to show or hide its details."

# Light, high-contrast palette
BG = "#FFFFFF"
FG = "#111111"
MUTED = "#3C4043"
NODATA_FG = "#5F6368"
PROVINCE_BG = "#E8EFF8"
BORDER = "#B8BEC6"
GRID = "#E1E4E8"
LINK = "#0B57D0"
SITE_LINE = "#0B57D0"
PROV_LINE = "#5F6368"
TARGET_LINE = "#B3261E"
SELECTED_BG = "#DCE7F7"
HOVER_BG = "#EEF5FD"
HOVER_PROVINCE_BG = "#DDE7F4"

IMAGING_CHOICES = ("CT", "MRI", "Breast screening")
IMAGING_CODES = {"CT": "CT", "MRI": "MRI", "Breast screening": "BREAST"}
WAIT_CHOICES = {label: code for code, label in core.SURGERY_WAITS.items()}

# Results columns: key, width at 96 dpi, anchor, stretch
COLUMNS = (("name", 380, "w", True), ("km", 70, "e", False), ("latest", 250, "e", False), ("median", 130, "e", False))

TRIANGLE_DOWN = "▼"
TRIANGLE_UP = "▲"
PRIORITY_HELP = ("P2-- Urgent; 2 day target\n"
	"P3-- Semi-Urgent/Suspected Cancer; 10 day target\n"
	"P4-- Non-Urgent/Elective; 28 day target")
TOOLTIP_BG = "#FFFFE1"


def fmt_days(v) -> str:
	return "n/a" if v is None else f"{v:.0f}"


def fmt_pct(v) -> str:
	return "n/a" if v is None else f"{v:.0f}%"


def fmt_checked(iso: str) -> str:
	try:
		return core.format_date(date.fromisoformat(iso))
	except (TypeError, ValueError):
		return iso


def scale_factor(widget: tk.Misc) -> float:
	"""1.0 at 96 dpi, larger on scaled displays."""
	try:
		return max(1.0, float(widget.tk.call("tk", "scaling")) / (96 / 72))
	except tk.TclError:
		return 1.0


def open_url(url: str) -> None:
	try:
		webbrowser.open(url, new=2)
	except webbrowser.Error:
		pass


def link_label(parent: tk.Misc, text: str, url: str, bg: str = BG) -> tk.Label:
	f = tkfont.nametofont("TkDefaultFont").copy()
	f.configure(underline=True)
	lab = tk.Label(parent, text=text, fg=LINK, bg=bg, cursor="hand2", font=f, padx=0, bd=0)
	lab.bind("<Button-1>", lambda _e: open_url(url))
	return lab


SETTINGS_FILE = core.default_cache_dir().parent / "settings.json"  # %LOCALAPPDATA%\CTWaits\settings.json


def load_settings() -> dict:
	"""Local, per-user settings (the last postal code). Never part of the project files."""
	try:
		data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def save_settings(**values) -> None:
	data = load_settings()
	data.update(values)
	try:
		SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
		SETTINGS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
	except OSError:
		pass


def domain_of(url: str) -> str:
	host = urllib.parse.urlparse(url).netloc
	return host[4:] if host.startswith("www.") else host or url


class Tooltip:
	"""A plain hover tooltip for one widget."""

	def __init__(self, widget: tk.Misc, text, delay_ms: int = 300):
		self.widget, self.text, self.delay = widget, text, delay_ms
		self.tip: Optional[tk.Toplevel] = None
		self._after: Optional[str] = None
		widget.bind("<Enter>", self._schedule, add="+")
		widget.bind("<Leave>", self.hide, add="+")
		widget.bind("<ButtonPress>", self.hide, add="+")

	def _schedule(self, _e=None) -> None:
		self._cancel()
		self._after = self.widget.after(self.delay, self.show)

	def _cancel(self) -> None:
		if self._after is not None:
			self.widget.after_cancel(self._after)
			self._after = None

	def show(self) -> None:
		self._after = None
		if self.tip is not None or not self.widget.winfo_viewable():
			return
		self.tip = tk.Toplevel(self.widget)
		self.tip.overrideredirect(True)
		self.tip.attributes("-topmost", True)
		text = self.text() if callable(self.text) else self.text
		tk.Label(self.tip, text=text, justify="left", bg=TOOLTIP_BG, fg="#000000", relief="solid", bd=1,
			padx=6, pady=4).pack()
		x = self.widget.winfo_rootx() + 4
		y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
		self.tip.geometry(f"+{x}+{y}")

	def hide(self, _e=None) -> None:
		self._cancel()
		if self.tip is not None:
			self.tip.destroy()
			self.tip = None


# ---------------------------------------------------------------- chart

class LineChart(tk.Canvas):
	"""A small line chart drawn on a Canvas: monthly values with gaps for
	missing months, an optional dashed target line, and a hover readout."""

	def __init__(self, master: tk.Misc, months: list[str], series: list[tuple[str, list, str]],
			target: Optional[float], y_title: str, width: int = 600, height: int = 230):
		s = scale_factor(master)
		self.W, self.H = int(width * s), int(height * s)
		super().__init__(master, width=self.W, height=self.H, bg=BG, highlightthickness=0)
		self.months, self.series, self.target, self.y_title = months, series, target, y_title
		self.font = tkfont.nametofont("TkDefaultFont")
		self.small = self.font.copy()
		self.small.configure(size=max(7, int(self.font.cget("size")) - 1) if int(self.font.cget("size")) > 0 else -11)
		lh = self.font.metrics("linespace")
		self.L, self.R, self.T, self.B = int(46 * s), int(12 * s), int(lh * 2.4), int(lh * 1.8)
		self._draw()
		self.bind("<Motion>", self._hover)
		self.bind("<Leave>", lambda _e: self.delete("hover"))

	def _x(self, i: int) -> float:
		n = max(1, len(self.months) - 1)
		return self.L + (self.W - self.L - self.R) * i / n

	def _y(self, v: float) -> float:
		return self.T + (self.H - self.T - self.B) * (1 - v / self.ymax)

	@staticmethod
	def _nice_step(span: float) -> float:
		raw = span / 4 or 1
		mag = 10 ** len(str(int(raw))) / 10
		for m in (1, 2, 2.5, 5, 10):
			if raw <= m * mag:
				return m * mag
		return 10 * mag

	def _draw(self) -> None:
		vals = [v for _n, ys, _c in self.series for v in ys if v is not None]
		if self.target is not None:
			vals.append(self.target)
		if not self.months or not vals:
			self.create_text(self.W / 2, self.H / 2, text="No reported values to graph.", fill=MUTED, font=self.font)
			return
		step = self._nice_step(max(vals))
		self.ymax = step * (int(max(vals) / step) + 1)
		x0, x1, y0, y1 = self.L, self.W - self.R, self.T, self.H - self.B
		v = 0.0
		while v <= self.ymax + 1e-9:
			y = self._y(v)
			self.create_line(x0, y, x1, y, fill=GRID)
			self.create_text(x0 - 6, y, text=f"{v:g}", anchor="e", fill=MUTED, font=self.small)
			v += step
		for i, m in enumerate(self.months):
			if m.endswith("01") or i == 0:
				x = self._x(i)
				self.create_line(x, y0, x, y1, fill=GRID)
				self.create_text(x, y1 + 4, text=m[:4] if m.endswith("01") else core.month_short(m),
					anchor="n", fill=MUTED, font=self.small)
		self.create_line(x0, y1, x1, y1, fill=BORDER)
		self.create_line(x0, y0, x0, y1, fill=BORDER)
		self.create_text(x0, 4, text=self.y_title, anchor="nw", fill=FG, font=self.small)
		if self.target is not None:
			yt = self._y(self.target)
			self.create_line(x0, yt, x1, yt, fill=TARGET_LINE, dash=(5, 3), width=1.5)
		for _name, ys, color in self.series:
			run: list[float] = []
			for i, val in enumerate(ys + [None]):
				if val is not None:
					run += [self._x(i), self._y(val)]
					continue
				if len(run) >= 4:
					self.create_line(*run, fill=color, width=2)
				elif len(run) == 2:
					self.create_oval(run[0] - 2.5, run[1] - 2.5, run[0] + 2.5, run[1] + 2.5, fill=color, outline=color)
				run = []
		# legend, on the line under the title
		lx, ly = x0, 4 + self.font.metrics("linespace") * 1.5
		items = [(n, c, None) for n, _ys, c in self.series]
		if self.target is not None:
			items.append((f"Target: {self.target:g} days", TARGET_LINE, (5, 3)))
		for name, color, dash in items:
			self.create_line(lx, ly + 2, lx + 18, ly + 2, fill=color, width=2, dash=dash or ())
			t = self.create_text(lx + 22, ly + 2, text=name, anchor="w", fill=FG, font=self.small)
			lx = self.bbox(t)[2] + 14

	def _hover(self, e) -> None:
		self.delete("hover")
		if not self.months or not hasattr(self, "ymax"):
			return
		n = len(self.months)
		span = (self.W - self.L - self.R) / max(1, n - 1)
		i = min(n - 1, max(0, round((e.x - self.L) / span))) if n > 1 else 0
		x = self._x(i)
		self.create_line(x, self.T, x, self.H - self.B, fill=BORDER, tags="hover")
		lines = [core.month_long(self.months[i])]
		for name, ys, color in self.series:
			v = ys[i]
			lines.append(f"{name}: {'n/a' if v is None else f'{v:g} days'}")
			if v is not None:
				y = self._y(v)
				self.create_oval(x - 3.5, y - 3.5, x + 3.5, y + 3.5, outline=color, width=2, fill=BG, tags="hover")
		t = self.create_text(0, 0, text="\n".join(lines), anchor="nw", fill=FG, font=self.small, tags="hover")
		bx = self.bbox(t)
		w, h = bx[2] - bx[0], bx[3] - bx[1]
		tx = x + 10 if x + 10 + w < self.W - 4 else x - 10 - w
		ty = self.T + 4
		self.coords(t, tx, ty)
		r = self.create_rectangle(tx - 5, ty - 4, tx + w + 5, ty + h + 4, fill=BG, outline=BORDER, tags="hover")
		self.tag_raise(t, r)



# ---------------------------------------------------------------- results table

@dataclass
class RowSpec:
	"""One results row: display values, a sort key per column (None sorts
	last), its kind ("site", "province" or "nodata"), and its detail builder."""
	values: tuple
	sort_keys: tuple
	kind: str
	build: Callable[[tk.Frame], None]


@dataclass
class _Panel:
	clip: tk.Frame
	frame: tk.Frame
	spacers: list
	height: int


class DetailTree(ttk.Frame):
	"""The results list: a plain ttk.Treeview, so it keeps the native look
	(headings that highlight on hover and sort on click, column boundaries
	that drag), whose rows open an indented detail panel directly underneath
	on a single click. Each open panel sits over blank child rows the tree
	reserves for it, so it scrolls and re-sorts with its row. Rows with no
	data always stay at the bottom."""

	INDENT = 24

	def __init__(self, master: tk.Misc):
		super().__init__(master)
		self.font = tkfont.nametofont("TkDefaultFont")
		bold = self.font.copy()
		bold.configure(weight="bold")
		self.rowh = self.font.metrics("linespace") + 5
		ttk.Style(self).configure("Treeview", rowheight=self.rowh)
		self.tree = ttk.Treeview(self, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
		s = scale_factor(self)
		for key, width, anchor, stretch in COLUMNS:
			self.tree.column(key, width=int(width * s), minwidth=int(40 * s), anchor=anchor, stretch=stretch)
		self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview)
		self.tree.configure(yscrollcommand=self._yscroll)
		self.tree.grid(row=0, column=0, sticky="nsew")
		self.sb.grid(row=0, column=1, sticky="ns")
		self.columnconfigure(0, weight=1)
		self.rowconfigure(0, weight=1)
		self.tree.tag_configure("province", background=PROVINCE_BG, font=bold)
		self.tree.tag_configure("nodata", foreground=NODATA_FG)
		self.tree.tag_configure("hover", background=HOVER_BG)
		# Treeview gives earlier-created tags priority, so a hovered province row
		# swaps its tag rather than adding one.
		self.tree.tag_configure("hover_province", background=HOVER_PROVINCE_BG, font=bold)
		self._hover: Optional[str] = None
		self.rows: dict[str, RowSpec] = {}
		self.panels: dict[str, _Panel] = {}
		self.headings = ["Site", "km", "", ""]
		self.sort_col, self.sort_desc = 3, False
		self._pressed: Optional[str] = None
		self._pending = False
		self._last_sel: Optional[str] = None
		self.tree.bind("<ButtonPress-1>", self._press, add="+")
		self.tree.bind("<ButtonRelease-1>", self._release, add="+")
		self.tree.bind("<Return>", lambda _e: self._toggle_selected())
		self.tree.bind("<space>", lambda _e: self._toggle_selected())
		self.tree.bind("<<TreeviewSelect>>", self._skip_spacers)
		self.tree.bind("<Configure>", lambda _e: self._schedule())
		self.tree.bind("<Motion>", self._on_motion, add="+")
		self.tree.bind("<Leave>", lambda _e: self._set_hover(None), add="+")
		self.bind_all("<MouseWheel>", self._wheel, add="+")
		self._draw_headings()

	# ------------------------------------------------------------ data

	def set_headings(self, headings: list[str]) -> None:
		self.headings = list(headings)
		self._draw_headings()

	def _draw_headings(self) -> None:
		for i, (key, *_rest) in enumerate(COLUMNS):
			text = self.headings[i]
			if i == self.sort_col and text and self.rows:
				text += " (desc)" if self.sort_desc else " (asc)"
			self.tree.heading(key, text=text, command=lambda c=i: self.sort_by(c))

	# ------------------------------------------------------------ hover tint

	def _on_motion(self, e) -> None:
		iid = self.tree.identify_row(e.y)
		self._set_hover(iid if iid in self.rows and self.tree.identify_region(e.x, e.y) in ("cell", "tree") else None)

	def _set_hover(self, iid: Optional[str]) -> None:
		if iid == self._hover:
			return
		for item in (self._hover, iid):
			if item is None or not self.tree.exists(item):
				continue
			kind = self.rows[item].kind if item in self.rows else ""
			if item != iid:
				tags = (kind,) if kind in ("province", "nodata") else ()
			elif kind == "province":
				tags = ("hover_province",)
			else:
				tags = ("nodata", "hover") if kind == "nodata" else ("hover",)
			self.tree.item(item, tags=tags)
		self._hover = iid

	def clear(self) -> None:
		self._hover = None
		for iid in list(self.panels):
			self._close(iid)
		self.tree.delete(*self.tree.get_children(""))
		self.rows = {}
		self._draw_headings()

	def populate(self, specs: list[RowSpec], sort_col: int) -> None:
		self.clear()
		for i, spec in enumerate(specs):
			iid = f"r{i}"
			tags = (spec.kind,) if spec.kind in ("province", "nodata") else ()
			self.tree.insert("", "end", iid=iid, values=spec.values, tags=tags)
			self.rows[iid] = spec
		self.sort_col, self.sort_desc = sort_col, False
		self._apply_sort()
		self.tree.yview_moveto(0)

	def first_site(self) -> Optional[str]:
		for iid in self.tree.get_children(""):
			if self.rows[iid].kind == "site":
				return iid
		return None

	# ------------------------------------------------------------ sorting

	def sort_by(self, col: int) -> None:
		if not self.rows or not self.headings[col]:
			return
		self.sort_desc = (not self.sort_desc) if col == self.sort_col else False
		self.sort_col = col
		self._apply_sort()

	def _apply_sort(self) -> None:
		col, desc = self.sort_col, self.sort_desc

		def ordered(iids):
			has = [i for i in iids if self.rows[i].sort_keys[col] is not None]
			none = [i for i in iids if self.rows[i].sort_keys[col] is None]
			has.sort(key=lambda i: self.rows[i].sort_keys[col], reverse=desc)
			return has + none

		data = [i for i in self.rows if self.rows[i].kind != "nodata"]
		empty = [i for i in self.rows if self.rows[i].kind == "nodata"]
		for idx, iid in enumerate(ordered(data) + ordered(empty)):
			self.tree.move(iid, "", idx)
		self._draw_headings()
		self._schedule()

	# ------------------------------------------------------------ detail panels

	def toggle(self, iid: str) -> None:
		"""Open or close one row's panel; opening it closes any other."""
		if iid in self.panels:
			self._close(iid)
		else:
			for other in list(self.panels):
				self._close(other)
			self._open(iid)
		self._schedule()

	def _open(self, iid: str) -> None:
		spec = self.rows.get(iid)
		if spec is None:
			return
		clip = tk.Frame(self.tree, bg=BG, bd=0, highlightthickness=0)
		frame = tk.Frame(clip, bg=BG, highlightthickness=1, highlightbackground=BORDER)
		spec.build(frame)
		frame.update_idletasks()
		h = frame.winfo_reqheight()
		n = max(1, math.ceil((h + 10) / self.rowh))
		blank = ("",) * len(COLUMNS)
		spacers = [self.tree.insert(iid, "end", iid=f"{iid}:{k}", values=blank, tags=("spacer",)) for k in range(n)]
		self.tree.item(iid, open=True)
		self.panels[iid] = _Panel(clip, frame, spacers, h)
		self.after_idle(lambda: self._reveal(iid))

	def _close(self, iid: str) -> None:
		p = self.panels.pop(iid, None)
		if p is None:
			return
		p.clip.destroy()
		if self.tree.exists(iid):
			self.tree.delete(*[s for s in p.spacers if self.tree.exists(s)])
			self.tree.item(iid, open=False)

	def _reveal(self, iid: str) -> None:
		p = self.panels.get(iid)
		if p is None or not self.tree.exists(iid):
			return
		self.tree.see(p.spacers[-1])
		self.tree.see(iid)
		self._schedule()

	def _schedule(self) -> None:
		if not self._pending:
			self._pending = True
			self.after_idle(self._reposition)

	def _visible_order(self) -> list[str]:
		out: list[str] = []
		for top in self.tree.get_children(""):
			out.append(top)
			if self.tree.item(top, "open"):
				out.extend(self.tree.get_children(top))
		return out

	def _reposition(self) -> None:
		self._pending = False
		if not self.panels:
			return
		index = {iid: i for i, iid in enumerate(self._visible_order())}
		first, y0 = None, 0
		for y in range(0, 300, 2):
			iid = self.tree.identify_row(y)
			if iid:
				bb = self.tree.bbox(iid)
				if bb:
					first, y0 = iid, bb[1]
					break
		width, height = self.tree.winfo_width(), self.tree.winfo_height()
		indent = int(self.INDENT * scale_factor(self))
		for p in self.panels.values():
			if first is None or p.spacers[0] not in index:
				p.clip.place_forget()
				continue
			top = y0 + (index[p.spacers[0]] - index[first]) * self.rowh
			full = len(p.spacers) * self.rowh
			vt, vb = max(top, y0), min(top + full, height - 1)
			if vb <= vt:
				p.clip.place_forget()
				continue
			p.clip.place(x=1, y=vt, width=max(1, width - 2), height=vb - vt)
			p.frame.place(x=indent, y=top - vt, width=max(1, width - indent - 14), height=p.height)

	def _yview(self, *args) -> None:
		self.tree.yview(*args)
		self._schedule()

	def _yscroll(self, first, last) -> None:
		self.sb.set(first, last)
		self._schedule()

	def _wheel(self, e) -> None:
		"""Scroll the list when the wheel turns over an open panel (the tree
		itself handles the wheel over its own rows)."""
		w = self.winfo_containing(e.x_root, e.y_root)
		if w is None or w is self.tree or not str(w).startswith(str(self.tree) + "."):
			return
		self.tree.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

	# ------------------------------------------------------------ clicks and keys

	def _press(self, e) -> None:
		region = self.tree.identify_region(e.x, e.y)
		self._pressed = self.tree.identify_row(e.y) if region in ("cell", "tree") else None

	def _release(self, e) -> None:
		iid = self.tree.identify_row(e.y)
		region = self.tree.identify_region(e.x, e.y)
		if region in ("cell", "tree") and iid and iid == self._pressed and iid in self.rows:
			self.toggle(iid)
		self._pressed = None

	def _toggle_selected(self) -> None:
		sel = self.tree.selection()
		if sel and sel[0] in self.rows:
			self.toggle(sel[0])

	def _skip_spacers(self, _e=None) -> None:
		"""Arrow keys step over the blank rows under an open panel."""
		sel = self.tree.selection()
		if not sel:
			return
		iid = sel[0]
		if iid in self.rows:
			self._last_sel = iid
			return
		parent = self.tree.parent(iid)
		if self._last_sel == parent:
			nxt = self.tree.next(parent)
			target = nxt or parent
		else:
			target = parent
		self.tree.selection_set(target)
		self.tree.see(target)


# ---------------------------------------------------------------- procedure search-select

class SearchSelect(ttk.Frame):
	"""An entry with a drop-down list of matching procedures; typing filters
	the list, Down or a click moves into it, Enter or a click selects."""

	MAX_SHOWN = 60

	def __init__(self, master: tk.Misc, width: int = 40, on_select: Optional[Callable] = None):
		super().__init__(master)
		self.var = tk.StringVar()
		self.entry = ttk.Entry(self, textvariable=self.var, width=width)
		self.entry.pack(fill="x")
		self.items: list[core.Procedure] = []
		self.shown: list[core.Procedure] = []
		self.selected: Optional[core.Procedure] = None
		self.on_select = on_select
		self.popup: Optional[tk.Toplevel] = None
		self.listbox: Optional[tk.Listbox] = None
		self.entry.bind("<KeyRelease>", self._typed)
		self.entry.bind("<Down>", self._down)
		self.entry.bind("<Return>", self._enter)
		self.entry.bind("<Escape>", lambda _e: self.hide())
		self.entry.bind("<Button-1>", lambda _e: self.after(10, self._show_matches))
		self.entry.bind("<FocusOut>", lambda _e: self.after(150, self._focus_check))
		top = self.winfo_toplevel()
		top.bind("<Configure>", lambda e: self.hide() if e.widget is top else None, add="+")

	def set_items(self, items: list[core.Procedure]) -> None:
		self.items = list(items)

	def set_state(self, enabled: bool, text: Optional[str] = None) -> None:
		if text is not None:
			self.var.set(text)
		self.entry.state(["!disabled"] if enabled else ["disabled"])

	def _typed(self, e) -> None:
		if e.keysym in ("Down", "Up", "Return", "Escape", "Tab"):
			return
		if self.selected is not None and self.var.get() != self.selected.name:
			self.selected = None
		self._show_matches()

	def _show_matches(self) -> None:
		if "disabled" in self.entry.state() or not self.items:
			return
		text = self.var.get()
		if self.selected is not None and text == self.selected.name:
			text = ""
		self.shown = core.find_procedures(text, self.items)[: self.MAX_SHOWN]
		if not self.shown:
			self.hide()
			return
		if self.popup is None or not self.popup.winfo_exists():
			self.popup = tk.Toplevel(self)
			self.popup.overrideredirect(True)
			self.popup.attributes("-topmost", True)
			frame = tk.Frame(self.popup, bg=BORDER, bd=0)
			frame.pack(fill="both", expand=True)
			self.listbox = tk.Listbox(frame, activestyle="dotbox", exportselection=False, bg=BG, fg=FG,
				selectbackground=SELECTED_BG, selectforeground=FG, highlightthickness=0, bd=0)
			sb = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
			self.listbox.configure(yscrollcommand=sb.set)
			self.listbox.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
			sb.pack(side="right", fill="y", pady=1)
			self.listbox.bind("<ButtonRelease-1>", lambda _e: self._choose())
			self.listbox.bind("<Return>", lambda _e: self._choose())
			self.listbox.bind("<Escape>", lambda _e: (self.hide(), self.entry.focus_set()))
			self.listbox.bind("<FocusOut>", lambda _e: self.after(150, self._focus_check))
			self.listbox.bind("<Up>", self._up)
		self.listbox.delete(0, "end")
		for p in self.shown:
			self.listbox.insert("end", p.name)
		self.listbox.configure(height=min(10, len(self.shown)))
		self.update_idletasks()
		x, y = self.entry.winfo_rootx(), self.entry.winfo_rooty() + self.entry.winfo_height()
		w = max(self.entry.winfo_width(), 320)
		self.popup.geometry(f"{w}x{self.listbox.winfo_reqheight() + 2}+{x}+{y}")
		self.popup.deiconify()
		self.popup.lift()

	def _down(self, _e=None):
		if self.popup is None or not self.popup.winfo_exists() or not self.popup.winfo_viewable():
			self._show_matches()
		if self.listbox is not None and self.shown:
			self.listbox.focus_set()
			self.listbox.selection_clear(0, "end")
			self.listbox.selection_set(0)
			self.listbox.activate(0)
		return "break"

	def _up(self, _e=None):
		if self.listbox is not None and self.listbox.curselection() == (0,):
			self.entry.focus_set()
			return "break"
		return None

	def _enter(self, _e=None):
		if self.shown and self.popup is not None and self.popup.winfo_exists() and self.popup.winfo_viewable():
			exact = [p for p in self.shown if p.name.casefold() == self.var.get().strip().casefold()]
			self._pick(exact[0] if exact else self.shown[0])
			return "break"
		return None

	def _choose(self) -> None:
		if self.listbox is None:
			return
		sel = self.listbox.curselection()
		if sel:
			self._pick(self.shown[sel[0]])

	def _pick(self, p: core.Procedure) -> None:
		self.selected = p
		self.var.set(p.name)
		self.hide()
		self.entry.focus_set()
		self.entry.icursor("end")
		if self.on_select:
			self.on_select(p)

	def _focus_check(self) -> None:
		f = self.focus_get()
		if f is self.entry or f is self.listbox:
			return
		if self.popup is not None and self.popup.winfo_exists():
			under = self.winfo_containing(*self.winfo_pointerxy())
			if under is not None and str(under).startswith(str(self.popup)):
				return
		self.hide()

	def hide(self) -> None:
		if self.popup is not None and self.popup.winfo_exists():
			self.popup.withdraw()



# ---------------------------------------------------------------- app

class App(tk.Tk):
	def __init__(self) -> None:
		super().__init__()
		self.title(APP_TITLE)
		s = scale_factor(self)
		w = min(int(1080 * s), self.winfo_screenwidth() - 40)
		h = min(int(800 * s), self.winfo_screenheight() - 80)
		self.geometry(f"{w}x{h}")
		self.minsize(min(w, int(900 * s)), min(h, int(600 * s)))
		self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
		self._analysis: Optional[core.Analysis] = None
		self._procedures: list[core.Procedure] = []
		self._busy = False
		self._loading_procs = False
		self._targets: dict[tuple[int, int], dict[int, Optional[float]]] = {}
		self._targets_loading: set[tuple[int, int]] = set()

		self.v_postal = tk.StringVar(value=str(load_settings().get("postal") or core.DEFAULT_POSTAL))
		self.v_radius = tk.StringVar(value=f"{core.DEFAULT_RADIUS_KM:g}")
		self.v_kind = tk.StringVar(value="imaging")
		self.v_imaging = tk.StringVar(value="CT")
		self.v_wait = tk.StringVar(value=core.SURGERY_WAITS[2])
		self.v_priority = tk.StringVar(value="4")
		self.v_updated = tk.StringVar(value=".")
		self.advanced_open = False

		self._build()
		self._on_kind()
		self.bind("<Return>", self._on_return)
		self._poll_after = self.after(POLL_MS, self._poll)
		self._load_procedures()

	# ------------------------------------------------------------ layout

	def _build(self) -> None:
		form = ttk.Frame(self, padding=(12, 10, 12, 6))
		form.pack(fill="x")

		def labelled(parent, col, text, widget):
			ttk.Label(parent, text=text).grid(row=0, column=col, sticky="w", padx=(0, 4))
			widget.grid(row=1, column=col, sticky="w", padx=(0, 16))
			return widget

		line1 = ttk.Frame(form)
		line1.pack(fill="x")
		labelled(line1, 0, "Postal code", ttk.Entry(line1, textvariable=self.v_postal, width=10))
		labelled(line1, 1, "Radius (km)", ttk.Entry(line1, textvariable=self.v_radius, width=7))
		kinds = ttk.Frame(line1)
		ttk.Radiobutton(kinds, text="Imaging", value="imaging", variable=self.v_kind,
			command=self._on_kind).pack(side="left", padx=(0, 12))
		ttk.Radiobutton(kinds, text="Surgery", value="surgery", variable=self.v_kind,
			command=self._on_kind).pack(side="left")
		labelled(line1, 2, "Search for", kinds)

		line2 = ttk.Frame(form, padding=(0, 8, 0, 0))
		line2.pack(fill="x")
		self.opt_cell = ttk.Frame(line2)
		self.opt_cell.grid(row=0, column=0, rowspan=2, sticky="sw")
		self.imaging_opts = ttk.Frame(self.opt_cell)
		self.cb_imaging = ttk.Combobox(self.imaging_opts, textvariable=self.v_imaging, values=IMAGING_CHOICES,
			state="readonly", width=17)
		self.cb_imaging.bind("<<ComboboxSelected>>", lambda _e: self._on_kind())
		labelled(self.imaging_opts, 0, "Imaging type", self.cb_imaging)
		self.surgery_opts = ttk.Frame(self.opt_cell)
		self.procedure = SearchSelect(self.surgery_opts, width=44, on_select=lambda _p: self._load_targets())
		labelled(self.surgery_opts, 0, "Procedure (type to search)", self.procedure)
		self.cb_wait = ttk.Combobox(self.surgery_opts, textvariable=self.v_wait, values=list(WAIT_CHOICES),
			state="readonly", width=34)
		self.cb_wait.bind("<<ComboboxSelected>>", lambda _e: self._load_targets())
		labelled(self.surgery_opts, 1, "Wait", self.cb_wait)

		self.cb_priority = ttk.Combobox(line2, textvariable=self.v_priority,
			values=[str(p) for p in core.PRIORITIES], state="readonly", width=4)
		prio_head = ttk.Frame(line2)
		prio_head.grid(row=0, column=1, sticky="w", padx=(0, 4))
		ttk.Label(prio_head, text="Priority").pack(side="left")
		self.prio_help = ttk.Label(prio_head, text="(?)", foreground=LINK, cursor="question_arrow")
		self.prio_help.pack(side="left", padx=(4, 0))
		self.prio_tip = Tooltip(self.prio_help, self._priority_help)
		self.cb_priority.grid(row=1, column=1, sticky="w", padx=(0, 16))

		# The Search button and the progress bar share one cell; the bar only
		# appears while a search runs.
		self.search_cell = ttk.Frame(line2)
		self.search_cell.grid(row=1, column=2, sticky="w", padx=(0, 8))
		self.btn_search = ttk.Button(self.search_cell, text="Search", command=self._search, width=12)
		self.btn_search.grid(row=0, column=0, sticky="nsew")
		self.progress = ttk.Progressbar(self.search_cell, mode="indeterminate", length=160)
		self.btn_export = ttk.Button(line2, text="Export CSV", command=self._export, state="disabled")
		self.btn_export.grid(row=1, column=3, sticky="w")

		self.results_box = ttk.LabelFrame(self, text=RESULTS_HINT, padding=6)
		self.results_box.pack(fill="both", expand=True, padx=12, pady=(4, 4))
		self.table = DetailTree(self.results_box)
		self.table.pack(fill="both", expand=True)

		adv = ttk.Frame(self, padding=(12, 0, 12, 0))
		adv.pack(fill="x")
		self.adv_toggle = ttk.Label(adv, text=f"{TRIANGLE_DOWN} Show advanced", cursor="hand2", padding=(0, 2))
		self.adv_toggle.pack(anchor="w")
		self.adv_toggle.bind("<Button-1>", lambda _e: self._toggle_advanced())
		self.adv_box = ttk.LabelFrame(self, text="Rank persistence: how well one month's ranking predicts later months",
			padding=6)
		lag_cols = (("lag", "Lag (months)", 100), ("rho", "Mean Spearman rho", 150),
			("top3", "Leader still top 3", 150), ("chance", "Chance (3/n)", 110), ("pairs", "Month pairs", 110))
		self.lag_tree = ttk.Treeview(self.adv_box, columns=[c[0] for c in lag_cols], show="headings", height=4)
		for key, label, width in lag_cols:
			self.lag_tree.heading(key, text=label)
			self.lag_tree.column(key, width=width, anchor="e", stretch=False)
		self.lag_tree.pack(fill="x")

		self.footer = tk.Frame(self, bg=ttk.Style(self).lookup("TFrame", "background") or self.cget("bg"))
		self.footer.pack(fill="x", padx=12, pady=(4, 10))
		fb = self.footer.cget("bg")
		tk.Label(self.footer, text="From ", bg=fb, fg=FG, padx=0, bd=0).pack(side="left")
		link_label(self.footer, core.SOURCE_LABEL, core.SOURCE_URL, bg=fb).pack(side="left")
		tk.Label(self.footer, textvariable=self.v_updated, bg=fb, fg=FG, padx=0, bd=0).pack(side="left")

	def _toggle_advanced(self) -> None:
		self.advanced_open = not self.advanced_open
		if self.advanced_open:
			self.adv_toggle.configure(text=f"{TRIANGLE_UP} Hide advanced")
			self.adv_box.pack(fill="x", padx=12, pady=(2, 4), before=self.footer)
		else:
			self.adv_toggle.configure(text=f"{TRIANGLE_DOWN} Show advanced")
			self.adv_box.pack_forget()

	def _on_kind(self) -> None:
		surgery = self.v_kind.get() == "surgery"
		if surgery:
			self.imaging_opts.pack_forget()
			self.surgery_opts.pack(anchor="w")
		else:
			self.surgery_opts.pack_forget()
			self.procedure.hide()
			self.imaging_opts.pack(anchor="w")
		screening = not surgery and self.v_imaging.get() == "Breast screening"
		self.cb_priority.state(["disabled"] if screening else ["!disabled", "readonly"])
		if screening:
			self.prio_tip.hide()
			self.prio_help.pack_forget()
		elif not self.prio_help.winfo_ismapped():
			self.prio_help.pack(side="left", padx=(4, 0))
		if surgery and not self._procedures:
			self._load_procedures()

	# ------------------------------------------------------------ priority targets

	def _priority_help(self) -> str:
		"""CT and MRI targets as given; for surgery, the selected procedure's
		targets from Ontario Health's provincial figures."""
		if self.v_kind.get() != "surgery":
			return PRIORITY_HELP
		proc = self.procedure.selected
		labels = {2: "P2-- Urgent", 3: "P3-- Semi-Urgent", 4: "P4-- Non-Urgent/Elective"}
		if proc is None:
			return "\n".join(labels.values()) + "\nPick a procedure to see its targets."
		targets = self._targets.get((proc.id, WAIT_CHOICES[self.v_wait.get()]))
		if targets is None:
			self._load_targets()
			return "\n".join(labels.values()) + "\nLoading this procedure's targets."

		def line(p):
			t = targets.get(p)
			return f"{labels[p]}; {t:g} day target" if t is not None else f"{labels[p]}; no target published"

		return "\n".join(line(p) for p in core.PRIORITIES)

	def _load_targets(self) -> None:
		proc = self.procedure.selected
		if proc is None:
			return
		key = (proc.id, WAIT_CHOICES[self.v_wait.get()])
		if key in self._targets or key in self._targets_loading:
			return
		self._targets_loading.add(key)
		svc = core.Service(core.SURGERY, proc, key[1])

		def work():
			try:
				t = core.site_history(core.PROVINCIAL_ID, svc).targets
			except Exception:
				t = None
			self._queue.put(("targets", (key, t)))

		threading.Thread(target=work, daemon=True).start()

	def _on_return(self, e) -> None:
		# Enter in the procedure box picks a procedure; anywhere else it searches.
		if e.widget is self.procedure.entry or e.widget is self.procedure.listbox:
			return
		if e.widget is self.table.tree:
			return
		self._search()

	# ------------------------------------------------------------ procedures

	def _load_procedures(self) -> None:
		if self._loading_procs:
			return
		self._loading_procs = True
		self.procedure.set_state(False, "Loading procedures...")

		def work():
			try:
				self._queue.put(("procedures", core.procedures()))
			except Exception as e:
				self._queue.put(("procedures_error", str(e) or type(e).__name__))

		threading.Thread(target=work, daemon=True).start()

	# ------------------------------------------------------------ search

	def _service(self) -> core.Service:
		if self.v_kind.get() == "surgery":
			proc = self.procedure.selected
			if proc is None:
				if not self._procedures:
					raise ValueError("The procedure list has not loaded yet. Check the connection and try again.")
				text = self.procedure.var.get().strip().casefold()
				exact = [p for p in self._procedures if p.name.casefold() == text]
				if not exact:
					raise ValueError("Choose a procedure from the list. Start typing, for example \"knee\".")
				proc = exact[0]
			return core.Service(core.SURGERY, proc, WAIT_CHOICES[self.v_wait.get()])
		return core.Service(IMAGING_CODES[self.v_imaging.get()])

	def _params(self) -> dict:
		svc = self._service()
		return dict(
			postal_code=core.normalize_postal(self.v_postal.get()),
			radius_km=core.normalize_radius(self.v_radius.get()),
			service=svc,
			priority=core.normalize_priority(self.v_priority.get()) if svc.has_history else 4,
		)

	def _search(self) -> None:
		if self._busy:
			return
		try:
			params = self._params()
		except ValueError as e:
			messagebox.showerror(APP_TITLE, str(e), parent=self)
			return
		self.procedure.hide()
		self._set_busy(True)
		threading.Thread(target=self._work, args=(params,), daemon=True).start()

	def _work(self, params: dict) -> None:
		try:
			self._queue.put(("done", core.analyze(**params)))
		except Exception as e:  # shown in a messagebox, never as a traceback
			self._queue.put(("error", str(e) or type(e).__name__))

	def _poll(self) -> None:
		try:
			while True:
				kind, payload = self._queue.get_nowait()
				if kind == "done":
					self._set_busy(False)
					self._show(payload)  # type: ignore[arg-type]
				elif kind == "error":
					self._set_busy(False)
					messagebox.showerror(APP_TITLE, str(payload), parent=self)
				elif kind == "procedures":
					self._loading_procs = False
					self._procedures = list(payload)  # type: ignore[arg-type]
					self.procedure.set_items(self._procedures)
					self.procedure.set_state(True, "")
				elif kind == "targets":
					key, targets = payload  # type: ignore[misc]
					self._targets_loading.discard(key)
					if targets:
						self._targets[key] = targets
				elif kind == "procedures_error":
					self._loading_procs = False
					self.procedure.set_state(False, "Procedure list unavailable")
					if self.v_kind.get() == "surgery":
						messagebox.showerror(APP_TITLE, f"Could not load the procedure list: {payload}", parent=self)
		except queue.Empty:
			pass
		self._poll_after = self.after(POLL_MS, self._poll)

	def _set_busy(self, busy: bool) -> None:
		self._busy = busy
		if busy:
			self.progress.configure(length=max(80, self.btn_search.winfo_width()))
			self.btn_search.grid_remove()
			self.progress.grid(row=0, column=0, sticky="ew", ipady=2)
			self.progress.start(12)
			self.config(cursor="watch")
		else:
			self.progress.stop()
			self.progress.grid_remove()
			self.btn_search.grid()
			self.config(cursor="")

	# ------------------------------------------------------------ results

	def _show(self, a: core.Analysis) -> None:
		self._analysis = a
		save_settings(postal=a.postal_code)
		svc = a.service
		if svc.is_surgery and a.province is not None and a.province.targets:
			self._targets[(svc.procedure.id, svc.wait)] = a.province.targets
		specs: list[RowSpec] = []
		if svc.is_screening:
			self.table.set_headings(["Site", "km", "Estimated wait", ""])
			for s in a.screening:
				has = s.wait_days is not None
				specs.append(RowSpec((s.name, f"{s.distance_km:.1f}", s.wait_text, ""),
					(s.name.casefold(), s.distance_km, s.sort_key if has else None, None),
					"site" if has else "nodata", lambda f, s=s: self._screening_detail(f, s)))
			sort_col = 2
			hint = RESULTS_HINT + ("" if a.screening_complete else f" {core.SCREENING_CAP_NOTE}")
		else:
			self.table.set_headings(["Site", "km", f"Latest {P90_LABEL}", "12-mo median"])

			def spec(r: core.Row, kind: str) -> RowSpec:
				km = None if r.is_province else r.site.distance_km
				return RowSpec(
					(r.site.name, "" if km is None else f"{km:.1f}", fmt_days(r.latest_p90), fmt_days(r.median_p90)),
					(r.site.name.casefold(), km, r.latest_p90,
						None if r.median_p90 is None else (r.median_p90, r.latest_p90 if r.latest_p90 is not None else 9e9)),
					kind, lambda f, r=r: self._history_detail(f, r))

			specs = [spec(r, "site") for r in a.rows]
			if a.province_row is not None:
				specs.append(spec(a.province_row, "province"))
			specs += [spec(r, "nodata") for r in a.no_data]
			sort_col = 3
			hint = RESULTS_HINT
		self.results_box.configure(text=hint)
		self.table.populate(specs, sort_col)
		top = self.table.first_site()
		if top is not None:
			self.table.tree.selection_set(top)
			self.table.tree.focus(top)
			self.table.toggle(top)
		self._fill_lags(a)
		self.v_updated.set(f". Ontario Health last updated: {a.last_updated}." if a.last_updated else ".")
		self.btn_export.state(["!disabled"])

	def _fill_lags(self, a: core.Analysis) -> None:
		for item in self.lag_tree.get_children():
			self.lag_tree.delete(item)
		for s in a.lag_stats:
			self.lag_tree.insert("", "end", values=(s.lag_months, f"{s.mean_rho:+.2f}",
				f"{100 * s.leader_top3_share:.0f}%", f"{100 * s.chance_share:.0f}%", s.pairs))
		if a.service.is_screening:
			self.lag_tree.insert("", "end", values=("n/a", "no monthly history", "", "", ""))
		elif not a.lag_stats:
			self.lag_tree.insert("", "end", values=("n/a", "too few sites", "", "", ""))

	# ------------------------------------------------------------ detail panels

	def _info_grid(self, parent: tk.Frame, rows: list[tuple[str, object]], wrap: int) -> None:
		bold = tkfont.nametofont("TkDefaultFont").copy()
		bold.configure(weight="bold")
		for i, (label, value) in enumerate(rows):
			tk.Label(parent, text=label, bg=BG, fg=FG, font=bold, anchor="nw", justify="left",
				wraplength=int(180 * scale_factor(self))).grid(row=i, column=0, sticky="nw", padx=(0, 10), pady=3)
			if isinstance(value, tk.Widget):
				value.grid(row=i, column=1, sticky="nw", pady=3)
			else:
				tk.Label(parent, text=str(value), bg=BG, fg=FG, anchor="nw", justify="left",
					wraplength=wrap).grid(row=i, column=1, sticky="nw", pady=3)

	def _text(self, parent: tk.Misc, text: str, wrap: int, fg: str = FG) -> tk.Label:
		return tk.Label(parent, text=text, bg=BG, fg=fg, anchor="w", justify="left", wraplength=wrap)

	def _contact_box(self, parent: tk.Misc, site: core.Site, svc: core.Service, wrap: int) -> tk.Frame:
		box = tk.Frame(parent, bg=BG)
		c = core.contact_for(site.id)
		lines: list[tuple[str, str]] = []  # (text, colour)
		sources: list[str] = []
		if c is not None:
			sources = list(c.source_urls[:2])
			if c.main_phone:
				lines.append((f"Main line: {c.main_phone}", FG))
			if svc.is_surgery:
				lines.append((f"Surgical referral fax: {c.surgery_fax or 'not published; ask the main line'}", FG))
				name = svc.procedure.name.lower()
				if "hip" in name or "knee" in name:
					for i in c.msk_intake:
						lines.append((f"{i.label} fax: {i.fax}", FG))
						sources.append(i.source)
			else:
				if c.di_phone:
					lines.append((f"Imaging booking: {c.di_phone}", FG))
				fax = c.imaging_fax(svc.modality)
				hub = c.intake_named(fax)
				lines.append((f"{svc.modality} requisition fax: {fax or 'not published'}"
					+ (f" ({hub.label})" if hub else ""), FG))
				for i in c.intake_for(svc.modality):
					if i is not hub:
						lines.append((f"{i.label} fax: {i.fax}", FG))
					if i.note:
						lines.append((i.note, MUTED))
					sources.append(i.source)
		if c is not None and any(text for text, _c in lines):
			for text, colour in lines:
				self._text(box, text, wrap, colour).pack(anchor="w")
			by_domain: dict[str, str] = {}
			for u in sources:
				if u:
					by_domain.setdefault(domain_of(u), u)
			if by_domain:
				src = tk.Frame(box, bg=BG)
				self._text(src, "Sources: " if len(by_domain) > 1 else "Source: ", wrap, MUTED).pack(side="left")
				for i, (dom, url) in enumerate(list(by_domain.items())[:3]):
					if i:
						self._text(src, ", ", wrap, MUTED).pack(side="left")
					link_label(src, dom, url).pack(side="left")
				src.pack(anchor="w")
			checked = f"Checked {fmt_checked(c.checked)}. " if c.checked else ""
			if c.confidence == "low":
				caution = "Found in a directory listing; confirm with the hospital before faxing."
			elif not c.verified:
				caution = "The source page could not be re-checked automatically; confirm before faxing."
			else:
				caution = "Confirm the fax number before sending patient information."
			self._text(box, checked + caution, wrap, MUTED).pack(anchor="w")
		else:
			self._text(box, "No published phone or fax number was found for this site.", wrap).pack(anchor="w")
			q = urllib.parse.quote_plus(f"{site.name} {svc.department} phone fax")
			link_label(box, "Search the web for this department's phone and fax",
				f"https://www.google.com/search?q={q}").pack(anchor="w")
		return box

	def _history_detail(self, frame: tk.Frame, r: core.Row) -> None:
		a = self._analysis
		if a is None:
			return
		p, svc = a.priority, a.service
		h = (a.province if r.is_province else a.histories.get(r.site.id)) or core.SiteHistory()
		months = sorted(set(h.p90) | set(a.province.p90 if a.province else {}))
		series = []
		if not r.is_province:
			series.append(("This site", [(h.p90.get(m) or {}).get(p) for m in months], SITE_LINE))
		if a.province is not None:
			series.append(("Ontario", [(a.province.p90.get(m) or {}).get(p) for m in months], PROV_LINE))
		target = h.targets.get(p) if h.targets else None
		if target is None and a.province is not None:
			target = a.province.targets.get(p)
		chart = LineChart(frame, months, series, target, f"Priority {p}, {P90_LABEL}, by month", width=500)
		chart.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

		info = tk.Frame(frame, bg=BG)
		info.grid(row=0, column=1, sticky="nw", padx=(4, 10), pady=8)
		frame.columnconfigure(1, weight=1)
		wrap = int(270 * scale_factor(self))
		rows: list[tuple[str, object]] = []
		if not r.is_province:
			rows.append(("Address", r.site.full_address or "Not listed"))
			rows.append(("Contact and fax", self._contact_box(info, r.site, svc, wrap)))
		mo, pct = h.within_latest(p, a.latest_month)
		pct12 = h.within_period(p, a.window)
		tgt = f" ({target:g}-day target)" if target is not None else ""
		latest_line = f"{fmt_pct(pct)} in {core.month_long(mo)}" if mo else "n/a in the latest month"
		rows.append((f"% patients {svc.within_verb} within target time{tgt}",
			f"{latest_line}\n{fmt_pct(pct12)} in last 12 months"))
		first, reported, total = h.recording_since(p)
		rows.append(("Recording data since", f"{core.month_long(first)} (reported in {reported} of {total} months)"
			if first else f"No priority {p} figures reported"))
		self._info_grid(info, rows, wrap)

	def _screening_detail(self, frame: tk.Frame, s: core.ScreeningSite) -> None:
		wrap = int(520 * scale_factor(self))
		info = tk.Frame(frame, bg=BG)
		info.pack(anchor="w", padx=10, pady=8)
		contact = tk.Frame(info, bg=BG)
		phone = s.phone + (f", ext. {s.ext}" if s.ext else "") if s.phone else "not listed"
		self._text(contact, f"Phone: {phone}", wrap).pack(anchor="w")
		if s.toll_free:
			self._text(contact, f"Toll-free: {s.toll_free}", wrap).pack(anchor="w")
		self._text(contact, "Fax: not published in Ontario Health's breast screening data", wrap).pack(anchor="w")
		hours = "\n".join(f"{d.capitalize()}: {s.hours.get(d, 'Closed')}" for d in core.WEEKDAYS)
		rows: list[tuple[str, object]] = [
			("Address", s.full_address or "Not listed"),
			("Contact and fax", contact),
			("Estimated wait", s.wait_text),
			("Hours", hours),
			("Wheelchair accessible", "Yes" if s.accessible else "No"),
			("Languages", s.languages or "Not listed"),
			("Last updated", core.format_date(s.last_updated) or "Not listed"),
		]
		self._info_grid(info, rows, wrap)

	# ------------------------------------------------------------ export

	def _export(self) -> None:
		a = self._analysis
		if a is None:
			return
		svc = a.service
		what = svc.procedure.name if svc.is_surgery else svc.modality
		slug = "-".join("".join(c if c.isalnum() else " " for c in what.lower()).split())
		prio = f"-p{a.priority}" if a.priority else ""
		default = f"{a.today.isoformat()}-waits-{a.postal_code.lower()}-{slug}{prio}.csv"
		path = filedialog.asksaveasfilename(parent=self, title="Export CSV", defaultextension=".csv",
			initialfile=default, filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
		if not path:
			return
		try:
			core.write_csv(path, a)
		except OSError as e:
			messagebox.showerror(APP_TITLE, f"Could not write the file: {e}", parent=self)

	def destroy(self) -> None:
		after = getattr(self, "_poll_after", None)
		if after is not None:
			try:
				self.after_cancel(after)
			except tk.TclError:
				pass
		super().destroy()

	def report_callback_exception(self, exc, val, tb) -> None:
		messagebox.showerror(APP_TITLE, f"{exc.__name__}: {val}", parent=self)


def main() -> None:
	if sys.platform == "win32":
		try:
			import ctypes
			ctypes.windll.shcore.SetProcessDpiAwareness(1)
		except Exception:
			pass
	app = App()
	app.mainloop()


if __name__ == "__main__":
	main()

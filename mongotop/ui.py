"""터미널 렌더링.

화면은 먼저 `Buffer`(문자 + 스타일 격자)에 그린 다음 curses로 출력한다.
그래서 렌더링 로직을 터미널 없이 테스트할 수 있다."""

from __future__ import annotations

import curses
import os
import queue
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import views
from .app import App, Detail, Help, Picker, Prompt
from .collector import Collector, Worker
from .model import Mode
from .util import disp
from .views import Tone

# --- 스타일과 화면 버퍼 --------------------------------------------------------


@dataclass(frozen=True)
class Style:
    fg: Optional[str] = None  # black red green yellow blue magenta cyan white gray darkgray
    bg: Optional[str] = None
    bold: bool = False
    underline: bool = False
    reverse: bool = False

    def over(self, base: "Style") -> "Style":
        """`base` 위에 이 스타일을 덮어쓴다."""
        return Style(
            fg=self.fg or base.fg,
            bg=self.bg or base.bg,
            bold=self.bold or base.bold,
            underline=self.underline or base.underline,
            reverse=self.reverse or base.reverse,
        )


PLAIN = Style()


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(s: str) -> int:
    return sum(char_width(c) for c in s)


class Buffer:
    """w×h 격자. 각 칸은 (문자, 스타일). 전각 문자의 오른쪽 칸은 ""."""

    def __init__(self, w: int, h: int):
        self.w, self.h = max(0, w), max(0, h)
        self.cells: List[List[Tuple[str, Style]]] = [
            [(" ", PLAIN)] * self.w for _ in range(self.h)
        ]

    def fill(self, y: int, x0: int, x1: int, style: Style = PLAIN) -> None:
        if not 0 <= y < self.h:
            return
        row = self.cells[y]
        for x in range(max(0, x0), min(self.w, x1)):
            row[x] = (" ", style)

    def put(self, y: int, x: int, text: str, style: Style = PLAIN, limit: Optional[int] = None) -> int:
        """(y, x)부터 `text`를 쓰고 다음 x를 반환한다. `limit` 열(기본: 화면 끝)에서 자른다."""
        end = self.w if limit is None else min(self.w, limit)
        if not 0 <= y < self.h:
            return x
        row = self.cells[y]
        for ch in text:
            if ch < " " or ch == "\x7f":
                ch = " "
            cw = char_width(ch)
            if cw == 0:
                continue
            if x + cw > end:
                break
            if x >= 0:
                # 전각 문자의 반쪽을 덮어쓰면 나머지 반쪽은 공백으로 만든다.
                if row[x][0] == "" and x > 0:
                    row[x - 1] = (" ", row[x - 1][1])
                row[x] = (ch, style)
                if cw == 2:
                    row[x + 1] = ("", style)
                nxt = x + cw
                if nxt < self.w and row[nxt][0] == "":
                    row[nxt] = (" ", row[nxt][1])
            x += cw
        return x

    def text(self) -> str:
        return "\n".join("".join(c for c, _ in row) for row in self.cells)


# --- 그리기 -----------------------------------------------------------------

TONE_STYLE = {
    Tone.NORMAL: PLAIN,
    Tone.DIM: Style(fg="darkgray"),
    Tone.GOOD: Style(fg="green"),
    Tone.WARN: Style(fg="yellow"),
    Tone.BAD: Style(fg="red", bold=True),
}

HELP = [
    ("", "Modes (upper-case, like innotop)"),
    ("Q", "Query list — running operations from $currentOp"),
    ("T", "Transactions — open multi-document transactions"),
    ("L", "Lock waits — ops waiting for locks, latches or flow control"),
    ("C", "Command summary — metrics.commands totals and rates"),
    ("S", "Status over time — one row of key rates per refresh"),
    ("B", "WiredTiger cache, eviction and tickets"),
    ("R", "Replication — replica set members, lag, oplog window"),
    ("O", "Collection activity — per-namespace load from `top`"),
    ("D", "Database sizes — dbStats for every database"),
    ("A", "All servers — one summary row per server; Enter opens it"),
    ("", ""),
    ("", "Actions"),
    ("↑ ↓ PgUp PgDn", "select row"),
    ("Enter / e", "show the full document of the selected row"),
    ("k / x", "kill the selected operation (T mode: kill session)"),
    ("f or /", "filter rows by substring;  c clears the filter"),
    ("< > ← →", "change sort column;  r reverses the order"),
    ("i", "toggle idle connections / inactive rows"),
    ("s", "set refresh interval;  space refreshes now"),
    ("p", "pause / resume"),
    ("n / N, Tab", "next / previous server"),
    ("w", "pick a server from a list (type to filter)"),
    ("q, Ctrl-C", "quit"),
]

KEYS_HINT = (
    "Q T L C S B R O D A: modes │ ↑↓ select │ ⏎ detail │ k kill │ f filter │ < > r sort │ "
    "i idle │ s interval │ p pause │ n/w server │ ? help │ q quit"
)

EMPTY_MSG = {
    Mode.QUERIES: "no active operations (i = include idle connections)",
    Mode.TRANSACTIONS: "no open transactions",
    Mode.LOCKS: "no operations are waiting for locks",
    Mode.STATUS: "collecting… (a row appears after every refresh)",
}


class UiState:
    """프레임 사이에 유지되는 화면 상태 (테이블 스크롤 위치)."""

    def __init__(self) -> None:
        self.offset = 0


def draw(app: App, buf: Buffer, ui: UiState) -> None:
    w, h = buf.w, buf.h
    if w <= 0 or h <= 0:
        return
    if app.mode is Mode.OVERVIEW:
        header = views.overview_header(app.servers)
    else:
        header = views.header(app.cur())
    # 레이아웃: 제목 1줄, 헤더, 본문(최소 3줄), 상태 1줄. 공간이 모자라면 헤더를 줄인다.
    header_h = max(0, min(len(header), h - 2 - 3))
    body_y = 1 + header_h
    body_h = max(0, h - 1 - body_y)

    _draw_title(app, buf)
    for i, line in enumerate(header[:header_h]):
        buf.put(1 + i, 0, line, Style(fg="cyan", bold=(i == 0)))
    _draw_table(app, buf, ui, body_y, body_h)
    if h >= 2:
        _draw_status(app, buf, h - 1)

    if isinstance(app.input, Help):
        _draw_help(buf)
    elif isinstance(app.input, Detail):
        _draw_detail(buf, app.input)
    elif isinstance(app.input, Picker):
        _draw_picker(app, buf, app.input)


def _draw_title(app: App, buf: Buffer) -> None:
    st = app.cur()
    bar = Style(bg="darkgray")
    buf.fill(0, 0, buf.w, bar)
    spans = [
        (" mongotop ", Style(fg="black", bg="green", bold=True)),
        (" ", PLAIN),
        (f"[{app.mode.key}] {app.mode.title}", Style(bold=True)),
        (
            f"  │ {len(app.servers)} servers  │ every {disp(app.interval)}s"
            if app.mode is Mode.OVERVIEW
            else f"  │ server {app.server + 1}/{len(app.servers)}: {st.name}  │ every {disp(app.interval)}s",
            PLAIN,
        ),
    ]
    if st.updated is not None and app.mode is not Mode.OVERVIEW:
        spans.append((f"  │ {st.updated:%H:%M:%S}", PLAIN))
    if app.paused:
        spans.append(("  PAUSED ", Style(fg="black", bg="yellow")))
    if app.show_idle:
        spans.append(("  +idle", Style(fg="magenta")))
    if app.filter:
        spans.append((f"  filter: {app.filter}", Style(fg="magenta")))
    x = 0
    for text, style in spans:
        x = buf.put(0, x, text, style.over(bar))


def _layout(columns: List[views.Column], width: int) -> List[Tuple[int, int]]:
    """컬럼별 (x, 너비). 너비 0 컬럼은 남는 공간을 나눠 갖는다 (최소 10)."""
    spacing = 1
    fixed = sum(c.width for c in columns) + spacing * max(0, len(columns) - 1)
    flex = [c for c in columns if c.width == 0]
    flex_w = max(10, (width - fixed) // len(flex)) if flex else 0
    out, x = [], 0
    for c in columns:
        cw = c.width or flex_w
        out.append((x, cw))
        x += cw + spacing
    return out


def _draw_table(app: App, buf: Buffer, ui: UiState, y0: int, h: int) -> None:
    if h <= 0:
        return
    st = app.cur()
    t = app.table()
    sort = (app.sort.get(app.mode) or t.default_sort) if t.sortable else None

    # 위쪽 테두리 + 제목
    buf.put(y0, 0, "─" * buf.w)
    if st.error and app.mode is not Mode.OVERVIEW:
        buf.put(y0, 0, f" ERROR: {st.error} ", Style(fg="red", bold=True))
    else:
        buf.put(y0, 0, f" {t.title} ", Style(fg="gray"))
    if h <= 1:
        return

    cols = _layout(t.columns, buf.w)
    hy = y0 + 1
    hstyle = Style(bold=True, underline=True)
    buf.fill(hy, 0, buf.w, hstyle)
    for i, (c, (x, cw)) in enumerate(zip(t.columns, cols)):
        title = c.title
        if sort is not None and sort[0] == i:
            title += "▼" if sort[1] else "▲"
        _cell(buf, hy, x, cw, title, c.right, hstyle)

    visible = h - 2
    rows = t.rows
    if rows and visible > 0:
        sel = min(app.selected, len(rows) - 1)
        if sel < ui.offset:
            ui.offset = sel
        elif sel >= ui.offset + visible:
            ui.offset = sel - visible + 1
        ui.offset = max(0, min(ui.offset, max(0, len(rows) - visible)))
        for i, r in enumerate(rows[ui.offset : ui.offset + visible]):
            y = hy + 1 + i
            style = TONE_STYLE[r.tone]
            if ui.offset + i == sel:
                style = Style(reverse=True).over(style)
            buf.fill(y, 0, buf.w, style)
            for cell, c, (x, cw) in zip(r.cells, t.columns, cols):
                _cell(buf, y, x, cw, cell.text, c.right, style)
    elif not rows:
        ui.offset = 0
        if (not st.error or app.mode is Mode.OVERVIEW) and h > 3:
            msg = EMPTY_MSG.get(app.mode, "no data yet")
            x = max(0, (buf.w - text_width(msg)) // 2)
            buf.put(y0 + 2, x, msg, Style(fg="darkgray"))


def _cell(buf: Buffer, y: int, x: int, cw: int, text: str, right: bool, style: Style) -> None:
    start = x
    if right:
        tw = text_width(text)
        if tw < cw:
            x += cw - tw
    buf.put(y, x, text, style, limit=start + cw)


def _draw_status(app: App, buf: Buffer, y: int) -> None:
    inp = app.input
    if isinstance(inp, Prompt):
        if inp.kind == "interval":
            label = "Refresh interval (seconds): "
        elif inp.kind == "filter":
            label = "Filter (substring, empty = none): "
        else:
            label = f"{inp.label} "
        x = buf.put(y, 0, label, Style(fg="yellow", bold=True))
        x = buf.put(y, x, inp.buf)
        buf.put(y, x, "█", Style(fg="yellow"))
        return
    msg = app.message()
    if msg is not None:
        buf.put(y, 0, msg, Style(fg="yellow"))
    else:
        buf.put(y, 0, KEYS_HINT, Style(fg="darkgray"))


def _centered(buf: Buffer, w_pct: int, h_pct: int) -> Tuple[int, int, int, int]:
    w = buf.w * w_pct // 100
    h = buf.h * h_pct // 100
    return (buf.w - w) // 2, (buf.h - h) // 2, w, h


def _box(buf: Buffer, x: int, y: int, w: int, h: int, title: str) -> None:
    if w < 2 or h < 2:
        return
    for yy in range(y, y + h):
        buf.fill(yy, x, x + w)
    buf.put(y, x, "┌" + "─" * (w - 2) + "┐")
    for yy in range(y + 1, y + h - 1):
        buf.put(yy, x, "│")
        buf.put(yy, x + w - 1, "│")
    buf.put(y + h - 1, x, "└" + "─" * (w - 2) + "┘")
    buf.put(y, x + 1, title, limit=x + w - 1)


def _draw_help(buf: Buffer) -> None:
    x, y, w, h = _centered(buf, 70, 80)
    _box(buf, x, y, w, h, " mongotop help — press any key ")
    for i, (k, v) in enumerate(HELP[: max(0, h - 2)]):
        yy, lim = y + 1 + i, x + w - 1
        if not k:
            buf.put(yy, x + 1, v, Style(fg="green", bold=True), limit=lim)
        else:
            xx = buf.put(yy, x + 1, f"  {k:<16}", Style(fg="yellow", bold=True), limit=lim)
            buf.put(yy, xx, v, limit=lim)


def _server_badge(st) -> Tuple[str, Style]:
    if st.error:
        return "DOWN", TONE_STYLE[Tone.BAD]
    if st.status is None:
        return "....", TONE_STYLE[Tone.DIM]
    return " UP ", TONE_STYLE[Tone.GOOD]


def _draw_picker(app: App, buf: Buffer, p: Picker) -> None:
    x, y, w, h = _centered(buf, 60, 70)
    if w < 10 or h < 4:
        return
    _box(buf, x, y, w, h, " servers — type to filter, ↑↓ select, Enter open, Esc cancel ")
    lim = x + w - 1
    xx = buf.put(y + 1, x + 2, "> ", Style(fg="yellow", bold=True), limit=lim)
    xx = buf.put(y + 1, xx, p.query, limit=lim)
    buf.put(y + 1, xx, "█", Style(fg="yellow"), limit=lim)
    matches = app.picker_matches(p.query)
    visible = h - 4
    if not matches:
        buf.put(y + 3, x + 2, "no matching server", Style(fg="darkgray"), limit=lim)
        return
    pos = matches.index(p.sel) if p.sel in matches else 0
    top = max(0, min(pos - visible // 2, len(matches) - visible))
    for row, i in enumerate(matches[top : top + visible]):
        st = app.servers[i]
        yy = y + 2 + row
        base = Style(reverse=True) if i == matches[pos] else PLAIN
        buf.fill(yy, x + 1, lim, base)
        mark = "*" if i == app.server else " "
        xx = buf.put(yy, x + 2, f"{mark} ", base, limit=lim)
        badge, bstyle = _server_badge(st)
        xx = buf.put(yy, xx, badge, bstyle.over(base), limit=lim)
        role = ""
        if st.status is not None:
            g = views.Gauges.from_status(st.status)
            role = f"{g.role} {g.set}".strip()
        buf.put(yy, xx, f" {st.name:<34} {role}", base, limit=lim)


def _draw_detail(buf: Buffer, d: Detail) -> None:
    x, y, w, h = _centered(buf, 90, 85)
    _box(buf, x, y, w, h, f" {d.title} — ↑↓ PgUp PgDn scroll, Esc close ")
    lines = d.text.split("\n")
    for i, line in enumerate(lines[d.scroll : d.scroll + max(0, h - 2)]):
        buf.put(y + 1 + i, x + 1, line.replace("\t", "    "), limit=x + w - 1)


# --- curses 출력과 이벤트 루프 -------------------------------------------------

_COLOR_NUM = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
    "gray": 7,
}


class _CursesOut:
    def __init__(self, scr) -> None:
        self.scr = scr
        self.pairs = {}
        self.colors = False
        self.default_colors = False
        try:
            curses.start_color()
            self.colors = curses.has_colors()
            try:
                curses.use_default_colors()
                self.default_colors = True
            except curses.error:
                pass
        except curses.error:
            pass

    def _color(self, name: Optional[str], bg: bool) -> Tuple[int, int]:
        """(색 번호, 추가 속성)."""
        if name is None:
            return (-1 if self.default_colors else (0 if bg else 7)), 0
        if name == "darkgray":
            if curses.COLORS >= 16:
                return 8, 0
            return (4, 0) if bg else (7, curses.A_DIM)
        return _COLOR_NUM.get(name, 7), 0

    def attr(self, s: Style) -> int:
        a = 0
        if s.bold:
            a |= curses.A_BOLD
        if s.underline:
            a |= curses.A_UNDERLINE
        if s.reverse:
            a |= curses.A_REVERSE
        if not self.colors or (s.fg is None and s.bg is None):
            return a
        fg, fa = self._color(s.fg, False)
        bg, _ = self._color(s.bg, True)
        key = (fg, bg)
        pair = self.pairs.get(key)
        if pair is None:
            pair = len(self.pairs) + 1
            if pair >= curses.COLOR_PAIRS:
                return a | fa
            try:
                curses.init_pair(pair, fg, bg)
            except curses.error:
                return a | fa
            self.pairs[key] = pair
        return a | fa | curses.color_pair(pair)

    def blit(self, buf: Buffer) -> None:
        self.scr.erase()
        for y, row in enumerate(buf.cells):
            # 맨 오른쪽 아래 칸에 쓰면 curses가 스크롤을 시도하므로 비워 둔다.
            width = buf.w - 1 if y == buf.h - 1 else buf.w
            x = 0
            while x < width:
                style = row[x][1]
                start, chars = x, []
                while x < width and row[x][1] == style:
                    chars.append(row[x][0])
                    x += 1
                try:
                    self.scr.addstr(y, start, "".join(chars), self.attr(style))
                except curses.error:
                    pass
        self.scr.refresh()


_SPECIAL_KEYS = {
    curses.KEY_UP: "UP",
    curses.KEY_DOWN: "DOWN",
    curses.KEY_LEFT: "LEFT",
    curses.KEY_RIGHT: "RIGHT",
    curses.KEY_PPAGE: "PGUP",
    curses.KEY_NPAGE: "PGDN",
    curses.KEY_HOME: "HOME",
    curses.KEY_END: "END",
    curses.KEY_ENTER: "ENTER",
    curses.KEY_BACKSPACE: "BACKSPACE",
    curses.KEY_BTAB: "BTAB",
    curses.KEY_F1: "F1",
}
_CHAR_KEYS = {
    "\n": "ENTER",
    "\r": "ENTER",
    "\x1b": "ESC",
    "\x7f": "BACKSPACE",
    "\x08": "BACKSPACE",
    "\t": "TAB",
    "\x03": "CTRL_C",
}
# windows-curses가 보내는 키패드 Enter
_PADENTER = getattr(curses, "PADENTER", None)


def map_key(ch) -> Optional[str]:
    if isinstance(ch, int):
        if _PADENTER is not None and ch == _PADENTER:
            return "ENTER"
        return _SPECIAL_KEYS.get(ch)
    if ch in _CHAR_KEYS:
        return _CHAR_KEYS[ch]
    return ch if ch.isprintable() else None


def run_tui(collectors: List[Collector], names: List[str], mode: Mode, interval: float, idle: bool) -> None:
    app = App(names, mode, interval, idle)
    out: queue.Queue = queue.Queue()
    worker = Worker(collectors, app.settings(), out)
    worker.start()
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(_loop, app, worker, out)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()


def _loop(scr, app: App, worker: Worker, out: queue.Queue) -> None:
    screen = _CursesOut(scr)
    ui = UiState()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(100)
    last_draw, dirty = 0.0, True
    while True:
        while True:
            try:
                app.on_message(out.get_nowait())
                dirty = True
            except queue.Empty:
                break
        # 알림 메시지가 사라지도록 최소 500ms마다 다시 그린다.
        now = time.monotonic()
        if dirty or now - last_draw >= 0.5:
            h, w = scr.getmaxyx()
            buf = Buffer(w, h)
            draw(app, buf, ui)
            screen.blit(buf)
            last_draw, dirty = now, False
        try:
            ch = scr.get_wch()
        except curses.error:
            continue
        if ch == curses.KEY_RESIZE:
            if sys.platform == "win32":
                try:
                    curses.resize_term(0, 0)
                except curses.error:
                    pass
            dirty = True
            continue
        key = map_key(ch)
        if key is None:
            continue
        dirty = True
        act = app.handle_key(key)
        if act.kind == "quit":
            return
        if act.kind == "settings":
            worker.update_settings(app.settings())
        elif act.kind == "command":
            worker.send(act.command)

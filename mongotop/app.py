"""애플리케이션 상태와 키보드 처리. 터미널 I/O가 없어 단위 테스트하기 쉽다.

키는 문자열로 다룬다: 출력 가능한 문자는 그 문자 자체("q", "S", " "),
특수 키는 "UP" "DOWN" "LEFT" "RIGHT" "PGUP" "PGDN" "HOME" "END" "ENTER"
"ESC" "BACKSPACE" "TAB" "BTAB" "F1" "CTRL_C"."""

from __future__ import annotations

import datetime as _dt
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import views
from .collector import (
    ErrorMsg,
    InfoMsg,
    Kill,
    KillTarget,
    Message,
    Refresh,
    Settings,
    Snapshot,
    SnapshotMsg,
)
from .metrics import Gauges, StatusRow, rates
from .model import DbStat, Mode, Op, ReplStatus, TopRate, TopRaw, top_rates
from .util import compact, disp, pretty

HISTORY = 1000
MESSAGE_SECS = 6.0


class ServerState:
    """모니터링 중인 서버 하나에 대해 알고 있는 모든 것."""

    def __init__(self, name: str):
        self.name = name
        self.status: Optional[dict] = None
        self.prev_status: Optional[dict] = None
        self.ops: List[Op] = []
        self.repl: Optional[ReplStatus] = None
        self.top: List[TopRate] = []
        self.top_ready = False
        self._top_prev: Optional[Tuple[float, Dict[str, TopRaw]]] = None
        self.dbs: List[DbStat] = []
        self.history: Deque[StatusRow] = deque(maxlen=HISTORY)  # 최신 항목이 앞
        self.updated: Optional[_dt.datetime] = None
        self.error: Optional[str] = None

    def apply(self, snap: Snapshot) -> None:
        now = _dt.datetime.now()
        if self.status is not None:
            self.prev_status = self.status
        if self.prev_status is not None:
            r = rates(snap.status, self.prev_status)
            if r is not None:
                self.history.appendleft(
                    StatusRow(now.strftime("%H:%M:%S"), r, Gauges.from_status(snap.status))
                )
        self.status = snap.status
        if snap.ops is not None:
            self.ops = snap.ops
        if snap.repl is not None:
            self.repl = snap.repl
        if snap.dbs is not None:
            self.dbs = snap.dbs
        if snap.top is not None:
            prev = self._top_prev
            self.top_ready = prev is not None
            if prev is not None:
                self.top = top_rates(snap.top, prev[1], snap.taken - prev[0])
            else:
                self.top = top_rates(snap.top, snap.top, 1.0)
            self._top_prev = (snap.taken, snap.top)
        self.updated = now
        self.error = None


# --- 입력 모드 ---------------------------------------------------------------


@dataclass
class Normal:
    pass


@dataclass
class Help:
    pass


@dataclass
class Prompt:
    kind: str  # "interval" | "filter" | "kill"
    buf: str = ""
    target: Optional[KillTarget] = None
    label: str = ""


@dataclass
class Detail:
    title: str
    text: str
    scroll: int = 0


# --- 키 입력 후 메인 루프가 해야 할 일 -------------------------------------------


@dataclass(frozen=True)
class Action:
    kind: str  # "none" | "quit" | "settings" | "command"
    command: Any = None


NONE = Action("none")
QUIT = Action("quit")
SETTINGS = Action("settings")


def command(c: Any) -> Action:
    return Action("command", c)


class App:
    def __init__(self, names: List[str], mode: Mode, interval: float, show_idle: bool):
        self.servers = [ServerState(n) for n in names]
        self.server = 0
        self.mode = mode
        self.interval = float(interval)
        self.paused = False
        self.show_idle = show_idle
        self.filter = ""
        self.sort: Dict[Mode, Tuple[int, bool]] = {}
        self.selected = 0
        self.input: Any = Normal()
        self._message: Optional[Tuple[str, float]] = None

    def settings(self) -> Settings:
        return Settings(self.server, self.mode, self.interval, self.paused, self.show_idle)

    def cur(self) -> ServerState:
        return self.servers[self.server]

    def flash(self, msg: str) -> None:
        self._message = (msg, time.monotonic())

    def message(self) -> Optional[str]:
        """아직 유효한(최근) 메시지가 있으면 반환한다."""
        if self._message and time.monotonic() - self._message[1] < MESSAGE_SECS:
            return self._message[0]
        return None

    def on_message(self, msg: Message) -> None:
        if isinstance(msg, SnapshotMsg):
            if not 0 <= msg.server < len(self.servers):
                return
            snap = msg.snap
            # 이미 벗어난 모드의 늦게 도착한 스냅샷은 작업 목록을 버린다
            # (작업 목록은 모드마다 다르게 필터링된다).
            if msg.server == self.server and snap.mode != self.mode and snap.mode.uses_current_op:
                snap = replace(snap, ops=None)
            self.servers[msg.server].apply(snap)
        elif isinstance(msg, ErrorMsg):
            if 0 <= msg.server < len(self.servers):
                self.servers[msg.server].error = msg.error
        elif isinstance(msg, InfoMsg):
            self.flash(msg.text)

    def table(self) -> views.Table:
        """필터와 정렬을 적용한 현재 모드의 테이블."""
        t = views.build(self.mode, self.cur(), self.show_idle)
        t.filter(self.filter)
        t.sort(self.sort.get(self.mode))
        return t

    def _current_sort(self, t: views.Table) -> Tuple[int, bool]:
        return self.sort.get(self.mode) or t.default_sort or (0, False)

    def _set_mode(self, m: Mode) -> Action:
        if m == self.mode:
            return command(Refresh())
        self.mode = m
        self.selected = 0
        if m.uses_current_op:
            self.servers[self.server].ops = []
        return SETTINGS

    def _move_sel(self, delta: int) -> None:
        length = len(self.table().rows)
        if length == 0:
            self.selected = 0
            return
        cur = min(self.selected, length - 1)
        self.selected = max(0, min(length - 1, cur + delta))

    def _selected_row(self) -> Optional[views.Row]:
        rows = self.table().rows
        if not rows:
            return None
        return rows[min(self.selected, len(rows) - 1)]

    def handle_key(self, key: str) -> Action:
        if key == "CTRL_C":
            return QUIT
        inp, self.input = self.input, Normal()
        if isinstance(inp, Normal):
            return self._normal_key(key)
        if isinstance(inp, Help):
            return NONE  # 아무 키나 누르면 도움말을 닫는다
        if isinstance(inp, Detail):
            lines = len(inp.text.splitlines())
            sc = inp.scroll
            if key in ("ESC", "q", "ENTER"):
                return NONE
            if key in ("DOWN", "j"):
                sc = min(sc + 1, lines)
            elif key in ("UP", "k"):
                sc = max(sc - 1, 0)
            elif key in ("PGDN", " "):
                sc = min(sc + 20, lines)
            elif key == "PGUP":
                sc = max(sc - 20, 0)
            elif key == "HOME":
                sc = 0
            self.input = replace(inp, scroll=sc)
            return NONE
        # Prompt
        if inp.kind == "kill":
            if key in ("y", "Y"):
                return command(Kill(self.server, inp.target))
            self.flash("kill cancelled")
            return NONE
        if key == "ESC":
            return NONE
        if key == "ENTER":
            return self._submit_prompt(inp)
        if key == "BACKSPACE":
            self.input = replace(inp, buf=inp.buf[:-1])
        elif len(key) == 1 and key.isprintable():
            self.input = replace(inp, buf=inp.buf + key)
        else:
            self.input = inp
        return NONE

    def _submit_prompt(self, p: Prompt) -> Action:
        if p.kind == "interval":
            try:
                v = float(p.buf.strip())
            except ValueError:
                v = None
            if v is not None and 0.1 <= v <= 3600.0:
                self.interval = v
                self.flash(f"refresh interval set to {disp(v)}s")
                return SETTINGS
            self.flash(f"invalid interval: {p.buf!r} (0.1 – 3600 seconds)")
            return NONE
        if p.kind == "filter":
            self.filter = p.buf.strip()
            self.selected = 0
        return NONE

    def _normal_key(self, key: str) -> Action:
        if key == "q":
            return QUIT
        if key in ("?", "F1"):
            self.input = Help()
            return NONE
        if len(key) == 1 and key.isascii() and key.isupper() and Mode.from_key(key):
            return self._set_mode(Mode.from_key(key))
        if key == "p":
            self.paused = not self.paused
            self.flash("paused" if self.paused else "resumed")
            return SETTINGS
        if key == "s":
            self.input = Prompt("interval", disp(self.interval))
            return NONE
        if key in ("f", "/"):
            self.input = Prompt("filter", self.filter)
            return NONE
        if key == "c":
            self.filter = ""
            self.flash("filter cleared")
            return NONE
        if key == "i":
            self.show_idle = not self.show_idle
            self.flash(
                "showing idle / inactive entries" if self.show_idle else "hiding idle entries"
            )
            return SETTINGS
        if key == " ":
            return command(Refresh())
        if key in ("n", "TAB"):
            return self._switch_server(1)
        if key in ("N", "BTAB"):
            return self._switch_server(-1)
        if key in ("DOWN", "j"):
            self._move_sel(1)
        elif key == "UP":
            self._move_sel(-1)
        elif key == "PGDN":
            self._move_sel(20)
        elif key == "PGUP":
            self._move_sel(-20)
        elif key == "HOME":
            self.selected = 0
        elif key == "END":
            self._move_sel(1 << 30)
        elif key in (">", "<", "RIGHT", "LEFT"):
            t = self.table()
            if not t.sortable:
                self.flash("this view has a fixed order")
                return NONE
            ncols = len(t.columns)
            c, desc = self._current_sort(t)
            c = (c + 1) % ncols if key in (">", "RIGHT") else (c - 1) % ncols
            self.sort[self.mode] = (c, desc)
            self.flash(f"sort by {t.columns[c].title}")
        elif key == "r":
            c, desc = self._current_sort(self.table())
            self.sort[self.mode] = (c, not desc)
        elif key in ("ENTER", "e"):
            row = self._selected_row()
            if row is not None and row.detail is not None:
                self.input = Detail(views.detail_title(row.detail), pretty(row.detail))
            else:
                self.flash("no detail available for this row")
        elif key in ("k", "x"):
            row = self._selected_row()
            target = row.kill if row is not None else None
            if target is None:
                self.flash("nothing to kill here (select a row in Q, T or L mode)")
            else:
                if target.kind == "op":
                    label = f"Kill operation {compact(target.value)}? (y/N)"
                else:
                    sid = target.value.get("id")
                    label = (
                        f"Kill session {'' if sid is None else compact(sid)} "
                        "and abort its transaction? (y/N)"
                    )
                self.input = Prompt("kill", "", target, label)
        return NONE

    def _switch_server(self, delta: int) -> Action:
        count = len(self.servers)
        if count <= 1:
            self.flash("only one server configured (pass several URIs to monitor more)")
            return NONE
        self.server = (self.server + delta) % count
        self.selected = 0
        self.flash(f"server {self.server + 1}: {self.servers[self.server].name}")
        return SETTINGS

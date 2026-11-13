"""서버 상태를 범용 테이블로 바꾼다. 같은 테이블이 대화형 TUI와
비대화형(batch, TSV) 출력 양쪽에 쓰인다."""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

from .collector import KillTarget
from .metrics import Gauges, interval_secs, pct, rate, rates
from .model import Mode, parse_commands
from .util import disp, fmt_bytes, fmt_num, fmt_pct, fmt_rate, fmt_secs, num, string

if TYPE_CHECKING:
    from .app import ServerState

Sort = Optional[Tuple[int, bool]]  # (컬럼, 내림차순 여부)


@dataclass
class Cell:
    text: str
    key: Union[float, str]  # 숫자는 숫자순, 문자열은 대소문자 무시 알파벳순


def s(text: str) -> Cell:
    """텍스트 셀 (알파벳순 정렬)."""
    return Cell(text, text)


def n(v: float, text: str) -> Cell:
    """`text`로 표시하고 `v`로 정렬하는 숫자 셀."""
    return Cell(text, float(v))


def _sort_key(c: Cell):
    # 숫자가 문자열보다 앞에 온다.
    return (0, c.key) if isinstance(c.key, float) else (1, c.key.lower())


class Tone(Enum):
    NORMAL = "normal"
    DIM = "dim"
    GOOD = "good"
    WARN = "warn"
    BAD = "bad"


@dataclass
class Row:
    cells: List[Cell]
    tone: Tone = Tone.NORMAL
    detail: Optional[dict] = None
    kill: Optional[KillTarget] = None


@dataclass(frozen=True)
class Column:
    title: str
    width: int  # 0이면 "남은 공간을 모두 사용"
    right: bool = False


def col(title: str, width: int) -> Column:
    return Column(title, width, False)


def num_col(title: str, width: int) -> Column:
    return Column(title, width, True)


@dataclass
class Table:
    title: str
    columns: List[Column]
    rows: List[Row] = field(default_factory=list)
    default_sort: Sort = None
    sortable: bool = True  # False면 행이 원래 순서를 유지한다 (예: 시계열)

    def filter(self, flt: str) -> None:
        """어느 셀이든 `flt`를 포함하는 행만 남긴다 (대소문자 무시)."""
        if not flt:
            return
        f = flt.lower()
        self.rows = [r for r in self.rows if any(f in c.text.lower() for c in r.cells)]

    def sort(self, sort: Sort) -> None:
        if not self.sortable:
            return
        sort = sort or self.default_sort
        if sort is None:
            return
        c, desc = sort
        if c < len(self.columns):
            # 안정 정렬: 동률 행은 원래 순서를 유지한다.
            self.rows.sort(key=lambda r: _sort_key(r.cells[c]), reverse=desc)


def header(st: "ServerState") -> List[str]:
    """모든 모드 위에 표시되는 헤더: 서버 정보, 처리량, 자원."""
    status = st.status
    if status is None:
        return [f"{st.name}: waiting for first sample…"]
    g = Gauges.from_status(status)
    role = f"{g.role} {g.set}" if g.set else g.role
    lines = [
        f"{g.host} │ {'mongos' if 'mongos' in g.process else 'mongod'} {g.version} │ {role} │ "
        f"up {fmt_secs(g.uptime)} │ conns {fmt_num(g.conn_current)} "
        f"(active {fmt_num(g.conn_active)}, avail {fmt_num(g.conn_available)}) │ "
        f"mem {fmt_bytes(g.resident_mb * 1024.0 * 1024.0)}"
    ]
    r = rates(status, st.prev_status) if st.prev_status is not None else None
    if r is None:
        lines.append("collecting baseline for rates…")
        return lines
    lines.append(
        f"ops/s {fmt_rate(r.total_ops())} │ ins {fmt_rate(r.insert)} qry {fmt_rate(r.query)} "
        f"upd {fmt_rate(r.update)} del {fmt_rate(r.delete)} gm {fmt_rate(r.getmore)} "
        f"cmd {fmt_rate(r.command)} │ repl {fmt_rate(r.repl_ops)} │ "
        f"net in {fmt_bytes(r.net_in)}/s out {fmt_bytes(r.net_out)}/s │ "
        f"new conns {fmt_rate(r.conns_created)}/s"
    )
    lines.append(
        f"latency r {r.lat_read_ms:.2f}ms w {r.lat_write_ms:.2f}ms c {r.lat_cmd_ms:.2f}ms │ "
        f"queue r/w {disp(g.queue_r)}/{disp(g.queue_w)} "
        f"active r/w {disp(g.active_r)}/{disp(g.active_w)} │ "
        f"cache {fmt_pct(g.cache_used_pct())} dirty {fmt_pct(g.cache_dirty_pct())} │ "
        f"txn open {disp(g.txn_open)} active {disp(g.txn_active)} inactive {disp(g.txn_inactive)}"
    )
    lines.append(
        f"docs/s ret {fmt_rate(r.docs_returned)} ins {fmt_rate(r.docs_inserted)} "
        f"upd {fmt_rate(r.docs_updated)} del {fmt_rate(r.docs_deleted)} │ "
        f"scanned/s keys {fmt_rate(r.keys_scanned)} docs {fmt_rate(r.docs_scanned)} │ "
        f"req/s {fmt_rate(r.requests)} │ "
        f"txn/s commit {fmt_rate(r.txn_committed)} abort {fmt_rate(r.txn_aborted)} │ "
        f"asserts/s {fmt_rate(r.asserts)} │ faults/s {fmt_rate(r.page_faults)}"
    )
    return lines


def build(mode: Mode, st: "ServerState", show_idle: bool) -> Table:
    if mode is Mode.QUERIES:
        return _queries(st, show_idle)
    if mode is Mode.TRANSACTIONS:
        return _transactions(st)
    if mode is Mode.LOCKS:
        return _locks(st)
    if mode is Mode.COMMANDS:
        return _commands(st, show_idle)
    if mode is Mode.STATUS:
        return _status_history(st)
    if mode is Mode.BUFFERS:
        return _buffers(st)
    if mode is Mode.REPLICATION:
        return _replication(st)
    if mode is Mode.TOP:
        return _top(st, show_idle)
    return _databases(st)


def _queries(st: "ServerState", show_all: bool) -> Table:
    hidden = 0 if show_all else sum(1 for o in st.ops if o.is_background())
    rows = []
    for o in st.ops:
        if not show_all and o.is_background():
            continue
        if o.waiting_lock:
            tone = Tone.BAD
        elif o.secs >= 10.0:
            tone = Tone.WARN
        elif not o.active:
            tone = Tone.DIM
        else:
            tone = Tone.NORMAL
        what = o.command or o.msg or o.desc
        rows.append(
            Row(
                [
                    s(o.opid_str),
                    n(o.secs, fmt_secs(o.secs)),
                    s(o.op or o.kind),
                    s(o.ns),
                    s(o.client or o.desc),
                    s(o.app),
                    s(o.user),
                    s(o.plan),
                    n(o.yields, str(o.yields)),
                    s(what),
                ],
                tone=tone,
                detail=o.raw,
                kill=KillTarget.op(o.opid) if o.opid is not None else None,
            )
        )
    title = (
        f"{len(rows)} operations ({hidden} background/idle hidden; i = show)"
        if hidden
        else f"{len(rows)} operations"
    )
    return Table(
        title=title,
        columns=[
            col("Opid", 10),
            num_col("Time", 7),
            col("Op", 9),
            col("Namespace", 24),
            col("Client", 21),
            col("App", 14),
            col("User", 12),
            col("Plan", 12),
            num_col("Yld", 5),
            col("Command", 0),
        ],
        rows=rows,
        default_sort=(1, True),
    )


def _transactions(st: "ServerState") -> Table:
    rows = []
    for o in st.ops:
        t = o.txn
        if t is None:
            continue
        if t.open_secs >= 30.0:
            tone = Tone.BAD
        elif t.open_secs >= 5.0:
            tone = Tone.WARN
        else:
            tone = Tone.NORMAL
        # 세션을 kill하면 idle 상태여도 트랜잭션이 중단된다.
        if o.lsid is not None:
            kill = KillTarget.session(o.lsid)
        elif o.opid is not None:
            kill = KillTarget.op(o.opid)
        else:
            kill = None
        rows.append(
            Row(
                [
                    s(o.opid_str),
                    s(o.lsid_str),
                    n(t.number, str(t.number)),
                    s("ACTIVE" if o.active else "INACTIVE"),
                    n(t.open_secs, fmt_secs(t.open_secs)),
                    n(t.active_secs, fmt_secs(t.active_secs)),
                    n(t.inactive_secs, fmt_secs(t.inactive_secs)),
                    s(t.read_concern),
                    s(o.client),
                    s(o.app),
                    s(o.ns),
                    s(o.command),
                ],
                tone=tone,
                detail=o.raw,
                kill=kill,
            )
        )
    return Table(
        title=f"{len(rows)} open transactions (k = kill session)",
        columns=[
            col("Opid", 10),
            col("Session", 12),
            num_col("TxnNum", 7),
            col("State", 8),
            num_col("Open", 7),
            num_col("Active", 7),
            num_col("Idle", 7),
            col("RC", 9),
            col("Client", 21),
            col("App", 14),
            col("Namespace", 22),
            col("Command", 0),
        ],
        rows=rows,
        default_sort=(4, True),
    )


def _locks(st: "ServerState") -> Table:
    rows = [
        Row(
            [
                s(o.opid_str),
                n(o.secs, fmt_secs(o.secs)),
                s(o.op),
                s(o.ns),
                s(o.waiting_for),
                s(o.locks),
                s(o.client),
                s(o.command),
            ],
            tone=Tone.BAD if o.secs >= 5.0 else Tone.WARN,
            detail=o.raw,
            kill=KillTarget.op(o.opid) if o.opid is not None else None,
        )
        for o in st.ops
        if o.waiting_lock
    ]
    return Table(
        title=f"{len(rows)} operations waiting for locks / latches / flow control",
        columns=[
            col("Opid", 10),
            num_col("Time", 7),
            col("Op", 9),
            col("Namespace", 24),
            col("Waiting", 16),
            col("Locks", 36),
            col("Client", 21),
            col("Command", 0),
        ],
        rows=rows,
        default_sort=(1, True),
    )


def _commands(st: "ServerState", show_all: bool) -> Table:
    rows: List[Row] = []
    title = "command counters (metrics.commands)"
    cur = st.status
    if cur is not None:
        c = parse_commands(cur)
        prev = st.prev_status
        dt = interval_secs(cur, prev) if prev is not None else None
        p = parse_commands(prev) if prev is not None else {}
        entries = []
        for name, (total, failed) in c.items():
            pt, pf = p.get(name, (total, failed))
            r = max(0.0, total - pt) / dt if dt else 0.0
            fr = max(0.0, failed - pf) / dt if dt else 0.0
            entries.append((name, total, failed, r, fr))
        sum_rate = sum(e[3] for e in entries)
        sum_total = sum(e[1] for e in entries)
        title = (
            f"{fmt_rate(sum_rate)} commands/s across {len(entries)} command types"
            f"{'' if show_all else ' (zero-total hidden; i = show all)'}"
        )
        # 이후의 안정 정렬이 동률(예: 0/s인 행이 많을 때)에서 이 순서를 유지한다.
        entries.sort(key=lambda e: (-e[1], e[0]))
        for name, total, failed, r, fr in entries:
            if not show_all and total == 0.0:
                continue
            tone = Tone.BAD if fr > 0 else (Tone.DIM if r == 0 else Tone.NORMAL)
            rows.append(
                Row(
                    [
                        s(name),
                        n(r, fmt_rate(r)),
                        n(pct(r, sum_rate), fmt_pct(pct(r, sum_rate))),
                        n(total, fmt_num(total)),
                        n(pct(total, sum_total), fmt_pct(pct(total, sum_total))),
                        n(failed, fmt_num(failed)),
                        n(fr, fmt_rate(fr)),
                    ],
                    tone=tone,
                )
            )
    return Table(
        title=title,
        columns=[
            col("Command", 32),
            num_col("Rate/s", 9),
            num_col("Rate%", 7),
            num_col("Total", 10),
            num_col("Total%", 7),
            num_col("Failed", 9),
            num_col("Fail/s", 8),
        ],
        rows=rows,
        default_sort=(1, True),
    )


def _status_history(st: "ServerState") -> Table:
    rows = []
    for h in st.history:
        r, g = h.rates, h.gauges
        rows.append(
            Row(
                [
                    s(h.time),
                    n(r.insert, fmt_rate(r.insert)),
                    n(r.query, fmt_rate(r.query)),
                    n(r.update, fmt_rate(r.update)),
                    n(r.delete, fmt_rate(r.delete)),
                    n(r.getmore, fmt_rate(r.getmore)),
                    n(r.command, fmt_rate(r.command)),
                    n(r.repl_ops, fmt_rate(r.repl_ops)),
                    n(g.conn_current, fmt_num(g.conn_current)),
                    n(r.net_in, fmt_bytes(r.net_in)),
                    n(r.net_out, fmt_bytes(r.net_out)),
                    s(f"{disp(g.queue_r)}|{disp(g.queue_w)}"),
                    s(f"{disp(g.active_r)}|{disp(g.active_w)}"),
                    n(r.lat_read_ms, f"{r.lat_read_ms:.2f}"),
                    n(r.lat_write_ms, f"{r.lat_write_ms:.2f}"),
                    n(g.cache_used_pct(), fmt_pct(g.cache_used_pct())),
                    n(g.cache_dirty_pct(), fmt_pct(g.cache_dirty_pct())),
                    n(r.docs_scanned, fmt_rate(r.docs_scanned)),
                ],
                tone=Tone.WARN if g.queue_r + g.queue_w > 0 else Tone.NORMAL,
            )
        )
    return Table(
        title="one row per refresh, newest first; rates are per second",
        columns=[
            col("Time", 8),
            num_col("ins", 7),
            num_col("qry", 7),
            num_col("upd", 7),
            num_col("del", 7),
            num_col("gm", 6),
            num_col("cmd", 7),
            num_col("repl", 7),
            num_col("conn", 6),
            num_col("netIn", 7),
            num_col("netOut", 7),
            num_col("qr|qw", 7),
            num_col("ar|aw", 7),
            num_col("rLat", 6),
            num_col("wLat", 6),
            num_col("used", 6),
            num_col("dirty", 6),
            num_col("scanObj", 8),
        ],
        rows=rows,
        sortable=False,
    )


_WT_COUNTERS = (
    ("bytes read into cache", "bytes read into cache", True),
    ("bytes written from cache", "bytes written from cache", True),
    ("pages read into cache", "pages read into cache", False),
    ("pages written from cache", "pages written from cache", False),
    ("pages requested from cache", "pages requested from the cache", False),
    ("unmodified pages evicted", "unmodified pages evicted", False),
    ("modified pages evicted", "modified pages evicted", False),
    ("app threads evicting pages", "pages evicted by application threads", False),
    ("eviction worker pages", "eviction worker thread evicting pages", False),
)


def _buffers(st: "ServerState") -> Table:
    rows: List[Row] = []
    cur = st.status
    if cur is not None:
        g = Gauges.from_status(cur)
        prev = st.prev_status
        dt = interval_secs(cur, prev) if prev is not None else None

        def wt(k: str) -> float:
            return num(cur, ["wiredTiger", "cache", k])

        def wt_rate(k: str) -> float:
            if prev is None or dt is None:
                return 0.0
            return rate(cur, prev, ["wiredTiger", "cache", k], dt)

        def gauge(name: str, v: str, tone: Tone = Tone.NORMAL) -> None:
            rows.append(Row([s(name), s(v), s("")], tone=tone))

        used, dirty = g.cache_used_pct(), g.cache_dirty_pct()
        gauge("storage engine", g.engine)
        gauge("cache size (max)", fmt_bytes(g.cache_max))
        gauge(
            "cache used",
            f"{fmt_bytes(g.cache_used)} ({fmt_pct(used)})",
            Tone.BAD if used > 95 else Tone.WARN if used > 80 else Tone.NORMAL,
        )
        gauge(
            "cache dirty",
            f"{fmt_bytes(g.cache_dirty)} ({fmt_pct(dirty)})",
            Tone.BAD if dirty > 20 else Tone.WARN if dirty > 5 else Tone.NORMAL,
        )
        gauge("pages in cache", fmt_num(wt("pages currently held in the cache")))
        for label, out, total in (
            ("read tickets", g.tickets_r_out, g.tickets_r_total),
            ("write tickets", g.tickets_w_out, g.tickets_w_total),
        ):
            gauge(
                label,
                f"{disp(out)} / {disp(total)} in use",
                Tone.BAD if total > 0 and out >= total else Tone.NORMAL,
            )
        txn = ["wiredTiger", "transaction"]
        gauge(
            "checkpoint running",
            "yes" if num(cur, txn + ["transaction checkpoint currently running"]) > 0 else "no",
        )
        gauge(
            "last checkpoint time",
            f"{disp(num(cur, txn + ['transaction checkpoint most recent time (msecs)']))} ms",
        )
        for label, key, is_bytes in _WT_COUNTERS:
            total = wt(key)
            r = wt_rate(key)
            fmt = fmt_bytes if is_bytes else fmt_num
            rate_text = f"{fmt_bytes(r)}/s" if is_bytes else f"{fmt_rate(r)}/s"
            # 애플리케이션 스레드가 eviction을 함 = 캐시 압박
            tone = Tone.WARN if "application threads" in key and r > 0 else Tone.NORMAL
            rows.append(Row([s(label), s(fmt(total)), s(rate_text)], tone=tone))
    return Table(
        title="WiredTiger cache, eviction and concurrency tickets",
        columns=[col("Metric", 30), num_col("Value", 24), num_col("Rate", 14)],
        rows=rows,
        sortable=False,
    )


def _replication(st: "ServerState") -> Table:
    title = "replica set"
    rows: List[Row] = []
    r = st.repl
    if r is not None:
        if r.error:
            title = f"not available: {r.error}"
        else:
            window = fmt_secs(r.oplog_window_secs) if r.oplog_window_secs is not None else "n/a"
            title = f"replica set {r.set} │ {len(r.members)} members │ oplog window {window}"
            for m in r.members:
                lag = m.lag_secs
                if m.health < 1.0:
                    tone = Tone.BAD
                elif lag is not None and lag >= 10.0:
                    tone = Tone.WARN
                elif m.state_num == 1:
                    tone = Tone.GOOD
                else:
                    tone = Tone.NORMAL
                optime = (
                    _dt.datetime.fromtimestamp(m.optime_ms / 1000).strftime("%m-%d %H:%M:%S")
                    if m.optime_ms is not None
                    else ""
                )
                rows.append(
                    Row(
                        [
                            s(f"{m.name} *" if m.is_self else m.name),
                            s(m.state),
                            n(m.health, "up" if m.health >= 1.0 else "DOWN"),
                            n(m.uptime, fmt_secs(m.uptime)),
                            s(optime),
                            n(-1.0 if lag is None else lag, "" if lag is None else fmt_secs(lag)),
                            n(
                                -1.0 if m.ping_ms is None else m.ping_ms,
                                "" if m.ping_ms is None else f"{disp(m.ping_ms)}ms",
                            ),
                            s(m.sync_source),
                            s(m.message),
                        ],
                        tone=tone,
                        detail=m.raw,
                    )
                )
    return Table(
        title=title,
        columns=[
            col("Member (* = this)", 26),
            col("State", 11),
            col("Health", 6),
            num_col("Uptime", 8),
            col("Optime", 15),
            num_col("Lag", 7),
            num_col("Ping", 6),
            col("Sync source", 22),
            col("Message", 0),
        ],
        rows=rows,
    )


def _top(st: "ServerState", show_all: bool) -> Table:
    rows = [
        Row(
            [
                s(t.ns),
                n(t.time_pct, fmt_pct(t.time_pct)),
                n(t.read_pct, fmt_pct(t.read_pct)),
                n(t.write_pct, fmt_pct(t.write_pct)),
                n(t.ops, fmt_rate(t.ops)),
                n(t.reads, fmt_rate(t.reads)),
                n(t.writes, fmt_rate(t.writes)),
                n(t.queries, fmt_rate(t.queries)),
                n(t.getmore, fmt_rate(t.getmore)),
                n(t.insert, fmt_rate(t.insert)),
                n(t.update, fmt_rate(t.update)),
                n(t.remove, fmt_rate(t.remove)),
                n(t.commands, fmt_rate(t.commands)),
            ],
            tone=Tone.WARN if t.time_pct >= 50.0 else Tone.NORMAL,
        )
        for t in st.top
        if show_all or t.ops > 0
    ]
    if st.top_ready:
        title = "per-collection activity; Time% = % of wall-clock spent" + (
            "" if show_all else " (idle hidden; i = show all)"
        )
    else:
        title = "collecting baseline… (rates appear after the second sample)"
    return Table(
        title=title,
        columns=[
            col("Namespace", 0),
            num_col("Time%", 7),
            num_col("R-Time%", 8),
            num_col("W-Time%", 8),
            num_col("Ops/s", 8),
            num_col("Reads/s", 8),
            num_col("Writes/s", 9),
            num_col("Query/s", 8),
            num_col("GetM/s", 7),
            num_col("Ins/s", 7),
            num_col("Upd/s", 7),
            num_col("Del/s", 7),
            num_col("Cmd/s", 7),
        ],
        rows=rows,
        default_sort=(1, True),
    )


def _databases(st: "ServerState") -> Table:
    total = sum(d.storage_size + d.index_size for d in st.dbs)
    rows = [
        Row(
            [
                s(d.name),
                n(d.collections, fmt_num(d.collections)),
                n(d.views, fmt_num(d.views)),
                n(d.objects, fmt_num(d.objects)),
                n(d.avg_obj, fmt_bytes(d.avg_obj)),
                n(d.data_size, fmt_bytes(d.data_size)),
                n(d.storage_size, fmt_bytes(d.storage_size)),
                n(d.indexes, fmt_num(d.indexes)),
                n(d.index_size, fmt_bytes(d.index_size)),
                n(
                    d.storage_size + d.index_size,
                    fmt_pct(pct(d.storage_size + d.index_size, total)),
                ),
            ],
            detail=d.raw,
        )
        for d in st.dbs
    ]
    return Table(
        title=f"{len(st.dbs)} databases, {fmt_bytes(total)} on disk (data + indexes)",
        columns=[
            col("Database", 0),
            num_col("Colls", 6),
            num_col("Views", 6),
            num_col("Objects", 9),
            num_col("AvgObj", 8),
            num_col("Data", 9),
            num_col("Storage", 9),
            num_col("Idx", 5),
            num_col("IdxSize", 9),
            num_col("Disk%", 7),
        ],
        rows=rows,
        default_sort=(6, True),
    )


def to_tsv(t: Table, with_header: bool) -> str:
    """batch 모드용 탭 구분 출력."""
    out = []
    if with_header:
        out.append("\t".join(c.title for c in t.columns) + "\n")
    for r in t.rows:
        out.append(
            "\t".join(c.text.replace("\t", " ").replace("\n", " ") for c in r.cells) + "\n"
        )
    return "".join(out)


def detail_title(d: dict) -> str:
    """상세 팝업 제목에 쓸 대표 키와 값."""
    for k in ("opid", "name", "db"):
        v = string(d, [k])
        if v:
            return f"{k}: {v}"
    return "detail"

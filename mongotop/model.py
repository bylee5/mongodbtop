"""데이터 모델: 화면 모드, 그리고 `$currentOp`, `replSetGetStatus`, `top`,
`dbStats` 등이 반환한 원본 문서를 타입이 있는 구조로 다룬다."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .util import boolean, compact, dt_ms, get, num, opt_num, string, truncate


class Mode(Enum):
    """화면 모드. innotop과 똑같이 대문자 키로 선택한다."""

    QUERIES = "Q"  # 실행 중인 작업 ($currentOp) — innotop의 "Query List"
    TRANSACTIONS = "T"  # 열린 다중 문서 트랜잭션 — "InnoDB Txns"
    LOCKS = "L"  # 락 / 래치를 기다리는 작업 — "Locks"
    COMMANDS = "C"  # 커맨드 카운터와 초당 처리량 — "Command Summary"
    STATUS = "S"  # 갱신마다 주요 지표를 한 줄씩 — "Variables & Status"
    BUFFERS = "B"  # WiredTiger 캐시 / 티켓 — "InnoDB Buffers"
    REPLICATION = "R"  # 레플리카셋 멤버와 지연 — "Replication Status"
    TOP = "O"  # `top` 기반 컬렉션별 활동 — "Open Tables"
    DATABASES = "D"  # `dbStats` 기반 데이터베이스 크기
    OVERVIEW = "A"  # 모든 서버를 한 줄씩 요약

    @property
    def key(self) -> str:
        return self.value

    @property
    def title(self) -> str:
        return _TITLES[self]

    @property
    def uses_current_op(self) -> bool:
        """`$currentOp`을 데이터 소스로 쓰는 모드."""
        return self in (Mode.QUERIES, Mode.TRANSACTIONS, Mode.LOCKS)

    @classmethod
    def from_key(cls, c: str) -> Optional["Mode"]:
        if len(c) != 1:
            return None
        c = c.upper()
        for m in cls:
            if m.value == c:
                return m
        return None


_TITLES = {
    Mode.QUERIES: "Query List",
    Mode.TRANSACTIONS: "Transactions",
    Mode.LOCKS: "Lock Waits",
    Mode.COMMANDS: "Command Summary",
    Mode.STATUS: "Status Over Time",
    Mode.BUFFERS: "WiredTiger Cache & Tickets",
    Mode.REPLICATION: "Replication Status",
    Mode.TOP: "Collection Activity (top)",
    Mode.DATABASES: "Database Sizes",
    Mode.OVERVIEW: "All Servers",
}


@dataclass
class Txn:
    number: int = 0
    open_secs: float = 0.0
    active_secs: float = 0.0
    inactive_secs: float = 0.0
    read_concern: str = ""


# 작업의 커맨드를 보여줄 때 잡음만 되는 필드들.
NOISY_CMD_FIELDS = (
    "lsid",
    "$clusterTime",
    "$db",
    "$readPreference",
    "$audit",
    "$client",
    "txnNumber",
    "autocommit",
)


def compact_command(cmd: Mapping) -> str:
    c = {k: v for k, v in cmd.items() if k not in NOISY_CMD_FIELDS}
    return truncate(compact(c), 1000)


@dataclass
class Op:
    """`$currentOp`의 항목 하나."""

    opid: Any = None
    opid_str: str = ""
    kind: str = ""
    active: bool = False
    secs: float = 0.0
    op: str = ""
    ns: str = ""
    client: str = ""
    app: str = ""
    user: str = ""
    desc: str = ""
    plan: str = ""
    waiting_lock: bool = False
    waiting_for: str = ""
    locks: str = ""
    yields: int = 0
    command: str = ""
    msg: str = ""
    lsid: Optional[dict] = None
    lsid_str: str = ""
    txn: Optional[Txn] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_doc(cls, d: Mapping) -> "Op":
        opid = d.get("opid")
        us = opt_num(d, ["microsecs_running"])
        if us is not None:
            secs = us / 1e6
        else:
            s = opt_num(d, ["secs_running"])
            secs = 0.0 if s is None else s

        client = string(d, ["client"]) or string(d, ["client_s"])

        users = get(d, ["effectiveUsers"])
        user = ""
        if isinstance(users, list):
            user = ",".join(
                f"{string(u, ['user'])}@{string(u, ['db'])}"
                for u in users
                if isinstance(u, Mapping)
            )

        command = ""
        cmd = get(d, ["command"])
        if isinstance(cmd, Mapping):
            orig = get(d, ["cursor", "originatingCommand"])
            # getMore: 원래 커맨드를 보여주는 편이 훨씬 유용하다.
            if "getMore" in cmd and isinstance(orig, Mapping):
                command = f"getMore ← {compact_command(orig)}"
            else:
                command = compact_command(cmd)

        lsid_v = get(d, ["lsid"])
        lsid = dict(lsid_v) if isinstance(lsid_v, Mapping) else None
        lsid_str = truncate(compact(lsid["id"]), 12) if lsid and "id" in lsid else ""

        txn = None
        t = get(d, ["transaction"])
        # retryable write에도 `transaction` 항목이 있지만 `autocommit: false`가
        # 없다. 이들은 다중 문서 트랜잭션이 아니다.
        if isinstance(t, Mapping) and get(t, ["parameters", "autocommit"]) is False:
            txn = Txn(
                number=int(num(t, ["parameters", "txnNumber"])),
                open_secs=num(t, ["timeOpenMicros"]) / 1e6,
                active_secs=num(t, ["timeActiveMicros"]) / 1e6,
                inactive_secs=num(t, ["timeInactiveMicros"]) / 1e6,
                read_concern=string(t, ["parameters", "readConcern", "level"]),
            )

        latch = get(d, ["waitingForLatch"])
        if boolean(d, ["waitingForLock"]):
            waiting_for = "lock"
        elif isinstance(latch, Mapping):
            waiting_for = f"latch:{string(latch, ['captureName'])}"
        elif boolean(d, ["waitingForFlowControl"]):
            waiting_for = "flowControl"
        else:
            waiting_for = ""

        locks = get(d, ["locks"])
        return cls(
            opid=opid,
            opid_str="" if opid is None else compact(opid),
            kind=string(d, ["type"]),
            active=boolean(d, ["active"]),
            secs=secs,
            op=string(d, ["op"]),
            ns=string(d, ["ns"]),
            client=client,
            app=string(d, ["appName"]),
            user=user,
            desc=string(d, ["desc"]),
            plan=string(d, ["planSummary"]),
            waiting_lock=bool(waiting_for),
            waiting_for=waiting_for,
            locks=compact(locks) if isinstance(locks, Mapping) and locks else "",
            yields=int(num(d, ["numYields"])),
            command=command,
            msg=string(d, ["msg"]),
            lsid=lsid,
            lsid_str=lsid_str,
            txn=txn,
            raw=dict(d),
        )

    def is_background(self) -> bool:
        """실행 중으로 보이지만 실제 작업은 아닌 롱폴링/내부 항목.
        예: 드라이버의 대기형 `hello`, Checkpointer 같은 서버 스레드.
        innotop이 sleep 중인 스레드를 숨기듯 기본으로 숨긴다."""
        cmd = self.raw.get("command")
        awaitable_hello = isinstance(cmd, Mapping) and "maxAwaitTimeMS" in cmd and any(
            k in cmd for k in ("hello", "isMaster", "ismaster")
        )
        internal_thread = not self.client and self.op in ("", "none")
        tailing_oplog = self.ns == "local.oplog.rs" and self.op == "getmore"
        return awaitable_hello or internal_thread or tailing_oplog


@dataclass
class ReplMember:
    name: str = ""
    state: str = ""
    state_num: int = 0
    health: float = 0.0
    uptime: float = 0.0
    optime_ms: Optional[int] = None
    lag_secs: Optional[float] = None
    ping_ms: Optional[float] = None
    sync_source: str = ""
    is_self: bool = False
    message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ReplStatus:
    set: str = ""
    members: List[ReplMember] = field(default_factory=list)
    oplog_window_secs: Optional[float] = None
    # 서버가 레플리카셋 멤버가 아닐 때(또는 커맨드가 실패했을 때) 설정된다.
    error: Optional[str] = None

    @classmethod
    def from_doc(cls, d: Mapping) -> "ReplStatus":
        members: List[ReplMember] = []
        ms = get(d, ["members"])
        for m in ms if isinstance(ms, list) else []:
            if not isinstance(m, Mapping):
                continue
            od = get(m, ["optimeDate"])
            members.append(
                ReplMember(
                    name=string(m, ["name"]),
                    state=string(m, ["stateStr"]),
                    state_num=int(num(m, ["state"])),
                    health=num(m, ["health"]),
                    uptime=num(m, ["uptime"]),
                    optime_ms=dt_ms(od) if isinstance(od, _dt.datetime) else None,
                    ping_ms=opt_num(m, ["pingMs"]),
                    sync_source=string(m, ["syncSourceHost"]) or string(m, ["syncingTo"]),
                    is_self=boolean(m, ["self"]),
                    message=string(m, ["lastHeartbeatMessage"]) or string(m, ["infoMessage"]),
                    raw=dict(m),
                )
            )
        # 지연은 프라이머리의 optime을 기준으로 재고, 프라이머리가 없으면
        # (예: 선출 중) 가장 최신 optime을 기준으로 잰다.
        reference = next(
            (m.optime_ms for m in members if m.state_num == 1 and m.optime_ms is not None),
            None,
        )
        if reference is None:
            reference = max(
                (m.optime_ms for m in members if m.optime_ms is not None), default=None
            )
        for m in members:
            # 아비터는 optime이 없으므로 건너뛴다.
            if reference is not None and m.optime_ms is not None:
                m.lag_secs = max(0.0, (reference - m.optime_ms) / 1000.0)
        return cls(set=string(d, ["set"]), members=members)


@dataclass
class TopRaw:
    """`top` 커맨드에서 얻은 네임스페이스 하나의 누적 카운터."""

    total_time: float = 0.0
    total_count: float = 0.0
    read_time: float = 0.0
    read_count: float = 0.0
    write_time: float = 0.0
    write_count: float = 0.0
    queries: float = 0.0
    getmore: float = 0.0
    insert: float = 0.0
    update: float = 0.0
    remove: float = 0.0
    commands: float = 0.0


def parse_top(d: Mapping) -> Dict[str, TopRaw]:
    out: Dict[str, TopRaw] = {}
    totals = d.get("totals")
    if not isinstance(totals, Mapping):
        return out
    for ns, e in totals.items():
        if not isinstance(e, Mapping):
            continue  # 예: "note" 문자열 항목
        out[ns] = TopRaw(
            total_time=num(e, ["total", "time"]),
            total_count=num(e, ["total", "count"]),
            read_time=num(e, ["readLock", "time"]),
            read_count=num(e, ["readLock", "count"]),
            write_time=num(e, ["writeLock", "time"]),
            write_count=num(e, ["writeLock", "count"]),
            queries=num(e, ["queries", "count"]),
            getmore=num(e, ["getmore", "count"]),
            insert=num(e, ["insert", "count"]),
            update=num(e, ["update", "count"]),
            remove=num(e, ["remove", "count"]),
            commands=num(e, ["commands", "count"]),
        )
    return out


@dataclass
class TopRate:
    """두 `top` 샘플 사이의 초당 변화율. 시간 값은 µs 단위이므로
    `*_pct`는 "1초 중 소비된 비율(%)", 즉 부하 지표가 된다."""

    ns: str = ""
    time_pct: float = 0.0
    read_pct: float = 0.0
    write_pct: float = 0.0
    ops: float = 0.0
    reads: float = 0.0
    writes: float = 0.0
    queries: float = 0.0
    getmore: float = 0.0
    insert: float = 0.0
    update: float = 0.0
    remove: float = 0.0
    commands: float = 0.0


def top_rates(cur: Dict[str, TopRaw], prev: Dict[str, TopRaw], dt: float) -> List[TopRate]:
    dt = max(dt, 1e-3)

    def r(c: float, p: float) -> float:
        return max(0.0, c - p) / dt

    out = []
    for ns, c in cur.items():
        p = prev.get(ns) or TopRaw()
        out.append(
            TopRate(
                ns=ns,
                time_pct=r(c.total_time, p.total_time) / 1e4,
                read_pct=r(c.read_time, p.read_time) / 1e4,
                write_pct=r(c.write_time, p.write_time) / 1e4,
                ops=r(c.total_count, p.total_count),
                reads=r(c.read_count, p.read_count),
                writes=r(c.write_count, p.write_count),
                queries=r(c.queries, p.queries),
                getmore=r(c.getmore, p.getmore),
                insert=r(c.insert, p.insert),
                update=r(c.update, p.update),
                remove=r(c.remove, p.remove),
                commands=r(c.commands, p.commands),
            )
        )
    out.sort(key=lambda t: (-t.time_pct, t.ns))
    return out


@dataclass
class DbStat:
    name: str = ""
    collections: float = 0.0
    views: float = 0.0
    objects: float = 0.0
    avg_obj: float = 0.0
    data_size: float = 0.0
    storage_size: float = 0.0
    indexes: float = 0.0
    index_size: float = 0.0
    raw: Optional[dict] = None

    @classmethod
    def from_doc(cls, name: str, d: Mapping) -> "DbStat":
        return cls(
            name=name,
            collections=num(d, ["collections"]),
            views=num(d, ["views"]),
            objects=num(d, ["objects"]),
            avg_obj=num(d, ["avgObjSize"]),
            data_size=num(d, ["dataSize"]),
            storage_size=num(d, ["storageSize"]),
            indexes=num(d, ["indexes"]),
            index_size=num(d, ["indexSize"]),
            raw=dict(d),
        )


def parse_commands(status: Mapping) -> Dict[str, Tuple[float, float]]:
    """`serverStatus.metrics.commands`의 커맨드 카운터: 이름 → (total, failed)."""
    out: Dict[str, Tuple[float, float]] = {}
    cmds = get(status, ["metrics", "commands"])
    if isinstance(cmds, Mapping):
        for name, c in cmds.items():
            if isinstance(c, Mapping) and "total" in c:
                out[name] = (num(c, ["total"]), num(c, ["failed"]))
    return out

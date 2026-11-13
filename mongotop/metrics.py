"""serverStatus에서 파생된 지표: 특정 시점의 게이지 값과
연속된 두 샘플로 계산한 초당 변화율."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

from .util import boolean, num, opt_num, string


@dataclass
class Gauges:
    """serverStatus 문서 하나에서 얻는 특정 시점의 값."""

    host: str = ""
    version: str = ""
    process: str = ""
    role: str = ""
    set: str = ""
    uptime: float = 0.0
    conn_current: float = 0.0
    conn_available: float = 0.0
    conn_active: float = 0.0
    queue_r: float = 0.0
    queue_w: float = 0.0
    active_r: float = 0.0
    active_w: float = 0.0
    cache_used: float = 0.0
    cache_max: float = 0.0
    cache_dirty: float = 0.0
    tickets_r_out: float = 0.0
    tickets_r_total: float = 0.0
    tickets_w_out: float = 0.0
    tickets_w_total: float = 0.0
    txn_active: float = 0.0
    txn_inactive: float = 0.0
    txn_open: float = 0.0
    resident_mb: float = 0.0
    engine: str = ""

    @classmethod
    def from_status(cls, s: Mapping) -> "Gauges":
        process = string(s, ["process"])
        if "mongos" in process:
            role = "mongos"
        elif "repl" not in s:
            role = "standalone"
        elif boolean(s, ["repl", "isWritablePrimary"]) or boolean(s, ["repl", "ismaster"]):
            role = "PRIMARY"
        elif boolean(s, ["repl", "secondary"]):
            role = "SECONDARY"
        elif boolean(s, ["repl", "arbiterOnly"]):
            role = "ARBITER"
        else:
            role = "OTHER"

        def cache(k: str) -> float:
            return num(s, ["wiredTiger", "cache", k])

        # MongoDB 7.0+ 에서는 티켓 정보가 queues.execution.* 으로 옮겨졌다.
        def ticket(rw: str, f: str) -> float:
            v = opt_num(s, ["queues", "execution", rw, f])
            if v is None:
                v = opt_num(s, ["wiredTiger", "concurrentTransactions", rw, f])
            return 0.0 if v is None else v

        return cls(
            host=string(s, ["host"]),
            version=string(s, ["version"]),
            process=process,
            role=role,
            set=string(s, ["repl", "setName"]),
            uptime=num(s, ["uptime"]),
            conn_current=num(s, ["connections", "current"]),
            conn_available=num(s, ["connections", "available"]),
            conn_active=num(s, ["connections", "active"]),
            queue_r=num(s, ["globalLock", "currentQueue", "readers"]),
            queue_w=num(s, ["globalLock", "currentQueue", "writers"]),
            active_r=num(s, ["globalLock", "activeClients", "readers"]),
            active_w=num(s, ["globalLock", "activeClients", "writers"]),
            cache_used=cache("bytes currently in the cache"),
            cache_max=cache("maximum bytes configured"),
            cache_dirty=cache("tracked dirty bytes in the cache"),
            tickets_r_out=ticket("read", "out"),
            tickets_r_total=ticket("read", "totalTickets"),
            tickets_w_out=ticket("write", "out"),
            tickets_w_total=ticket("write", "totalTickets"),
            txn_active=num(s, ["transactions", "currentActive"]),
            txn_inactive=num(s, ["transactions", "currentInactive"]),
            txn_open=num(s, ["transactions", "currentOpen"]),
            resident_mb=num(s, ["mem", "resident"]),
            engine=string(s, ["storageEngine", "name"]),
        )

    def cache_used_pct(self) -> float:
        return pct(self.cache_used, self.cache_max)

    def cache_dirty_pct(self) -> float:
        return pct(self.cache_dirty, self.cache_max)


def pct(part: float, whole: float) -> float:
    return part / whole * 100.0 if whole > 0 else 0.0


def interval_secs(cur: Mapping, prev: Mapping) -> Optional[float]:
    """두 샘플 사이의 경과 시간(초). 네트워크 지연이 변화율을 왜곡하지 않도록
    서버 자체 시계(uptimeMillis)를 사용한다. 그 사이에 서버가 재시작됐으면 None."""
    c = opt_num(cur, ["uptimeMillis"])
    p = opt_num(prev, ["uptimeMillis"])
    if c is None or p is None:
        return None
    dt = (c - p) / 1000.0
    return dt if dt > 0 else None


def rate(cur: Mapping, prev: Mapping, path: Sequence[str], dt: float) -> float:
    """누적 카운터의 초당 변화율 (음수가 되지 않음)."""
    return max(0.0, num(cur, path) - num(prev, path)) / dt


def _latency_ms(cur: Mapping, prev: Mapping, bucket: str) -> float:
    """opLatencies 버킷의 구간 평균 지연(ms)."""
    ops = num(cur, ["opLatencies", bucket, "ops"]) - num(prev, ["opLatencies", bucket, "ops"])
    lat = num(cur, ["opLatencies", bucket, "latency"]) - num(
        prev, ["opLatencies", bucket, "latency"]
    )
    return lat / ops / 1000.0 if ops > 0 else 0.0


@dataclass
class Rates:
    dt: float = 0.0  # 두 샘플 사이의 시간(초)
    insert: float = 0.0
    query: float = 0.0
    update: float = 0.0
    delete: float = 0.0
    getmore: float = 0.0
    command: float = 0.0
    repl_ops: float = 0.0
    net_in: float = 0.0
    net_out: float = 0.0
    requests: float = 0.0
    conns_created: float = 0.0
    docs_returned: float = 0.0
    docs_inserted: float = 0.0
    docs_updated: float = 0.0
    docs_deleted: float = 0.0
    keys_scanned: float = 0.0
    docs_scanned: float = 0.0
    lat_read_ms: float = 0.0
    lat_write_ms: float = 0.0
    lat_cmd_ms: float = 0.0
    page_faults: float = 0.0
    asserts: float = 0.0
    txn_committed: float = 0.0
    txn_aborted: float = 0.0

    def total_ops(self) -> float:
        return self.insert + self.query + self.update + self.delete + self.getmore + self.command


def rates(cur: Mapping, prev: Mapping) -> Optional[Rates]:
    dt = interval_secs(cur, prev)
    if dt is None:
        return None

    def r(*path: str) -> float:
        return rate(cur, prev, path, dt)

    return Rates(
        dt=dt,
        insert=r("opcounters", "insert"),
        query=r("opcounters", "query"),
        update=r("opcounters", "update"),
        delete=r("opcounters", "delete"),
        getmore=r("opcounters", "getmore"),
        command=r("opcounters", "command"),
        repl_ops=sum(
            r("opcountersRepl", k)
            for k in ("insert", "query", "update", "delete", "getmore", "command")
        ),
        net_in=r("network", "bytesIn"),
        net_out=r("network", "bytesOut"),
        requests=r("network", "numRequests"),
        conns_created=r("connections", "totalCreated"),
        docs_returned=r("metrics", "document", "returned"),
        docs_inserted=r("metrics", "document", "inserted"),
        docs_updated=r("metrics", "document", "updated"),
        docs_deleted=r("metrics", "document", "deleted"),
        keys_scanned=r("metrics", "queryExecutor", "scanned"),
        docs_scanned=r("metrics", "queryExecutor", "scannedObjects"),
        lat_read_ms=_latency_ms(cur, prev, "reads"),
        lat_write_ms=_latency_ms(cur, prev, "writes"),
        lat_cmd_ms=_latency_ms(cur, prev, "commands"),
        page_faults=r("extra_info", "page_faults"),
        asserts=sum(
            r("asserts", k) for k in ("regular", "warning", "msg", "user", "rollovers")
        ),
        txn_committed=r("transactions", "totalCommitted"),
        txn_aborted=r("transactions", "totalAborted"),
    )


@dataclass
class StatusRow:
    """S(시간별 상태) 화면의 한 줄."""

    time: str
    rates: Rates
    gauges: Gauges

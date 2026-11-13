"""util / model / metrics 단위 테스트 (Rust 버전의 테스트를 옮긴 것)."""

import datetime as dt

from bson import Int64
from bson.binary import Binary

from mongotop.metrics import Gauges, rates
from mongotop.model import Mode, Op, ReplStatus, parse_commands, parse_top, top_rates
from mongotop.util import (
    boolean,
    compact,
    fmt_bytes,
    fmt_num,
    fmt_rate,
    fmt_secs,
    num,
    opt_num,
    truncate,
)

# --- util ---


def test_path_lookup_and_numbers():
    d = {"a": {"b c": {"x.y": Int64(3)}}, "f": 1.5, "i": 7, "t": True}
    assert num(d, ["a", "b c", "x.y"]) == 3.0
    assert num(d, ["f"]) == 1.5
    assert num(d, ["i"]) == 7.0
    assert num(d, ["missing", "x"]) == 0.0
    assert boolean(d, ["t"])
    assert opt_num(d, ["a", "nope"]) is None


def test_formatting():
    assert fmt_num(12.0) == "12"
    assert fmt_num(12345.0) == "12.3k"
    assert fmt_num(2_500_000.0) == "2.50M"
    assert fmt_bytes(1536.0) == "1.5K"
    assert fmt_bytes(3.0 * 1024 * 1024 * 1024) == "3.0G"
    assert fmt_secs(3.25) == "3.2s"
    assert fmt_secs(125.0) == "2m05s"
    assert fmt_secs(90061.0) == "1d01h"
    assert fmt_rate(0.0) == "0"
    assert fmt_rate(3.25) == "3.2"
    assert fmt_rate(250.5) == "251"
    assert truncate("abcdef", 4) == "abc…"


def test_compact():
    assert compact(1234) == "1234"
    assert compact("shard01:55") == "shard01:55"
    assert compact(Binary(b"\x01\xab", 4)) == "01ab"
    assert compact({"find": "orders", "n": Int64(2)}) == '{"find":"orders","n":2}'


# --- model ---


def test_mode_keys_roundtrip():
    for m in Mode:
        assert Mode.from_key(m.key) is m
        assert Mode.from_key(m.key.lower()) is m
    assert Mode.from_key("Z") is None


def test_parses_current_op():
    d = {
        "type": "op",
        "opid": 1234,
        "active": True,
        "microsecs_running": Int64(2_500_000),
        "op": "query",
        "ns": "shop.orders",
        "client": "10.0.0.5:51234",
        "appName": "api",
        "effectiveUsers": [{"user": "app", "db": "admin"}],
        "command": {"find": "orders", "filter": {"status": "A"}, "lsid": {"id": 1}, "$db": "shop"},
        "planSummary": "COLLSCAN",
        "numYields": 7,
        "waitingForLock": True,
        "locks": {"Global": "r"},
        "transaction": {
            "parameters": {"txnNumber": Int64(3), "autocommit": False},
            "timeOpenMicros": Int64(4_000_000),
            "timeActiveMicros": Int64(1_000_000),
            "timeInactiveMicros": Int64(3_000_000),
        },
    }
    retryable = Op.from_doc({"transaction": {"parameters": {"txnNumber": Int64(9)}}})
    assert retryable.txn is None, "retryable writes are not transactions"
    op = Op.from_doc(d)
    assert op.opid_str == "1234"
    assert abs(op.secs - 2.5) < 1e-9
    assert op.user == "app@admin"
    assert op.plan == "COLLSCAN"
    assert not op.is_background()
    assert op.waiting_lock
    assert op.waiting_for == "lock"
    assert '"find":"orders"' in op.command
    assert "lsid" not in op.command, f"noisy fields stripped: {op.command}"
    assert op.txn.number == 3
    assert abs(op.txn.open_secs - 4.0) < 1e-9


def test_background_ops_detected():
    hello = Op.from_doc(
        {"op": "command", "client": "1.1.1.1:1", "command": {"hello": 1, "maxAwaitTimeMS": 10000}}
    )
    assert hello.is_background()
    thread = Op.from_doc({"op": "none", "desc": "Checkpointer"})
    assert thread.is_background()
    plain_hello = Op.from_doc({"op": "command", "client": "1.1.1.1:1", "command": {"hello": 1}})
    assert not plain_hello.is_background()


def test_parses_getmore_originating_command():
    op = Op.from_doc(
        {
            "opid": "shard01:55",
            "command": {"getMore": Int64(99), "collection": "c"},
            "cursor": {"originatingCommand": {"aggregate": "c", "pipeline": []}},
            "client_s": "1.2.3.4:5",
        }
    )
    assert op.opid_str == "shard01:55"
    assert op.client == "1.2.3.4:5"
    assert op.command.startswith("getMore ← ")
    assert "aggregate" in op.command


def test_repl_lag_relative_to_primary():
    t0 = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    d = {
        "set": "rs0",
        "members": [
            {"name": "a:27017", "state": 1, "stateStr": "PRIMARY", "health": 1.0,
             "optimeDate": t0, "self": True},
            {"name": "b:27017", "state": 2, "stateStr": "SECONDARY", "health": 1.0,
             "optimeDate": t0 - dt.timedelta(seconds=5), "pingMs": Int64(3),
             "syncSourceHost": "a:27017"},
            {"name": "c:27017", "state": 7, "stateStr": "ARBITER", "health": 0.0},
        ],
    }
    r = ReplStatus.from_doc(d)
    assert r.set == "rs0"
    assert r.members[0].lag_secs == 0.0
    assert r.members[1].lag_secs == 5.0
    assert r.members[1].sync_source == "a:27017"
    assert r.members[2].lag_secs is None


def test_top_rates_are_deltas():
    def sample(total_time, total_count, write_count):
        return {"totals": {"note": "x", "db.c": {
            "total": {"time": Int64(total_time), "count": Int64(total_count)},
            "readLock": {"time": 0, "count": 5},
            "writeLock": {"time": 0, "count": write_count},
            "insert": {"time": 0, "count": write_count}}}}

    prev = parse_top(sample(1_000_000, 10, 5))
    cur = parse_top(sample(1_500_000, 30, 25))
    assert len(cur) == 1, "string entries like 'note' are skipped"
    r = top_rates(cur, prev, 2.0)
    assert r[0].ns == "db.c"
    assert abs(r[0].ops - 10.0) < 1e-9
    assert abs(r[0].insert - 10.0) < 1e-9
    # 2초 동안 0.5초 작업 = 25% 부하
    assert abs(r[0].time_pct - 25.0) < 1e-9


def test_parses_command_counters():
    s = {"metrics": {"commands": {
        "find": {"failed": 1, "total": 10},
        "update": {"arrayFilters": 0, "failed": 0, "total": 4},
        "nested": {"sub": {"total": 1}},
    }}}
    c = parse_commands(s)
    assert c["find"] == (10.0, 1.0)
    assert c["update"] == (4.0, 0.0)
    assert "nested" not in c


# --- metrics ---


def _status(up_ms, ins, bytes_in, lat, lat_ops):
    return {
        "host": "h1", "version": "8.0.0", "process": "mongod", "uptime": 100.0,
        "uptimeMillis": Int64(up_ms),
        "opcounters": {"insert": ins, "query": 0, "update": 0, "delete": 0, "getmore": 0, "command": 0},
        "network": {"bytesIn": bytes_in, "bytesOut": 0, "numRequests": 0},
        "opLatencies": {"writes": {"latency": lat, "ops": lat_ops}},
        "repl": {"setName": "rs0", "isWritablePrimary": True},
        "wiredTiger": {"cache": {"bytes currently in the cache": 50,
                                 "maximum bytes configured": 200,
                                 "tracked dirty bytes in the cache": 10}},
        "queues": {"execution": {"read": {"out": 1, "totalTickets": 128}}},
    }


def test_computes_rates_from_server_clock():
    r = rates(_status(12_000, 300, 5000, 30_000, 10), _status(10_000, 100, 1000, 0, 0))
    assert r.dt == 2.0
    assert r.insert == 100.0
    assert r.net_in == 2000.0
    assert r.lat_write_ms == 3.0
    assert r.lat_read_ms == 0.0


def test_restart_yields_no_rates():
    assert rates(_status(1_000, 5, 0, 0, 0), _status(50_000, 100, 0, 0, 0)) is None


def test_gauges_role_and_cache():
    g = Gauges.from_status(_status(1, 0, 0, 0, 0))
    assert g.role == "PRIMARY"
    assert g.set == "rs0"
    assert g.cache_used_pct() == 25.0
    assert g.cache_dirty_pct() == 5.0
    assert g.tickets_r_total == 128.0
    assert Gauges.from_status({"process": "mongod"}).role == "standalone"
    assert Gauges.from_status({"process": "mongos"}).role == "mongos"

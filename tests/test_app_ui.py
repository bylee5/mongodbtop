"""앱 상태 · 키 처리 · 화면 렌더링 테스트 (터미널 없이 Buffer에 그린다)."""

import time

from bson import Int64

from mongotop import views
from mongotop.app import QUIT, SETTINGS, App, Help, Normal, Prompt, command
from mongotop.collector import Kill, KillTarget, Refresh, Snapshot, SnapshotMsg
from mongotop.model import Mode, Op
from mongotop.ui import Buffer, UiState, draw


def status(up_ms, ins):
    return {"host": "h", "version": "8.0.0", "process": "mongod", "uptime": 10.0,
            "uptimeMillis": Int64(up_ms), "opcounters": {"insert": ins},
            "connections": {"current": 3, "available": 100}}


def snap(mode, up_ms, ops=None):
    return SnapshotMsg(0, Snapshot(mode=mode, status=status(up_ms, up_ms // 10), ops=ops,
                                   taken=time.monotonic()))


def new_app():
    return App(["a:1", "b:2"], Mode.QUERIES, 1.0, False)


def test_mode_switch_and_server_cycle():
    a = new_app()
    assert a.handle_key("S") == SETTINGS
    assert a.mode is Mode.STATUS
    assert a.handle_key("S") == command(Refresh())
    assert a.handle_key("n") == SETTINGS
    assert a.server == 1
    a.handle_key("n")
    assert a.server == 0
    assert a.handle_key("q") == QUIT


def test_interval_prompt():
    a = new_app()
    a.handle_key("s")
    assert a.input == Prompt("interval", "1")
    a.handle_key("BACKSPACE")
    for c in "2.5":
        a.handle_key(c)
    assert a.handle_key("ENTER") == SETTINGS
    assert a.interval == 2.5
    assert a.input == Normal()


def test_history_and_filter_and_kill():
    a = new_app()
    ops = [
        Op.from_doc({"opid": 1, "active": True, "secs_running": 1, "op": "query", "ns": "a.x"}),
        Op.from_doc({"opid": 2, "active": True, "secs_running": 50, "op": "update", "ns": "b.y"}),
    ]
    a.on_message(snap(Mode.QUERIES, 1000, ops))
    assert not a.cur().history, "no rates from a single sample"
    a.on_message(snap(Mode.QUERIES, 2000, ops))
    assert len(a.cur().history) == 1
    assert a.cur().history[0].rates.insert == 100.0

    # 기본 정렬: 가장 오래 실행 중인 작업이 먼저
    assert a.table().rows[0].cells[0].text == "2"

    a.filter = "a.x"
    assert len(a.table().rows) == 1
    a.filter = ""

    # 선택된(첫 번째) 행을 kill하고 y로 확인
    a.handle_key("k")
    assert isinstance(a.input, Prompt) and a.input.kind == "kill"
    assert a.handle_key("y") == command(Kill(0, KillTarget.op(2)))

    # 정렬 방향 반전
    a.handle_key("r")
    assert a.table().rows[0].cells[0].text == "1"


def test_restart_resets_rates():
    a = new_app()
    a.on_message(snap(Mode.STATUS, 5000))
    a.on_message(snap(Mode.STATUS, 100))
    assert not a.cur().history
    assert len(views.header(a.cur())) == 2, "restart: identity + baseline notice"


def test_every_mode_builds_with_and_without_data():
    a = new_app()
    for m in Mode:
        a.mode = m
        a.table()
        views.header(a.cur())
    a.on_message(snap(Mode.STATUS, 1000))
    a.on_message(snap(Mode.STATUS, 2000))
    for m in Mode:
        a.mode = m
        t = a.table()
        for r in t.rows:
            assert len(r.cells) == len(t.columns), f"{m} row width"


def screen(app, w, h):
    buf = Buffer(w, h)
    draw(app, buf, UiState())
    return buf.text()


def test_renders_every_mode_and_popups():
    app = App(["db1:27017"], Mode.QUERIES, 1.0, False)
    ops = [Op.from_doc({"opid": 42, "active": True, "secs_running": 12, "op": "query",
                        "ns": "shop.orders", "client": "10.0.0.1:5000", "planSummary": "COLLSCAN",
                        "command": {"find": "orders"}})]
    for up in (1000, 2000):
        st = {"host": "db1", "version": "8.0.3", "process": "mongod", "uptime": 99.0,
              "uptimeMillis": Int64(up), "opcounters": {"query": up}, "connections": {"current": 5}}
        app.on_message(SnapshotMsg(0, Snapshot(mode=Mode.QUERIES, status=st, ops=list(ops))))
    s = screen(app, 160, 30)
    assert "[Q] Query List" in s, s
    assert "shop.orders" in s
    assert "COLLSCAN" in s
    assert "ops/s" in s
    assert "Time▼" in s
    for m in Mode:
        app.mode = m
        assert m.title in screen(app, 160, 30), m
        # 아주 작은 터미널에서도 예외가 나면 안 된다
        screen(app, 20, 5)
        screen(app, 1, 1)
        screen(app, 0, 0)
    app.mode = Mode.QUERIES
    app.input = Help()
    assert "mongotop help" in screen(app, 120, 40)
    app.input = Normal()
    app.handle_key("ENTER")
    assert "opid: 42" in screen(app, 120, 40)


def test_buffer_wide_chars():
    b = Buffer(6, 1)
    b.put(0, 0, "한글ab")
    assert b.text() == "한글ab"
    b.put(0, 1, "x")  # 전각 문자의 반쪽을 덮어쓴다
    assert b.text() == " x글ab"
    b2 = Buffer(3, 1)
    b2.put(0, 0, "가나")  # 두 번째 글자는 들어갈 자리가 없다
    assert b2.text() == "가 "

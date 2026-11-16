"""실제 서버를 대상으로 하는 테스트. 실행 방법:

    MONGODBTOP_TEST_URI=mongodb://localhost:27017 pytest tests/test_live.py -s

환경 변수가 없으면 건너뛴다. `test_live_screens`는 실데이터로 모든 모드 화면을 출력한다.
"""

import os
import threading
import time

import pytest

from mongodbtop.app import App
from mongodbtop.collector import Collector, ConnectOpts, KillTarget, SnapshotMsg, connect
from mongodbtop.model import Mode, Op
from mongodbtop.ui import Buffer, UiState, draw

URI = os.environ.get("MONGODBTOP_TEST_URI")
pytestmark = pytest.mark.skipif(not URI, reason="MONGODBTOP_TEST_URI not set")


@pytest.fixture
def collector():
    name, client = connect(URI, ConnectOpts())
    c = Collector(client)
    c.name = name
    yield c
    client.close()


def test_every_mode_collects(collector):
    for m in Mode:
        collector.collect(m)


def test_kill_op_terminates_a_slow_query(collector):
    client = collector.client
    coll = client.mongodbtop_test.slow
    coll.insert_one({"x": 1})
    where = "sleep(60000); return false"
    result = {}

    def slow():
        try:
            coll.find_one({"$where": where})
            result["ok"] = True
        except Exception as e:  # 중단되면 Interrupted 에러
            result["err"] = e

    t = threading.Thread(target=slow, daemon=True)
    t.start()
    # 우리 작업은 appName 때문에 화면에서 숨겨지므로 $currentOp에서 직접 찾는다.
    opid = None
    for _ in range(50):
        time.sleep(0.2)
        docs = list(client.admin.aggregate([
            {"$currentOp": {"allUsers": True}},
            {"$match": {"command.filter.$where": where}},
        ]))
        if docs:
            opid = docs[0]["opid"]
            break
    assert opid is not None, "slow op visible in $currentOp"
    assert "killOp" in collector.kill(KillTarget.op(opid))
    t.join(10)
    assert not t.is_alive(), "query ended after kill"
    assert "err" in result, "killed query returns an error (Interrupted)"
    client.drop_database("mongodbtop_test")


def test_kill_session_aborts_idle_transaction(collector):
    client = collector.client
    coll = client.mongodbtop_test2.t
    coll.insert_one({"seed": 1})
    with client.start_session() as session:
        session.start_transaction()
        coll.insert_one({"in_txn": 1}, session=session)
        my_id = session.session_id["id"]
        found = None
        for _ in range(20):
            docs = list(client.admin.aggregate([
                {"$currentOp": {"allUsers": True, "idleSessions": True}},
                {"$match": {"transaction.parameters.autocommit": False}},
            ]))
            found = next((o for o in map(Op.from_doc, docs)
                          if o.lsid and o.lsid.get("id") == my_id), None)
            if found:
                break
            time.sleep(0.2)
        assert found is not None, "idle transaction visible"
        assert found.txn is not None
        collector.kill(KillTarget.session(found.lsid))
        with pytest.raises(Exception):
            session.commit_transaction()
    collector.collect(Mode.TRANSACTIONS)
    client.drop_database("mongodbtop_test2")


def test_expand_members(collector):
    from mongodbtop.collector import discover_members, expand_members

    if not discover_members(collector.client):
        pytest.skip("not a replica set")
    seed_name, seed = connect(URI, ConnectOpts())
    members = expand_members("rs", URI, ConnectOpts(), seed)
    try:
        assert members and all(label.startswith("rs/") for label, _ in members)
        for _, client in members:
            snap = Collector(client).collect(Mode.OVERVIEW)
            assert snap.repl is not None and not snap.repl.error
            assert any(m.is_self for m in snap.repl.members)
    finally:
        for _, client in members:
            client.close()


def test_live_screens(collector):
    app = App([collector.name], Mode.QUERIES, 1.0, False)
    for m in Mode:
        app.mode = m
        for _ in range(2):
            app.on_message(SnapshotMsg(0, collector.collect(m)))
            time.sleep(0.7)
        buf = Buffer(150, 22)
        draw(app, buf, UiState())
        print(buf.text() + "\n")

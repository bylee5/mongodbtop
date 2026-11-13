"""테스트 서버에 부하를 걸어 mongotop 화면(Q, T, O, S, C 모드)에 데이터가 보이게 한다.

    python scripts/loadgen.py [URI] [--seconds N]

만드는 작업:
  - 계속되는 insert / update / find (shop.orders)
  - 몇 초씩 걸리는 COLLSCAN 쿼리 ($where sleep) → Q 모드의 오래 실행되는 작업
  - 열어 둔 채 쉬는 다중 문서 트랜잭션 → T 모드
테스트용 서버에만 쓰세요. `shop` 데이터베이스에 데이터를 쓴다.
"""

from __future__ import annotations

import argparse
import random
import threading
import time

from pymongo import MongoClient
from pymongo.errors import PyMongoError

APP = "mongotop-loadgen"


def writer(coll, stop: threading.Event) -> None:
    n = 0
    while not stop.is_set():
        docs = [
            {"status": random.choice("ABCD"), "qty": random.randint(1, 100), "seq": n + i}
            for i in range(50)
        ]
        coll.insert_many(docs)
        coll.update_many({"status": random.choice("ABCD"), "qty": {"$lt": 5}}, {"$inc": {"qty": 1}})
        n += 50
        time.sleep(0.05)


def reader(coll, stop: threading.Event) -> None:
    while not stop.is_set():
        list(coll.find({"status": random.choice("ABCD"), "qty": {"$gt": 90}}).limit(20))
        time.sleep(0.02)


def slow_query(coll, stop: threading.Event, secs: int) -> None:
    """서버에서 `secs`초 동안 도는 쿼리를 반복한다 (COLLSCAN, 오래 실행)."""
    while not stop.is_set():
        try:
            coll.find_one({"$where": f"sleep({secs * 1000}); return false"})
        except PyMongoError:
            pass  # mongotop에서 kill하면 여기로 온다


def idle_txn(client, coll, stop: threading.Event) -> None:
    """트랜잭션을 열고 문서 하나를 쓴 뒤 20초 동안 아무것도 하지 않는다."""
    while not stop.is_set():
        try:
            with client.start_session() as s:
                s.start_transaction()
                coll.insert_one({"txn": True, "at": time.time()}, session=s)
                stop.wait(20)
                s.abort_transaction()
        except PyMongoError:
            stop.wait(1)  # mongotop에서 세션을 kill하면 여기로 온다


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("uri", nargs="?", default="mongodb://localhost:27017")
    p.add_argument("--seconds", type=float, default=0, help="run time (0 = until Ctrl-C)")
    a = p.parse_args()

    client = MongoClient(a.uri, appname=APP)
    coll = client.shop.orders
    coll.insert_one({"status": "A", "qty": 1, "seq": -1})
    stop = threading.Event()
    jobs = [
        (writer, (coll, stop)),
        (reader, (coll, stop)),
        (reader, (coll, stop)),
        (slow_query, (coll, stop, 3)),
        (slow_query, (coll, stop, 7)),
        (slow_query, (coll, stop, 30)),
        (idle_txn, (client, coll, stop)),
    ]
    for fn, args in jobs:
        threading.Thread(target=fn, args=args, daemon=True).start()
    print(f"load running against {a.uri} (app {APP}); Ctrl-C to stop", flush=True)
    try:
        if a.seconds:
            time.sleep(a.seconds)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    stop.set()


if __name__ == "__main__":
    main()

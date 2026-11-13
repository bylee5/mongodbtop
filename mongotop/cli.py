"""mongotop — innotop 스타일의 MongoDB 실시간 모니터. CLI 진입점과 batch 모드."""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time
from typing import List, Optional

from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

from . import __version__, views
from .app import App
from .collector import CollectError, Collector, ConnectOpts, connect, short_err
from .model import Mode

EPILOG = """\
MODES: Q queries, T transactions, L lock waits, C commands, S status over time,
       B WiredTiger cache, R replication, O collection activity (top), D database sizes

EXAMPLES:
  mongotop
  mongotop mongodb://admin:secret@db1:27017,db2:27017/?replicaSet=rs0 -m R
  mongotop mongodb://db1:27017 mongodb://db2:27017      (press n to switch)
  mongotop -b -m S -c 10 -i 1                           (vmstat-like batch output)
"""


def _mode(s: str) -> Mode:
    if len(s) != 1:
        raise argparse.ArgumentTypeError("mode is a single letter: Q T L C S B R O D")
    m = Mode.from_key(s)
    if m is None:
        raise argparse.ArgumentTypeError(f"unknown mode '{s}'")
    return m


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mongotop",
        description="innotop-style real-time monitor for MongoDB",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "uris",
        nargs="*",
        metavar="URI",
        help="MongoDB URI(s); several can be given and switched with `n` "
        "[env: MONGODB_URI, default: mongodb://localhost:27017]",
    )
    p.add_argument("-i", "--interval", type=float, default=2.0, help="refresh interval in seconds (default 2)")
    p.add_argument("-m", "--mode", type=_mode, default=Mode.QUERIES, help="start mode: Q T L C S B R O D")
    p.add_argument("-u", "--username", help="user name (overrides the URI)")
    p.add_argument(
        "-p",
        "--password",
        default=os.environ.get("MONGODB_PASSWORD"),
        help="password (overrides the URI) [env: MONGODB_PASSWORD]",
    )
    p.add_argument("--auth-db", help="authentication database")
    p.add_argument("--idle", action="store_true", help="start with idle connections / inactive rows shown")
    p.add_argument("--no-direct", action="store_true", help="do not force a direct connection for single-host URIs")
    p.add_argument("-b", "--batch", action="store_true", help="non-interactive mode: tab-separated output instead of the TUI")
    p.add_argument("-c", "--count", type=int, default=1, help="batch iterations (0 = forever, default 1)")
    p.add_argument("-V", "--version", action="version", version=f"mongotop {__version__}")
    return p


def friendly_error(server: str, e: BaseException) -> str:
    """흔한 접속 실패를 짧고 조치 가능한 메시지로 바꾼다."""
    hint_auth = (
        "pass credentials, e.g.\n"
        '  mongotop "mongodb://USER:PASSWORD@HOST:27017/?authSource=admin"\n'
        "  mongotop mongodb://HOST:27017 -u USER -p PASSWORD --auth-db admin"
    )
    if isinstance(e, OperationFailure):
        msg = (e.details or {}).get("errmsg") or str(e)
        if e.code == 13 and "requires authentication" in msg:
            return f"{server} requires authentication — {hint_auth}"
        if e.code == 13:
            return (
                f"{server}: not authorized ({msg}). The user needs the clusterMonitor role "
                "(plus killop to kill operations)."
            )
        if e.code == 18 or "Authentication failed" in msg:
            return (
                f"{server}: authentication failed ({msg}). Check user/password and "
                "the auth database (--auth-db / authSource, usually admin)."
            )
    if isinstance(e, ServerSelectionTimeoutError):
        return (
            f"{server}: cannot connect (server selection timed out). Check host/port, "
            "firewall and whether TLS is required (tls=true in the URI)."
        )
    return f"cannot run serverStatus on {server}: {short_err(e)}"


def run_batch(collector: Collector, name: str, mode: Mode, interval: float, count: int, idle: bool) -> None:
    """innotop의 비대화형 모드: 탭 구분 출력을 `count`회 반복한다."""
    app = App([name], mode, interval, idle)
    out = sys.stdout
    # 초당 값을 쓰는 모드는 기준 샘플이 먼저 필요하다.
    if mode in (Mode.STATUS, Mode.COMMANDS, Mode.BUFFERS, Mode.TOP):
        app.servers[0].apply(collector.collect(mode, idle))
        time.sleep(interval)
    i = 0
    while True:
        app.servers[0].apply(collector.collect(mode, idle))
        table = app.table()
        if mode is Mode.STATUS:
            # vmstat 스타일: 헤더는 한 번만, 이후 샘플마다 한 줄.
            table.rows = table.rows[:1]
            out.write(views.to_tsv(table, i == 0))
        else:
            if i > 0:
                out.write("\n")
            out.write(f"# {_dt.datetime.now():%Y-%m-%d %H:%M:%S} {name} — {table.title}\n")
            out.write(views.to_tsv(table, True))
        out.flush()
        i += 1
        if count and i >= count:
            return
        time.sleep(interval)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0.1 <= args.interval <= 3600.0:
        print("error: --interval must be between 0.1 and 3600 seconds", file=sys.stderr)
        return 2
    if args.count < 0:
        print("error: --count must be 0 or more", file=sys.stderr)
        return 2
    uris = args.uris or [os.environ.get("MONGODB_URI", "mongodb://localhost:27017")]
    copts = ConnectOpts(args.username, args.password, args.auth_db, args.no_direct)

    names: List[str] = []
    collectors: List[Collector] = []
    try:
        for uri in uris:
            name, client = connect(uri, copts)
            names.append(name)
            collectors.append(Collector(client))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # 첫 번째 서버에 접속할 수 없으면 명확한 메시지와 함께 바로 종료한다.
    try:
        collectors[0].server_status()
    except PyMongoError as e:
        print(f"error: {friendly_error(names[0], e)}", file=sys.stderr)
        return 1

    try:
        if args.batch:
            try:
                # 파이프/파일로 보낼 때는 로캘 코드페이지(예: cp949) 대신 UTF-8로 쓴다.
                if sys.stdout.isatty():
                    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
                else:
                    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                pass
            run_batch(collectors[0], names[0], args.mode, args.interval, args.count, args.idle)
        else:
            from .ui import run_tui  # curses는 TUI에서만 필요하다

            run_tui(collectors, names, args.mode, args.interval, args.idle)
    except CollectError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # 예: mongotop -b ... | head
        return 0
    finally:
        for c in collectors:
            c.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

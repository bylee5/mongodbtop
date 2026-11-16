"""mongodbtop — innotop 스타일의 MongoDB 실시간 모니터. CLI 진입점과 batch 모드."""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

from . import __version__, config, views
from .app import App
from .collector import CollectError, Collector, connect, expand_members, short_err
from .model import Mode

EPILOG = """\
MODES: Q queries, T transactions, L lock waits, C commands, S status over time,
       B WiredTiger cache, R replication, O collection activity (top), D database sizes,
       A all servers (one summary row per server)

SERVERS: a target is a URI, or a server / group name from the config file
  (~/.mongodbtop.ini, or --config / MONGODBTOP_CONFIG). See --list.

PASSWORDS are never read from the config file. Lookup order:
  -p, password_env, MONGODBTOP_PASSWORD_<NAME>, MONGODB_PASSWORD, keyring, prompt

EXAMPLES:
  mongodbtop
  mongodbtop prod-rs                                      (name from the config file)
  mongodbtop -g prod                                      (every server in group prod)
  mongodbtop mongodb://db1:27017/?replicaSet=rs0 --expand (one entry per member)
  mongodbtop mongodb://db1:27017 mongodb://db2:27017      (press n / w to switch)
  mongodbtop -b -m S -c 10 -i 1                           (vmstat-like batch output)
  mongodbtop -b -m A -g prod -c 0 -i 10                   (all servers, every 10s)
"""


def _mode(s: str) -> Mode:
    if len(s) != 1:
        raise argparse.ArgumentTypeError("mode is a single letter: Q T L C S B R O D A")
    m = Mode.from_key(s)
    if m is None:
        raise argparse.ArgumentTypeError(f"unknown mode '{s}'")
    return m


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mongodbtop",
        description="innotop-style real-time monitor for MongoDB",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="MongoDB URI, or server / group name from the config file "
        "[default: config 'default', env MONGODB_URI, or mongodb://localhost:27017]",
    )
    p.add_argument("-g", "--group", action="append", default=[], help="monitor every server in a config group (repeatable)")
    p.add_argument("--config", help="config file (default ~/.mongodbtop.ini or env MONGODBTOP_CONFIG)")
    p.add_argument("--list", action="store_true", help="list servers from the config file and exit")
    p.add_argument("--expand", action="store_true", help="monitor every replica set member separately")
    p.add_argument("-i", "--interval", type=float, help="refresh interval in seconds (default 2)")
    p.add_argument("-m", "--mode", type=_mode, help="start mode: Q T L C S B R O D A (default Q; A for several servers)")
    p.add_argument("-u", "--username", help="user name (overrides the URI / config)")
    p.add_argument("-p", "--password", help="password (overrides everything else)")
    p.add_argument("--auth-db", help="authentication database")
    p.add_argument("--idle", action="store_true", default=None, help="start with idle connections / inactive rows shown")
    p.add_argument("--no-direct", action="store_true", help="do not force a direct connection for single-host URIs")
    p.add_argument("-b", "--batch", action="store_true", help="non-interactive mode: tab-separated output instead of the TUI")
    p.add_argument("-c", "--count", type=int, default=1, help="batch iterations (0 = forever, default 1)")
    p.add_argument("-V", "--version", action="version", version=f"mongodbtop {__version__}")
    return p


def friendly_error(server: str, e: BaseException) -> str:
    """흔한 접속 실패를 짧고 조치 가능한 메시지로 바꾼다."""
    hint_auth = (
        "pass credentials, e.g.\n"
        '  mongodbtop "mongodb://USER:PASSWORD@HOST:27017/?authSource=admin"\n'
        "  mongodbtop mongodb://HOST:27017 -u USER -p PASSWORD --auth-db admin\n"
        "  or set user / password_env for the server in ~/.mongodbtop.ini"
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


def open_targets(targets: List[config.Target], warn=None) -> List[Tuple[str, MongoClient]]:
    """접속 대상마다 클라이언트를 만들고, expand 대상은 레플리카셋 멤버로 펼친다.
    멤버 탐색에 실패하면 경고하고 시드 연결만 쓴다 (나중에 살아나면 화면에서 보인다)."""
    warn = warn or (lambda m: print(f"warning: {m}", file=sys.stderr))
    out: List[Tuple[str, MongoClient]] = []
    for t in targets:
        hosts, client = connect(t.uri, t.opts)
        label = t.label or hosts
        if not t.expand:
            out.append((label, client))
            continue
        try:
            out.extend(expand_members(label, t.uri, t.opts, client))
        except PyMongoError as e:
            warn(f"{label}: cannot list replica set members ({short_err(e)}); monitoring the seed only")
            out.append((label, client))
    # 같은 노드가 여러 대상에 들어 있으면 한 번만 본다.
    seen, unique = set(), []
    for name, c in out:
        if name in seen:
            c.close()
            continue
        seen.add(name)
        unique.append((name, c))
    return unique


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


def run_batch_overview(collectors: List[Collector], names: List[str], interval: float, count: int) -> None:
    """A 모드 batch: 모든 서버를 동시에 수집해 서버마다 한 줄씩 출력한다.
    응답하지 않는 서버는 Status 컬럼에 DOWN으로 나온다."""
    app = App(names, Mode.OVERVIEW, interval, False)
    out = sys.stdout

    def sample(pool: ThreadPoolExecutor) -> None:
        def one(c: Collector):
            try:
                return c.collect(Mode.OVERVIEW)
            except CollectError as e:
                return e

        for st, res in zip(app.servers, pool.map(one, collectors)):
            if isinstance(res, CollectError):
                st.error = str(res)
            else:
                st.apply(res)

    with ThreadPoolExecutor(max_workers=min(32, len(collectors))) as pool:
        sample(pool)  # 초당 값의 기준 샘플
        time.sleep(interval)
        i = 0
        while True:
            sample(pool)
            table = app.table()
            if i > 0:
                out.write("\n")
            out.write(f"# {_dt.datetime.now():%Y-%m-%d %H:%M:%S} — {views.overview_header(app.servers)[0]}\n")
            out.write(views.to_tsv(table, True))
            out.flush()
            i += 1
            if count and i >= count:
                return
            time.sleep(interval)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.list:
        print(config.describe(cfg))
        return 0

    interval = args.interval if args.interval is not None else (cfg.interval or 2.0)
    if not 0.1 <= interval <= 3600.0:
        print("error: --interval must be between 0.1 and 3600 seconds", file=sys.stderr)
        return 2
    if args.count < 0:
        print("error: --count must be 0 or more", file=sys.stderr)
        return 2
    idle = args.idle if args.idle is not None else bool(cfg.idle)

    cli = config.CliAuth(args.username, args.password, args.auth_db, args.no_direct, args.expand)
    try:
        targets = config.resolve(args.targets, args.group, cfg, cli)
        opened = open_targets(targets)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):  # 비밀번호 입력 중 취소
        return 130
    names = [n for n, _ in opened]
    collectors = [Collector(c) for _, c in opened]

    mode = args.mode or cfg.mode or (Mode.OVERVIEW if len(collectors) > 1 else Mode.QUERIES)

    # 서버가 하나뿐이면 접속할 수 없을 때 명확한 메시지와 함께 바로 종료한다.
    # 여러 대일 때는 죽은 서버가 있어도 시작하고, 화면에 DOWN으로 표시한 뒤 계속 재시도한다.
    if len(collectors) == 1 or (args.batch and mode is not Mode.OVERVIEW):
        try:
            collectors[0].server_status()
        except PyMongoError as e:
            print(f"error: {friendly_error(names[0], e)}", file=sys.stderr)
            for c in collectors:
                c.client.close()
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
            if mode is Mode.OVERVIEW:
                run_batch_overview(collectors, names, interval, args.count)
            else:
                if len(collectors) > 1:
                    print(f"note: batch mode {mode.key} shows the first server only ({names[0]}); "
                          "use -m A for all servers", file=sys.stderr)
                run_batch(collectors[0], names[0], mode, interval, args.count, idle)
        else:
            from .ui import run_tui  # curses는 TUI에서만 필요하다

            run_tui(collectors, names, mode, interval, idle)
    except CollectError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # 예: mongodbtop -b ... | head
        return 0
    finally:
        for c in collectors:
            c.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

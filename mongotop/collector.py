"""MongoDB와 통신한다. 모니터링 대상 서버마다 `Collector`가 하나씩 있으며,
`Worker` 스레드가 백그라운드에서 현재 수집기를 돌려 스냅샷을 UI로 보낸다."""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs

from pymongo import MongoClient
from pymongo import uri_parser
from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

from .model import DbStat, Mode, Op, ReplStatus, TopRaw, parse_top
from .util import compact

APP_NAME = "mongotop"


class CollectError(Exception):
    """수집 실패. 메시지는 이미 사람이 읽을 수 있는 형태다."""


def short_err(e: BaseException) -> str:
    """드라이버 에러를 짧은 메시지로 만든다. 서버 커맨드 에러는
    "CodeName: message" 형태로 보여준다."""
    if isinstance(e, OperationFailure):
        details = e.details or {}
        name = details.get("codeName") or f"code {e.code}"
        msg = details.get("errmsg") or str(e)
        return f"{name}: {msg}"
    if isinstance(e, ServerSelectionTimeoutError):
        # 토폴로지 설명이 길게 붙으므로 앞부분만 쓴다.
        return str(e).split(", Timeout:")[0]
    return str(e) or type(e).__name__


def mask_uri(uri: str) -> str:
    """에러 메시지에 비밀번호가 찍히지 않도록 URI의 인증 정보를 가린다."""
    return re.sub(r"(mongodb(?:\+srv)?://)[^@/]*@", r"\1***@", uri)


@dataclass
class ConnectOpts:
    username: Optional[str] = None
    password: Optional[str] = None
    auth_db: Optional[str] = None
    # 단일 호스트 URI여도 direct connection을 강제하지 않는다.
    no_direct: bool = False


def connect(uri: str, o: ConnectOpts) -> Tuple[str, MongoClient]:
    """URI 하나에 대한 클라이언트를 만든다. `replicaSet`이 없는 단일 호스트 URI는
    토폴로지 탐색으로 프라이머리에 라우팅되지 않고 (innotop처럼) *그 노드*를
    모니터링하도록 직접 접속한다."""
    srv = uri.startswith("mongodb+srv://")
    query = uri.split("?", 1)[1] if "?" in uri else ""
    given = {k.lower() for k in parse_qs(query, keep_blank_values=True)}

    if srv:
        # SRV는 DNS 조회가 필요하므로 여기서는 해석하지 않는다.
        netloc = uri[len("mongodb+srv://") :].split("/", 1)[0].split("?", 1)[0]
        has_user = "@" in netloc
        hosts = [netloc.rsplit("@", 1)[-1]]
    else:
        try:
            parsed = uri_parser.parse_uri(uri)
        except Exception as e:  # InvalidURI, ConfigurationError, ValueError ...
            raise ValueError(f"invalid MongoDB URI: {mask_uri(uri)}: {e}") from e
        has_user = bool(parsed.get("username"))
        hosts = [f"{h}:{p}" for h, p in parsed["nodelist"]]

    kwargs: Dict[str, Any] = {"appname": APP_NAME, "tz_aware": True}
    if "serverselectiontimeoutms" not in given:
        kwargs["serverSelectionTimeoutMS"] = 5000
    if "connecttimeoutms" not in given:
        kwargs["connectTimeoutMS"] = 5000
    if (
        not o.no_direct
        and not srv
        and len(hosts) == 1
        and "replicaset" not in given
        and "directconnection" not in given
    ):
        kwargs["directConnection"] = True
    if o.username is not None or o.password is not None:
        if o.username is not None:
            kwargs["username"] = o.username
        if o.password is not None:
            kwargs["password"] = o.password
        if o.auth_db:
            kwargs["authSource"] = o.auth_db
    elif o.auth_db and has_user:
        kwargs["authSource"] = o.auth_db

    try:
        client = MongoClient(uri, **kwargs)
    except Exception as e:
        raise ValueError(f"invalid MongoDB URI: {mask_uri(uri)}: {e}") from e
    return ",".join(hosts), client


_URI_RE = re.compile(r"^(mongodb(?:\+srv)?)://([^@/?]*@)?([^/?]*)(/[^?]*)?(\?.*)?$")


def member_uri(uri: str, host: str, extra: Optional[Dict[str, str]] = None) -> str:
    """`uri`의 인증 정보와 옵션은 그대로 두고 호스트만 `host` 하나로 바꾼다.
    replicaSet / directConnection은 빼므로 `connect`가 그 노드에 직접 접속한다."""
    m = _URI_RE.match(uri)
    if not m:
        raise ValueError(f"invalid MongoDB URI: {mask_uri(uri)}")
    _, userinfo, _, path, query = m.groups()
    params = []
    for part in (query or "")[1:].split("&"):
        if part and part.split("=", 1)[0].lower() not in ("replicaset", "directconnection"):
            params.append(part)
    given = {p.split("=", 1)[0].lower() for p in params}
    for k, v in (extra or {}).items():
        if k.lower() not in given:
            params.append(f"{k}={v}")
    q = ("?" + "&".join(params)) if params else ""
    return f"mongodb://{userinfo or ''}{host}{path or ('/' if q else '')}{q}"


def discover_members(client: MongoClient) -> List[str]:
    """레플리카셋 멤버(데이터를 가진 노드) 목록. 레플리카셋이 아니면 빈 목록.
    `hello`는 권한 없이도 실행되며 프라이머리를 찾을 필요도 없다."""
    h = client.admin.command("hello")
    if not h.get("setName"):
        return []
    return list(h.get("hosts", [])) + list(h.get("passives", []))


def expand_members(label: str, uri: str, opts: ConnectOpts, seed: MongoClient) -> List[Tuple[str, MongoClient]]:
    """레플리카셋 URI 하나를 멤버별 직접 연결 목록으로 펼친다.
    레플리카셋이 아니면 `(label, seed)` 하나를 그대로 돌려준다.
    탐색 실패는 예외(PyMongoError)로 알린다 — 호출자가 시드만 쓰도록 결정한다."""
    hosts = discover_members(seed)
    if not hosts:
        return [(label, seed)]
    extra: Dict[str, str] = {}
    if uri.startswith("mongodb+srv://"):
        # SRV는 TLS가 기본이고 authSource가 TXT 레코드에 있을 수 있다.
        extra["tls"] = "true"
        try:
            src = uri_parser.parse_uri(uri).get("options", {}).get("authsource")
        except Exception:
            src = None
        if src and not opts.auth_db:
            extra["authSource"] = str(src)
    out = []
    for h in hosts:
        _, client = connect(member_uri(uri, h, extra), replace(opts, no_direct=False))
        out.append((f"{label}/{h}", client))
    seed.close()
    return out


@dataclass
class Snapshot:
    """serverStatus와 모드별 데이터. 해당 모드가 쓰지 않는 필드는 None."""

    mode: Mode
    status: dict
    ops: Optional[List[Op]] = None
    repl: Optional[ReplStatus] = None
    top: Optional[Dict[str, TopRaw]] = None
    dbs: Optional[List[DbStat]] = None
    taken: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class KillTarget:
    """kind: "op" (value = opid) 또는 "session" (value = lsid 문서)."""

    kind: str
    value: Any

    @classmethod
    def op(cls, opid: Any) -> "KillTarget":
        return cls("op", opid)

    @classmethod
    def session(cls, lsid: dict) -> "KillTarget":
        return cls("session", lsid)

    def __hash__(self) -> int:  # value가 dict일 수 있다
        return hash((self.kind, compact(self.value)))


class Collector:
    def __init__(self, client: MongoClient):
        self.client = client
        self._dbs_cache: Optional[Tuple[float, List[DbStat]]] = None

    @property
    def admin(self):
        return self.client.admin

    def server_status(self) -> dict:
        return self.admin.command("serverStatus")

    def collect(self, mode: Mode, show_idle: bool = False) -> Snapshot:
        try:
            status = self.server_status()
        except PyMongoError as e:
            raise CollectError(f"serverStatus failed: {short_err(e)}") from e
        snap = Snapshot(mode=mode, status=status)
        if mode.uses_current_op:
            snap.ops = self._current_op(mode, show_idle)
        elif mode is Mode.REPLICATION:
            snap.repl = self._repl_status()
        elif mode is Mode.TOP:
            try:
                snap.top = parse_top(self.admin.command("top"))
            except PyMongoError as e:
                raise CollectError(
                    f"top failed (not supported on mongos?): {short_err(e)}"
                ) from e
        elif mode is Mode.DATABASES:
            snap.dbs = self._db_stats()
        snap.taken = time.monotonic()
        return snap

    def _current_op(self, mode: Mode, show_idle: bool) -> List[Op]:
        stage = {
            "allUsers": True,
            "idleConnections": show_idle,
            # 비활성 트랜잭션은 idle 세션으로 나타난다.
            "idleSessions": True if mode is Mode.TRANSACTIONS else show_idle,
        }
        pipeline: List[dict] = [{"$currentOp": stage}]
        if mode is Mode.TRANSACTIONS:
            # retryable write에도 `transaction` 필드가 있으므로,
            # autocommit: false 인 다중 문서 트랜잭션만 고른다.
            pipeline.append({"$match": {"transaction.parameters.autocommit": False}})
        elif mode is Mode.LOCKS:
            pipeline.append(
                {
                    "$match": {
                        "$or": [
                            {"waitingForLock": True},
                            {"waitingForLatch": {"$exists": True}},
                            {"waitingForFlowControl": True},
                        ]
                    }
                }
            )
        try:
            docs = list(self.admin.aggregate(pipeline))
        except PyMongoError as e:
            raise CollectError(f"$currentOp failed: {short_err(e)}") from e
        # innotop이 자기 자신을 숨기듯 우리 모니터링 연결은 숨긴다.
        return [o for o in map(Op.from_doc, docs) if o.app != APP_NAME]

    def _repl_status(self) -> ReplStatus:
        try:
            d = self.admin.command("replSetGetStatus")
        except PyMongoError as e:
            return ReplStatus(error=short_err(e))
        r = ReplStatus.from_doc(d)
        try:
            r.oplog_window_secs = self._oplog_window()
        except PyMongoError:
            r.oplog_window_secs = None
        return r

    def _oplog_window(self) -> Optional[float]:
        """가장 오래된 oplog 항목과 가장 최근 항목 사이의 시간."""
        oplog = self.client.local["oplog.rs"]
        first = oplog.find_one({}, {"ts": 1}, sort=[("$natural", 1)])
        last = oplog.find_one({}, {"ts": 1}, sort=[("$natural", -1)])

        def ts(d: Optional[dict]) -> Optional[float]:
            t = (d or {}).get("ts")
            return float(t.time) if hasattr(t, "time") else None

        a, b = ts(first), ts(last)
        return b - a if a is not None and b is not None else None

    def _db_stats(self) -> List[DbStat]:
        """dbStats는 비용이 클 수 있어 결과를 10초간 캐시한다."""
        if self._dbs_cache and time.monotonic() - self._dbs_cache[0] < 10.0:
            return list(self._dbs_cache[1])
        try:
            listing = self.admin.command({"listDatabases": 1, "nameOnly": True})
        except PyMongoError as e:
            raise CollectError(f"listDatabases failed: {short_err(e)}") from e
        names = [
            d["name"]
            for d in listing.get("databases", [])
            if isinstance(d, dict) and isinstance(d.get("name"), str)
        ]
        out: List[DbStat] = []
        for name in names[:500]:
            try:
                out.append(DbStat.from_doc(name, self.client[name].command("dbStats")))
            except PyMongoError as e:
                out.append(DbStat(name=f"{name} (error: {short_err(e)})"))
        self._dbs_cache = (time.monotonic(), out)
        return list(out)

    def kill(self, target: KillTarget) -> str:
        if target.kind == "op":
            self.admin.command({"killOp": 1, "op": target.value})
            return f"killOp sent for opid {compact(target.value)}"
        self.admin.command({"killSessions": [target.value]})
        return "killSessions sent (transaction will be aborted)"


@dataclass
class Settings:
    """백그라운드 스레드가 해야 할 일. UI가 갱신한다."""

    server: int
    mode: Mode
    interval: float
    paused: bool
    show_idle: bool


@dataclass(frozen=True)
class Refresh:
    pass


@dataclass(frozen=True)
class Kill:
    server: int
    target: KillTarget


Command = Union[Refresh, Kill]


@dataclass
class SnapshotMsg:
    server: int
    snap: Snapshot


@dataclass
class ErrorMsg:
    server: int
    error: str


@dataclass
class InfoMsg:
    text: str


Message = Union[SnapshotMsg, ErrorMsg, InfoMsg]


BACKGROUND_INTERVAL = 5.0  # 화면에 띄우지 않은 서버의 최소 수집 주기
MAX_BACKOFF = 30.0  # 실패가 이어질 때 재시도 간격의 상한


def plan(s: Settings, index: int, failures: int) -> Tuple[Mode, float]:
    """서버 `index`가 이번에 수집할 모드와 다음 수집까지 기다릴 시간.

    - 화면에 띄운 서버: 현재 모드 전체 수집, 갱신 주기마다
    - 그 밖의 서버: serverStatus만 (S 모드 이력용), 최소 5초마다
    실패가 이어지면 화면에 띄운 서버가 아닌 한 간격을 두 배씩 늘린다 (최대 30초)."""
    if index == s.server:
        return s.mode, s.interval
    mode, wait = Mode.STATUS, max(s.interval, BACKGROUND_INTERVAL)
    if failures:
        wait = min(MAX_BACKOFF, max(wait, wait * 2 ** min(failures - 1, 5)))
    return mode, wait


class _ServerWorker(threading.Thread):
    """서버 하나를 맡는 수집 스레드. 서버가 응답하지 않아도 다른 서버에는 영향이 없다.
    연결이 끊기면 드라이버가 다음 수집 때 다시 접속한다."""

    def __init__(self, index: int, collector: Collector, pool: "Worker", out: "queue.Queue[Message]"):
        super().__init__(name=f"mongotop-collector-{index}", daemon=True)
        self.index = index
        self._collector = collector
        self._pool = pool
        self._out = out
        self.wake = threading.Event()
        self.commands: "queue.Queue[Command]" = queue.Queue()

    def run(self) -> None:
        failures = 0
        while not self._pool.stopped.is_set():
            s = self._pool.settings()
            mode, wait = plan(s, self.index, failures)
            if not s.paused:
                try:
                    snap = self._collector.collect(mode, s.show_idle)
                    self._out.put(SnapshotMsg(self.index, snap))
                    failures = 0
                except CollectError as e:
                    failures += 1
                    self._out.put(ErrorMsg(self.index, str(e)))
                except Exception as e:  # 예기치 못한 오류에도 스레드는 살아 있어야 한다
                    failures += 1
                    self._out.put(ErrorMsg(self.index, f"{type(e).__name__}: {e}"))
                _, wait = plan(s, self.index, failures)
            self.wake.wait(wait)
            self.wake.clear()
            while True:
                try:
                    cmd = self.commands.get_nowait()
                except queue.Empty:
                    break
                if isinstance(cmd, Kill):
                    try:
                        text = self._collector.kill(cmd.target)
                    except Exception as e:
                        text = f"kill failed: {short_err(e)}"
                    self._out.put(InfoMsg(text))
                else:  # Refresh: 깨어난 것만으로 충분하다
                    failures = 0


class Worker:
    """서버마다 수집 스레드를 하나씩 돌린다. 수집이 별도 스레드에서 돌기 때문에
    느린 서버나 죽은 서버가 있어도 키 입력과 다른 서버의 갱신이 막히지 않는다."""

    def __init__(self, collectors: List[Collector], settings: Settings, out: "queue.Queue[Message]"):
        self._settings = settings
        self._lock = threading.Lock()
        self.stopped = threading.Event()
        self._threads = [_ServerWorker(i, c, self, out) for i, c in enumerate(collectors)]

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    def update_settings(self, s: Settings) -> None:
        with self._lock:
            self._settings = s
        for t in self._threads:
            t.wake.set()

    def send(self, cmd: Command) -> None:
        if isinstance(cmd, Kill):
            targets = [self._threads[cmd.server]]
        else:
            targets = [self._threads[self.settings().server]]
        for t in targets:
            t.commands.put(cmd)
            t.wake.set()

    def stop(self) -> None:
        self.stopped.set()
        for t in self._threads:
            t.wake.set()

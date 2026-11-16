"""서버 목록 설정 파일(~/.mongotop.ini)과 접속 대상·인증 정보 결정.

    [defaults]
    interval = 2
    user = monitor
    auth_db = admin
    default = prod            ; 인자 없이 실행했을 때 접속할 서버 또는 그룹

    [server:prod-rs]
    uri = mongodb://db1:27017,db2:27017/?replicaSet=rs0
    groups = prod
    expand = members          ; 레플리카셋 멤버마다 따로 접속

비밀번호는 설정 파일에서 읽지 않는다. 찾는 순서:
  -p  >  password_env로 지정한 환경변수  >  MONGOTOP_PASSWORD_<NAME>
      >  MONGODB_PASSWORD  >  OS 키체인(keyring, 선택)  >  터미널에서 입력
"""

from __future__ import annotations

import configparser
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from .collector import ConnectOpts, mask_uri
from .model import Mode

KEYRING_SERVICE = "mongotop"
_SERVER_KEYS = {"uri", "groups", "user", "auth_db", "password_env", "expand", "direct"}
_DEFAULT_KEYS = {"interval", "mode", "idle", "user", "auth_db", "password_env", "default", "expand"}


class ConfigError(Exception):
    pass


def default_path() -> Path:
    env = os.environ.get("MONGOTOP_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".mongotop.ini"


def _bool(v: str, where: str) -> bool:
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on", "members"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError(f"{where}: expected yes/no, got {v!r}")


def _list(v: str) -> List[str]:
    return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]


@dataclass
class ServerDef:
    name: str
    uri: str
    groups: List[str] = field(default_factory=list)
    user: Optional[str] = None
    auth_db: Optional[str] = None
    password_env: Optional[str] = None
    expand: Optional[bool] = None
    direct: bool = True


@dataclass
class Config:
    path: Optional[Path] = None
    servers: Dict[str, ServerDef] = field(default_factory=dict)  # 파일에 적힌 순서
    interval: Optional[float] = None
    mode: Optional[Mode] = None
    idle: Optional[bool] = None
    user: Optional[str] = None
    auth_db: Optional[str] = None
    password_env: Optional[str] = None
    default: List[str] = field(default_factory=list)
    expand: bool = False

    def groups(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for s in self.servers.values():
            for g in s.groups:
                out.setdefault(g, []).append(s.name)
        return out


def load(path: Optional[str] = None) -> Config:
    """설정 파일을 읽는다. 경로를 명시하지 않았고 기본 파일이 없으면 빈 설정."""
    p = Path(path).expanduser() if path else default_path()
    if not p.exists():
        if path or os.environ.get("MONGOTOP_CONFIG"):
            raise ConfigError(f"config file not found: {p}")
        return Config()
    # 줄 끝 주석은 앞에 공백이 있는 ; 또는 # 만 (URI 안의 문자는 그대로). URI의 %도 그대로.
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        with open(p, encoding="utf-8") as f:
            cp.read_file(f)
    except (OSError, configparser.Error) as e:
        raise ConfigError(f"{p}: {e}") from e

    cfg = Config(path=p)
    for sec in cp.sections():
        items = dict(cp.items(sec))
        if "password" in items:
            raise ConfigError(
                f"{p} [{sec}]: passwords are not read from the config file — use password_env, "
                "MONGOTOP_PASSWORD_<NAME>, MONGODB_PASSWORD, keyring or the prompt"
            )
        if sec == "defaults":
            unknown = set(items) - _DEFAULT_KEYS
            if unknown:
                raise ConfigError(f"{p} [defaults]: unknown key(s): {', '.join(sorted(unknown))}")
            if "interval" in items:
                try:
                    cfg.interval = float(items["interval"])
                except ValueError:
                    raise ConfigError(f"{p} [defaults]: interval must be a number") from None
            if "mode" in items:
                cfg.mode = Mode.from_key(items["mode"].strip())
                if cfg.mode is None:
                    raise ConfigError(f"{p} [defaults]: unknown mode {items['mode']!r}")
            if "idle" in items:
                cfg.idle = _bool(items["idle"], f"{p} [defaults] idle")
            if "expand" in items:
                cfg.expand = _bool(items["expand"], f"{p} [defaults] expand")
            cfg.user = items.get("user") or None
            cfg.auth_db = items.get("auth_db") or None
            cfg.password_env = items.get("password_env") or None
            cfg.default = _list(items.get("default", ""))
        elif sec.startswith("server:"):
            name = sec[len("server:"):].strip()
            if not name or "://" in name:
                raise ConfigError(f"{p} [{sec}]: invalid server name")
            unknown = set(items) - _SERVER_KEYS
            if unknown:
                raise ConfigError(f"{p} [{sec}]: unknown key(s): {', '.join(sorted(unknown))}")
            uri = items.get("uri", "").strip()
            if not uri.startswith(("mongodb://", "mongodb+srv://")):
                raise ConfigError(f"{p} [{sec}]: uri must start with mongodb:// or mongodb+srv://")
            where = f"{p} [{sec}]"
            cfg.servers[name] = ServerDef(
                name=name,
                uri=uri,
                groups=_list(items.get("groups", "")),
                user=items.get("user") or None,
                auth_db=items.get("auth_db") or None,
                password_env=items.get("password_env") or None,
                expand=_bool(items["expand"], f"{where} expand") if "expand" in items else None,
                direct=_bool(items["direct"], f"{where} direct") if "direct" in items else True,
            )
        else:
            raise ConfigError(f"{p}: unknown section [{sec}] (use [defaults] or [server:NAME])")
    return cfg


def describe(cfg: Config) -> str:
    """--list 출력."""
    if not cfg.servers:
        where = cfg.path or default_path()
        return f"no servers configured ({where} not found or empty)"
    rows = [("NAME", "GROUPS", "USER", "EXPAND", "URI")]
    for s in cfg.servers.values():
        user, _ = uri_userinfo(s.uri)
        exp = s.expand if s.expand is not None else cfg.expand
        rows.append((s.name, ",".join(s.groups), user or s.user or cfg.user or "",
                     "members" if exp else "", mask_uri(s.uri)))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r[:4], widths)) + "  " + r[4] for r in rows]
    lines.append(f"\n({cfg.path})")
    return "\n".join(lines)


# --- 접속 대상 결정 ------------------------------------------------------------


@dataclass
class Target:
    label: Optional[str]  # 설정의 서버 이름 (URI를 직접 줬으면 None → 호스트 목록을 쓴다)
    uri: str
    opts: ConnectOpts
    expand: bool = False


@dataclass
class CliAuth:
    username: Optional[str] = None
    password: Optional[str] = None
    auth_db: Optional[str] = None
    no_direct: bool = False
    expand: bool = False


def uri_userinfo(uri: str) -> Tuple[Optional[str], bool]:
    """URI에 적힌 (사용자 이름, 비밀번호 포함 여부)."""
    m = re.match(r"mongodb(?:\+srv)?://([^@/?]*)@", uri)
    if not m:
        return None, False
    user, sep, _ = m.group(1).partition(":")
    return (unquote(user) or None), bool(sep)


def env_name(name: str) -> str:
    return "MONGOTOP_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _keyring(key: str, user: str) -> Optional[str]:
    try:
        import keyring  # 선택 의존성
    except ImportError:
        return None
    try:
        # 서버 이름별 항목이 먼저, 없으면 사용자 이름별 항목
        return keyring.get_password(KEYRING_SERVICE, key) or keyring.get_password(KEYRING_SERVICE, user)
    except Exception:
        return None


class PasswordSource:
    """비밀번호를 찾는다. 같은 사용자에 대해서는 한 번만 묻는다."""

    def __init__(self, prompt: Optional[Callable[[str], str]] = None,
                 environ: Optional[Dict[str, str]] = None, use_keyring: bool = True):
        self._environ = os.environ if environ is None else environ
        self._prompt = prompt
        self._asked: Dict[str, str] = {}
        self._use_keyring = use_keyring

    def find(self, key: str, user: str, envs: List[str]) -> Optional[str]:
        for e in envs:
            v = self._environ.get(e)
            if v:
                return v
        if user in self._asked:
            return self._asked[user]
        if self._use_keyring:
            v = _keyring(key, user)
            if v:
                return v
        prompt = self._prompt
        if prompt is None and sys.stdin.isatty():
            import getpass

            prompt = getpass.getpass
        if prompt is None:
            return None
        v = prompt(f"password for {user} ({key}): ")
        self._asked[user] = v
        return v


def _target(label: Optional[str], uri: str, sd: Optional[ServerDef], cfg: Config,
            cli: CliAuth, pw: PasswordSource) -> Target:
    uri_user, uri_has_pw = uri_userinfo(uri)
    user = cli.username or uri_user or (sd.user if sd else None) or cfg.user
    auth_db = cli.auth_db or (sd.auth_db if sd else None) or cfg.auth_db
    password = cli.password
    if password is None and user and not (uri_has_pw and user == uri_user):
        envs = []
        if sd and sd.password_env:
            envs.append(sd.password_env)
        if label:
            envs.append(env_name(label))
        if cfg.password_env:
            envs.append(cfg.password_env)
        envs.append("MONGODB_PASSWORD")
        password = pw.find(label or mask_uri(uri), user, envs)
    opts = ConnectOpts(
        username=user if (user and user != uri_user) or password is not None else None,
        password=password,
        auth_db=auth_db,
        no_direct=cli.no_direct or (sd is not None and not sd.direct),
    )
    expand = cli.expand or (sd.expand if sd and sd.expand is not None else cfg.expand)
    return Target(label, uri, opts, expand)


def resolve(names: List[str], groups: List[str], cfg: Config, cli: CliAuth,
            pw: Optional[PasswordSource] = None, environ: Optional[Dict[str, str]] = None) -> List[Target]:
    """CLI 인자(서버 이름 · 그룹 이름 · URI)와 -g 그룹을 접속 대상 목록으로 바꾼다."""
    env = os.environ if environ is None else environ
    pw = pw or PasswordSource(environ=env)
    all_groups = cfg.groups()
    picked: List[Tuple[Optional[str], str]] = []  # (설정 이름, URI)

    def add_name(n: str) -> None:
        if n in cfg.servers:
            picked.append((n, cfg.servers[n].uri))
        elif n in all_groups:
            picked.extend((m, cfg.servers[m].uri) for m in all_groups[n])
        else:
            known = ", ".join(list(cfg.servers) + [f"@{g}" for g in all_groups]) or "none"
            raise ConfigError(f"unknown server or group '{n}' (configured: {known}; see --list)")

    for n in names:
        if "://" in n:
            picked.append((None, n))
        else:
            add_name(n[1:] if n.startswith("@") else n)
    for g in groups:
        if g not in all_groups:
            raise ConfigError(f"unknown group '{g}' (groups: {', '.join(all_groups) or 'none'})")
        add_name(g)
    if not picked:
        if cfg.default:
            for n in cfg.default:
                add_name(n)
        else:
            picked.append((None, env.get("MONGODB_URI", "mongodb://localhost:27017")))

    seen = set()
    out = []
    for label, uri in picked:
        if (label, uri) in seen:
            continue
        seen.add((label, uri))
        out.append(_target(label, uri, cfg.servers.get(label) if label else None, cfg, cli, pw))
    return out

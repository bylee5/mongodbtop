"""설정 파일, 접속 대상·비밀번호 결정, 멤버 URI 테스트."""

import pytest

from mongotop import config
from mongotop.collector import member_uri
from mongotop.config import CliAuth, ConfigError, PasswordSource
from mongotop.model import Mode

INI = """
[defaults]
interval = 5
user = monitor
auth_db = admin
default = stage

[server:prod-rs]
uri = mongodb://db1:27017,db2:27017/?replicaSet=rs0
groups = prod
expand = members

[server:prod-analytics]
uri = mongodb://db9:27017
groups = prod, analytics
password_env = ANALYTICS_PW

[server:stage]
uri = mongodb://admin:inline@stage-db:27017
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "m.ini"
    p.write_text(INI, encoding="utf-8")
    return config.load(str(p))


def no_prompt(_):
    raise AssertionError("must not prompt")


def resolve(cfg, names=(), groups=(), cli=None, env=None, prompt=no_prompt):
    env = {} if env is None else env
    pw = PasswordSource(prompt=prompt, environ=env, use_keyring=False)
    return config.resolve(list(names), list(groups), cfg, cli or CliAuth(), pw=pw, environ=env)


def test_load(cfg):
    assert list(cfg.servers) == ["prod-rs", "prod-analytics", "stage"]
    assert cfg.interval == 5.0
    assert cfg.groups() == {"prod": ["prod-rs", "prod-analytics"], "analytics": ["prod-analytics"]}
    assert cfg.servers["prod-rs"].expand is True
    assert "prod-analytics" in config.describe(cfg)
    assert "inline" not in config.describe(cfg), "passwords masked in --list"


def test_missing_default_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("MONGOTOP_CONFIG", raising=False)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    assert config.load().servers == {}
    with pytest.raises(ConfigError):
        config.load(str(tmp_path / "nope.ini"))


@pytest.mark.parametrize("body", [
    "[server:x]\nuri = mongodb://h\npassword = secret\n",
    "[server:x]\nuri = mongodb://h\nusr = typo\n",
    "[server:x]\nuri = http://h\n",
    "[whatever]\n",
    "[defaults]\nmode = Z\n",
])
def test_bad_config(tmp_path, body):
    p = tmp_path / "bad.ini"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load(str(p))


def test_resolve_names_groups_and_uris(cfg):
    env = {"MONGOTOP_PASSWORD_PROD_RS": "rs-pw", "ANALYTICS_PW": "an-pw"}
    ts = resolve(cfg, groups=["prod"], env=env)
    assert [t.label for t in ts] == ["prod-rs", "prod-analytics"]
    rs, an = ts
    assert rs.expand and not an.expand
    assert (rs.opts.username, rs.opts.password, rs.opts.auth_db) == ("monitor", "rs-pw", "admin")
    assert an.opts.password == "an-pw", "password_env wins over the generic variable"

    # @그룹, 그룹 이름 그대로, URI 섞기, 중복 제거
    ts = resolve(cfg, names=["@analytics", "prod-analytics", "mongodb://x:1"], env={"MONGODB_PASSWORD": "g"})
    assert [t.label for t in ts] == ["prod-analytics", None]
    assert ts[1].opts.password == "g"

    with pytest.raises(ConfigError):
        resolve(cfg, names=["nope"])
    with pytest.raises(ConfigError):
        resolve(cfg, groups=["nope"])


def test_default_target_and_inline_password(cfg):
    (t,) = resolve(cfg)  # [defaults] default = stage
    assert t.label == "stage"
    # URI에 비밀번호가 있으면 찾지도 묻지도 않는다
    assert t.opts.username is None and t.opts.password is None


def test_no_config_falls_back_to_env_uri():
    (t,) = resolve(config.Config(), env={"MONGODB_URI": "mongodb://e:1"})
    assert t.uri == "mongodb://e:1" and t.opts.username is None


def test_prompt_once_per_user(cfg):
    asked = []

    def prompt(msg):
        asked.append(msg)
        return "typed"

    ts = resolve(cfg, groups=["prod"], prompt=prompt)
    assert [t.opts.password for t in ts] == ["typed", "typed"]
    assert len(asked) == 1


def test_cli_overrides(cfg):
    cli = CliAuth(username="root", password="pw", auth_db="other", no_direct=True, expand=True)
    (t,) = resolve(cfg, names=["stage"], cli=cli)
    assert (t.opts.username, t.opts.password, t.opts.auth_db) == ("root", "pw", "other")
    assert t.opts.no_direct and t.expand


def test_member_uri():
    u = "mongodb://u:p%40ss@db1:27017,db2:27017/admin?replicaSet=rs0&tls=true&directConnection=false"
    assert member_uri(u, "db2:27017") == "mongodb://u:p%40ss@db2:27017/admin?tls=true"
    assert member_uri("mongodb://a:1/?replicaSet=x", "b:2") == "mongodb://b:2/"
    assert member_uri("mongodb+srv://c.example.net/?retryWrites=true", "n1:27017", {"tls": "true"}) \
        == "mongodb://n1:27017/?retryWrites=true&tls=true"
    assert member_uri("mongodb://h?tls=false", "n:1", {"tls": "true"}) == "mongodb://n:1/?tls=false"

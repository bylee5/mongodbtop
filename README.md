# mongotop

[innotop](https://github.com/innotop/innotop)에서 영감을 받은, **Python으로 작성한 MongoDB용 실시간 모니터링 TUI**입니다.
innotop처럼 대문자 키 하나로 모드를 전환하고, 실행 중인 작업을 보고, 오래 걸리는 작업을 바로 kill할 수 있습니다.
TSV를 출력하는 비대화형(batch) 모드도 있습니다.

```
 mongotop  [Q] Query List  │ server 1/1: localhost:27017  │ every 1s  │ 13:59:35
a97b9adcd89f │ mongod 8.0.3 │ PRIMARY rs0 │ up 3m28s │ conns 25 (active 9, avail 203.5k) │ mem 331.0M
ops/s 615 │ ins 299 qry 262 upd 51.9 del 0 gm 0 cmd 1.4 │ repl 0 │ net in 191.4K/s out 382.1K/s │ new conns 0/s
latency r 0.33ms w 4.60ms c 1.09ms │ queue r/w 0/0 active r/w 1/0 │ cache 0.9% dirty 0.6% │ txn open 0 active 0 inactive 0
docs/s ret 1322 ins 299 upd 53.3 del 0 │ scanned/s keys 0 docs 2.55M │ req/s 615 │ txn/s commit 0 abort 0 │ asserts/s 0
 3 operations (8 background/idle hidden; i = show) ──────────────────────────────────────────────────────────────
Opid         Time▼ Op        Namespace                Client                App            Plan           Yld Command
97281        2m26s query     shop.orders              127.0.0.1:36420       mongosh 2.14.2 COLLSCAN      1461 {"find":"orders","filter":{"$where":…
251083        0.0s insert    shop.orders              127.0.0.1:40746       mongosh 2.14.2                  0 {"insert":"orders","documents":[…
```

## 모드

| 키 | 모드 | 데이터 소스 | innotop 대응 |
|---|---|---|---|
| `Q` | 쿼리 목록 — 실행 중인 작업 | `$currentOp` | Query List |
| `T` | 열린 다중 문서 트랜잭션 (idle 포함) | `$currentOp` (`idleSessions`) | InnoDB Transactions |
| `L` | 락 / 래치 / flow control 대기 작업 | `$currentOp` | Locks / Deadlocks |
| `C` | 커맨드별 누적·초당 처리량, 실패율 | `serverStatus.metrics.commands` | Command Summary |
| `S` | 갱신마다 한 줄씩 쌓이는 상태 추이 (vmstat 스타일) | `serverStatus` | Variables & Status |
| `B` | WiredTiger 캐시, eviction, 티켓 | `serverStatus.wiredTiger` / `queues` | InnoDB Buffers |
| `R` | 레플리카셋 멤버, 지연(lag), oplog window | `replSetGetStatus`, `local.oplog.rs` | Replication Status |
| `O` | 컬렉션별 부하 (시간 점유율, ops/s) | `top` | Open Tables |
| `D` | 데이터베이스별 크기 | `listDatabases` + `dbStats` (10초 캐시) | — |

상단 헤더는 모든 모드에서 공통으로 표시됩니다: 호스트/버전/역할, ops/s (insert·query·update·delete·getmore·command),
복제 ops, 네트워크, 평균 지연(opLatencies), 글로벌 락 큐, 캐시 사용률/dirty 비율, 트랜잭션, 문서/스캔 처리량, assert 등.
모든 초당 값은 **서버 시계(`uptimeMillis`) 기준 두 샘플의 차이**로 계산하므로 네트워크 지연에 영향받지 않고, 서버 재시작도 감지합니다.

## 키

| 키 | 동작 |
|---|---|
| `Q T L C S B R O D` | 모드 전환 (같은 키를 다시 누르면 즉시 새로고침) |
| `↑ ↓ PgUp PgDn Home End` | 행 선택 |
| `Enter` / `e` | 선택 행의 원본 문서(JSON) 보기 |
| `k` / `x` | 선택한 작업 kill (`killOp`). **T 모드에서는 `killSessions`로 트랜잭션 중단.** `y`로 확인 |
| `f` 또는 `/` | 부분 문자열 필터, `c` 필터 해제 |
| `<` `>` (`←` `→`) | 정렬 컬럼 변경, `r` 정렬 방향 반전 |
| `i` | idle 연결 / 백그라운드 작업 / 0인 행 표시 토글 |
| `s` | 갱신 주기 변경, `space` 즉시 갱신 |
| `p` | 일시정지 / 재개 |
| `n` / `N` / `Tab` | 다음 / 이전 서버 |
| `?` | 도움말, `q` / `Ctrl-C` 종료 |

innotop이 Sleep 스레드를 기본으로 숨기듯, Q 모드는 드라이버의 대기형 `hello`(maxAwaitTimeMS), 클라이언트가 없는 내부 스레드
(Checkpointer 등), oplog tailing getMore, 그리고 mongotop 자신의 연결을 기본으로 숨깁니다. `i`로 표시할 수 있습니다.

색상: 빨강 = 락 대기·멤버 다운·실패 발생, 노랑 = 오래 실행(10초+)·복제 지연(10초+)·캐시 압박, 회색 = 비활성.

## 사용법

```sh
mongotop                                           # 설정의 default, 없으면 mongodb://localhost:27017
mongotop prod-rs                                   # 설정 파일에 등록한 서버 이름
mongotop -g prod                                   # 그룹 전체, n 으로 전환
mongotop prod-rs stage mongodb://db7:27017         # 이름과 URI를 섞어서
mongotop mongodb://db1:27017/?replicaSet=rs0 --expand   # 레플리카셋 멤버마다 따로
mongotop --list                                    # 등록된 서버 목록
mongotop mongodb://user:pw@db1:27017 -m R          # 복제 상태부터 시작
mongotop -u admin -p secret --auth-db admin        # URI 대신 옵션으로 인증
MONGODB_URI=mongodb+srv://... mongotop             # 환경변수

# 비대화형(batch) 모드: TSV 출력. 스크립트/로그 수집용
mongotop -b -m S -c 10 -i 1     # vmstat 처럼 1초마다 한 줄, 10회
mongotop -b -m Q                # 현재 실행 중인 작업 1회 덤프
mongotop -b -m O -c 0 -i 5      # 5초마다 컬렉션 부하, 무한 반복 (batch는 첫 서버만)
```

옵션: `-i/--interval` 초 단위 갱신 주기(기본 2), `-m/--mode` 시작 모드,
`--idle` idle 표시로 시작, `-g/--group`, `--config`, `--list`, `--expand`,
`-b/--batch`, `-c/--count`(0 = 무한), `--no-direct`.

## 여러 서버 관리

### 설정 파일 `~/.mongotop.ini`

경로는 `--config` 또는 환경변수 `MONGOTOP_CONFIG`로 바꿀 수 있습니다. 파일이 없으면 지금처럼 URI로만 동작합니다.

```ini
[defaults]
interval = 2
user = monitor            ; 모든 서버의 기본 사용자
auth_db = admin
default = prod            ; 인자 없이 실행했을 때 접속할 서버 또는 그룹 (여러 개는 쉼표로)

[server:prod-rs]
uri = mongodb://db1:27017,db2:27017,db3:27017/?replicaSet=rs0
groups = prod
expand = members          ; 멤버마다 따로 모니터링

[server:prod-analytics]
uri = mongodb://db9:27017
groups = prod, analytics
password_env = ANALYTICS_PW   ; 이 서버의 비밀번호를 읽을 환경변수

[server:stage]
uri = mongodb://stage-db:27017
user = stage_monitor
```

- 서버 키: `uri`(필수), `groups`, `user`, `auth_db`, `password_env`, `expand`, `direct`(`no`면 `--no-direct`와 같음)
- `[defaults]` 키: `interval`, `mode`, `idle`, `user`, `auth_db`, `password_env`, `default`, `expand`
- 모르는 키가 있으면 오타로 보고 오류를 냅니다. 인자로 `@prod`처럼 쓰면 그룹을 명시할 수 있습니다.

### 비밀번호

**설정 파일에는 비밀번호를 쓰지 않습니다**(`password` 키가 있으면 오류). 그래서 설정 파일을 팀과 공유하거나 git에 넣어도 됩니다.
사용자 이름이 정해졌는데 비밀번호가 없을 때 찾는 순서는 다음과 같습니다.

1. `-p`
2. 서버의 `password_env`가 가리키는 환경변수
3. `MONGOTOP_PASSWORD_<NAME>` — 이름을 대문자로, 영숫자 외 문자는 `_`로 (`prod-rs` → `MONGOTOP_PASSWORD_PROD_RS`)
4. `[defaults] password_env`, 그다음 `MONGODB_PASSWORD`
5. OS 키체인 — `pip install "mongotop[keyring]"` 후 `keyring set mongotop prod-rs` (서버 이름 대신 사용자 이름으로 저장해도 됨)
6. 터미널에서 입력 (같은 사용자는 한 번만 묻습니다)

URI에 비밀번호가 들어 있으면 그대로 씁니다.

### 레플리카셋 멤버 펼치기

mongotop은 innotop처럼 *노드 하나*를 봅니다. 레플리카셋 URI를 그대로 주면 프라이머리만 보게 되는데,
`expand = members` 또는 `--expand`를 쓰면 `hello`로 멤버 목록(arbiter 제외)을 받아 **멤버마다 직접 연결**을 만듭니다.
화면에는 `prod-rs/db2:27017`처럼 표시되고, 세컨더리의 느린 쿼리나 지연도 각각 볼 수 있습니다.
인증 정보와 URI 옵션(TLS 등)은 그대로 이어받고, `mongodb+srv://`는 `tls=true`와 TXT의 `authSource`를 붙입니다.
멤버 목록은 시작할 때 한 번 가져옵니다. 시작할 때 멤버를 찾지 못하면 경고를 내고 시드 연결만 씁니다.

### 필요 권한

`clusterMonitor` 역할이면 충분합니다(`serverStatus`, `$currentOp` allUsers, `replSetGetStatus`, `top`, `dbStats`).
kill 기능에는 `killop` / `killAnySession` 권한(예: `clusterManager` 또는 `hostManager`)이 필요합니다.

## 설치

Python 3.9 이상이 필요합니다. 의존성은 `pymongo`, 그리고 Windows에서만 `windows-curses`입니다.

```sh
pip install .                 # mongotop 명령 설치
python -m mongotop --help     # 설치 없이 실행 (의존성은 별도 설치)

# 개발용
python -m venv .venv
.venv/bin/pip install -e ".[test]"        # Windows: .venv\Scripts\pip ...
```

- `mongodb+srv://`(Atlas)는 pymongo가 함께 설치하는 `dnspython`으로 바로 동작하며, TLS도 별도 빌드 옵션이 필요 없습니다.

## 부하 생성 (데모용)

서버가 한가하면 Q·T 모드에 보일 작업이 없습니다. 테스트 서버라면 부하 스크립트로 화면을 채울 수 있습니다.

```sh
python scripts/loadgen.py mongodb://localhost:27017   # Ctrl-C로 중지
```

insert/update/find 반복, 몇~수십 초짜리 COLLSCAN 쿼리(`$where sleep`), 20초간 쉬는 트랜잭션을 만듭니다.
`shop.orders`에 데이터를 쓰므로 운영 서버에는 쓰지 마세요.

## 테스트

```sh
pytest                                                      # 단위 + 렌더링 테스트
MONGOTOP_TEST_URI=mongodb://localhost:27017 pytest -s      # 실서버 테스트 포함
```

실서버 테스트(`tests/test_live.py`)는 모든 모드 수집, `killOp`로 슬로우 쿼리 종료, `killSessions`로 idle 트랜잭션 중단을 검증하고,
`test_live_screens`는 실데이터로 모든 모드 화면을 렌더링해 출력합니다(`-s`). 환경 변수가 없으면 건너뜁니다. 테스트용 서버 예:

```sh
docker run -d --name mongo-test -p 27017:27017 mongo:8.0 --replSet rs0
docker exec mongo-test mongosh --eval 'rs.initiate({_id:"rs0",members:[{_id:0,host:"localhost:27017"}]})'
```

## 구조

```
mongotop/
  cli.py        CLI(argparse), 접속 오류 안내, batch 모드
  config.py     설정 파일(~/.mongotop.ini), 서버·그룹 이름 해석, 비밀번호 찾기
  collector.py  MongoDB 접속·수집, 레플리카셋 멤버 펼치기, kill, 백그라운드 수집 스레드
  model.py      모드 정의, $currentOp / replSetGetStatus / top / dbStats 파싱
  metrics.py    serverStatus 게이지와 초당 변화율 계산
  app.py        앱 상태, 서버별 상태/이력, 키 처리 (터미널 I/O 없음)
  views.py      상태 → 범용 테이블 (TUI와 batch TSV가 공유), 필터·정렬
  ui.py         화면 버퍼 렌더링과 curses 출력·이벤트 루프
  util.py       BSON 값 추출, 숫자/바이트/시간 포맷
tests/          pytest 테스트
```

수집은 별도 스레드에서 돌고 UI는 큐로 스냅샷을 받으므로, 느린 서버에서도 키 입력이 막히지 않습니다.
서버별 상태(S 모드 이력 포함)는 서버를 전환해도 유지됩니다. 화면은 먼저 문자 버퍼에 그린 뒤 curses로 출력하므로
렌더링을 터미널 없이 테스트할 수 있습니다.

## 라이선스

MIT

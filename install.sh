#!/usr/bin/env bash
# mongodbtop 설치 / 업데이트 / 제거 (Linux, macOS)
#
#   ./install.sh                 설치 (저장소 안에서 실행하면 그 소스로 설치)
#   ./install.sh --update        git pull 후 다시 설치
#   ./install.sh --uninstall     명령 링크와 가상환경 제거 (~/.mongodbtop.ini는 남김)
#
# 저장소 밖에서 실행하면 --repo를 --dir에 clone한 뒤 설치한다.
# 설치 결과: <dir>/.venv 에 가상환경, <bin-dir>/mongodbtop 에 실행 링크.
set -euo pipefail

REPO="${MONGODBTOP_REPO:-https://github.com/bylee5/mongodbtop.git}"
BRANCH="main"
DIR="${MONGODBTOP_HOME:-$HOME/.local/share/mongodbtop}"
BIN_DIR="$HOME/.local/bin"
PYTHON=""
EXTRAS=""
ACTION="install"

usage() {
    cat <<EOF
usage: $0 [--update | --uninstall] [options]

options:
  --repo URL       clone할 저장소 (기본 $REPO, 환경변수 MONGODBTOP_REPO)
                   SSH 예: git@github.com:bylee5/mongodbtop.git
  --branch NAME    브랜치 또는 태그 (기본 $BRANCH)
  --dir DIR        저장소 밖에서 실행할 때 소스를 둘 위치 (기본 $DIR, 환경변수 MONGODBTOP_HOME)
  --bin-dir DIR    mongodbtop 링크를 만들 위치 (기본 $BIN_DIR)
  --python PATH    사용할 Python 3.9+ (기본: 자동 탐색)
  --keyring        OS 키체인 비밀번호 조회용 keyring 패키지도 설치
  -h, --help       도움말
EOF
}

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --update)    ACTION="update" ;;
        --uninstall) ACTION="uninstall" ;;
        --repo)      REPO="${2:?--repo needs a value}"; shift ;;
        --branch)    BRANCH="${2:?--branch needs a value}"; shift ;;
        --dir)       DIR="${2:?--dir needs a value}"; shift ;;
        --bin-dir)   BIN_DIR="${2:?--bin-dir needs a value}"; shift ;;
        --python)    PYTHON="${2:?--python needs a value}"; shift ;;
        --keyring)   EXTRAS="[keyring]" ;;
        -h|--help)   usage; exit 0 ;;
        *)           usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

# 이 스크립트가 mongodbtop 저장소 안에 있으면 그 소스를 쓴다.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
DIR_ABS="$(cd "$DIR" 2>/dev/null && pwd || echo "$DIR")"
if [ -n "$SCRIPT_DIR" ] && [ "$SCRIPT_DIR" != "$DIR_ABS" ] \
    && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/mongodbtop" ]; then
    SRC="$SCRIPT_DIR"
    MANAGED=0          # 사용자가 직접 받은 소스: 제거할 때 소스는 지우지 않는다
else
    SRC="$DIR"
    MANAGED=1          # 설치 스크립트가 clone한 소스
fi
VENV="$SRC/.venv"
LINK="$BIN_DIR/mongodbtop"

ours_link() {          # 이 설치 스크립트가 만든 링크인가 (소스 위치는 상관없음)
    [ -L "$LINK" ] || return 1
    case "$(readlink "$LINK")" in
        */.venv/bin/mongodbtop|*/.venv/Scripts/mongodbtop.exe) return 0 ;;
    esac
    return 1
}

venv_bin() {           # Git Bash(Windows)에서 시험할 때는 Scripts/
    if [ -d "$VENV/Scripts" ]; then echo "$VENV/Scripts"; else echo "$VENV/bin"; fi
}

py_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; }

find_python() {
    local c
    if [ -n "$PYTHON" ]; then
        command -v "$PYTHON" >/dev/null 2>&1 || die "--python $PYTHON not found"
        py_ok "$PYTHON" || die "$PYTHON is older than Python 3.9"
        command -v "$PYTHON"
        return 0
    fi
    for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
        if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

fetch_source() {
    if [ "$MANAGED" = 0 ]; then
        if [ "$ACTION" = "update" ]; then
            if [ -d "$SRC/.git" ]; then
                say "updating source in $SRC"
                git -C "$SRC" pull --ff-only
            else
                warn "$SRC is not a git checkout; reinstalling the files as they are"
            fi
        fi
        return
    fi
    command -v git >/dev/null 2>&1 || die "git is required (e.g. apt install git / yum install git)"
    if [ -d "$SRC/.git" ]; then
        say "updating source in $SRC"
        git -C "$SRC" fetch --quiet origin "$BRANCH"
        git -C "$SRC" checkout --quiet "$BRANCH"
        git -C "$SRC" pull --ff-only --quiet origin "$BRANCH"
    elif [ -e "$SRC" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
        die "$SRC exists and is not a mongodbtop checkout; choose another --dir"
    else
        say "cloning $REPO ($BRANCH) into $SRC"
        mkdir -p "$(dirname "$SRC")"
        git clone --quiet --branch "$BRANCH" "$REPO" "$SRC" || die "git clone failed.
  Check network access to github.com (proxy / firewall) and the --repo URL."
    fi
}

install() {
    local py
    py="$(find_python)" || die "Python 3.9 or newer is required (e.g. apt install python3 python3-venv)"
    say "using $py ($("$py" -c 'import sys; print(sys.version.split()[0])'))"

    # 같은 이름의 다른 프로그램은 덮어쓰지 않는다.
    if { [ -e "$LINK" ] || [ -L "$LINK" ]; } && ! ours_link; then
        die "$LINK already exists and was not created by this installer.
  Remove it yourself, or install the link elsewhere with --bin-dir DIR"
    fi

    fetch_source

    if [ ! -x "$(venv_bin)/python" ] && [ ! -x "$(venv_bin)/python.exe" ]; then
        say "creating virtualenv $VENV"
        rm -rf "$VENV"
        "$py" -m venv "$VENV" || die "cannot create a virtualenv.
  On Debian/Ubuntu install the venv module: apt install python3-venv"
    fi
    local bin
    bin="$(venv_bin)"
    say "installing mongodbtop$EXTRAS"
    "$bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$bin/python" -m pip install --quiet --disable-pip-version-check -e "$SRC$EXTRAS"

    local exe="$bin/mongodbtop"
    [ -e "$exe" ] || exe="$bin/mongodbtop.exe"
    mkdir -p "$BIN_DIR"
    ln -sf "$exe" "$LINK"
    say "linked $LINK -> $exe"
    "$LINK" --version

    local first
    first="$(command -v mongodbtop 2>/dev/null || true)"
    if [ -n "$first" ] && [ "$first" != "$LINK" ]; then
        warn "another mongodbtop comes first on PATH: $first
  Run $LINK directly, or put $BIN_DIR earlier in PATH"
    fi
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR is not on PATH. Add this to ~/.bashrc (or ~/.profile):
  export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac

    if [ ! -f "$HOME/.mongodbtop.ini" ] && [ -z "${MONGODBTOP_CONFIG:-}" ]; then
        echo
        echo "No server list yet. To create one:"
        echo "  cp $SRC/mongodbtop.ini.example ~/.mongodbtop.ini && \${EDITOR:-vi} ~/.mongodbtop.ini"
        echo "  mongodbtop --list"
    fi
    echo
    say "done. Update later with: $SRC/install.sh --update"
}

uninstall() {
    if ours_link; then
        rm -f "$LINK"
        say "removed $LINK"
    elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
        warn "$LINK was not created by this installer; left in place"
    fi
    if [ "$MANAGED" = 1 ]; then
        if [ -d "$SRC" ]; then
            rm -rf "$SRC"
            say "removed $SRC"
        fi
    elif [ -d "$VENV" ]; then
        rm -rf "$VENV"
        say "removed $VENV (source left in place)"
    fi
    [ -f "$HOME/.mongodbtop.ini" ] && say "kept ~/.mongodbtop.ini"
    return 0
}

case "$ACTION" in
    install|update) install ;;
    uninstall)      uninstall ;;
esac

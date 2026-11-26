#!/usr/bin/env bash
# Amazon Linux 2023 (x86_64) 용 RPM 을 Docker 컨테이너에서 만든다.
#
#   packaging/rpm/build.sh
#
# 결과: dist/mongodbtop-<version>-1.amzn2023.x86_64.rpm
# 필요: docker (인터넷 연결: amazonlinux 이미지, dnf, PyPI)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${AL2023_IMAGE:-amazonlinux:2023}"

mkdir -p "$ROOT/dist"
# Git Bash 에서 /src 같은 경로가 Windows 경로로 바뀌지 않게 한다.
export MSYS_NO_PATHCONV=1
# --platform: ARM 호스트(Apple Silicon 등)에서도 x86_64 컨테이너로 빌드한다.
docker run --rm --platform linux/amd64 \
    -v "$ROOT:/src:ro" -v "$ROOT/dist:/out" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "$IMAGE" bash -euo pipefail -c '
dnf install -y -q rpm-build python3-pip tar >/dev/null
VERSION=$(sed -n "s/^__version__ = \"\(.*\)\"/\1/p" /src/mongodbtop/__init__.py)
echo "==> mongodbtop $VERSION, $(python3 -V), $(uname -m)"

# 소스 복사 (빌드 산출물 제외)
mkdir -p /build/src
tar -C /src --exclude=.git --exclude=.venv --exclude=build --exclude=dist \
    --exclude="*.egg-info" --exclude=__pycache__ -cf - . | tar -C /build/src -xf -

echo "==> collecting dependencies"
STAGE=/build/stage
PIP="python3 -m pip install -q --disable-pip-version-check --target $STAGE/lib"
# pymongo 와 dnspython: 이 컨테이너의 python3(3.9) / x86_64 용 wheel
$PIP --only-binary=:all: "pymongo>=4.6"
# mongodbtop 자체 (순수 Python)
$PIP --no-deps /build/src

echo "==> rpmbuild"
rpmbuild -bb --quiet \
    --define "_topdir /build/rpm" \
    --define "pkg_version $VERSION" \
    --define "_stagedir $STAGE" \
    --define "_srcdir /build/src" \
    /build/src/packaging/rpm/mongodbtop.spec
cp /build/rpm/RPMS/x86_64/*.rpm /out/
chown "$HOST_UID:$HOST_GID" /out/*.rpm 2>/dev/null || true
ls -l /out/*.rpm
'

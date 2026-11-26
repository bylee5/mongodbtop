# mongodbtop RPM (Amazon Linux 2023)
#
# 직접 rpmbuild 하지 말고 packaging/rpm/build.sh 를 쓰세요.
# build.sh 가 pymongo 등 의존성을 _stagedir/lib 에 미리 받아 두고
# 이 spec 은 그것을 그대로 담는다 (AL2023 저장소에는 pymongo 패키지가 없다).
#
# 필요한 정의: pkg_version, _stagedir, _srcdir

%global debug_package %{nil}
%global _build_id_links none
# 미리 컴파일된 의존성(.so, .pyc)을 그대로 담는다: strip / 바이트컴파일 / shebang 수정 생략
%global __os_install_post %{nil}
%global appdir %{_libdir}/mongodbtop

Name:           mongodbtop
Version:        %{pkg_version}
Release:        1%{?dist}
Summary:        innotop-style real-time TUI monitor for MongoDB
# mongodbtop: MIT, 포함한 pymongo: Apache-2.0, dnspython: ISC
License:        MIT AND Apache-2.0 AND ISC
URL:            https://github.com/bylee5/mongodbtop
ExclusiveArch:  x86_64

# 포함한 pymongo C 확장은 cp39 용이다 (AL2023 의 python3 = 3.9).
Requires:       python(abi) = 3.9
# 포함한 모듈을 시스템 python 패키지로 내보내지 않는다.
AutoReqProv:    no

%description
mongodbtop is an innotop-style real-time monitoring TUI for MongoDB:
running operations (with kill), transactions, lock waits, command rates,
WiredTiger cache, replication, collection activity and an all-servers overview.
It is unrelated to the mongotop tool in MongoDB Database Tools and does not
conflict with it.

%prep
# 소스는 _srcdir, 의존성은 _stagedir 에서 바로 가져온다.

%build
# 빌드할 것 없음

%install
mkdir -p %{buildroot}%{appdir} %{buildroot}%{_bindir}
cp -a %{_stagedir}/lib/. %{buildroot}%{appdir}/
rm -rf %{buildroot}%{appdir}/bin

cat > %{buildroot}%{_bindir}/mongodbtop <<'EOF'
#!/usr/bin/python3 -s
import sys
sys.path.insert(0, "%{appdir}")
from mongodbtop.cli import main
sys.exit(main())
EOF
chmod 0755 %{buildroot}%{_bindir}/mongodbtop

install -Dm0644 %{_srcdir}/README.md %{buildroot}%{_docdir}/%{name}/README.md
install -Dm0644 %{_srcdir}/mongodbtop.ini.example %{buildroot}%{_docdir}/%{name}/mongodbtop.ini.example
install -Dm0644 %{_srcdir}/LICENSE %{buildroot}%{_licensedir}/%{name}/LICENSE

%files
%{_bindir}/mongodbtop
%{appdir}
%doc %{_docdir}/%{name}/README.md
%doc %{_docdir}/%{name}/mongodbtop.ini.example
%license %{_licensedir}/%{name}/LICENSE

%changelog
* Thu Nov 26 2026 bylee5 - 0.1.0-1
- First RPM package

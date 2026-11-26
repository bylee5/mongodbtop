"""PyInstaller 진입점: 패키지 밖에서 실행되므로 상대 import 없이 cli.main을 부른다."""

import sys

from mongodbtop.cli import main

if __name__ == "__main__":
    sys.exit(main())

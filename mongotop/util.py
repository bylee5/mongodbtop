"""타입이 느슨한 BSON 문서에서 값을 꺼내고, innotop처럼 숫자를
(짧고 읽기 쉽게) 포맷하는 도우미 함수들."""

from __future__ import annotations

import datetime as _dt
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from bson.timestamp import Timestamp

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_JSON_OPTS = json_util.RELAXED_JSON_OPTIONS


def get(doc: Any, path: Sequence[str]) -> Any:
    """중첩된 문서를 키 경로를 따라 탐색한다. 없으면 None.

    MongoDB 키에는 공백(WiredTiger 통계)이나 점(`top`의 네임스페이스)이
    들어갈 수 있으므로 점 표기 대신 키 목록을 받는다."""
    cur = doc
    for p in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(p)
    return cur


def dt_ms(d: _dt.datetime) -> int:
    """datetime → epoch 밀리초 (tz 없는 값은 UTC로 본다)."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return (d - _EPOCH) // _dt.timedelta(milliseconds=1)


def to_float(v: Any) -> Optional[float]:
    """숫자로 볼 수 있는 BSON 값을 float으로 변환한다."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Decimal128):
        try:
            return float(v.to_decimal())
        except (ValueError, ArithmeticError):
            return None
    if isinstance(v, _dt.datetime):
        return float(dt_ms(v))
    if isinstance(v, Timestamp):
        return float(v.time)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def opt_num(doc: Any, path: Sequence[str]) -> Optional[float]:
    return to_float(get(doc, path))


def num(doc: Any, path: Sequence[str]) -> float:
    """`path`의 숫자 값. 없으면 0."""
    v = opt_num(doc, path)
    return 0.0 if v is None else v


def boolean(doc: Any, path: Sequence[str]) -> bool:
    v = get(doc, path)
    if isinstance(v, bool):
        return v
    f = to_float(v)
    return f is not None and f != 0.0


def string(doc: Any, path: Sequence[str]) -> str:
    """`path`의 문자열 값 (문자열이 아니면 압축 표기). 없으면 빈 문자열."""
    v = get(doc, path)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return compact(v)


def compact(v: Any) -> str:
    """BSON 값을 한 줄짜리 압축 JSON(relaxed extended JSON)으로 표현한다."""
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):  # bson.Binary 포함
        return bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return v.hex
    return json_util.dumps(
        v, json_options=_JSON_OPTS, separators=(",", ":"), ensure_ascii=False
    )


def pretty(doc: Any) -> str:
    """문서를 보기 좋은 여러 줄 JSON으로 표현한다 (상세 팝업용)."""
    try:
        return json_util.dumps(doc, json_options=_JSON_OPTS, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(doc)


def truncate(s: str, max_len: int) -> str:
    """문자열을 최대 `max_len`자로 줄이고 말줄임표를 붙인다."""
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)] + "…"


def disp(v: float) -> str:
    """정수 값이면 소수점 없이, 아니면 최소 자릿수로 표시한다 (0.0 → "0")."""
    if math.isfinite(v) and v == int(v):
        return str(int(v))
    return repr(v)


def _round_half_away(v: float) -> float:
    return math.copysign(math.floor(abs(v) + 0.5), v)


def fmt_num(v: float) -> str:
    """숫자 포맷: 1234567 -> "1.23M"."""
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.2f}T"
    if a >= 1e9:
        return f"{v / 1e9:.2f}G"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e4:
        return f"{v / 1e3:.1f}k"
    if (math.isfinite(v) and v == math.floor(v)) or a >= 100.0:
        return f"{v:.0f}"
    return f"{v:.1f}"


def fmt_rate(v: float) -> str:
    """초당 값: fmt_num과 같지만 작은 값은 항상 소수점을 보여준다."""
    if v == 0.0:
        return "0"
    if abs(v) < 100.0:
        return f"{v:.1f}"
    return fmt_num(_round_half_away(v))


_UNITS = ("B", "K", "M", "G", "T", "P")


def fmt_bytes(v: float) -> str:
    """바이트 포맷: 12.3M 등."""
    i = 0
    while abs(v) >= 1024.0 and i < len(_UNITS) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{v:.0f}B"
    return f"{v:.1f}{_UNITS[i]}"


def fmt_secs(s: float) -> str:
    """초 단위 시간 포맷: "3d04h", "5h12m", "12m03s", "4.2s"."""
    if s < 0 or not math.isfinite(s):
        return "-"
    t = int(s)
    d, h, m, sec = t // 86400, (t % 86400) // 3600, (t % 3600) // 60, t % 60
    if d > 0:
        return f"{d}d{h:02}h"
    if h > 0:
        return f"{h}h{m:02}m"
    if m > 0:
        return f"{m}m{sec:02}s"
    if s < 10.0:
        return f"{s:.1f}s"
    return f"{sec}s"


def fmt_pct(v: float) -> str:
    return f"{v:.1f}%"

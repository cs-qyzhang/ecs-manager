from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
import re
from typing import Any


def now_iso_utc() -> str:
    """UTC timestamp like 2025-12-16T12:34:56Z (no microseconds)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def format_cmd(cmd: list[str]) -> str:
    """Human-readable command string for display/logging."""
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(c) for c in cmd)


def coerce_value(raw: str) -> Any:
    """Parse common scalar values from `config set key=value`."""
    s = raw.strip()
    low = s.lower()

    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "none"}:
        return None

    # int (avoid surprising octal-like parsing)
    try:
        if s.startswith("0") and len(s) > 1 and s[1].isdigit():
            raise ValueError
        return int(s)
    except ValueError:
        pass

    # float
    try:
        return float(s)
    except ValueError:
        pass

    # json object/array
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass

    return raw


_ZONE_LIKE_1 = re.compile(r"^(.+-\d+)[a-z]$")  # ap-northeast-1c -> ap-northeast-1
_ZONE_LIKE_2 = re.compile(r"^(.+)-[a-z]$")  # cn-hangzhou-i -> cn-hangzhou


def normalize_region_id(value: str) -> tuple[str, str | None]:
    """
    Normalize Aliyun region id.

    Users sometimes mistakenly pass a ZoneId (e.g. ap-northeast-1c, cn-hangzhou-i).
    In that case, return (region_id, original_zone_id). Otherwise return (value, None).
    """
    s = (value or "").strip()
    if not s:
        return s, None

    m = _ZONE_LIKE_1.match(s)
    if m:
        return m.group(1), s
    m = _ZONE_LIKE_2.match(s)
    if m:
        return m.group(1), s
    return s, None



"""时段规则：日期校验、区间重叠与整周期覆盖判断（纯函数，不依赖数据库）。"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Iterable, Mapping

from errors import BusinessError

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: object, field: str = "date") -> str:
    """把输入规范化为 ISO 日期字符串；非法输入抛出 422。"""
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise BusinessError(f"{field} 必须是 YYYY-MM-DD 格式的日期", 422, "invalid_date")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise BusinessError(f"{field} 不是有效日期", 422, "invalid_date")


def validate_period(start: object, end: object) -> tuple[str, str]:
    s = parse_date(start, "start")
    e = parse_date(end, "end")
    if s > e:
        raise BusinessError("开始日期不能晚于结束日期", 422, "invalid_period")
    return s, e


def overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """闭区间重叠判断；ISO 日期字符串可直接按字典序比较。"""
    return start_a <= end_b and start_b <= end_a


def find_busy_conflict(
    busy_intervals: Iterable[Mapping[str, str]], review_start: str, review_end: str
) -> Mapping[str, str] | None:
    """返回与评审时段重叠的第一条忙碌区间；无重叠返回 None。

    评审人必须整个评审周期都可用（任何重叠都视为冲突），才算覆盖该论文。
    """
    for interval in busy_intervals:
        if overlaps(interval["start_date"], interval["end_date"], review_start, review_end):
            return interval
    return None

"""时段规则模块：评审起止窗口与评审人忙碌区间的纯规则实现。

只负责三件事：
1. 解析并规范化 ISO 8601 时间（统一存 UTC，保证字符串比较即时间比较）；
2. 校验论文评审窗口（start < end）；
3. 判定忙碌区间是否与评审窗口重叠（半开区间 [start, end)，端点相接不算冲突）。

不依赖分配事务与 HTTP 层，可独立维护与测试。
"""
from __future__ import annotations

from datetime import datetime, timezone

from errors import BusinessError

STORE_FMT = "%Y-%m-%dT%H:%M:%S+00:00"


def parse_iso_dt(value: str, field: str) -> datetime:
    """解析 ISO 8601 时间；缺少时区按 UTC 处理。非法输入抛 422。"""
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(f"{field} 不能为空", 422, "invalid_datetime")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise BusinessError(f"{field} 必须是 ISO 8601 时间", 422, "invalid_datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def store_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(STORE_FMT)


def parse_window(start: str, end: str) -> tuple[str, str]:
    """解析并校验评审起止，返回可入库的 (start, end) 字符串。"""
    start_dt = parse_iso_dt(start, "review_start")
    end_dt = parse_iso_dt(end, "review_end")
    if start_dt >= end_dt:
        raise BusinessError("评审开始时间必须早于结束时间", 422, "invalid_review_window")
    return store_dt(start_dt), store_dt(end_dt)


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """半开区间重叠判定：[s, e) 相交当且仅当 a_start < b_end 且 b_start < a_end。"""
    return a_start < b_end and b_start < a_end


def conflicting_busy(conn, reviewer_id: str, start: str, end: str):
    """返回评审人在 [start, end) 内重叠的忙碌区间行列表（可能为空）。"""
    return conn.execute(
        "SELECT * FROM busy_periods WHERE reviewer_id=? AND start<? AND end>? ORDER BY start",
        (reviewer_id, end, start),
    ).fetchall()

"""时间规范化工具。"""
from datetime import datetime, timezone


def to_utc_iso(dt: datetime) -> str:
    """归一化为 UTC ISO-8601 字符串（固定 6 位微秒，Z 结尾）。

    无时区的时间按 UTC 处理。固定宽度格式保证字符串字典序与时间序一致，
    因此可以直接用 SQL ORDER BY sampled_at, event_no 完成排序。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc))

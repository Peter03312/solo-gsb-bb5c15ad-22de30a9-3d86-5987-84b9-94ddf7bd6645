"""超温区间计算与差异比对（纯函数，便于单测）。

规则：
- 同设备有效（未撤销）读数按 (采样时间, 事件号) 升序排列；
- 温度严格大于上限的读数为“超限”；
- 连续 >= 2 条超限读数构成一个超温区间；
- 区间起点为首个超限读数，终点为区间后首个不超限读数；
- 若超限一直延续到最后一条读数，则区间未结束，终点为空；
- 区间以起点读数的事件号为唯一标识。

按采样时间生效的温度上限策略：每条读数适用其采样时间所处策略的上限
（生效时间不晚于该读数采样时间的最近一条策略），无策略覆盖的读数
沿用全局默认上限。
"""
from bisect import bisect_right
from typing import Callable, Dict, List, Sequence, Tuple


def _compute(readings: List[dict], exceeds: Callable[[dict], bool]) -> List[dict]:
    """按给定的超限判定函数计算当前全部超温区间（公共实现）。"""
    intervals: List[dict] = []
    run: List[dict] = []

    def close(run: List[dict], end: dict | None) -> dict:
        return {
            "start_event_no": run[0]["event_no"],
            "start_sampled_at": run[0]["sampled_at"],
            "start_temperature": run[0]["temperature"],
            "end_event_no": end["event_no"] if end else None,
            "end_sampled_at": end["sampled_at"] if end else None,
            "end_temperature": end["temperature"] if end else None,
            "reading_count": len(run),
            "max_temperature": max(r["temperature"] for r in run),
        }

    for r in readings:
        if exceeds(r):
            run.append(r)
        else:
            if len(run) >= 2:
                intervals.append(close(run, r))
            run = []
    if len(run) >= 2:
        intervals.append(close(run, None))
    return intervals


def compute_intervals(readings: List[dict], limit: float) -> List[dict]:
    """单一上限：全部读数共用同一超限阈值。"""
    return _compute(readings, lambda r: r["temperature"] > limit)


def applicable_policy(
    sampled_at: str,
    policies: Sequence[Tuple[str, float]],
) -> int:
    """返回 sampled_at 时刻适用策略在（按生效时间升序的）policies 中的下标。

    适用“生效时间不晚于该时刻的最近一条策略”（生效时间相等即生效）；
    返回 -1 表示该时刻无策略覆盖，调用方应沿用全局默认上限。
    生效时间与采样时间同为 UTC 固定宽度 ISO 字符串，可直接按字典序比较。
    """
    return bisect_right([p[0] for p in policies], sampled_at) - 1


def compute_intervals_with_policies(
    readings: List[dict],
    policies: Sequence[Tuple[str, float]],
    default_limit: float,
) -> List[dict]:
    """按采样时间生效的策略上限计算超温区间。

    policies 为 (生效时间, 上限) 列表，按生效时间升序；生效时间与读数
    采样时间同为 UTC 固定宽度 ISO 字符串，可直接按字典序比较。
    每条读数适用生效时间不晚于其采样时间的最近一条策略的上限；
    此前无策略时沿用 default_limit（全局上限）。
    """
    if not policies:
        return compute_intervals(readings, default_limit)
    effective_times = [p[0] for p in policies]
    limits = [p[1] for p in policies]

    def exceeds(r: dict) -> bool:
        idx = bisect_right(effective_times, r["sampled_at"]) - 1
        limit = limits[idx] if idx >= 0 else default_limit
        return r["temperature"] > limit

    return _compute(readings, exceeds)


def diff_intervals(before: List[dict], after: List[dict]) -> Dict[str, List[dict]]:
    """以起点事件号为键比对前后区间集合。

    - added:       新出现的事件号；
    - invalidated: 消失的事件号（起点改变时旧区间即在此体现）；
    - changed:     事件号相同但内容不同（如终点、峰值变化），取变更后版本。
    """
    before_by_key = {i["start_event_no"]: i for i in before}
    after_by_key = {i["start_event_no"]: i for i in after}
    added = [i for k, i in after_by_key.items() if k not in before_by_key]
    invalidated = [i for k, i in before_by_key.items() if k not in after_by_key]
    changed = [
        after_by_key[k]
        for k in before_by_key.keys() & after_by_key.keys()
        if before_by_key[k] != after_by_key[k]
    ]
    return {"added": added, "changed": changed, "invalidated": invalidated}

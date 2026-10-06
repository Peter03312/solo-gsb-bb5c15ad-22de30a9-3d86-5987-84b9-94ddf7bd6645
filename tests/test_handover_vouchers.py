"""冷链交接温控凭证端到端测试。

覆盖：时间窗左闭右开、未撤销读数按 (采样时间, 事件号) 排序、逐条冻结
事件号 / 时间 / 温度 / 当时适用策略号与上限（无策略用全局上限）/ 是否
严格超限，读数数 / 超限数 / 最高温度汇总（空窗为 null）；同设备同单号
等价时区同窗重试原样返回首次凭证，异窗冲突；凭证生成后补报、更正、撤销
与策略变化均不改写已存凭证，新单号按当前数据生成；非法时间与结束不晚于
开始拒绝；按单号 / 列表查询、设备隔离，以及既有读数 / 区间行为不受影响。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

DEVICE = "truck-001"
W0 = "2026-10-01T08:00:00Z"
W1 = "2026-10-01T09:00:00Z"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TEMP_LIMIT", "8.0")
    with TestClient(app) as c:
        yield c


def post(client, event_no, ts, temp, device=DEVICE):
    return client.post(
        f"/devices/{device}/readings",
        json={"event_no": event_no, "sampled_at": ts, "temperature": temp},
    )


def put_policy(client, policy_no, effective_from, temp_limit, device=DEVICE):
    return client.post(
        f"/devices/{device}/limit-policies",
        json={
            "policy_no": policy_no,
            "effective_from": effective_from,
            "temp_limit": temp_limit,
        },
    )


def create_voucher(client, handover_no, start, end, device=DEVICE):
    return client.post(
        f"/devices/{device}/handover-vouchers",
        json={
            "handover_no": handover_no,
            "window_start": start,
            "window_end": end,
        },
    )


# ---------------------------------------------------------------- 冻结内容与窗口

def test_voucher_freezes_window_readings_with_policy_and_flags(client):
    # 全局上限 8.0；策略 P1 自 08:05 起把上限降到 5.0
    post(client, "E0", "2026-10-01T07:59:00Z", 10.0)   # 窗外
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)    # 全局上限 → 超限
    post(client, "E2", "2026-10-01T08:10:00Z", 7.0)    # P1 → 超限
    post(client, "E3", "2026-10-01T08:20:00Z", 6.0)    # P1 → 超限
    post(client, "E4", "2026-10-01T08:30:00Z", 5.0)    # P1，等于上限 → 不超限
    post(client, "E5", "2026-10-01T09:00:00Z", 99.0)   # 恰为窗右端点（窗外）
    assert put_policy(client, "P1", "2026-10-01T08:05:00Z", 5.0).status_code == 201

    r = create_voucher(client, "H1", W0, W1)
    assert r.status_code == 201
    v = r.json()["voucher"]
    assert v["device_id"] == DEVICE
    assert v["handover_no"] == "H1"
    assert v["window_start"] == "2026-10-01T08:00:00.000000Z"
    assert v["window_end"] == "2026-10-01T09:00:00.000000Z"
    assert v["limit"] == 8.0

    # 左闭右开：E0/E5 不在窗内
    assert [x["event_no"] for x in v["readings"]] == ["E1", "E2", "E3", "E4"]

    e1, e2, e3, e4 = v["readings"]
    assert e1["sampled_at"] == "2026-10-01T08:00:00.000000Z"
    assert e1["temperature"] == 9.0
    assert e1["policy_no"] is None        # 无策略覆盖
    assert e1["temp_limit"] == 8.0        # 用全局上限
    assert e1["exceeds_limit"] is True
    assert e2["policy_no"] == "P1" and e2["temp_limit"] == 5.0
    assert e2["exceeds_limit"] is True
    assert e3["exceeds_limit"] is True
    assert e4["policy_no"] == "P1" and e4["temp_limit"] == 5.0
    assert e4["exceeds_limit"] is False   # 严格大于才算超限

    assert v["reading_count"] == 4
    assert v["exceedance_count"] == 3
    assert v["max_temperature"] == 9.0


def test_window_is_half_open_at_both_endpoints(client):
    # 恰在起点的读数计入，恰在终点的读数不计入
    post(client, "E1", W0, 9.0)
    post(client, "E2", W1, 9.5)
    v = create_voucher(client, "H1", W0, W1).json()["voucher"]
    assert [x["event_no"] for x in v["readings"]] == ["E1"]
    assert v["reading_count"] == 1
    assert v["exceedance_count"] == 1
    assert v["max_temperature"] == 9.0


def test_readings_sorted_by_sampled_at_then_event_no(client):
    # 乱序提交、同时刻多条：凭证内按 (采样时间, 事件号) 排序
    post(client, "E9", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E5", "2026-10-01T07:30:00Z", 9.0)
    post(client, "E3", "2026-10-01T08:30:00Z", 9.0)
    v = create_voucher(client, "H1", "2026-10-01T07:00:00Z", W1).json()["voucher"]
    assert [x["event_no"] for x in v["readings"]] == ["E5", "E1", "E9", "E3"]


def test_only_unrevoked_readings_frozen_at_creation(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    assert client.delete(f"/devices/{DEVICE}/readings/E2").status_code == 200
    v = create_voucher(client, "H1", W0, W1).json()["voucher"]
    assert [x["event_no"] for x in v["readings"]] == ["E1"]
    assert v["reading_count"] == 1 and v["exceedance_count"] == 1


def test_empty_window_allowed_with_null_max_temperature(client):
    r = create_voucher(client, "H1", "2026-10-02T00:00:00Z", "2026-10-02T01:00:00Z")
    assert r.status_code == 201
    v = r.json()["voucher"]
    assert v["readings"] == []
    assert v["reading_count"] == 0
    assert v["exceedance_count"] == 0
    assert v["max_temperature"] is None


def test_voucher_allowed_on_unknown_device(client):
    # 凭证按时间窗冻结，不要求设备已上报读数（空窗凭证可生成）
    r = create_voucher(client, "H1", W0, W1, device="ghost")
    assert r.status_code == 201
    v = r.json()["voucher"]
    assert v["device_id"] == "ghost"
    assert v["readings"] == []
    assert v["max_temperature"] is None


def test_policy_applies_when_effective_from_equals_sampled_at(client):
    post(client, "E1", W0, 6.0)  # 全局 8.0 下不超限；策略生效时间恰为采样时间
    assert put_policy(client, "P1", W0, 5.0).status_code == 201
    v = create_voucher(client, "H1", W0, W1).json()["voucher"]
    assert v["readings"][0]["policy_no"] == "P1"
    assert v["readings"][0]["temp_limit"] == 5.0
    assert v["readings"][0]["exceeds_limit"] is True


# ---------------------------------------------------------------- 幂等、冲突与冻结不变性

def test_replay_same_window_equivalent_timezone_returns_first_voucher(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    first = create_voucher(client, "H1", W0, W1)
    assert first.status_code == 201

    # 等价时区表示的同一时间窗 → 200，原样返回首次凭证
    replay = create_voucher(
        client, "H1", "2026-10-01T16:00:00+08:00", "2026-10-01T17:00:00+08:00"
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_same_handover_no_different_window_conflicts(client):
    create_voucher(client, "H1", W0, W1)
    # 起点不同
    r = create_voucher(client, "H1", "2026-10-01T08:30:00Z", W1)
    assert r.status_code == 409
    assert "H1" in r.json()["detail"]
    # 终点不同（等价时区仍只比较时刻）
    r = create_voucher(
        client, "H1", "2026-10-01T16:00:00+08:00", "2026-10-01T16:30:00+08:00"
    )
    assert r.status_code == 409


def test_voucher_immutable_after_late_report_correction_and_revoke(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    first = create_voucher(client, "H1", W0, W1)
    assert first.status_code == 201

    # 凭证生成后：迟到补报、更正、撤销全部发生
    post(client, "E0", "2026-10-01T08:05:00Z", 12.0)
    client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 1.0},
    )
    client.delete(f"/devices/{DEVICE}/readings/E1")

    # 重放与按单号查询都原样返回首次凭证
    replay = create_voucher(client, "H1", W0, W1)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    got = client.get(f"/devices/{DEVICE}/handover-vouchers/H1")
    assert got.status_code == 200
    assert got.json() == first.json()

    # 新单号按当前数据生成：E1 已撤销、E2 已更正、E0 为新补报
    fresh = create_voucher(client, "H2", W0, W1)
    assert fresh.status_code == 201
    fv = fresh.json()["voucher"]
    assert [x["event_no"] for x in fv["readings"]] == ["E0", "E2"]
    assert fv["max_temperature"] == 12.0
    assert fv["exceedance_count"] == 1


def test_voucher_immutable_after_policy_change(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5)
    first = create_voucher(client, "H1", W0, W1)
    assert first.status_code == 201
    assert first.json()["voucher"]["exceedance_count"] == 0

    # 凭证生成后再写入回溯策略：历史读数当前已超限，但已存凭证不变
    assert put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0).status_code == 201
    replay = create_voucher(client, "H1", W0, W1)
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # 新单号按当前（策略已生效）数据生成：两条均超限，且冻结到 P1
    fresh = create_voucher(client, "H2", W0, W1).json()["voucher"]
    assert fresh["exceedance_count"] == 2
    assert all(x["policy_no"] == "P1" for x in fresh["readings"])


# ---------------------------------------------------------------- 输入校验

def test_invalid_window_times_rejected(client):
    r = create_voucher(client, "H1", "not-a-time", W1)
    assert r.status_code == 422
    r = create_voucher(client, "H1", W0, "not-a-time")
    assert r.status_code == 422


def test_end_must_be_strictly_later_than_start(client):
    # 结束等于开始（含等价时区）→ 422
    r = create_voucher(client, "H1", W0, W0)
    assert r.status_code == 422
    r = create_voucher(
        client, "H1", "2026-10-01T16:00:00+08:00", "2026-10-01T08:00:00Z"
    )
    assert r.status_code == 422
    # 结束早于开始（跨时区比较）→ 422
    r = create_voucher(
        client, "H1", "2026-10-01T09:00:00+00:00", "2026-10-01T08:00:00+00:00"
    )
    assert r.status_code == 422


def test_invalid_handover_no_rejected(client):
    r = create_voucher(client, "bad/no", W0, W1)
    assert r.status_code == 422


def test_missing_fields_rejected(client):
    r = client.post(
        f"/devices/{DEVICE}/handover-vouchers",
        json={"handover_no": "H1", "window_start": W0},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- 查询与隔离

def test_get_voucher_by_handover_no_and_404(client):
    r = client.get(f"/devices/{DEVICE}/handover-vouchers/H1")
    assert r.status_code == 404
    assert "H1" in r.json()["detail"]

    create_voucher(client, "H1", W0, W1)
    r = client.get(f"/devices/{DEVICE}/handover-vouchers/H1")
    assert r.status_code == 200
    assert r.json()["voucher"]["handover_no"] == "H1"


def test_list_vouchers_sorted_by_created_at_then_no(client):
    create_voucher(client, "H2", "2026-10-02T00:00:00Z", "2026-10-02T01:00:00Z")
    create_voucher(client, "H1", W0, W1)
    body = client.get(f"/devices/{DEVICE}/handover-vouchers").json()
    assert body["device_id"] == DEVICE
    assert [v["handover_no"] for v in body["vouchers"]] == ["H2", "H1"]


def test_vouchers_isolated_per_device(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0, device="dev-A")
    post(client, "E1", "2026-10-01T08:00:00Z", 1.0, device="dev-B")
    a = create_voucher(client, "H1", W0, W1, device="dev-A")
    b = create_voucher(client, "H1", W0, W1, device="dev-B")
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["voucher"]["exceedance_count"] == 1
    assert b.json()["voucher"]["exceedance_count"] == 0
    # 设备 B 查不到设备 A 的凭证
    assert client.get("/devices/dev-B/handover-vouchers/H1").json() == b.json()
    assert (
        client.get("/devices/dev-A/handover-vouchers").json()["vouchers"][0]
        == a.json()["voucher"]
    )


# ---------------------------------------------------------------- 既有行为不变

def test_voucher_generation_does_not_change_readings_or_intervals(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    post(client, "E3", "2026-10-01T08:20:00Z", 6.0)
    before = client.get(f"/devices/{DEVICE}/intervals").json()
    readings_before = client.get(f"/devices/{DEVICE}/readings").json()
    policies_before = client.get(f"/devices/{DEVICE}/limit-policies").json()

    r = create_voucher(client, "H1", W0, W1)
    assert r.status_code == 201

    # 凭证生成只新增证据存档，读数 / 区间 / 策略视图保持原状
    assert client.get(f"/devices/{DEVICE}/intervals").json() == before
    assert client.get(f"/devices/{DEVICE}/readings").json() == readings_before
    assert client.get(f"/devices/{DEVICE}/limit-policies").json() == policies_before
    # 既有区间照常工作（凭证内逐条判定与区间接口互不干扰）
    assert [i["start_event_no"] for i in before["intervals"]] == ["E1"]

"""按采样时间生效的温度上限策略端到端测试。

覆盖：策略首次写入与区间重算（含回溯历史读数）、多策略按时间顺序决定上限、
未覆盖时间沿用全局上限、同号同内容幂等重放（返回首次回执）、同号异内容与
同生效时间冲突、非法阈值与未知设备错误、策略对读数 / 批次 / 争议接口生效、
设备间隔离与策略查询。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

DEVICE = "truck-001"
EMPTY_CHANGES = {"added": [], "changed": [], "invalidated": []}


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


# ---------------------------------------------------------------- 首次写入与重算

def test_policy_recomputes_intervals_for_history(client):
    # 全局上限 8.0：两条 7.x 读数不超限，无区间
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5)
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []

    # 回溯生效的策略：上限降到 5.0，历史读数立即按新上限重判
    r = put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0)
    assert r.status_code == 201
    body = r.json()
    assert body["device_id"] == DEVICE
    assert body["limit"] == 8.0  # 全局默认上限仍在响应中保留
    assert body["policy"]["policy_no"] == "P1"
    assert body["policy"]["effective_from"] == "2026-10-01T00:00:00.000000Z"
    assert body["policy"]["temp_limit"] == 5.0
    # 两条历史读数在新上限下连续超限 → 新增起点 E1 的未闭合区间
    assert len(body["intervals"]) == 1
    iv = body["intervals"][0]
    assert iv["start_event_no"] == "E1"
    assert iv["end_event_no"] is None
    assert iv["reading_count"] == 2
    assert [i["start_event_no"] for i in body["changes"]["added"]] == ["E1"]
    assert body["changes"]["invalidated"] == []
    # 查询接口确认重算结果已持久生效
    assert len(client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]) == 1


def test_policy_can_invalidate_existing_interval(client):
    # 全局上限下 E1、E2 连续超限，区间未闭合
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    # 策略自 08:05 起把上限抬到 10.0：E2 不再超限，E1 只剩单条 → 区间失效
    r = put_policy(client, "P1", "2026-10-01T08:05:00Z", 10.0)
    assert r.status_code == 201
    body = r.json()
    assert body["intervals"] == []
    assert [i["start_event_no"] for i in body["changes"]["invalidated"]] == ["E1"]


def test_readings_before_policy_keep_global_limit(client):
    # E1 在策略生效前（沿用全局 8.0，9.0 超限），E2 在策略生效后（上限 10.0，不超限）
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    post(client, "E3", "2026-10-01T08:20:00Z", 9.2)
    r = put_policy(client, "P1", "2026-10-01T08:05:00Z", 10.0)
    assert r.status_code == 201
    # E1 单条超限不构成区间，E2、E3 按策略上限不超限 → 无区间
    assert r.json()["intervals"] == []


def test_multiple_policies_apply_in_time_order(client):
    # 全局上限 8.0 下：E1(9.0) 单条超限，E2/E3(6.x) 不超限，无区间
    post(client, "E1", "2026-10-01T08:30:00Z", 9.0)
    post(client, "E2", "2026-10-01T09:30:00Z", 6.0)
    post(client, "E3", "2026-10-01T09:40:00Z", 6.5)
    # P1 自 08:00 起上限 10.0：E1 不再超限
    r = put_policy(client, "P1", "2026-10-01T08:00:00Z", 10.0)
    assert r.status_code == 201
    assert r.json()["intervals"] == []
    # P2 自 09:00 起上限 5.0：E2、E3 连续超限 → 区间新增（E1 仍适用 P1）
    r = put_policy(client, "P2", "2026-10-01T09:00:00Z", 5.0)
    assert r.status_code == 201
    body = r.json()
    assert [i["start_event_no"] for i in body["intervals"]] == ["E2"]
    assert body["intervals"][0]["reading_count"] == 2
    assert [i["start_event_no"] for i in body["changes"]["added"]] == ["E2"]


def test_policy_applies_at_exact_effective_from(client):
    # 先生成历史读数（全局上限下不超限），再回溯生效策略
    post(client, "E1", "2026-10-01T08:00:00Z", 6.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 6.5)
    # 生效时间等于采样时间时策略即生效（effective_from <= sampled_at）
    r = put_policy(client, "P1", "2026-10-01T08:00:00Z", 5.0)
    assert r.status_code == 201
    assert [i["start_event_no"] for i in r.json()["intervals"]] == ["E1"]


def test_temperature_equal_to_policy_limit_is_not_exceeding(client):
    post(client, "E0", "2026-10-01T07:00:00Z", 1.0)  # 登记设备
    put_policy(client, "P1", "2026-10-01T08:00:00Z", 5.0)
    post(client, "E1", "2026-10-01T08:10:00Z", 5.0)  # 等于策略上限不算超限
    r = post(client, "E2", "2026-10-01T08:20:00Z", 6.0)
    assert r.json()["intervals"] == []


# ---------------------------------------------------------------- 幂等与冲突

def test_same_policy_no_same_content_replay_returns_first_receipt(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5)
    first = put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0)
    assert first.status_code == 201

    # 等价时区表示也算同内容 → 200，回执与首次完全一致
    replay = put_policy(client, "P1", "2026-10-01T08:00:00+08:00", 5.0)
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # 首次回执存档：即使读数随后被更正，重放仍原样返回首次区间与差异
    client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 1.0},
    )
    replay2 = put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0)
    assert replay2.status_code == 200
    assert replay2.json() == first.json()
    # 策略本身只写入一次
    policies = client.get(f"/devices/{DEVICE}/limit-policies").json()["policies"]
    assert len(policies) == 1


def test_same_policy_no_different_content_conflicts(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0)
    # 阈值不同 → 409
    assert put_policy(client, "P1", "2026-10-01T00:00:00Z", 6.0).status_code == 409
    # 生效时间不同 → 409
    assert put_policy(client, "P1", "2026-10-01T01:00:00Z", 5.0).status_code == 409


def test_same_effective_from_different_policy_no_conflicts(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0)
    # 同一生效时间不得重复（即使阈值相同）
    r = put_policy(client, "P2", "2026-10-01T08:00:00+08:00", 6.0)
    assert r.status_code == 409
    assert "P1" in r.json()["detail"]
    r = put_policy(client, "P2", "2026-10-01T00:00:00Z", 5.0)
    assert r.status_code == 409


def test_failed_policy_write_leaves_no_trace(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0)
    # 冲突的策略号 / 生效时间修正后均可重新首次提交
    assert put_policy(client, "P2", "2026-10-01T00:00:00Z", 6.0).status_code == 409
    assert put_policy(client, "P2", "2026-10-01T01:00:00Z", 6.0).status_code == 201
    assert put_policy(client, "P1", "2026-10-01T02:00:00Z", 5.0).status_code == 409


# ---------------------------------------------------------------- 错误处理

def test_unknown_device_rejected(client):
    r = put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0, device="ghost")
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_invalid_temp_limit_rejected(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    # 非数值
    r = put_policy(client, "P1", "2026-10-01T00:00:00Z", "cold")
    assert r.status_code == 422
    # 超出物理范围
    assert put_policy(client, "P1", "2026-10-01T00:00:00Z", -500).status_code == 422
    assert put_policy(client, "P1", "2026-10-01T00:00:00Z", 2000).status_code == 422
    # 无穷：直接发原始报文
    r = client.post(
        f"/devices/{DEVICE}/limit-policies",
        content='{"policy_no":"P1","effective_from":"2026-10-01T00:00:00Z",'
        '"temp_limit":1e999}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_invalid_policy_no_and_time_rejected(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    assert put_policy(client, "bad/no", "2026-10-01T00:00:00Z", 5.0).status_code == 422
    assert put_policy(client, "P1", "not-a-time", 5.0).status_code == 422


# ---------------------------------------------------------------- 对既有接口生效

def test_policy_affects_single_reading_writes(client):
    post(client, "E0", "2026-10-01T07:00:00Z", 1.0)  # 登记设备
    put_policy(client, "P1", "2026-10-01T08:00:00Z", 5.0)
    # 6.0 / 6.5 在全局上限 8.0 下本不超限，策略生效后构成区间
    post(client, "E1", "2026-10-01T08:10:00Z", 6.0)
    r = post(client, "E2", "2026-10-01T08:20:00Z", 6.5)
    assert [i["start_event_no"] for i in r.json()["changes"]["added"]] == ["E1"]


def test_policy_affects_batch_submission(client):
    post(client, "E0", "2026-10-01T07:00:00Z", 1.0)  # 登记设备
    put_policy(client, "P1", "2026-10-01T08:00:00Z", 5.0)
    r = client.post(
        f"/devices/{DEVICE}/reading-batches",
        json={
            "batch_no": "B1",
            "readings": [
                {"event_no": "E1", "sampled_at": "2026-10-01T08:10:00Z",
                 "temperature": 6.0},
                {"event_no": "E2", "sampled_at": "2026-10-01T08:20:00Z",
                 "temperature": 6.5},
            ],
        },
    )
    assert r.status_code == 201
    assert [i["start_event_no"] for i in r.json()["intervals"]] == ["E1"]


def test_policy_affects_dispute_execution(client):
    # 全局上限 8.0 下 E1(9.0)、E2(9.5) 构成区间
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    # 策略上限 9.2：E1 不再超限，E2 单条超限 → 原区间失效
    r = put_policy(client, "P1", "2026-10-01T00:00:00Z", 9.2)
    assert r.json()["intervals"] == []

    # 争议把 E1 更正为 9.4：批准后按策略上限重判，E1、E2 连续超限 → 区间新增
    r = client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D1",
            "target_event_no": "E1",
            "action": "correct",
            "reason": "probe recalibrated",
            "sampled_at": "2026-10-01T08:00:00Z",
            "temperature": 9.4,
        },
    )
    assert r.status_code == 201
    r = client.post(
        f"/devices/{DEVICE}/disputes/D1/decisions", json={"decision": "approve"}
    )
    execution = r.json()["dispute"]["execution"]
    assert execution["outcome"] == "applied"
    assert [i["start_event_no"] for i in execution["changes"]["added"]] == ["E1"]


# ---------------------------------------------------------------- 查询与隔离

def test_list_policies_sorted_by_effective_from(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    put_policy(client, "P2", "2026-10-01T09:00:00Z", 5.0)
    put_policy(client, "P1", "2026-10-01T08:00:00Z", 6.0)
    body = client.get(f"/devices/{DEVICE}/limit-policies").json()
    assert body["device_id"] == DEVICE
    assert body["limit"] == 8.0
    assert [p["policy_no"] for p in body["policies"]] == ["P1", "P2"]
    assert body["policies"][0]["effective_from"] == "2026-10-01T08:00:00.000000Z"


def test_policies_isolated_per_device(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0, device="dev-A")
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5, device="dev-A")
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0, device="dev-B")
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5, device="dev-B")
    # 策略只作用于 dev-A；dev-B 仍按全局上限判定
    put_policy(client, "P1", "2026-10-01T00:00:00Z", 5.0, device="dev-A")
    assert len(client.get("/devices/dev-A/intervals").json()["intervals"]) == 1
    assert client.get("/devices/dev-B/intervals").json()["intervals"] == []
    # 不同设备可复用同一策略号与同一生效时间
    assert put_policy(
        client, "P1", "2026-10-01T00:00:00Z", 5.0, device="dev-B"
    ).status_code == 201

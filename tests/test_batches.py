"""离线网关成批上报端到端测试。

覆盖：整批提交与逐条状态、区间一次性纳入与整批差异、同号同内容幂等重放
（含等价时区、事后更正 / 争议不影响首次回执）、同批次号异内容冲突、
批内事件号重复与无效读数整批拒绝、库中同号异内容整批回滚、批次号设备隔离。
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


def submit_batch(client, batch_no, readings, device=DEVICE):
    return client.post(
        f"/devices/{device}/reading-batches",
        json={"batch_no": batch_no, "readings": readings},
    )


def rd(event_no, ts, temp):
    return {"event_no": event_no, "sampled_at": ts, "temperature": temp}


# ---------------------------------------------------------------- 首次成功提交

def test_batch_first_commit_creates_readings_and_interval_one_shot(client):
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
        rd("E3", "2026-10-01T08:20:00Z", 6.0),
    ])
    assert r.status_code == 201
    body = r.json()
    assert body["device_id"] == DEVICE
    assert body["batch_no"] == "B1"
    assert body["limit"] == 8.0
    # 逐条状态：全部新建，顺序与请求一致，时间被规范化
    assert [x["status"] for x in body["results"]] == ["created", "created", "created"]
    assert [x["event_no"] for x in body["results"]] == ["E1", "E2", "E3"]
    assert body["results"][0]["sampled_at"] == "2026-10-01T08:00:00.000000Z"
    # 提交后全量区间：E1、E2 连续超限，E3 闭合
    iv = body["intervals"][0]
    assert iv["start_event_no"] == "E1"
    assert iv["end_event_no"] == "E3"
    assert iv["reading_count"] == 2
    assert iv["max_temperature"] == 9.5
    # 整批一次性差异：仅新增 E1 区间（不是按条增量出现 added+changed）
    assert [i["start_event_no"] for i in body["changes"]["added"]] == ["E1"]
    assert body["changes"]["changed"] == []
    assert body["changes"]["invalidated"] == []
    # 读数确实落库，按规则排序
    rows = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert [x["event_no"] for x in rows] == ["E1", "E2", "E3"]


def test_batch_one_shot_diff_is_not_per_reading_incremental(client):
    # 已有一条孤立超限读数 E3（不构成区间）
    post(client, "E3", "2026-10-01T08:30:00Z", 9.0)
    # 一批补两条更早的超限读数：最终区间为起点 E1、条数 3 的未闭合区间
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    body = r.json()
    assert [i["start_event_no"] for i in body["intervals"]] == ["E1"]
    assert body["intervals"][0]["reading_count"] == 3
    # 一次性差异只比较批次前后：added E1，不存在“先 added 再 changed”的中间态
    assert body["changes"] == {
        "added": [body["intervals"][0]],
        "changed": [],
        "invalidated": [],
    }


def test_batch_results_follow_request_order_regardless_of_sample_time(client):
    r = submit_batch(client, "B1", [
        rd("E2", "2026-10-01T08:20:00Z", 9.5),
        rd("E1", "2026-10-01T08:00:00Z", 9.0),  # 更早采样，但排在列表后面
    ])
    body = r.json()
    assert [x["event_no"] for x in body["results"]] == ["E2", "E1"]
    # 区间仍按 (采样时间, 事件号) 规则计算，起点为 E1
    assert body["intervals"][0]["start_event_no"] == "E1"


def test_batch_marks_same_number_same_content_as_existed(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),   # 同号同内容 → existed
        rd("E2", "2026-10-01T08:10:00Z", 9.5),   # 新读数 → created
    ])
    assert r.status_code == 201
    statuses = {x["event_no"]: x["status"] for x in r.json()["results"]}
    assert statuses == {"E1": "existed", "E2": "created"}
    # existed 不重复写入
    rows = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert len(rows) == 2


def test_batch_all_existing_same_content_has_empty_changes_but_still_receipt(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r.status_code == 201
    body = r.json()
    assert {x["status"] for x in body["results"]} == {"existed"}
    assert body["changes"] == EMPTY_CHANGES
    assert [i["start_event_no"] for i in body["intervals"]] == ["E1"]


# ---------------------------------------------------------------- 同批次重试

def test_batch_retry_same_normalized_content_returns_first_receipt(client):
    first = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert first.status_code == 201
    first_body = first.json()

    # 等价时区表示 + 同批次号 → 200，返回首次回执
    second = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T16:00:00+08:00", 9.0),
        rd("E2", "2026-10-01T16:10:00+08:00", 9.5),
    ])
    assert second.status_code == 200
    assert second.json() == first_body
    # 不产生重复读数
    rows = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert len(rows) == 2


def test_batch_retry_returns_first_receipt_even_after_correction(client):
    first_body = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ]).json()
    assert [i["start_event_no"] for i in first_body["intervals"]] == ["E1"]

    # 随后直接更正 E2 为不超限 → 当前区间消失
    r = client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 5.0},
    )
    assert r.json()["intervals"] == []

    # 同批次同内容重试：仍返回首次回执（含当时的区间与差异），不重新应用
    retry = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert retry.status_code == 200
    assert retry.json() == first_body
    assert [i["start_event_no"] for i in retry.json()["intervals"]] == ["E1"]
    # 读数维持更正后的状态
    rows = {
        x["event_no"]: x
        for x in client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    }
    assert rows["E2"]["temperature"] == 5.0


def test_batch_retry_returns_first_receipt_even_after_dispute_decision(client):
    submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D1", "target_event_no": "E2", "action": "revoke",
            "reason": "gateway duplicated packet",
        },
    )
    decided = client.post(
        f"/devices/{DEVICE}/disputes/D1/decisions", json={"decision": "approve"}
    )
    assert decided.json()["dispute"]["status"] == "approved"

    # 争议撤销 E2 后重放批次 → 首次回执原样返回（当时 E2 仍在、区间仍存在）
    retry = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert retry.status_code == 200
    assert [i["start_event_no"] for i in retry.json()["intervals"]] == ["E1"]
    assert [x["status"] for x in retry.json()["results"]] == ["created", "created"]


# ---------------------------------------------------------------- 同批次号异内容

def test_same_batch_no_different_content_conflicts(client):
    submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    # 温度不同
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.1),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r.status_code == 409
    # 采样时间不同
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:01:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r.status_code == 409


def test_same_batch_no_reordered_content_conflicts(client):
    submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    # 同批读数但顺序不同 → 规范化后的有序内容不同 → 冲突
    r = submit_batch(client, "B1", [
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
    ])
    assert r.status_code == 409


# ---------------------------------------------------------------- 整批拒绝

def test_duplicate_event_no_within_batch_rejected_with_422(client):
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E1", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r.status_code == 422
    # 没有任何读数落库
    assert client.get(f"/devices/{DEVICE}/readings").json()["readings"] == []


def test_invalid_reading_in_batch_rejected_with_422(client):
    # 无效温度
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", -500),
    ])
    assert r.status_code == 422
    # 无效时间
    r = submit_batch(client, "B2", [
        rd("E1", "not-a-time", 9.0),
    ])
    assert r.status_code == 422
    # 非法事件号
    r = submit_batch(client, "B3", [
        rd("bad/no", "2026-10-01T08:00:00Z", 9.0),
    ])
    assert r.status_code == 422
    # 空读数列表 / 缺字段
    r = client.post(
        f"/devices/{DEVICE}/reading-batches", json={"batch_no": "B4", "readings": []}
    )
    assert r.status_code == 422
    r = client.post(
        f"/devices/{DEVICE}/reading-batches", json={"batch_no": "B5"}
    )
    assert r.status_code == 422
    # 非法批次号
    r = client.post(
        f"/devices/{DEVICE}/reading-batches",
        json={"batch_no": "BAD/1", "readings": [rd("E1", "2026-10-01T08:00:00Z", 9.0)]},
    )
    assert r.status_code == 422
    # 整批拒绝：无任何读数与批次落库
    assert client.get(f"/devices/{DEVICE}/readings").json()["readings"] == []


def test_existing_same_number_different_content_rejects_whole_batch(client):
    # 库中已有 E1（9.0）
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = submit_batch(client, "B1", [
        rd("E2", "2026-10-01T08:10:00Z", 9.5),  # 新读数，本可新建
        rd("E1", "2026-10-01T08:00:00Z", 7.0),  # 同号异内容 → 整批拒绝
    ])
    assert r.status_code == 409
    assert "E1" in r.json()["detail"]
    rows = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert [x["event_no"] for x in rows] == ["E1"]  # E2 未写入
    assert rows[0]["temperature"] == 9.0            # E1 内容不变
    # 区间不变（E1 单条超限不构成区间）
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []


def test_failed_batch_leaves_no_trace_and_batch_no_can_be_reused(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 7.0),  # 冲突 → 整批拒绝
    ])
    assert r.status_code == 409
    # 失败批次不持久化：修正内容后同批次号可作为全新批次首次提交（201）
    r = submit_batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r.status_code == 201
    assert [x["status"] for x in r.json()["results"]] == ["existed", "created"]


# ---------------------------------------------------------------- 撤销读数与隔离

def test_batch_treats_revoked_reading_like_single_endpoint(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    client.delete(f"/devices/{DEVICE}/readings/E1")
    # 同号同内容：与单条接口一致，幂等视为 existed，不复活
    r = submit_batch(client, "B1", [rd("E1", "2026-10-01T08:00:00Z", 9.0)])
    assert r.status_code == 201
    assert r.json()["results"][0]["status"] == "existed"
    rows = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert rows[0]["revoked"] is True
    # 同号异内容：冲突，整批拒绝
    r = submit_batch(client, "B2", [rd("E1", "2026-10-01T08:00:00Z", 9.1)])
    assert r.status_code == 409


def test_batch_no_isolated_per_device(client):
    body = {
        "batch_no": "B1",
        "readings": [
            rd("E1", "2026-10-01T08:00:00Z", 9.0),
            rd("E2", "2026-10-01T08:10:00Z", 9.5),
        ],
    }
    r1 = client.post("/devices/dev-A/reading-batches", json=body)
    assert r1.status_code == 201
    # 同批次号在另一设备是独立的首次提交
    r2 = client.post("/devices/dev-B/reading-batches", json=body)
    assert r2.status_code == 201
    assert [x["status"] for x in r2.json()["results"]] == ["created", "created"]
    # 设备间读数 / 区间互不可见
    assert client.get("/devices/dev-B/intervals").json()["intervals"][0][
        "start_event_no"
    ] == "E1"
    assert client.get("/devices/dev-C/intervals").json()["intervals"] == []

"""冷链交接温控凭证端到端测试。

覆盖：左闭右开时间窗与排序、逐条证据（策略号 / 适用上限 / 严格超限标记）、
读数数 / 超限数 / 最高温度汇总（含空窗 null）、撤销读数排除、同设备同单号
等价时区时间窗重试原样返回首次凭证（200）、异窗冲突（409）、凭证存档后
不受补报 / 更正 / 撤销 / 策略变化影响、新单号按当前数据重新生成、
非法起止时间与非正时间窗拒绝（422）、按单号查询（404）、设备隔离。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

DEVICE = "truck-001"


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


def issue(client, handover_no, start, end, device=DEVICE):
    return client.post(
        f"/devices/{device}/handover-credentials",
        json={
            "handover_no": handover_no,
            "window_start": start,
            "window_end": end,
        },
    )


# ---------------------------------------------------------------- 基本生成与汇总

def test_credential_freezes_entries_with_global_limit(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)   # 超全局上限
    post(client, "E2", "2026-10-01T08:10:00Z", 8.0)   # 等于上限不算超限
    post(client, "E3", "2026-10-01T08:20:00Z", 9.5)
    r = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert r.status_code == 201
    cred = r.json()["credential"]
    assert cred["device_id"] == DEVICE
    assert cred["handover_no"] == "H1"
    assert cred["window_start"] == "2026-10-01T08:00:00.000000Z"
    assert cred["window_end"] == "2026-10-01T09:00:00.000000Z"
    assert cred["limit"] == 8.0  # 无策略：全局上限随凭证冻结
    assert [e["event_no"] for e in cred["entries"]] == ["E1", "E2", "E3"]
    assert [e["exceeds_limit"] for e in cred["entries"]] == [True, False, True]
    assert all(e["policy_no"] is None and e["limit"] == 8.0 for e in cred["entries"])
    assert cred["reading_count"] == 3
    assert cred["exceed_count"] == 2
    assert cred["max_temperature"] == 9.5
    assert "created_at" in cred


def test_entries_sorted_by_sampled_at_then_event_no(client):
    # 乱序 / 迟到补报：凭证内仍按 (采样时间, 事件号) 排序
    post(client, "E3", "2026-10-01T08:20:00Z", 9.0)
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    # 同一采样时间不同事件号按事件号排序
    post(client, "AB", "2026-10-01T08:30:00Z", 9.0)
    post(client, "AA", "2026-10-01T08:30:00Z", 9.0)
    r = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert [e["event_no"] for e in r.json()["credential"]["entries"]] == [
        "E1", "E2", "E3", "AA", "AB"
    ]


def test_window_is_half_open(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)  # 起点：含
    post(client, "E2", "2026-10-01T09:00:00Z", 9.0)  # 终点：不含
    r = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    entries = r.json()["credential"]["entries"]
    assert [e["event_no"] for e in entries] == ["E1"]
    assert r.json()["credential"]["reading_count"] == 1


def test_empty_window_is_allowed(client):
    # 设备本身也可以从未上报读数：空窗可生成
    r = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert r.status_code == 201
    cred = r.json()["credential"]
    assert cred["entries"] == []
    assert cred["reading_count"] == 0
    assert cred["exceed_count"] == 0
    assert cred["max_temperature"] is None

    # 有读数但窗外为空同样是空窗
    post(client, "E1", "2026-10-02T08:00:00Z", 9.0)
    r = issue(client, "H2", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    cred = r.json()["credential"]
    assert cred["reading_count"] == 0
    assert cred["max_temperature"] is None


def test_revoked_readings_excluded(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    client.delete(f"/devices/{DEVICE}/readings/E1")
    r = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    cred = r.json()["credential"]
    assert [e["event_no"] for e in cred["entries"]] == ["E2"]
    assert cred["reading_count"] == 1
    assert cred["exceed_count"] == 1
    assert cred["max_temperature"] == 9.5


# ---------------------------------------------------------------- 策略适用

def test_entries_freeze_applicable_policy_no_and_limit(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 6.0)  # 无策略：全局 8.0，不超限
    post(client, "E2", "2026-10-01T08:10:00Z", 6.0)  # P1 上限 5.0：超限
    post(client, "E3", "2026-10-01T09:10:00Z", 6.0)  # P2 上限 10.0：不超限
    put_policy(client, "P1", "2026-10-01T08:00:00Z", 5.0)
    put_policy(client, "P2", "2026-10-01T09:00:00Z", 10.0)
    r = issue(client, "H1", "2026-10-01T07:00:00Z", "2026-10-01T10:00:00Z")
    entries = {e["event_no"]: e for e in r.json()["credential"]["entries"]}
    assert (entries["E1"]["policy_no"], entries["E1"]["limit"],
            entries["E1"]["exceeds_limit"]) == ("P1", 5.0, True)
    assert (entries["E2"]["policy_no"], entries["E2"]["limit"],
            entries["E2"]["exceeds_limit"]) == ("P1", 5.0, True)
    assert (entries["E3"]["policy_no"], entries["E3"]["limit"],
            entries["E3"]["exceeds_limit"]) == ("P2", 10.0, False)
    assert r.json()["credential"]["exceed_count"] == 2


def test_policy_before_window_still_applies_inside(client):
    # 策略生效时间远早于时间窗：窗内读数仍适用该策略
    post(client, "E1", "2026-10-01T12:00:00Z", 6.0)
    put_policy(client, "P1", "2026-09-01T00:00:00Z", 5.0)
    r = issue(client, "H1", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
    e = r.json()["credential"]["entries"][0]
    assert e["policy_no"] == "P1"
    assert e["limit"] == 5.0
    assert e["exceeds_limit"] is True


# ---------------------------------------------------------------- 重试与冲突

def test_same_handover_no_equivalent_window_returns_first_credential(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    first = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert first.status_code == 201

    # 等价时区表示的同一时间窗 → 200，原样返回首次凭证
    replay = issue(
        client, "H1", "2026-10-01T16:00:00+08:00", "2026-10-01T17:00:00+08:00"
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_same_handover_no_different_window_conflicts(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    # 起点不同
    r = issue(client, "H1", "2026-10-01T08:30:00Z", "2026-10-01T09:00:00Z")
    assert r.status_code == 409
    # 终点不同（等价时区也不行：归一化后仍不同）
    r = issue(client, "H1", "2026-10-01T16:00:00+08:00", "2026-10-01T17:30:00+08:00")
    assert r.status_code == 409


def test_conflict_does_not_overwrite_first_credential(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    first = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert issue(
        client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T08:30:00Z"
    ).status_code == 409
    # 原窗重试仍然返回首次凭证
    replay = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert replay.status_code == 200
    assert replay.json() == first.json()


# ---------------------------------------------------------------- 凭证冻结性

def test_credential_immutable_after_late_submission_and_correction(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    first = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    first_body = first.json()

    # 后续补报、更正、撤销均不得改动已存凭证
    post(client, "E3", "2026-10-01T08:20:00Z", 12.0)  # 窗内迟到补报
    client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 1.0},
    )
    client.delete(f"/devices/{DEVICE}/readings/E1")

    got = client.get(f"/devices/{DEVICE}/handover-credentials/H1").json()
    assert got == first_body
    # 重放同样原样返回首次凭证
    replay = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert replay.status_code == 200
    assert replay.json() == first_body


def test_credential_immutable_after_policy_change(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 6.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 6.0)
    put_policy(client, "P1", "2026-10-01T08:00:00Z", 5.0)
    first = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert first.json()["credential"]["exceed_count"] == 2

    # 新增策略改变当前判定（P2 抬高上限），已存凭证仍冻结在 P1 口径
    put_policy(client, "P2", "2026-10-01T08:05:00Z", 10.0)
    got = client.get(f"/devices/{DEVICE}/handover-credentials/H1").json()
    assert got == first.json()
    entries = {e["event_no"]: e for e in got["credential"]["entries"]}
    assert entries["E2"]["policy_no"] == "P1"
    assert entries["E2"]["limit"] == 5.0
    assert entries["E2"]["exceeds_limit"] is True


def test_new_handover_no_uses_current_data(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")

    # 撤销 E2 后用新单号、同时间窗：按当前数据重新生成
    client.delete(f"/devices/{DEVICE}/readings/E2")
    r = issue(client, "H2", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert r.status_code == 201
    cred = r.json()["credential"]
    assert [e["event_no"] for e in cred["entries"]] == ["E1"]
    assert cred["reading_count"] == 1
    assert cred["exceed_count"] == 1


# ---------------------------------------------------------------- 查询与隔离

def test_get_credential_by_handover_no(client):
    issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    r = client.get(f"/devices/{DEVICE}/handover-credentials/H1")
    assert r.status_code == 200
    cred = r.json()["credential"]
    assert cred["handover_no"] == "H1"
    assert cred["reading_count"] == 0
    assert cred["max_temperature"] is None


def test_get_unknown_credential_404(client):
    r = client.get(f"/devices/{DEVICE}/handover-credentials/nope")
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_handover_no_isolated_per_device(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0, device="dev-A")
    post(client, "E1", "2026-10-01T08:00:00Z", 1.0, device="dev-B")
    ra = issue(client, "H1", "2026-10-01T08:00:00Z",
               "2026-10-01T09:00:00Z", device="dev-A")
    rb = issue(client, "H1", "2026-10-01T08:00:00Z",
               "2026-10-01T09:00:00Z", device="dev-B")
    assert ra.status_code == rb.status_code == 201
    assert ra.json()["credential"]["exceed_count"] == 1
    assert rb.json()["credential"]["exceed_count"] == 0
    # 各设备按自己的单号查询
    assert client.get("/devices/dev-A/handover-credentials/H1").status_code == 200
    assert client.get("/devices/dev-B/handover-credentials/H1").status_code == 200
    assert client.get("/devices/dev-A/handover-credentials/HX").status_code == 404


# ---------------------------------------------------------------- 输入校验

def test_invalid_window_rejected(client):
    url = f"/devices/{DEVICE}/handover-credentials"
    # 结束等于开始
    r = client.post(url, json={
        "handover_no": "H1",
        "window_start": "2026-10-01T08:00:00Z",
        "window_end": "2026-10-01T08:00:00Z",
    })
    assert r.status_code == 422
    # 结束早于开始
    r = client.post(url, json={
        "handover_no": "H1",
        "window_start": "2026-10-01T09:00:00Z",
        "window_end": "2026-10-01T08:00:00Z",
    })
    assert r.status_code == 422
    # 等价时区归一化后仍不合法
    r = client.post(url, json={
        "handover_no": "H1",
        "window_start": "2026-10-01T09:00:00Z",
        "window_end": "2026-10-01T16:00:00+08:00",  # 同为 08:00 UTC
    })
    assert r.status_code == 422


def test_invalid_time_and_handover_no_rejected(client):
    url = f"/devices/{DEVICE}/handover-credentials"
    r = client.post(url, json={
        "handover_no": "H1",
        "window_start": "not-a-time",
        "window_end": "2026-10-01T09:00:00Z",
    })
    assert r.status_code == 422
    r = client.post(url, json={
        "handover_no": "bad/no",
        "window_start": "2026-10-01T08:00:00Z",
        "window_end": "2026-10-01T09:00:00Z",
    })
    assert r.status_code == 422
    r = client.post(url, json={
        "window_start": "2026-10-01T08:00:00Z",
        "window_end": "2026-10-01T09:00:00Z",
    })
    assert r.status_code == 422


def test_rejected_credential_leaves_no_trace(client):
    url = f"/devices/{DEVICE}/handover-credentials"
    # 非法时间窗不留痕：修正后同一单号可首次提交
    r = client.post(url, json={
        "handover_no": "H1",
        "window_start": "2026-10-01T09:00:00Z",
        "window_end": "2026-10-01T08:00:00Z",
    })
    assert r.status_code == 422
    r = issue(client, "H1", "2026-10-01T08:00:00Z", "2026-10-01T09:00:00Z")
    assert r.status_code == 201

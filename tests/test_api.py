"""端到端 API 测试：覆盖区间判定、迟到重算、更正、撤销、幂等与错误处理。"""
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


# ---------------------------------------------------------------- 区间判定

def test_interval_forms_only_after_two_consecutive_exceedances(client):
    r = post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    assert r.status_code == 200
    assert r.json()["intervals"] == []  # 仅一条超限，不构成区间

    r = post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    body = r.json()
    assert len(body["intervals"]) == 1
    iv = body["intervals"][0]
    assert iv["start_event_no"] == "E1"
    assert iv["end_event_no"] is None  # 未结束，终点为空
    assert iv["reading_count"] == 2
    assert body["changes"]["added"][0]["start_event_no"] == "E1"


def test_interval_closes_on_first_non_exceeding_reading(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    r = post(client, "E3", "2026-10-01T08:20:00Z", 6.0)
    body = r.json()
    iv = body["intervals"][0]
    assert iv["end_event_no"] == "E3"  # 终点为首次不超限读数
    assert body["changes"]["changed"][0]["start_event_no"] == "E1"


def test_single_exceedance_never_forms_interval(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = post(client, "E2", "2026-10-01T08:10:00Z", 5.0)
    assert r.json()["intervals"] == []


def test_temperature_equal_to_limit_is_not_exceeding(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 8.0)  # 等于上限不算超限
    r = post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    assert r.json()["intervals"] == []


def test_multiple_intervals_on_same_device(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    post(client, "E3", "2026-10-01T08:20:00Z", 5.0)
    post(client, "E4", "2026-10-01T08:30:00Z", 10.0)
    r = post(client, "E5", "2026-10-01T08:40:00Z", 10.0)
    starts = [i["start_event_no"] for i in r.json()["intervals"]]
    assert starts == ["E1", "E4"]


# ---------------------------------------------------------------- 迟到与更正重算

def test_late_reading_recomputes_intervals(client):
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    post(client, "E3", "2026-10-01T08:20:00Z", 9.5)
    # 迟到的读数插到中间，且温度不超限，应拆断原区间
    r = post(client, "E1", "2026-10-01T08:15:00Z", 6.0)
    body = r.json()
    assert body["intervals"] == []
    assert body["changes"]["invalidated"][0]["start_event_no"] == "E2"


def test_start_change_invalidates_old_and_adds_new(client):
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    post(client, "E3", "2026-10-01T08:20:00Z", 9.5)
    # 迟到读数成为新的首个超限读数 → 旧区间失效、新区间新增
    r = post(client, "E1", "2026-10-01T08:00:00Z", 8.5)
    changes = r.json()["changes"]
    assert [i["start_event_no"] for i in changes["invalidated"]] == ["E2"]
    assert [i["start_event_no"] for i in changes["added"]] == ["E1"]
    assert [i["start_event_no"] for i in r.json()["intervals"]] == ["E1"]


def test_correction_recomputes_intervals(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    r = client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 5.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["intervals"] == []
    assert body["changes"]["invalidated"][0]["start_event_no"] == "E1"


def test_correction_can_create_interval(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 5.0)
    r = client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 9.5},
    )
    assert [i["start_event_no"] for i in r.json()["changes"]["added"]] == ["E1"]


def test_revoke_recomputes_intervals(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    post(client, "E3", "2026-10-01T08:20:00Z", 9.2)
    r = client.delete(f"/devices/{DEVICE}/readings/E2")
    body = r.json()
    # E2 撤销后 E1、E3 仍连续超限，区间保留但读数条数与峰值变化
    assert len(body["intervals"]) == 1
    assert body["intervals"][0]["reading_count"] == 2
    assert body["changes"]["changed"][0]["start_event_no"] == "E1"


def test_revoke_breaks_interval(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    r = client.delete(f"/devices/{DEVICE}/readings/E2")
    body = r.json()
    assert body["intervals"] == []
    assert body["changes"]["invalidated"][0]["start_event_no"] == "E1"


# ---------------------------------------------------------------- 幂等与冲突

def test_idempotent_resubmit_same_content(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    assert r.status_code == 200
    changes = r.json()["changes"]
    assert changes == {"added": [], "changed": [], "invalidated": []}
    assert len(client.get(f"/devices/{DEVICE}/readings").json()["readings"]) == 1


def test_idempotent_resubmit_equivalent_timezone(client):
    post(client, "E1", "2026-10-01T16:00:00+08:00", 9.0)
    r = post(client, "E1", "2026-10-01T08:00:00Z", 9.0)  # 同一时刻，等价表示
    assert r.status_code == 200


def test_conflict_on_same_event_no_different_content(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = post(client, "E1", "2026-10-01T08:00:00Z", 9.1)
    assert r.status_code == 409
    r = post(client, "E1", "2026-10-01T08:05:00Z", 9.0)
    assert r.status_code == 409


def test_revoke_is_idempotent(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    assert client.delete(f"/devices/{DEVICE}/readings/E1").status_code == 200
    r = client.delete(f"/devices/{DEVICE}/readings/E1")
    assert r.status_code == 200
    assert r.json()["changes"] == {"added": [], "changed": [], "invalidated": []}


# ---------------------------------------------------------------- 错误处理

def test_correct_missing_reading_returns_404(client):
    r = client.put(
        f"/devices/{DEVICE}/readings/NOPE",
        json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 5.0},
    )
    assert r.status_code == 404


def test_revoke_missing_reading_returns_404(client):
    assert client.delete(f"/devices/{DEVICE}/readings/NOPE").status_code == 404


def test_correct_revoked_reading_returns_409(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    client.delete(f"/devices/{DEVICE}/readings/E1")
    r = client.put(
        f"/devices/{DEVICE}/readings/E1",
        json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 5.0},
    )
    assert r.status_code == 409


def test_invalid_time_rejected(client):
    r = post(client, "E1", "not-a-time", 9.0)
    assert r.status_code == 422


def test_invalid_temperature_rejected(client):
    assert post(client, "E1", "2026-10-01T08:00:00Z", "hot").status_code == 422
    assert post(client, "E1", "2026-10-01T08:00:00Z", -500).status_code == 422
    # inf：客户端标准 JSON 无法序列化，直接发原始报文
    r = client.post(
        f"/devices/{DEVICE}/readings",
        content='{"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":1e999}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_invalid_event_no_rejected(client):
    r = post(client, "bad/no", "2026-10-01T08:00:00Z", 9.0)
    assert r.status_code == 422


# ---------------------------------------------------------------- 查询接口

def test_readings_sorted_by_time_then_event_no(client):
    post(client, "E9", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)  # 同时刻按事件号排序
    post(client, "E5", "2026-10-01T07:00:00Z", 7.0)
    readings = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert [r["event_no"] for r in readings] == ["E5", "E1", "E9"]


def test_devices_are_isolated(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0, device="dev-A")
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0, device="dev-A")
    assert client.get("/devices/dev-B/intervals").json()["intervals"] == []
    assert len(client.get("/devices/dev-A/intervals").json()["intervals"]) == 1


def test_health_and_root(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["temp_limit"] == 8.0

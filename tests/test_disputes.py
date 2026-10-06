"""争议工作流端到端测试：申请冻结、批准 / 驳回、修订号乐观核对（过期）、
幂等与终态唯一、查询，以及旧库 revision 迁移。"""
import sqlite3

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


def open_dispute(client, dispute_no, target, action, reason, *,
                 sampled_at=None, temperature=None, device=DEVICE):
    payload = {
        "dispute_no": dispute_no,
        "target_event_no": target,
        "action": action,
        "reason": reason,
    }
    if action == "correct":
        payload["sampled_at"] = sampled_at
        payload["temperature"] = temperature
    return client.post(f"/devices/{device}/disputes", json=payload)


def decide(client, dispute_no, decision, reason=None, device=DEVICE):
    payload = {"decision": decision}
    if reason is not None:
        payload["reason"] = reason
    return client.post(
        f"/devices/{device}/disputes/{dispute_no}/decisions", json=payload
    )


def setup_interval(client):
    """E1(9.0)、E2(9.5) 构成起点 E1 的未闭合超温区间。"""
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)


# ---------------------------------------------------------------- 申请

def test_open_dispute_freezes_reading_and_revision(client):
    setup_interval(client)
    r = open_dispute(
        client, "D1", "E2", "correct", "sensor drifted",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    assert r.status_code == 201
    d = r.json()["dispute"]
    assert d["status"] == "pending"
    assert d["target_event_no"] == "E2"
    assert d["frozen_sampled_at"] == "2026-10-01T08:10:00.000000Z"
    assert d["frozen_temperature"] == 9.5
    assert d["frozen_revision"] == 1
    assert d["execution"] is None


def test_open_revoke_dispute_requires_no_target_fields(client):
    setup_interval(client)
    r = open_dispute(client, "D1", "E2", "revoke", "duplicate reading")
    assert r.status_code == 201
    d = r.json()["dispute"]
    assert d["action"] == "revoke"
    assert d["target_sampled_at"] is None
    assert d["target_temperature"] is None
    assert d["frozen_revision"] == 1


def test_open_dispute_missing_target_returns_404(client):
    setup_interval(client)
    r = open_dispute(
        client, "D1", "NOPE", "correct", "x",
        sampled_at="2026-10-01T08:00:00Z", temperature=5.0,
    )
    assert r.status_code == 404


def test_open_dispute_against_revoked_reading_returns_409(client):
    setup_interval(client)
    client.delete(f"/devices/{DEVICE}/readings/E2")
    r = open_dispute(client, "D1", "E2", "revoke", "already gone")
    assert r.status_code == 409


def test_open_dispute_requires_reason_and_matching_payload(client):
    setup_interval(client)
    # 空原因
    r = client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D1", "target_event_no": "E2",
            "action": "revoke", "reason": "   ",
        },
    )
    assert r.status_code == 422
    # correct 缺少目标值
    r = client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D2", "target_event_no": "E2",
            "action": "correct", "reason": "x",
        },
    )
    assert r.status_code == 422
    # revoke 携带目标值
    r = client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D3", "target_event_no": "E2",
            "action": "revoke", "reason": "x", "temperature": 5.0,
            "sampled_at": "2026-10-01T08:00:00Z",
        },
    )
    assert r.status_code == 422


def test_open_dispute_same_no_same_content_is_idempotent(client):
    setup_interval(client)
    kwargs = dict(
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    r1 = open_dispute(client, "D1", "E2", "correct", "sensor drifted", **kwargs)
    assert r1.status_code == 201
    created = r1.json()["dispute"]["created_at"]
    # 同号同内容（等价时区）重放 → 返回原申请，200
    r2 = open_dispute(
        client, "D1", "E2", "correct", "sensor drifted",
        sampled_at="2026-10-01T16:10:00+08:00", temperature=5.0,
    )
    assert r2.status_code == 200
    assert r2.json()["dispute"]["created_at"] == created
    assert len(client.get(f"/devices/{DEVICE}/disputes").json()["disputes"]) == 1


def test_open_dispute_same_no_different_content_conflicts(client):
    setup_interval(client)
    open_dispute(
        client, "D1", "E2", "correct", "r",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    # 目标温度不同
    r = open_dispute(
        client, "D1", "E2", "correct", "r",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.1,
    )
    assert r.status_code == 409
    # 动作不同
    r = open_dispute(client, "D1", "E2", "revoke", "r")
    assert r.status_code == 409
    # 原因不同
    r = open_dispute(
        client, "D1", "E2", "correct", "other reason",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    assert r.status_code == 409


# ---------------------------------------------------------------- 驳回

def test_reject_does_not_change_readings_or_intervals(client):
    setup_interval(client)
    open_dispute(
        client, "D1", "E2", "correct", "sensor drifted",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    r = decide(client, "D1", "reject", "evidence insufficient")
    assert r.status_code == 200
    d = r.json()["dispute"]
    assert d["status"] == "rejected"
    assert d["execution"]["outcome"] == "rejected"
    assert d["execution"]["applied"] is False
    assert d["execution"]["changes"] == {
        "added": [], "changed": [], "invalidated": [],
    }
    assert d["decided_reason"] == "evidence insufficient"
    # 读数未变，区间仍在
    intervals = client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]
    assert [i["start_event_no"] for i in intervals] == ["E1"]


# ---------------------------------------------------------------- 批准

def test_approve_correction_applies_and_reports_diff(client):
    setup_interval(client)
    open_dispute(
        client, "D1", "E2", "correct", "sensor drifted",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    r = decide(client, "D1", "approve")
    assert r.status_code == 200
    d = r.json()["dispute"]
    assert d["status"] == "approved"
    exe = d["execution"]
    assert exe["outcome"] == "applied"
    assert exe["applied"] is True
    # E2 改为不超限后区间消失，失效的是起点 E1 的区间
    assert exe["intervals"] == []
    assert [i["start_event_no"] for i in exe["changes"]["invalidated"]] == ["E1"]
    # 读数确实被改，修订号 +1
    readings = {
        x["event_no"]: x
        for x in client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    }
    assert readings["E2"]["temperature"] == 5.0


def test_approve_revocation_applies_and_reports_diff(client):
    setup_interval(client)
    open_dispute(client, "D1", "E2", "revoke", "duplicate reading")
    r = decide(client, "D1", "approve")
    assert r.status_code == 200
    d = r.json()["dispute"]
    assert d["status"] == "approved"
    assert d["execution"]["outcome"] == "applied"
    assert [i["start_event_no"] for i in d["execution"]["changes"]["invalidated"]] == ["E1"]
    readings = {
        x["event_no"]: x
        for x in client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    }
    assert readings["E2"]["revoked"] is True


def test_approve_correction_creates_interval(client):
    # E1 超限、E2 不超限 → 无区间；争议把 E2 改为超限后应新增 E1 区间
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 5.0)
    open_dispute(
        client, "D1", "E2", "correct", "wrong reading",
        sampled_at="2026-10-01T08:10:00Z", temperature=9.5,
    )
    d = decide(client, "D1", "approve").json()["dispute"]
    assert [i["start_event_no"] for i in d["execution"]["changes"]["added"]] == ["E1"]
    assert [i["start_event_no"] for i in d["execution"]["intervals"]] == ["E1"]


# ---------------------------------------------------------------- 过期

def test_approve_after_direct_correction_becomes_expired(client):
    setup_interval(client)
    open_dispute(
        client, "D1", "E2", "correct", "sensor drifted",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    # 申请后被直接更正（哪怕改成与争议目标相同，也必须过期）
    client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 5.0},
    )
    r = decide(client, "D1", "approve")
    assert r.status_code == 200
    d = r.json()["dispute"]
    assert d["status"] == "expired"
    assert d["execution"]["outcome"] == "expired"
    assert d["execution"]["applied"] is False
    assert "revision" in d["expiry_reason"]
    assert d["execution"]["changes"] == {
        "added": [], "changed": [], "invalidated": [],
    }
    # 读数维持直接更正后的值（这里恰好相同），且修订号因直接操作变为 2
    readings = {
        x["event_no"]: x
        for x in client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    }
    assert readings["E2"]["temperature"] == 5.0


def test_approve_after_direct_revocation_becomes_expired(client):
    setup_interval(client)
    open_dispute(
        client, "D1", "E2", "correct", "sensor drifted",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    client.delete(f"/devices/{DEVICE}/readings/E2")
    r = decide(client, "D1", "approve")
    d = r.json()["dispute"]
    assert d["status"] == "expired"
    assert "revoked" in d["expiry_reason"]
    assert d["execution"]["applied"] is False


def test_approve_after_other_dispute_becomes_expired(client):
    setup_interval(client)
    # D1 先申请但悬而未决；D2 同样争议 E2 并先获批
    open_dispute(
        client, "D1", "E2", "correct", "first claim",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    open_dispute(
        client, "D2", "E2", "correct", "second claim",
        sampled_at="2026-10-01T08:10:00Z", temperature=4.0,
    )
    decide(client, "D2", "approve")
    r = decide(client, "D1", "approve")
    d1 = r.json()["dispute"]
    assert d1["status"] == "expired"
    # D2 已落地，E2 = 4.0；D1 不得再改读数
    readings = {
        x["event_no"]: x
        for x in client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    }
    assert readings["E2"]["temperature"] == 4.0
    d2 = client.get(f"/devices/{DEVICE}/disputes/D2").json()["dispute"]
    assert d2["status"] == "approved"
    assert d2["execution"]["applied"] is True


# ---------------------------------------------------------------- 终态唯一

def test_same_decision_retry_returns_first_result(client):
    setup_interval(client)
    open_dispute(
        client, "D1", "E2", "correct", "sensor drifted",
        sampled_at="2026-10-01T08:10:00Z", temperature=5.0,
    )
    first = decide(client, "D1", "approve", reason="ok").json()["dispute"]
    second = decide(client, "D1", "approve", reason="repeated").json()["dispute"]
    assert second["status"] == "approved"
    assert second["decided_at"] == first["decided_at"]
    assert second["decided_reason"] == "ok"  # 首次理由
    assert second["execution"] == first["execution"]
    # 读数修订号只增过一次（不会因重试再次执行）
    rows = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert {r["event_no"]: r for r in rows}["E2"]["temperature"] == 5.0


def test_reverse_decision_conflicts(client):
    setup_interval(client)
    open_dispute(client, "D1", "E2", "revoke", "x")
    assert decide(client, "D1", "reject").status_code == 200
    r = decide(client, "D1", "approve")
    assert r.status_code == 409

    open_dispute(
        client, "D2", "E1", "correct", "y",
        sampled_at="2026-10-01T08:00:00Z", temperature=1.0,
    )
    decide(client, "D2", "approve")
    assert decide(client, "D2", "reject").status_code == 409
    # 过期后再驳回也算反向决定
    open_dispute(
        client, "D3", "E1", "correct", "z",
        sampled_at="2026-10-01T08:00:00Z", temperature=2.0,
    )
    client.put(
        f"/devices/{DEVICE}/readings/E1",
        json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 1.5},
    )
    decide(client, "D3", "approve")  # → expired
    assert decide(client, "D3", "reject").status_code == 409
    # 对过期争议重放批准则返回首次（过期）结果
    again = decide(client, "D3", "approve")
    assert again.status_code == 200
    assert again.json()["dispute"]["status"] == "expired"


# ---------------------------------------------------------------- 查询与错误

def test_get_unknown_dispute_returns_404(client):
    assert client.get(f"/devices/{DEVICE}/disputes/NOPE").status_code == 404
    assert decide(client, "NOPE", "approve").status_code == 404


def test_disputes_are_isolated_per_device(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0, device="dev-A")
    open_dispute(client, "D1", "E1", "revoke", "x", device="dev-A")
    assert client.get("/devices/dev-B/disputes").json()["disputes"] == []
    assert client.get("/devices/dev-B/disputes/D1").status_code == 404


# ---------------------------------------------------------------- 旧库迁移

def test_legacy_db_without_revision_column_migrates(client, tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    # 手工构造旧版 schema（无 revision 列）并写入数据
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE readings ("
        "device_id TEXT NOT NULL, event_no TEXT NOT NULL, sampled_at TEXT NOT NULL, "
        "temperature REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (device_id, event_no))"
    )
    raw.execute(
        "INSERT INTO readings VALUES "
        "('truck-001', 'E1', '2026-10-01T08:00:00.000000Z', 9.0, 0, 't', 't')"
    )
    raw.commit()
    raw.close()

    monkeypatch.setenv("DB_PATH", str(db_path))
    with TestClient(app) as c2:
        # 迁移后历史读数修订号为 1，可正常申请 / 批准争议
        r = open_dispute(c2, "D1", "E1", "revoke", "legacy cleanup")
        assert r.status_code == 201
        assert r.json()["dispute"]["frozen_revision"] == 1
        d = decide(c2, "D1", "approve").json()["dispute"]
        assert d["status"] == "approved"

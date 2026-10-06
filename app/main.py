"""冷链运输温度事件核验服务 HTTP API。"""
import json
import math
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .db import connect, init_db
from .intervals import compute_intervals_with_policies, diff_intervals
from .models import (
    BatchIn,
    BatchReceiptResponse,
    CorrectionIn,
    DecisionIn,
    DisputeIn,
    DisputeListResponse,
    DisputeResponse,
    IntervalsResponse,
    LimitPolicyIn,
    LimitPolicyListResponse,
    LimitPolicyResponse,
    ReadingIn,
    ReadingsResponse,
)
from .util import to_utc_iso, utc_now_iso

# 未引起读数变化的决定（驳回 / 过期）对应的空区间差异
_EMPTY_CHANGES = {"added": [], "changed": [], "invalidated": []}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时完成数据库初始化（建表，幂等）
    settings = get_settings()
    init_db(settings.db_path)
    yield


app = FastAPI(
    title="冷链运输温度事件核验服务",
    version="1.3.0",
    description=(
        "按设备提交 / 更正 / 撤销温度读数，自动核验并维护超温区间。"
        "连续两次有效读数超过上限即形成超温区间，区间以起点事件号标识。"
        "另支持离线网关按批次号成批上报：整批成功或整批拒绝，逐条返回"
        "新建 / 已存在状态，并给出提交后全量区间与整批一次性差异，"
        "同批次号同内容重试返回首次回执。"
        "还支持按争议号对读数提出更正 / 撤销争议：申请时冻结读数与修订号，"
        "批准时在同一事务内核对修订号，读数已被改变则争议转为过期。"
        "支持按采样时间生效的温度上限策略：每条读数适用其采样时间所处"
        "策略的上限，未覆盖时间沿用全局上限；策略写入后在同一事务内"
        "重算区间（含历史读数），同策略号同内容重放幂等。"
    ),
    lifespan=lifespan,
)


def _json_safe(value):
    """递归清洗校验错误内容，保证任何非法输入都能序列化为清晰的 422 响应。"""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Exception):
        return str(value)
    return value


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # 默认处理器在遇到 inf 等非法值时会序列化失败，这里统一清洗后返回
    return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})


# ---------------------------------------------------------------- 公共辅助

def _load_valid_readings(conn, device_id: str) -> list[dict]:
    """读取设备全部有效读数，按 (采样时间, 事件号) 排序。"""
    rows = conn.execute(
        "SELECT event_no, sampled_at, temperature FROM readings "
        "WHERE device_id = ? AND revoked = 0 "
        "ORDER BY sampled_at, event_no",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_policies(conn, device_id: str) -> list[tuple[str, float]]:
    """读取设备全部温度上限策略，按生效时间升序。"""
    rows = conn.execute(
        "SELECT effective_from, temp_limit FROM limit_policies "
        "WHERE device_id = ? ORDER BY effective_from, policy_no",
        (device_id,),
    ).fetchall()
    return [(r["effective_from"], r["temp_limit"]) for r in rows]


def _current_intervals(conn, device_id: str, default_limit: float) -> list[dict]:
    """按每条读数采样时间所处的策略上限（无策略覆盖用全局上限）计算区间。"""
    return compute_intervals_with_policies(
        _load_valid_readings(conn, device_id),
        _load_policies(conn, device_id),
        default_limit,
    )


def _mutate(settings: Settings, device_id: str, op: Callable) -> dict:
    """在单事务内执行写操作并重算区间，返回 当前区间 + 本次差异。

    BEGIN IMMEDIATE 保证同一时刻只有一个写事务，避免并发写导致区间不一致。
    """
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = _current_intervals(conn, device_id, settings.temp_limit)
            op(conn)
            after = _current_intervals(conn, device_id, settings.temp_limit)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "device_id": device_id,
        "limit": settings.temp_limit,
        "intervals": after,
        "changes": diff_intervals(before, after),
    }


def _get_reading(conn, device_id: str, event_no: str):
    return conn.execute(
        "SELECT event_no, sampled_at, temperature, revoked, revision FROM readings "
        "WHERE device_id = ? AND event_no = ?",
        (device_id, event_no),
    ).fetchone()


def _apply_correction(conn, device_id: str, event_no: str,
                      sampled_at: str, temperature: float) -> None:
    """按现有规则执行更正，并使读数修订号 +1。

    直接更正接口与争议批准执行共用同一套写入逻辑。
    """
    conn.execute(
        "UPDATE readings SET sampled_at = ?, temperature = ?, "
        "updated_at = ?, revision = revision + 1 "
        "WHERE device_id = ? AND event_no = ?",
        (sampled_at, temperature, utc_now_iso(), device_id, event_no),
    )


def _apply_revocation(conn, device_id: str, event_no: str) -> None:
    """按现有规则执行撤销（墓碑标记），并使读数修订号 +1。"""
    conn.execute(
        "UPDATE readings SET revoked = 1, updated_at = ?, revision = revision + 1 "
        "WHERE device_id = ? AND event_no = ?",
        (utc_now_iso(), device_id, event_no),
    )


# ---------------------------------------------------------------- 写接口

@app.post(
    "/devices/{device_id}/readings",
    response_model=IntervalsResponse,
    summary="提交一条温度读数（同号同内容幂等，同号异内容拒绝）",
)
def submit_reading(
    device_id: str,
    body: ReadingIn,
    settings: Settings = Depends(get_settings),
):
    sampled_at = to_utc_iso(body.sampled_at)

    def op(conn):
        existing = _get_reading(conn, device_id, body.event_no)
        if existing is not None:
            same = (
                existing["sampled_at"] == sampled_at
                and existing["temperature"] == body.temperature
            )
            if same:
                return  # 幂等重放：内容完全一致，不产生任何变化
            raise HTTPException(
                status_code=409,
                detail=(
                    f"event_no '{body.event_no}' already exists on device "
                    f"'{device_id}' with different content; "
                    f"use PUT /devices/{device_id}/readings/{body.event_no} to correct it"
                ),
            )
        now = utc_now_iso()
        conn.execute(
            "INSERT INTO readings "
            "(device_id, event_no, sampled_at, temperature, revoked, revision, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
            (device_id, body.event_no, sampled_at, body.temperature, now, now),
        )

    return _mutate(settings, device_id, op)


@app.put(
    "/devices/{device_id}/readings/{event_no}",
    response_model=IntervalsResponse,
    summary="按事件号更正读数的采样时间与温度",
)
def correct_reading(
    device_id: str,
    event_no: str,
    body: CorrectionIn,
    settings: Settings = Depends(get_settings),
):
    sampled_at = to_utc_iso(body.sampled_at)

    def op(conn):
        existing = _get_reading(conn, device_id, event_no)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"reading '{event_no}' not found on device '{device_id}'",
            )
        if existing["revoked"]:
            raise HTTPException(
                status_code=409,
                detail=f"reading '{event_no}' on device '{device_id}' has been revoked "
                f"and cannot be corrected",
            )
        _apply_correction(conn, device_id, event_no, sampled_at, body.temperature)

    return _mutate(settings, device_id, op)


@app.delete(
    "/devices/{device_id}/readings/{event_no}",
    response_model=IntervalsResponse,
    summary="按事件号撤销读数（墓碑式，重复撤销幂等）",
)
def revoke_reading(
    device_id: str,
    event_no: str,
    settings: Settings = Depends(get_settings),
):
    def op(conn):
        existing = _get_reading(conn, device_id, event_no)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"reading '{event_no}' not found on device '{device_id}'",
            )
        if existing["revoked"]:
            return  # 已撤销：幂等，不产生新的区间变化
        _apply_revocation(conn, device_id, event_no)

    return _mutate(settings, device_id, op)


# ---------------------------------------------------------------- 批量提交（离线网关）

def _canonical_batch(items: list[tuple[str, str, float]]) -> str:
    """规范化后的有序批次内容：(事件号, UTC 采样时间, 温度) 列表的 canonical JSON。

    同批次号重试时按它判定“同内容”：顺序或任一读数不同即视为异内容冲突。
    separators 去空格、sort_keys=False 保留列表顺序，保证同一内容字节一致。
    """
    return json.dumps(
        [[event_no, sampled_at, temperature]
         for event_no, sampled_at, temperature in items],
        ensure_ascii=False,
        separators=(",", ":"),
    )


@app.post(
    "/devices/{device_id}/reading-batches",
    response_model=BatchReceiptResponse,
    status_code=201,
    summary="离线网关成批上报读数（整批成功或整批拒绝；同批次号同内容重试返回首次回执）",
)
def submit_batch(
    device_id: str,
    body: BatchIn,
    settings: Settings = Depends(get_settings),
):
    # 时间统一规范化为 UTC 后再入库 / 比较（与单条提交相同的等价时区语义）
    normalized = [
        (r.event_no, to_utc_iso(r.sampled_at), r.temperature) for r in body.readings
    ]
    canonical = _canonical_batch(normalized)

    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_batch = conn.execute(
                "SELECT readings, receipt FROM reading_batches "
                "WHERE device_id = ? AND batch_no = ?",
                (device_id, body.batch_no),
            ).fetchone()
            if existing_batch is not None:
                # 批次号已占用：查存档，内容一致则原样返回首次回执（即使读数
                # 随后被更正 / 撤销 / 争议改变）；异内容一律冲突，不重放。
                if existing_batch["readings"] != canonical:
                    conn.rollback()
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"batch_no '{body.batch_no}' already exists on device "
                            f"'{device_id}' with different content"
                        ),
                    )
                receipt = json.loads(existing_batch["receipt"])
                conn.commit()
                return JSONResponse(status_code=200, content=receipt)

            # 先做全部校验，再执行任何写入：任一读数同号异内容 → 整批拒绝，
            # 已有读数、修订号与区间保持不变。
            conflicts: list[str] = []
            statuses: list[str] = []
            for event_no, sampled_at, temperature in normalized:
                existing = _get_reading(conn, device_id, event_no)
                if existing is None:
                    statuses.append("created")
                    continue
                same = (
                    existing["sampled_at"] == sampled_at
                    and existing["temperature"] == temperature
                )
                if same:
                    statuses.append("existed")
                else:
                    conflicts.append(event_no)
            if conflicts:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"batch rejected: event_no(s) already exist on device "
                        f"'{device_id}' with different content: "
                        f"{', '.join(conflicts)}"
                    ),
                )

            before = _current_intervals(conn, device_id, settings.temp_limit)
            now = utc_now_iso()
            for (event_no, sampled_at, temperature), status in zip(
                normalized, statuses
            ):
                if status == "existed":
                    continue  # 同号同内容仅标记已存在，不覆盖、不改修订号
                conn.execute(
                    "INSERT INTO readings "
                    "(device_id, event_no, sampled_at, temperature, revoked, revision, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
                    (device_id, event_no, sampled_at, temperature, now, now),
                )
            after = _current_intervals(conn, device_id, settings.temp_limit)

            receipt = {
                "device_id": device_id,
                "batch_no": body.batch_no,
                "limit": settings.temp_limit,
                "results": [
                    {
                        "event_no": event_no,
                        "sampled_at": sampled_at,
                        "temperature": temperature,
                        "status": status,
                    }
                    for (event_no, sampled_at, temperature), status in zip(
                        normalized, statuses
                    )
                ],
                # 成功批次按既有 (采样时间, 事件号) 规则纳入区间；
                # 差异整批一次性计算，只比较批次前后。
                "intervals": after,
                "changes": diff_intervals(before, after),
                "created_at": now,
            }
            # 批次写入与回执持久化在同一事务完成
            conn.execute(
                "INSERT INTO reading_batches "
                "(device_id, batch_no, readings, status, receipt, created_at) "
                "VALUES (?, ?, ?, 'committed', ?, ?)",
                (
                    device_id,
                    body.batch_no,
                    canonical,
                    json.dumps(receipt, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise
    return receipt


# ---------------------------------------------------------------- 温度上限策略

def _get_policy(conn, device_id: str, policy_no: str):
    return conn.execute(
        "SELECT * FROM limit_policies WHERE device_id = ? AND policy_no = ?",
        (device_id, policy_no),
    ).fetchone()


def _policy_payload(row) -> dict:
    return {
        "policy_no": row["policy_no"],
        "effective_from": row["effective_from"],
        "temp_limit": row["temp_limit"],
        "created_at": row["created_at"],
    }


@app.post(
    "/devices/{device_id}/limit-policies",
    response_model=LimitPolicyResponse,
    status_code=201,
    summary="提交按采样时间生效的温度上限策略（同号同内容幂等，异内容冲突）",
)
def create_limit_policy(
    device_id: str,
    body: LimitPolicyIn,
    settings: Settings = Depends(get_settings),
):
    # 生效时间统一规范化为 UTC 后再入库 / 比较（与读数采样时间同一排序键）
    effective_from = to_utc_iso(body.effective_from)

    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 设备由已提交的读数隐式定义：从未上报读数的设备视为未知设备
            known = conn.execute(
                "SELECT 1 FROM readings WHERE device_id = ? LIMIT 1",
                (device_id,),
            ).fetchone()
            if known is None:
                conn.rollback()
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"device '{device_id}' not found: "
                        f"no readings have been submitted for it"
                    ),
                )

            existing = _get_policy(conn, device_id, body.policy_no)
            if existing is not None:
                same = (
                    existing["effective_from"] == effective_from
                    and existing["temp_limit"] == body.temp_limit
                )
                if same:
                    # 同号同内容重放：不重复写入、不重算，原样返回首次回执
                    # （即使读数或其他策略随后发生变化）
                    receipt = json.loads(existing["receipt"])
                    conn.commit()
                    return JSONResponse(status_code=200, content=receipt)
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"policy_no '{body.policy_no}' already exists on device "
                        f"'{device_id}' with different content"
                    ),
                )

            clash = conn.execute(
                "SELECT policy_no FROM limit_policies "
                "WHERE device_id = ? AND effective_from = ?",
                (device_id, effective_from),
            ).fetchone()
            if clash is not None:
                # 同设备同一生效时间只允许一条策略
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"effective_from '{effective_from}' on device '{device_id}' "
                        f"is already used by policy '{clash['policy_no']}'; "
                        f"duplicate effective time is not allowed"
                    ),
                )

            # 同一事务内：写策略并按每条读数所处策略重算区间（含历史读数，
            # 回溯生效时间立即反映到当前区间与差异中）
            before = _current_intervals(conn, device_id, settings.temp_limit)
            now = utc_now_iso()
            conn.execute(
                "INSERT INTO limit_policies "
                "(device_id, policy_no, effective_from, temp_limit, receipt, created_at) "
                "VALUES (?, ?, ?, ?, '', ?)",
                (device_id, body.policy_no, effective_from, body.temp_limit, now),
            )
            after = _current_intervals(conn, device_id, settings.temp_limit)

            receipt = {
                "device_id": device_id,
                "limit": settings.temp_limit,
                "policy": {
                    "policy_no": body.policy_no,
                    "effective_from": effective_from,
                    "temp_limit": body.temp_limit,
                    "created_at": now,
                },
                "intervals": after,
                "changes": diff_intervals(before, after),
            }
            # 策略写入与首次回执存档在同一事务提交
            conn.execute(
                "UPDATE limit_policies SET receipt = ? "
                "WHERE device_id = ? AND policy_no = ?",
                (json.dumps(receipt, ensure_ascii=False), device_id, body.policy_no),
            )
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise
    return receipt


@app.get(
    "/devices/{device_id}/limit-policies",
    response_model=LimitPolicyListResponse,
    summary="查询设备全部温度上限策略（按生效时间排序）",
)
def list_limit_policies(device_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT policy_no, effective_from, temp_limit, created_at "
            "FROM limit_policies WHERE device_id = ? "
            "ORDER BY effective_from, policy_no",
            (device_id,),
        ).fetchall()
    return {
        "device_id": device_id,
        "limit": settings.temp_limit,
        "policies": [
            {
                "policy_no": r["policy_no"],
                "effective_from": r["effective_from"],
                "temp_limit": r["temp_limit"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- 争议工作流

def _get_dispute(conn, device_id: str, dispute_no: str):
    return conn.execute(
        "SELECT * FROM disputes WHERE device_id = ? AND dispute_no = ?",
        (device_id, dispute_no),
    ).fetchone()


def _dispute_payload(row) -> dict:
    """把争议行转为响应对象；执行结果从 JSON 存档中原样解析。"""
    return {
        "device_id": row["device_id"],
        "dispute_no": row["dispute_no"],
        "target_event_no": row["target_event_no"],
        "action": row["action"],
        "reason": row["reason"],
        "target_sampled_at": row["target_sampled_at"],
        "target_temperature": row["target_temperature"],
        "frozen_sampled_at": row["frozen_sampled_at"],
        "frozen_temperature": row["frozen_temperature"],
        "frozen_revision": row["frozen_revision"],
        "status": row["status"],
        "decided_at": row["decided_at"],
        "decided_reason": row["decided_reason"],
        "expiry_reason": row["expiry_reason"],
        "execution": json.loads(row["execution"]) if row["execution"] else None,
        "created_at": row["created_at"],
    }


def _dispute_same_request(row, *, action: str, reason: str, target_event_no: str,
                          target_sampled_at, target_temperature) -> bool:
    """判断争议申请是否与首次完全同内容（幂等重放判定）。"""
    return (
        row["action"] == action
        and row["reason"] == reason
        and row["target_event_no"] == target_event_no
        and row["target_sampled_at"] == target_sampled_at
        and row["target_temperature"] == target_temperature
    )


@app.post(
    "/devices/{device_id}/disputes",
    response_model=DisputeResponse,
    status_code=201,
    summary="对读数提出争议（冻结读数与修订号；同号同内容幂等，异内容冲突）",
)
def create_dispute(
    device_id: str,
    body: DisputeIn,
    settings: Settings = Depends(get_settings),
):
    target_sampled_at = (
        to_utc_iso(body.sampled_at) if body.action == "correct" else None
    )
    target_temperature = (
        body.temperature if body.action == "correct" else None
    )

    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _get_dispute(conn, device_id, body.dispute_no)
            if existing is not None:
                if _dispute_same_request(
                    existing,
                    action=body.action,
                    reason=body.reason,
                    target_event_no=body.target_event_no,
                    target_sampled_at=target_sampled_at,
                    target_temperature=target_temperature,
                ):
                    # 同号同内容重放：不新建、不重复冻结，返回首次申请（200）
                    payload = _dispute_payload(existing)
                    conn.commit()
                    return JSONResponse(
                        status_code=200,
                        content={"dispute": payload},
                    )
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"dispute_no '{body.dispute_no}' already exists on device "
                        f"'{device_id}' with different content"
                    ),
                )

            reading = _get_reading(conn, device_id, body.target_event_no)
            if reading is None:
                # 目标不存在：没有可冻结的依据，拒绝申请
                conn.rollback()
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"target reading '{body.target_event_no}' not found on device "
                        f"'{device_id}'"
                    ),
                )
            if reading["revoked"]:
                # 目标已撤销：撤销终态不可再争议更正 / 撤销
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"target reading '{body.target_event_no}' on device "
                        f"'{device_id}' has been revoked and cannot be disputed"
                    ),
                )

            now = utc_now_iso()
            conn.execute(
                "INSERT INTO disputes ("
                "device_id, dispute_no, target_event_no, action, reason, "
                "target_sampled_at, target_temperature, "
                "frozen_sampled_at, frozen_temperature, frozen_revision, "
                "status, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    device_id,
                    body.dispute_no,
                    body.target_event_no,
                    body.action,
                    body.reason,
                    target_sampled_at,
                    target_temperature,
                    reading["sampled_at"],
                    reading["temperature"],
                    reading["revision"],
                    now,
                ),
            )
            row = _get_dispute(conn, device_id, body.dispute_no)
            payload = _dispute_payload(row)
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise
    return {"dispute": payload}


@app.post(
    "/devices/{device_id}/disputes/{dispute_no}/decisions",
    response_model=DisputeResponse,
    summary="对待审争议作出决定（批准 / 驳回；同决定重放首次结果，反向决定冲突）",
)
def decide_dispute(
    device_id: str,
    dispute_no: str,
    body: DecisionIn,
    settings: Settings = Depends(get_settings),
):
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _get_dispute(conn, device_id, dispute_no)
            if row is None:
                conn.rollback()
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"dispute '{dispute_no}' not found on device '{device_id}'"
                    ),
                )

            if row["status"] != "pending":
                # 争议只有一个终态：同决定重试返回首次结果，反向决定冲突
                same = (
                    (body.decision == "approve" and row["status"] in ("approved", "expired"))
                    or (body.decision == "reject" and row["status"] == "rejected")
                )
                conn.rollback()
                if same:
                    return {"dispute": _dispute_payload(row)}
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"dispute '{dispute_no}' is already in terminal state "
                        f"'{row['status']}'; reverse decision '{body.decision}' is not allowed"
                    ),
                )

            decided_at = utc_now_iso()

            if body.decision == "reject":
                # 驳回：不改动任何读数，记录驳回理由；区间差异为空
                execution = {
                    "outcome": "rejected",
                    "applied": False,
                    "detail": body.reason,
                    "intervals": [],
                    "changes": _EMPTY_CHANGES,
                }
                conn.execute(
                    "UPDATE disputes SET status = 'rejected', decided_at = ?, "
                    "decided_reason = ?, expiry_reason = NULL, execution = ? "
                    "WHERE device_id = ? AND dispute_no = ?",
                    (
                        decided_at,
                        body.reason,
                        json.dumps(execution, ensure_ascii=False),
                        device_id,
                        dispute_no,
                    ),
                )
            else:  # approve：同一事务内核对冻结修订号
                reading = _get_reading(conn, device_id, row["target_event_no"])
                expired_reason = None
                if reading is None:
                    expired_reason = (
                        f"target reading '{row['target_event_no']}' no longer exists; "
                        f"frozen revision was {row['frozen_revision']}"
                    )
                elif reading["revoked"] != 0:
                    expired_reason = (
                        f"target reading '{row['target_event_no']}' has been revoked "
                        f"(current revision {reading['revision']}) since the dispute "
                        f"was frozen at revision {row['frozen_revision']}"
                    )
                elif reading["revision"] != row["frozen_revision"]:
                    # 即使现值恰好与目标一致也算过期：修订号变了即不再核对内容
                    expired_reason = (
                        f"target reading '{row['target_event_no']}' revision changed "
                        f"from {row['frozen_revision']} to {reading['revision']} "
                        f"before approval"
                    )

                if expired_reason is not None:
                    # 过期：保留原因，不再改读数
                    execution = {
                        "outcome": "expired",
                        "applied": False,
                        "detail": expired_reason,
                        "intervals": [],
                        "changes": _EMPTY_CHANGES,
                    }
                    conn.execute(
                        "UPDATE disputes SET status = 'expired', decided_at = ?, "
                        "decided_reason = ?, expiry_reason = ?, execution = ? "
                        "WHERE device_id = ? AND dispute_no = ?",
                        (
                            decided_at,
                            body.reason,
                            expired_reason,
                            json.dumps(execution, ensure_ascii=False),
                            device_id,
                            dispute_no,
                        ),
                    )
                else:
                    # 仍匹配冻结依据：按现有规则执行更正或撤销，记录当次区间差异
                    before = _current_intervals(
                        conn, device_id, settings.temp_limit
                    )
                    if row["action"] == "correct":
                        _apply_correction(
                            conn,
                            device_id,
                            row["target_event_no"],
                            row["target_sampled_at"],
                            row["target_temperature"],
                        )
                    else:
                        _apply_revocation(
                            conn, device_id, row["target_event_no"]
                        )
                    after = _current_intervals(
                        conn, device_id, settings.temp_limit
                    )
                    execution = {
                        "outcome": "applied",
                        "applied": True,
                        "detail": None,
                        "intervals": after,
                        "changes": diff_intervals(before, after),
                    }
                    conn.execute(
                        "UPDATE disputes SET status = 'approved', decided_at = ?, "
                        "decided_reason = ?, expiry_reason = NULL, execution = ? "
                        "WHERE device_id = ? AND dispute_no = ?",
                        (
                            decided_at,
                            body.reason,
                            json.dumps(execution, ensure_ascii=False),
                            device_id,
                            dispute_no,
                        ),
                    )

            row = _get_dispute(conn, device_id, dispute_no)
            payload = _dispute_payload(row)
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise
    return {"dispute": payload}


@app.get(
    "/devices/{device_id}/disputes",
    response_model=DisputeListResponse,
    summary="查询设备全部争议（含冻结依据、终态与执行结果）",
)
def list_disputes(device_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM disputes WHERE device_id = ? ORDER BY created_at, dispute_no",
            (device_id,),
        ).fetchall()
    return {"device_id": device_id, "disputes": [_dispute_payload(r) for r in rows]}


@app.get(
    "/devices/{device_id}/disputes/{dispute_no}",
    response_model=DisputeResponse,
    summary="按争议号查询争议详情（未知争议号报错 404）",
)
def get_dispute(device_id: str, dispute_no: str,
                settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        row = _get_dispute(conn, device_id, dispute_no)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"dispute '{dispute_no}' not found on device '{device_id}'",
        )
    return {"dispute": _dispute_payload(row)}


# ---------------------------------------------------------------- 查询接口

@app.get(
    "/devices/{device_id}/intervals",
    response_model=IntervalsResponse,
    summary="查询设备当前全部超温区间",
)
def list_intervals(device_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        intervals = _current_intervals(conn, device_id, settings.temp_limit)
    return {
        "device_id": device_id,
        "limit": settings.temp_limit,
        "intervals": intervals,
        "changes": {"added": [], "changed": [], "invalidated": []},
    }


@app.get(
    "/devices/{device_id}/readings",
    response_model=ReadingsResponse,
    summary="查询设备全部读数（含已撤销），按采样时间、事件号排序",
)
def list_readings(device_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT event_no, sampled_at, temperature, revoked FROM readings "
            "WHERE device_id = ? ORDER BY sampled_at, event_no",
            (device_id,),
        ).fetchall()
    return {
        "device_id": device_id,
        "readings": [
            {
                "event_no": r["event_no"],
                "sampled_at": r["sampled_at"],
                "temperature": r["temperature"],
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ],
    }


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}


@app.get("/", summary="服务信息")
def root(settings: Settings = Depends(get_settings)):
    return {
        "service": "coldchain-temperature-verification",
        "version": "1.3.0",
        "temp_limit": settings.temp_limit,
        "docs": "/docs",
    }

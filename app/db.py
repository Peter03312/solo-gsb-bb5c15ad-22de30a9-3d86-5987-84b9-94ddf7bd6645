"""SQLite 存取层：建表、迁移与连接管理。"""
import sqlite3
from contextlib import contextmanager

# readings.revision 为单调修订号：初始为 1，每次更正 / 撤销（直接操作或争议批准执行）
# 都会 +1。争议申请时冻结该修订号，批准时在同一事务内核对，不一致即判定为过期。
# reading_batches 持久化离线网关每个批次号的规范化有序内容与首次回执：同批次重试
# 按内容判同后原样返回首次回执，批次写入与回执存档在同一事务完成。
# limit_policies 持久化按采样时间生效的温度上限策略：读数适用生效时间不晚于其
# 采样时间的最近一条策略的上限，未覆盖时间沿用全局上限；同号同内容重放按存档
# 回执原样返回，同设备同一生效时间只允许一条策略。
# handover_credentials 持久化冷链交接温控凭证：以 (设备, 交接单号) 为键冻结
# 指定左闭右开时间窗内当时未撤销读数的逐条证据（含当时适用策略号与上限），
# 凭证生成与证据 JSON 存档在同一事务完成；同单号等价时间窗重试原样返回首次
# 凭证，异窗冲突；存档后读数 / 策略的任何后续变化都不影响已存凭证。
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    device_id   TEXT NOT NULL,
    event_no    TEXT NOT NULL,
    sampled_at  TEXT NOT NULL,          -- UTC ISO-8601，固定微秒宽度，可直接排序
    temperature REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,  -- 撤销采用墓碑标记，保留审计痕迹
    revision    INTEGER NOT NULL DEFAULT 1,  -- 单调修订号，每次内容写操作 +1
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, event_no)
);

CREATE TABLE IF NOT EXISTS disputes (
    device_id          TEXT NOT NULL,
    dispute_no         TEXT NOT NULL,    -- 设备内唯一争议号
    target_event_no    TEXT NOT NULL,    -- 被争议读数的事件号
    action             TEXT NOT NULL,    -- correct | revoke
    reason             TEXT NOT NULL,    -- 申请原因（非空）
    target_sampled_at  TEXT,             -- action=correct 时的目标采样时间（UTC 归一化）
    target_temperature REAL,             -- action=correct 时的目标温度
    frozen_sampled_at  TEXT NOT NULL,    -- 申请时冻结的读数采样时间
    frozen_temperature REAL NOT NULL,    -- 申请时冻结的读数温度
    frozen_revision    INTEGER NOT NULL, -- 申请时冻结的读数修订号
    status             TEXT NOT NULL,    -- pending | approved | rejected | expired
    decided_at         TEXT,             -- 首次作出决定的时间
    decided_reason     TEXT,             -- 决定时附带的说明（可选）
    expiry_reason      TEXT,             -- 批准时发现过期的原因说明
    execution          TEXT,             -- 首次决定的执行结果（JSON 存档，重试原样返回）
    created_at         TEXT NOT NULL,
    PRIMARY KEY (device_id, dispute_no)
);

CREATE TABLE IF NOT EXISTS reading_batches (
    device_id   TEXT NOT NULL,
    batch_no    TEXT NOT NULL,    -- 设备内唯一次批号（离线网关一批一报）
    readings    TEXT NOT NULL,    -- 规范化后的有序读数内容（canonical JSON），重试按“整批同内容”判同
    status      TEXT NOT NULL,    -- committed（整批成功提交；失败批次不留痕）
    receipt     TEXT NOT NULL,    -- 首次回执（JSON 存档：逐条状态、全量区间、整批一次性差异）
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, batch_no)
);

CREATE TABLE IF NOT EXISTS limit_policies (
    device_id      TEXT NOT NULL,
    policy_no      TEXT NOT NULL,    -- 设备内唯一策略号
    effective_from TEXT NOT NULL,    -- 生效时间（UTC ISO-8601，固定宽度，可直接排序）
    temp_limit     REAL NOT NULL,    -- 自生效时间起适用的温度上限（°C）
    receipt        TEXT NOT NULL,    -- 首次写入回执（JSON 存档，同号同内容重放原样返回）
    created_at     TEXT NOT NULL,
    PRIMARY KEY (device_id, policy_no)
);

-- 同一设备同一生效时间只允许一条策略（应用层先行校验并返回 409，索引兜底）
CREATE UNIQUE INDEX IF NOT EXISTS ux_limit_policies_effective
    ON limit_policies (device_id, effective_from);

CREATE TABLE IF NOT EXISTS handover_credentials (
    device_id  TEXT NOT NULL,
    handover_no TEXT NOT NULL,   -- 设备内唯一交接单号
    window_start TEXT NOT NULL,  -- 采样时间窗起点（UTC 归一化，左闭，参与重试判同）
    window_end   TEXT NOT NULL,  -- 采样时间窗终点（UTC 归一化，右开，参与重试判同）
    evidence   TEXT NOT NULL,    -- 首次凭证完整快照（JSON 存档，重试原样返回）
    created_at TEXT NOT NULL,
    PRIMARY KEY (device_id, handover_no)
);
"""

# 旧版本库（无 revision 列）的一次性迁移；默认值 1 即历史读数的初始修订号。
_ADD_REVISION_SQL = (
    "ALTER TABLE readings ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """确保所有表 / 列存在（幂等），并对旧库做增量迁移。"""
    # executescript 会先提交当前事务，因此必须在业务事务（BEGIN IMMEDIATE）之前调用。
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
    if "revision" not in columns:
        conn.execute(_ADD_REVISION_SQL)
    conn.commit()


def init_db(path: str) -> None:
    """初始化数据库 schema（幂等），服务启动时调用。"""
    conn = sqlite3.connect(path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()


@contextmanager
def connect(path: str):
    """打开一个连接并确保 schema 存在（幂等，便于任意入口直接使用）。"""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()

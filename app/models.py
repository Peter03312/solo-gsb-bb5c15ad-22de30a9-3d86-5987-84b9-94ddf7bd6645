"""请求 / 响应数据模型与输入校验。"""
import math
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# 温度的合理物理范围（°C），超出即视为无效温度
MIN_TEMPERATURE = -273.15
MAX_TEMPERATURE = 1000.0

# 事件号允许字母、数字、点、下划线、连字符，且以字母或数字开头，
# 保证事件号可安全地出现在 URL 路径中（更正 / 撤销接口）。
EVENT_NO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def _validate_finite_range(v: float, name: str) -> float:
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    if not (MIN_TEMPERATURE <= v <= MAX_TEMPERATURE):
        raise ValueError(
            f"{name} must be within [{MIN_TEMPERATURE}, {MAX_TEMPERATURE}] °C"
        )
    return v


def _validate_temperature(v: float) -> float:
    return _validate_finite_range(v, "temperature")


def _coerce_no(v):
    # 允许客户端以 JSON 数字形式提交编号（事件号 / 策略号等）
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = format(v, "g")
    return v


class ReadingIn(BaseModel):
    """提交读数请求体。"""

    event_no: str = Field(pattern=EVENT_NO_PATTERN)
    sampled_at: datetime
    temperature: float

    @field_validator("event_no", mode="before")
    @classmethod
    def coerce_event_no(cls, v):
        return _coerce_no(v)

    @field_validator("temperature")
    @classmethod
    def temperature_plausible(cls, v: float) -> float:
        return _validate_temperature(v)


class CorrectionIn(BaseModel):
    """更正读数请求体（按事件号整体替换采样时间与温度）。"""

    sampled_at: datetime
    temperature: float

    @field_validator("temperature")
    @classmethod
    def temperature_plausible(cls, v: float) -> float:
        return _validate_temperature(v)


class BatchIn(BaseModel):
    """离线网关成批上报请求体：设备号 + 批次号 + 有序读数列表。

    读数组沿用单条提交 ReadingIn 的全部校验（事件号、采样时间、温度）；
    批内事件号重复属于请求本身无效，整批拒绝（422）。
    读数按列表顺序视为有序内容参与“同批次号同内容”幂等判定。
    """

    batch_no: str = Field(
        description="设备内唯一次批号（规则同事件号，字母 / 数字开头，≤ 64 字符）",
        pattern=EVENT_NO_PATTERN,
    )
    readings: List[ReadingIn] = Field(min_length=1)

    @field_validator("readings")
    @classmethod
    def no_duplicate_event_no_within_batch(cls, v: List[ReadingIn]) -> List[ReadingIn]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in v:
            if item.event_no in seen and item.event_no not in duplicates:
                duplicates.append(item.event_no)
            seen.add(item.event_no)
        if duplicates:
            raise ValueError(
                "event_no must be unique within a batch; duplicates: "
                + ", ".join(duplicates)
            )
        return v


class DisputeIn(BaseModel):
    """争议申请请求体。

    - action=correct：必须携带 sampled_at 与 temperature（目标采样时间 / 温度）；
    - action=revoke：不得携带 sampled_at / temperature（申请撤销整条例外读数）；
    - reason 必填且非空白，作为处理争议的依据。
    """

    action: Literal["correct", "revoke"]
    reason: str = Field(min_length=1, max_length=1000)
    dispute_no: str = Field(
        description="设备内唯一争议号（字母 / 数字 / `.` `_` `-`，≤ 64 字符）",
        pattern=EVENT_NO_PATTERN,
    )
    target_event_no: str = Field(pattern=EVENT_NO_PATTERN)
    sampled_at: Optional[datetime] = None
    temperature: Optional[float] = None

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be blank")
        return v

    @field_validator("temperature")
    @classmethod
    def temperature_plausible(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        return _validate_temperature(v)

    @model_validator(mode="after")
    def payload_matches_action(self):
        if self.action == "correct":
            missing = [
                name for name in ("sampled_at", "temperature")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"action 'correct' requires non-null fields: {', '.join(missing)}"
                )
        else:  # revoke：目标字段必须缺省，避免“撤销但又带目标值”的歧义
            extra = [
                name for name in ("sampled_at", "temperature")
                if getattr(self, name) is not None
            ]
            if extra:
                raise ValueError(
                    f"action 'revoke' must not carry fields: {', '.join(extra)}"
                )
        return self


class DecisionIn(BaseModel):
    """争议决定请求体：approve（批准）或 reject（驳回），reason 可选。"""

    decision: Literal["approve", "reject"]
    reason: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("reason must not be blank")
        return v


class Interval(BaseModel):
    """超温区间。以起点读数的事件号为标识；未结束时终点字段为空。"""

    start_event_no: str
    start_sampled_at: str
    start_temperature: float
    end_event_no: Optional[str]
    end_sampled_at: Optional[str]
    end_temperature: Optional[float]
    reading_count: int = Field(description="区间内连续超限读数条数")
    max_temperature: float = Field(description="区间内最高温度")


class IntervalChanges(BaseModel):
    """本次操作引起的区间差异。"""

    added: List[Interval] = Field(description="本次新增的区间")
    changed: List[Interval] = Field(description="起点不变但内容变化的区间（变更后版本）")
    invalidated: List[Interval] = Field(description="本次失效的区间（变更前版本）")


class IntervalsResponse(BaseModel):
    """写操作与区间查询的统一响应：当前全部区间 + 本次差异。"""

    device_id: str
    limit: float
    intervals: List[Interval] = Field(description="该设备当前全部有效超温区间")
    changes: IntervalChanges


class ReadingOut(BaseModel):
    event_no: str
    sampled_at: str
    temperature: float
    revoked: bool


class ReadingsResponse(BaseModel):
    device_id: str
    readings: List[ReadingOut]


class BatchReadingResult(BaseModel):
    """批内逐条读数的处理结果。"""

    event_no: str
    sampled_at: str = Field(description="规范化（UTC）后的采样时间")
    temperature: float
    status: Literal["created", "existed"] = Field(
        description="created=本次新建；existed=库中已有同号同内容，仅标记已存在"
    )


class BatchReceiptResponse(BaseModel):
    """批次提交回执：逐条新建 / 已存在状态 + 提交后全量区间 + 整批一次性差异。"""

    device_id: str
    batch_no: str
    limit: float
    results: List[BatchReadingResult] = Field(
        description="按请求列表顺序给出的逐条处理结果"
    )
    intervals: List[Interval] = Field(description="该批提交后设备的全部有效超温区间")
    changes: IntervalChanges = Field(
        description="整批一次性区间差异（仅比较该批次提交前后，不按条拆分）"
    )
    created_at: str = Field(description="首次成功提交时间；同批次重试原样返回首次回执")


class DisputeExecution(BaseModel):
    """争议决定的执行结果（终态后随争议一起返回，重放时原样回传首次结果）。"""

    outcome: Literal["applied", "rejected", "expired"] = Field(
        description="applied=批准已按规则执行；rejected=驳回未改读数；expired=过期未改读数"
    )
    applied: bool = Field(description="是否真正改写了读数（仅 applied 为 true）")
    detail: Optional[str] = Field(default=None, description="过期 / 驳回的原因说明")
    intervals: List[Interval] = Field(
        default_factory=list,
        description="决定生效时点该设备的全部有效超温区间",
    )
    changes: IntervalChanges = Field(
        description="本次决定引起的区间差异（未改读数时三个列表均为空）"
    )


class DisputeOut(BaseModel):
    """争议对象：含冻结依据、终态与执行结果。"""

    device_id: str
    dispute_no: str
    target_event_no: str
    action: Literal["correct", "revoke"]
    reason: str
    target_sampled_at: Optional[str] = Field(
        default=None, description="action=correct 时申请写入的采样时间"
    )
    target_temperature: Optional[float] = Field(
        default=None, description="action=correct 时申请写入的温度"
    )
    frozen_sampled_at: str = Field(description="申请时冻结的原读数采样时间")
    frozen_temperature: float = Field(description="申请时冻结的原读数温度")
    frozen_revision: int = Field(description="申请时冻结的原读数修订号")
    status: Literal["pending", "approved", "rejected", "expired"]
    decided_at: Optional[str] = None
    decided_reason: Optional[str] = Field(
        default=None, description="决定请求中附带的说明（驳回理由等）"
    )
    expiry_reason: Optional[str] = Field(
        default=None, description="批准时发现读数已变化、转为过期的具体原因"
    )
    execution: Optional[DisputeExecution] = Field(
        default=None, description="终态争议的首次执行结果；待审争议为 null"
    )
    created_at: str


class DisputeResponse(BaseModel):
    dispute: DisputeOut


class DisputeListResponse(BaseModel):
    device_id: str
    disputes: List[DisputeOut]


class LimitPolicyIn(BaseModel):
    """温度上限策略请求体：策略号 + 生效时间 + 阈值。

    策略自 effective_from（采样时间口径）起生效，覆盖其后读数的上限判定；
    生效时间可回溯到历史读数之前 / 之间，写入后按每条读数所处策略重算区间。
    """

    policy_no: str = Field(
        description="设备内唯一策略号（字母 / 数字 / `.` `_` `-`，≤ 64 字符）",
        pattern=EVENT_NO_PATTERN,
    )
    effective_from: datetime = Field(description="策略生效时间（采样时间口径）")
    temp_limit: float = Field(description="自生效时间起适用的温度上限（°C）")

    @field_validator("policy_no", mode="before")
    @classmethod
    def coerce_policy_no(cls, v):
        return _coerce_no(v)

    @field_validator("temp_limit")
    @classmethod
    def limit_plausible(cls, v: float) -> float:
        return _validate_finite_range(v, "temp_limit")


class LimitPolicyOut(BaseModel):
    policy_no: str
    effective_from: str = Field(description="规范化（UTC）后的生效时间")
    temp_limit: float
    created_at: str


class LimitPolicyResponse(BaseModel):
    """策略写入响应：策略内容 + 重算后的当前全量区间与相对变更。"""

    device_id: str
    limit: float = Field(description="全局默认上限（未被策略覆盖的时间沿用该值）")
    policy: LimitPolicyOut
    intervals: List[Interval] = Field(description="策略写入后该设备的全部有效超温区间")
    changes: IntervalChanges = Field(
        description="本次策略写入引起的区间差异（按每条读数所处策略重算）"
    )


class LimitPolicyListResponse(BaseModel):
    device_id: str
    limit: float = Field(description="全局默认上限（未被策略覆盖的时间沿用该值）")
    policies: List[LimitPolicyOut] = Field(
        description="该设备全部温度上限策略，按生效时间、策略号排序"
    )


# ---------------------------------------------------------------- 冷链交接温控凭证

class HandoverVoucherIn(BaseModel):
    """交接温控凭证请求体：交接单号 + 左闭右开的采样时间窗 [start, end)。"""

    handover_no: str = Field(
        description="设备内唯一交接单号（字母 / 数字 / `.` `_` `-`，≤ 64 字符）",
        pattern=EVENT_NO_PATTERN,
    )
    window_start: datetime = Field(description="采样时间窗起点（含，左闭）")
    window_end: datetime = Field(description="采样时间窗终点（不含，右开；必须晚于起点）")

    @field_validator("handover_no", mode="before")
    @classmethod
    def coerce_handover_no(cls, v):
        return _coerce_no(v)

    @model_validator(mode="after")
    def window_end_after_start(self):
        # 先归一化为带时区的 aware datetime（无时区按 UTC），再比较，
        # 因此 naive / aware 混用与等价时区表示都能正确判定先后。
        start = self.window_start
        end = self.window_end
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
            raise ValueError(
                "window_end must be strictly later than window_start "
                "(window is half-open [window_start, window_end))"
            )
        return self


class VoucherReadingOut(BaseModel):
    """凭证内逐条冻结的读数证据。"""

    event_no: str
    sampled_at: str = Field(description="生成凭证时读数的采样时间（UTC 归一化）")
    temperature: float = Field(description="生成凭证时读数的温度（°C）")
    policy_no: Optional[str] = Field(
        description="该时刻适用的温度上限策略号；无策略覆盖（沿用全局上限）时为 null"
    )
    temp_limit: float = Field(
        description="该时刻适用的温度上限（策略上限；无策略时为全局上限）"
    )
    exceeds_limit: bool = Field(description="温度是否严格大于该时刻适用上限（等于不算）")


class HandoverVoucherOut(BaseModel):
    """冷链交接温控凭证：生成时刻冻结的时间窗、逐条证据与汇总。"""

    device_id: str
    handover_no: str
    window_start: str = Field(description="规范化（UTC）后的时间窗起点（含）")
    window_end: str = Field(description="规范化（UTC）后的时间窗终点（不含）")
    limit: float = Field(description="生成凭证时的全局默认上限（无策略读数按它判定）")
    readings: List[VoucherReadingOut] = Field(
        description="窗内当时未撤销读数，按采样时间、事件号排序逐条冻结"
    )
    reading_count: int = Field(description="窗内读数条数（空窗为 0）")
    exceedance_count: int = Field(description="窗内严格超限条数")
    max_temperature: Optional[float] = Field(
        description="窗内最高温度（°C）；空窗为 null"
    )
    created_at: str = Field(description="首次生成时间；同单号重试原样返回首次凭证")


class HandoverVoucherResponse(BaseModel):
    voucher: HandoverVoucherOut


class HandoverVoucherListResponse(BaseModel):
    device_id: str
    vouchers: List[HandoverVoucherOut] = Field(
        description="该设备全部交接凭证，按生成时间、交接单号排序"
    )

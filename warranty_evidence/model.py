"""质保证据核心数据模型。

约定：
- 电量一律使用 Decimal，单位千瓦时（kWh），JSON 中以字符串表示；
- 时间一律使用带时区的 datetime，ISO 8601 序列化，拒绝朴素时间戳；
- 所有需要摘要/哈希的对象都经过 canonical() 稳定序列化；
- 实体 ID 采用内容寻址（sha256 截断），同一内容永远得到同一 ID。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

# ---------------------------------------------------------------------------
# 基础序列化工具
# ---------------------------------------------------------------------------


def parse_ts(value: str) -> datetime:
    """解析 ISO 8601 时间戳；必须携带时区，否则拒绝（证据时间不容含糊）。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳必须携带时区: {value!r}")
    return dt


def fmt_ts(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间戳必须携带时区")
    return value.isoformat()


def as_decimal(value: Union[str, int, float, Decimal]) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # 浮点入参一律走字符串，避免二进制误差渗入证据链
        return Decimal(str(value))
    return Decimal(value)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return fmt_ts(obj)
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"不可 JSON 序列化的类型: {type(obj)!r}")


def canonical(obj: Any) -> str:
    """稳定 JSON 序列化（键排序、紧凑分隔），供摘要与哈希链使用。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    )


def dump_json(obj: Any) -> str:
    """供人阅读/落盘的 JSON（保留缩进），语义与 canonical 一致。"""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)


def digest_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_file(path: Union[str, Path]) -> str:
    return digest_bytes(Path(path).read_bytes())


def short_id(prefix: str, payload: Any) -> str:
    """内容寻址短 ID，例如 rdg:9f2c1a…。同一内容永远得到同一 ID。"""
    hex16 = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{hex16}"


# ---------------------------------------------------------------------------
# 读数与修订
# ---------------------------------------------------------------------------


class SourceKind(str, Enum):
    """读数来源大类。原表/新表的区分由身份沿革解析，不在这里固化。"""

    METER = "meter"  # 表计直读（遥测/集抄）
    MANUAL = "manual"  # 人工补录（抄表、调档）


@dataclass(frozen=True)
class ReadingRevision:
    """一条计量读数的一个修订版本。

    原始记录永不覆盖：更正产生新的 ReadingRevision，supersedes 指向被取代者，
    并强制记录操作者与理由。
    """

    reading_id: str
    revision_no: int
    meter_id: Optional[str]  # 人工补录的资产级读数允许为空
    source: SourceKind
    observed_at: datetime
    cumulative_kwh: Decimal
    operator: str  # 本修订的记录人/更正人
    reason: Optional[str]  # 修订理由；首版为 None
    recorded_at: datetime  # 本修订进入台账的时刻
    import_digest: Optional[str] = None  # 首次导入来源文件摘要
    note: Optional[str] = None
    supersedes: Optional[str] = None  # 被取代修订的 revision_id
    revision_id: Optional[str] = None  # 内容寻址，缺省自动计算

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.recorded_at.tzinfo is None:
            raise ValueError("读数时间必须携带时区")
        if self.cumulative_kwh < 0:
            raise ValueError("累计表码不得为负")
        if not self.operator:
            raise ValueError("必须注明操作者")
        if self.revision_no > 1 and not self.reason:
            raise ValueError("更正必须注明理由")
        if self.source is SourceKind.MANUAL and self.revision_no == 1 and not self.reason:
            # 人工补录首版即需说明来历（为何不是遥测）
            raise ValueError("人工补录必须注明理由")
        if self.revision_id is None:
            payload = self.to_payload(include_id=False)
            object.__setattr__(self, "revision_id", short_id("rev", payload))

    # -- 序列化 --

    def to_payload(self, include_id: bool = True) -> dict:
        payload = {
            "reading_id": self.reading_id,
            "revision_no": self.revision_no,
            "meter_id": self.meter_id,
            "source": self.source.value,
            "observed_at": fmt_ts(self.observed_at),
            "cumulative_kwh": str(self.cumulative_kwh),
            "operator": self.operator,
            "reason": self.reason,
            "recorded_at": fmt_ts(self.recorded_at),
            "import_digest": self.import_digest,
            "note": self.note,
            "supersedes": self.supersedes,
        }
        if include_id:
            payload["revision_id"] = self.revision_id
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> "ReadingRevision":
        return cls(
            reading_id=payload["reading_id"],
            revision_no=int(payload["revision_no"]),
            meter_id=payload.get("meter_id"),
            source=SourceKind(payload["source"]),
            observed_at=parse_ts(payload["observed_at"]),
            cumulative_kwh=as_decimal(payload["cumulative_kwh"]),
            operator=payload["operator"],
            reason=payload.get("reason"),
            recorded_at=parse_ts(payload["recorded_at"]),
            import_digest=payload.get("import_digest"),
            note=payload.get("note"),
            supersedes=payload.get("supersedes"),
            revision_id=payload.get("revision_id"),
        )


def make_reading_id(meter_id: Optional[str], observed_at: datetime, cumulative_kwh: Decimal,
                    source: SourceKind) -> str:
    """读数 ID 由内容决定：同一读数无论来自哪个文件都是同一 ID（幂等导入）。"""
    return short_id("rdg", {
        "meter_id": meter_id,
        "observed_at": fmt_ts(observed_at),
        "cumulative_kwh": str(cumulative_kwh),
        "source": source.value,
    })


# ---------------------------------------------------------------------------
# 换表记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeterChange:
    """换表记录：旧表终值、新表初值与生效时刻三者共同确定累计量衔接。"""

    asset_id: str
    changed_at: datetime
    old_meter_id: str
    old_final_kwh: Decimal
    new_meter_id: str
    new_initial_kwh: Decimal
    operator: str
    recorded_at: datetime
    reason: Optional[str] = None
    change_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.changed_at.tzinfo is None or self.recorded_at.tzinfo is None:
            raise ValueError("换表时间必须携带时区")
        if self.old_meter_id == self.new_meter_id:
            raise ValueError("换表前后表计必须不同")
        if self.old_final_kwh < 0 or self.new_initial_kwh < 0:
            raise ValueError("换表起止表码不得为负")
        if not self.operator:
            raise ValueError("必须注明操作者")
        if self.change_id is None:
            object.__setattr__(self, "change_id", short_id("mc", self.to_payload(include_id=False)))

    def to_payload(self, include_id: bool = True) -> dict:
        payload = {
            "asset_id": self.asset_id,
            "changed_at": fmt_ts(self.changed_at),
            "old_meter_id": self.old_meter_id,
            "old_final_kwh": str(self.old_final_kwh),
            "new_meter_id": self.new_meter_id,
            "new_initial_kwh": str(self.new_initial_kwh),
            "operator": self.operator,
            "recorded_at": fmt_ts(self.recorded_at),
            "reason": self.reason,
        }
        if include_id:
            payload["change_id"] = self.change_id
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> "MeterChange":
        return cls(
            asset_id=payload["asset_id"],
            changed_at=parse_ts(payload["changed_at"]),
            old_meter_id=payload["old_meter_id"],
            old_final_kwh=as_decimal(payload["old_final_kwh"]),
            new_meter_id=payload["new_meter_id"],
            new_initial_kwh=as_decimal(payload["new_initial_kwh"]),
            operator=payload["operator"],
            recorded_at=parse_ts(payload["recorded_at"]),
            reason=payload.get("reason"),
            change_id=payload.get("change_id"),
        )


# ---------------------------------------------------------------------------
# 合同口径（质保规则摘录）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractVersion:
    """一个合同版本的计量口径：生效时刻起适用，直至下一版本生效。"""

    version: str
    effective_from: datetime
    rated_energy_kwh: Decimal  # 额定能量：等效循环次数的分母
    minimum_availability: Decimal  # 最低可用率阈值（合同义务线）

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "effective_from": fmt_ts(self.effective_from),
            "rated_energy_kwh": str(self.rated_energy_kwh),
            "minimum_availability": str(self.minimum_availability),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContractVersion":
        return cls(
            version=data["version"],
            effective_from=parse_ts(data["effective_from"]),
            rated_energy_kwh=as_decimal(data["rated_energy_kwh"]),
            minimum_availability=as_decimal(data["minimum_availability"]),
        )


@dataclass(frozen=True)
class RuleSet:
    """质保规则摘录：双方认可的合同口径集合，按生效时间选择版本。"""

    ruleset_id: str
    versions: tuple  # tuple[ContractVersion, ...]，按 effective_from 升序

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.versions, key=lambda v: v.effective_from))
        object.__setattr__(self, "versions", ordered)
        if not ordered:
            raise ValueError("规则集至少包含一个合同版本")

    def version_at(self, moment: datetime) -> Optional[ContractVersion]:
        """某一时刻生效的合同版本；早于首个版本生效期则返回 None。"""
        chosen = None
        for ver in self.versions:
            if ver.effective_from <= moment:
                chosen = ver
            else:
                break
        return chosen

    def to_dict(self) -> dict:
        return {
            "ruleset_id": self.ruleset_id,
            "contract_versions": [v.to_dict() for v in self.versions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RuleSet":
        return cls(
            ruleset_id=data.get("ruleset_id", "ruleset"),
            versions=tuple(ContractVersion.from_dict(v) for v in data["contract_versions"]),
        )


# ---------------------------------------------------------------------------
# 设备身份沿革
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeterService:
    """一只表计在资产上的服役区间（身份沿革的一环）。"""

    meter_id: str
    service_from: datetime
    service_until: Optional[datetime]  # None 表示现役
    role: str = "结算表"
    initial_kwh: Optional[Decimal] = None
    final_kwh: Optional[Decimal] = None

    def to_dict(self) -> dict:
        return {
            "meter_id": self.meter_id,
            "service_from": fmt_ts(self.service_from),
            "service_until": fmt_ts(self.service_until) if self.service_until else None,
            "role": self.role,
            "initial_kwh": str(self.initial_kwh) if self.initial_kwh is not None else None,
            "final_kwh": str(self.final_kwh) if self.final_kwh is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeterService":
        return cls(
            meter_id=data["meter_id"],
            service_from=parse_ts(data["service_from"]),
            service_until=parse_ts(data["service_until"]) if data.get("service_until") else None,
            role=data.get("role", "结算表"),
            initial_kwh=as_decimal(data["initial_kwh"]) if data.get("initial_kwh") is not None else None,
            final_kwh=as_decimal(data["final_kwh"]) if data.get("final_kwh") is not None else None,
        )


@dataclass(frozen=True)
class IdentityMap:
    """设备身份映射：一台资产与历任表计的对应关系。"""

    asset_id: str
    meters: tuple  # tuple[MeterService, ...]，按服役先后排序
    asset_label: str = ""

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.meters, key=lambda m: m.service_from))
        object.__setattr__(self, "meters", ordered)

    def meter_at(self, moment: datetime) -> Optional[MeterService]:
        for svc in self.meters:
            if svc.service_from <= moment and (svc.service_until is None or moment < svc.service_until):
                return svc
        return None

    def service_of(self, meter_id: str) -> Optional[MeterService]:
        for svc in self.meters:
            if svc.meter_id == meter_id:
                return svc
        return None

    def ordinal_of(self, meter_id: str) -> Optional[int]:
        """表计在身份沿革中的序号（1 起）。"""
        for idx, svc in enumerate(self.meters, start=1):
            if svc.meter_id == meter_id:
                return idx
        return None

    def meter_label(self, meter_id: str) -> str:
        """沿革称谓：第 1 任为原表，第 2 任为新表，其后按序号称呼。"""
        ordinal = self.ordinal_of(meter_id)
        if ordinal is None:
            return f"未知表 {meter_id}"
        if ordinal == 1:
            return f"原表 {meter_id}"
        if ordinal == 2:
            return f"新表 {meter_id}"
        return f"第{ordinal}任表 {meter_id}"

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "asset_label": self.asset_label,
            "meters": [m.to_dict() for m in self.meters],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IdentityMap":
        return cls(
            asset_id=data["asset_id"],
            asset_label=data.get("asset_label", ""),
            meters=tuple(MeterService.from_dict(m) for m in data["meters"]),
        )


# ---------------------------------------------------------------------------
# 评估窗口与发现项
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """质保评估窗口。窗口边缘若未被读数覆盖，将形成覆盖缺口而非插值。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("评估窗口必须携带时区")
        if not self.start < self.end:
            raise ValueError("评估窗口起点必须早于终点")

    def to_dict(self) -> dict:
        return {"start": fmt_ts(self.start), "end": fmt_ts(self.end)}


@dataclass(frozen=True)
class Finding:
    """完整性发现项：不阻断计算，但必须随报告呈现。"""

    code: str
    message: str
    refs: tuple = ()  # 相关证据 ID（读数/修订/换表记录等）

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "refs": list(self.refs)}

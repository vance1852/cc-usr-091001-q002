"""质保案卷的领域值对象。

所有数值统一使用 :class:`decimal.Decimal`，避免千瓦时累计量在浮点运算下
产生无法向供应商解释的尾差。时间一律使用带时区的 :class:`datetime.datetime`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def parse_ts(value: str | datetime) -> datetime:
    """把 ISO 8601 字符串解析为带时区时间；朴素时间一律拒绝。"""
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError(f"时间戳必须带时区: {value!r}")
    return ts.astimezone(timezone.utc)


def to_decimal(value: str | int | Decimal | float) -> Decimal:
    """以字符串/整数/Decimal 构造精确数值；禁止 float 隐式入库。"""
    if isinstance(value, float):
        raise ValueError("数值必须以字符串或 Decimal 提供，禁止 float")
    return Decimal(str(value))


class ReadingSource(str, Enum):
    """读数来源。事实口径只认这两类，且各有不同的证据强度。"""

    METER_EXPORT = "meter_export"  # 表计自身导出的读数
    MANUAL_ENTRY = "manual_entry"  # 人工补录（必须携带理由与操作者）


class RevisionKind(str, Enum):
    """修订类型。修订永远是新增事件，绝不覆盖既有读数。"""

    CORRECTION = "correction"  # 更正既有读数
    GAP_CLOSURE = "gap_closure"  # 补录此前缺失区间
    METER_CHANGE_AMEND = "meter_change_amend"  # 更正换表端点


class EventType(str, Enum):
    """只追加账本中的事件类型。"""

    FILE_IMPORT = "file_import"
    READING = "reading"
    REVISION = "revision"
    METER_CHANGE = "meter_change"
    GAP_DECLARED = "gap_declared"
    FREEZE = "freeze"


@dataclass(frozen=True)
class Actor:
    """操作者：工号 + 姓名，用于修订与导入的可追责性。"""

    user_id: str
    display_name: str


@dataclass(frozen=True)
class FileDigest:
    """导入文件摘要。同案卷下相同摘要只允许导入一次。"""

    algorithm: str
    hexdigest: str

    @classmethod
    def parse(cls, raw: str) -> "FileDigest":
        algo, _, value = raw.partition(":")
        if not algo or not value:
            raise ValueError(f"摘要格式应为 '算法:值'，实际为 {raw!r}")
        return cls(algo.lower(), value.lower())

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.hexdigest}"


@dataclass(frozen=True)
class Reading:
    """一条累计量读数。

    ``value`` 是该表自投运以来的累计千瓦时读数（绝对值，非增量）。
    ``seq`` 是案卷内全局递增序号，用于下钻时稳定排序与引用。
    """

    seq: int
    asset_id: str
    meter_id: str
    ts: datetime
    value: Decimal
    source: ReadingSource
    import_digest: str
    actor: Actor
    note: str = ""
    superseded_by: int | None = None  # 修订事件序号；原值保留但不再参与计算
    revision_seq: int | None = None  # 若由修订引入，指向所属修订


@dataclass(frozen=True)
class Revision:
    """一条修订。更正后的值由紧随其后的新读数承载，本对象只记录依据。"""

    seq: int
    kind: RevisionKind
    target_seq: int | None  # 被更正的读数序号；缺口闭合可为空
    actor: Actor
    reason: str
    created_ts: datetime
    new_reading_seq: int | None = None
    refs: tuple[str, ...] = field(default_factory=tuple)  # 佐证材料编号


@dataclass(frozen=True)
class MeterChange:
    """换表记录：旧表终值、新表初值、生效时刻三者缺一不可。"""

    seq: int
    asset_id: str
    old_meter_id: str
    old_final: Decimal
    new_meter_id: str
    new_initial: Decimal
    changed_ts: datetime
    actor: Actor
    reason: str = ""
    amends_seq: int | None = None  # 若非空，表示本记录取代哪一条原换表记录


@dataclass(frozen=True)
class Gap:
    """证据缺口：``[start_ts, end_ts)`` 内没有可信读数，明确不插值。"""

    seq: int
    asset_id: str
    start_ts: datetime
    end_ts: datetime
    reason: str
    declared_by: Actor
    closed_by_seq: int | None = None  # 缺口闭合修订序号；冻结后补录只进子分支


@dataclass(frozen=True)
class ContractVersion:
    """合同版本，按 ``effective_from`` 构成对设备生效的规则时间线。"""

    version: str
    effective_from: datetime
    terms: dict[str, Any]

    def term(self, name: str) -> Decimal:
        return to_decimal(self.terms[name])


@dataclass(frozen=True)
class ImportRecord:
    """一次文件导入的留痕。"""

    seq: int
    digest: FileDigest
    filename: str
    imported_ts: datetime
    actor: Actor
    reading_seqs: tuple[int, ...]
    meter_id: str | None = None
    source: ReadingSource = ReadingSource.METER_EXPORT


@dataclass(frozen=True)
class FrozenSnapshot:
    """案卷冻结点：对当时全部账本事件做哈希，构成不可变送审版本。"""

    seq: int
    label: str
    frozen_ts: datetime
    parent_label: str | None
    last_event_seq: int
    digest: str
    actor: Actor


@dataclass(frozen=True)
class MeterTenure:
    """身份沿革中的一段：某块表在设备上的有效服役区间。"""

    meter_id: str
    from_ts: datetime
    to_ts: datetime | None  # None 表示截至案卷末端仍在役
    end_reason: str  # "meter_change" | "case_end" | "open"
    initial: Decimal | None = None
    final: Decimal | None = None
    change_seq: int | None = None

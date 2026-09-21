"""读数来源溯源：把每一条读数的来历讲清楚。

回答审核页面上的追问：这个数是谁量的、哪只表量的、哪次导入进来的、
被谁更正过、依据是什么。原表/新表的称谓由设备身份沿革解析，
人工补录必须能指到具体操作者与理由。
"""
from __future__ import annotations

from typing import Optional

from .ledger import LedgerView
from .model import (
    IdentityMap,
    MeterChange,
    ReadingRevision,
    RuleSet,
    SourceKind,
    fmt_ts,
)


def source_kind_of(rev: ReadingRevision, identity: IdentityMap) -> str:
    """来源类别：old_meter / new_meter / later_meter / manual / unknown。"""
    if rev.source is SourceKind.MANUAL:
        return "manual"
    if rev.meter_id is None:
        return "unknown"
    ordinal = identity.ordinal_of(rev.meter_id)
    if ordinal == 1:
        return "old_meter"
    if ordinal == 2:
        return "new_meter"
    if ordinal is not None:
        return "later_meter"
    return "unknown"


def source_label_of(rev: ReadingRevision, identity: IdentityMap) -> str:
    """给人看的来源一句话，例如「原表 M-88 直读」「人工补录（op-liu，对应新表 M-104）」。"""
    if rev.source is SourceKind.MANUAL:
        if rev.meter_id:
            return f"人工补录（{rev.operator}，对应{identity.meter_label(rev.meter_id)}）"
        return f"人工补录（{rev.operator}，资产级表码）"
    if rev.meter_id is None:
        return "来源不明（缺表计身份）"
    return f"{identity.meter_label(rev.meter_id)} 直读"


def explain_reading(
    rev: ReadingRevision,
    view: LedgerView,
    identity: IdentityMap,
    rules: RuleSet,
    meter_changes: Optional[list[MeterChange]] = None,
) -> dict:
    """一条读数的完整来源卡：从当前取值一路追到导入文件与修订沿革。"""
    chain = view.revision_chain(rev.reading_id)
    service = identity.service_of(rev.meter_id) if rev.meter_id else None
    contract = rules.version_at(rev.observed_at)

    related_changes = []
    for change in meter_changes or []:
        if rev.meter_id and rev.meter_id in (change.old_meter_id, change.new_meter_id):
            related_changes.append({
                "change_id": change.change_id,
                "changed_at": fmt_ts(change.changed_at),
                "old_meter_id": change.old_meter_id,
                "old_final_kwh": change.old_final_kwh,
                "new_meter_id": change.new_meter_id,
                "new_initial_kwh": change.new_initial_kwh,
            })

    return {
        "reading_id": rev.reading_id,
        "revision_id": rev.revision_id,
        "revision_no": rev.revision_no,
        "observed_at": fmt_ts(rev.observed_at),
        "cumulative_kwh": rev.cumulative_kwh,
        "source_kind": source_kind_of(rev, identity),
        "source_label": source_label_of(rev, identity),
        "meter_id": rev.meter_id,
        "recorded_by": rev.operator,
        "recorded_at": fmt_ts(rev.recorded_at),
        "reason": rev.reason,
        "note": rev.note,
        "import_digest": rev.import_digest,
        "meter_service": {
            "service_from": fmt_ts(service.service_from),
            "service_until": fmt_ts(service.service_until) if service.service_until else None,
            "role": service.role,
        } if service else None,
        "contract_version_at_reading": contract.version if contract else None,
        "related_meter_changes": related_changes,
        "revision_chain": [
            {
                "revision_id": r.revision_id,
                "revision_no": r.revision_no,
                "cumulative_kwh": r.cumulative_kwh,
                "observed_at": fmt_ts(r.observed_at),
                "operator": r.operator,
                "reason": r.reason,
                "recorded_at": fmt_ts(r.recorded_at),
                "supersedes": r.supersedes,
                "is_current": r.revision_id == rev.revision_id,
            }
            for r in chain
        ],
        "superseded": view.current_of(rev.reading_id).revision_id != rev.revision_id,
    }


def identity_lineage(identity: IdentityMap, meter_changes: list[MeterChange]) -> list[dict]:
    """身份沿革时间线：历任表计服役区间 + 对应的换表记录。"""
    change_by_new = {c.new_meter_id: c for c in meter_changes}
    lineage = []
    for svc in identity.meters:
        change = change_by_new.get(svc.meter_id)
        lineage.append({
            "meter_id": svc.meter_id,
            "label": identity.meter_label(svc.meter_id),
            "role": svc.role,
            "service_from": fmt_ts(svc.service_from),
            "service_until": fmt_ts(svc.service_until) if svc.service_until else None,
            "initial_kwh": svc.initial_kwh,
            "final_kwh": svc.final_kwh,
            "installed_by_change": change.change_id if change else None,
        })
    return lineage

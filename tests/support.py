"""测试共享构造器：小型合成案卷与读数。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from warranty_evidence.casefile import CaseFile
from warranty_evidence.ledger import EVT_ADD_READING, EVT_METER_CHANGE, EvidenceLedger
from warranty_evidence.model import (
    ContractVersion,
    IdentityMap,
    MeterChange,
    MeterService,
    ReadingRevision,
    RuleSet,
    SourceKind,
    Window,
    as_decimal,
    make_reading_id,
    parse_ts,
)

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=CST)  # 默认事件落账时刻


def ts(text: str) -> datetime:
    return parse_ts(text)


def make_rules() -> RuleSet:
    return RuleSet(
        ruleset_id="TEST-RULES",
        versions=(
            ContractVersion("2025-A", ts("2025-01-01T00:00:00+08:00"),
                            as_decimal("200.0"), as_decimal("0.97")),
            ContractVersion("2026-B", ts("2026-01-01T00:00:00+08:00"),
                            as_decimal("215.0"), as_decimal("0.975")),
        ),
    )


def make_identity(two_meters: bool = True) -> IdentityMap:
    meters = [
        MeterService(
            meter_id="M-88",
            service_from=ts("2025-01-01T00:00:00+08:00"),
            service_until=ts("2026-06-15T10:30:00+08:00") if two_meters else None,
            final_kwh=as_decimal("18420.50") if two_meters else None,
        )
    ]
    if two_meters:
        meters.append(MeterService(
            meter_id="M-104",
            service_from=ts("2026-06-15T10:30:00+08:00"),
            service_until=None,
            initial_kwh=as_decimal("12.25"),
        ))
    return IdentityMap(asset_id="cabinet-A17", meters=tuple(meters))


def make_change(recorded_at: datetime = T0) -> MeterChange:
    return MeterChange(
        asset_id="cabinet-A17",
        changed_at=ts("2026-06-15T10:30:00+08:00"),
        old_meter_id="M-88",
        old_final_kwh=as_decimal("18420.50"),
        new_meter_id="M-104",
        new_initial_kwh=as_decimal("12.25"),
        operator="tester",
        recorded_at=recorded_at,
    )


def make_window() -> Window:
    return Window(start=ts("2025-01-01T00:00:00+08:00"),
                  end=ts("2026-09-01T00:00:00+08:00"))


def make_case(two_meters: bool = True, with_change: bool = True, **kwargs) -> CaseFile:
    case = CaseFile(
        case_id="TEST-CASE",
        asset_id="cabinet-A17",
        window=kwargs.pop("window", make_window()),
        rules=kwargs.pop("rules", make_rules()),
        identity=kwargs.pop("identity", make_identity(two_meters)),
        **kwargs,
    )
    if with_change and two_meters:
        case.record_meter_change(make_change(), operator="tester", at=T0)
    return case


def rev(meter_id, at, kwh, *, source=SourceKind.METER, operator="tester",
        reason=None, note=None, recorded_at=T0, import_digest=None) -> ReadingRevision:
    if isinstance(source, str):
        source = SourceKind(source)
    if isinstance(at, str):
        at = parse_ts(at)
    kwh = as_decimal(kwh)
    return ReadingRevision(
        reading_id=make_reading_id(meter_id, at, kwh, source),
        revision_no=1,
        meter_id=meter_id,
        source=source,
        observed_at=at,
        cumulative_kwh=kwh,
        operator=operator,
        reason=reason,
        recorded_at=recorded_at,
        import_digest=import_digest,
        note=note,
    )


def ledger_with(revisions, changes=()) -> EvidenceLedger:
    """直接把读数/换表塞进台账（绕过文件导入，便于构造边界情形）。"""
    ledger = EvidenceLedger()
    for change in changes:
        ledger.append(EVT_METER_CHANGE, {"meter_change": change.to_payload()},
                      operator="tester", at=T0, branch="B1")
    for revision in revisions:
        ledger.append(EVT_ADD_READING, {"revision": revision.to_payload()},
                      operator="tester", at=T0, branch="B1")
    return ledger


def write_readings_file(directory: Path, records: list[dict], name="readings.jsonl") -> Path:
    """把读数写成 JSONL 文件，供导入测试。"""
    path = Path(directory) / name
    lines = []
    for record in records:
        record = dict(record)
        record.setdefault("source", "meter")
        lines.append(json.dumps(record, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

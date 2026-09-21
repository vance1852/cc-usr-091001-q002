"""参考争议案卷：用仓库内双方认可的资料口径，完整走一遍审核流程。

故事线（对应 reference/ 下的资料）：
1. 依据质保规则摘录与设备身份映射立案，登记换表记录；
2. 导入供应商遥测导出文件（含一处年末抄表错位），冻结送审版 V1；
3. 供应商补交人工抄表与调档读数（进入新分支 B2）；
4. 审核人按工单更正年末错位读数，冻结复审版 V2；
5. diff(V1, V2) 列出两个版本之间多算/少算的全部明细。
"""
from __future__ import annotations

import json
from pathlib import Path

from .casefile import CaseFile
from .model import IdentityMap, MeterChange, RuleSet, Window, as_decimal, parse_ts

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference"

CASE_ID = "DISP-2026-014"
AUDITOR = "auditor-wang"


def reference_dir() -> Path:
    return REFERENCE_DIR


def load_rules(path=None) -> RuleSet:
    path = Path(path) if path else REFERENCE_DIR / "warranty_rules.json"
    return RuleSet.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_identity(path=None) -> IdentityMap:
    path = Path(path) if path else REFERENCE_DIR / "device_identity.json"
    return IdentityMap.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_dispute_case(path=None) -> dict:
    path = Path(path) if path else REFERENCE_DIR / "warranty_case.json"
    return json.loads(path.read_text(encoding="utf-8"))


def new_case() -> CaseFile:
    """依据双方认可的资料口径立案，并登记换表记录。"""
    dispute = load_dispute_case()
    rules = load_rules()
    identity = load_identity()
    case = CaseFile(
        case_id=CASE_ID,
        asset_id=dispute["asset_id"],
        window=Window(
            start=parse_ts("2025-01-01T00:00:00+08:00"),
            end=parse_ts("2026-09-01T00:00:00+08:00"),
        ),
        rules=rules,
        identity=identity,
        description="换表导致累计量断层，供应商退回索赔争议的质保证据案卷",
    )
    change = dispute["meter_change"]
    case.record_meter_change(
        MeterChange(
            asset_id=dispute["asset_id"],
            changed_at=parse_ts(change["changed_at"]),
            old_meter_id=change["old_meter"]["id"],
            old_final_kwh=as_decimal(change["old_meter"]["final_kwh"]),
            new_meter_id=change["new_meter"]["id"],
            new_initial_kwh=as_decimal(change["new_meter"]["initial_kwh"]),
            operator=AUDITOR,
            recorded_at=parse_ts("2026-09-04T09:00:00+08:00"),
            reason="依据双方认可的争议案卷登记换表事实",
        ),
        operator=AUDITOR,
        at=parse_ts("2026-09-04T09:00:00+08:00"),
    )
    return case


def build_reference_case(upto: str = "V2") -> CaseFile:
    """把参考故事线推进到指定阶段：'V1' 或 'V2'。"""
    case = new_case()
    case.import_readings_file(
        REFERENCE_DIR / "readings" / "2025-2026_telemetry.jsonl",
        operator=AUDITOR,
        at=parse_ts("2026-09-04T09:30:00+08:00"),
    )
    case.freeze(
        operator=AUDITOR,
        reason="首次送审：基于供应商遥测导出的计量结论",
        at=parse_ts("2026-09-05T09:30:00+08:00"),
    )
    if upto == "V1":
        return case

    # 冻结之后：补录进入新分支 B2
    case.import_readings_file(
        REFERENCE_DIR / "readings" / "2026-09_supplement.jsonl",
        operator=AUDITOR,
        at=parse_ts("2026-09-12T14:00:00+08:00"),
    )
    boundary_reading = case.find_reading("M-88", "2026-01-01T00:00:00+08:00")
    case.correct_reading(
        boundary_reading,
        operator=AUDITOR,
        reason="供应商确认年末抄表错位，按工单 WX-221 修正（13768.00 → 13462.00）",
        at=parse_ts("2026-09-13T10:00:00+08:00"),
        cumulative_kwh="13462.00",
    )
    case.freeze(
        operator=AUDITOR,
        reason="复审：纳入补录与年末读数更正",
        at=parse_ts("2026-09-15T10:00:00+08:00"),
    )
    return case

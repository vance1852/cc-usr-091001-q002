"""端到端审核走查：以双方认可的争议案例 reference/warranty_case.json 为起点。

运行：python3 examples/audit_walkthrough.py

场景对应实际争议：
1. 旧表 M-88 累计到换表，2026-05 采集链路中断形成证据缺口；
2. 6 月 15 日换表，旧表终值 18420.50、新表 M-104 初值 12.25；
3. 审核人冻结 v1 送审（跨缺口增量隔离，不插值）；
4. 供应商补交现场手抄表，在子分支闭合缺口并更正一条新表读数，冻结 v2；
5. 输出两版汇总、逐笔下钻与多算/少算比对。

额定能量等参考文件未载明的条款，以独立合同摘录号经 extra_terms 提供，
模块本身不会替任何一方编造数字。
"""

from __future__ import annotations

import json
from pathlib import Path

from warranty_evidence import (
    Actor,
    build_report,
    compare_versions,
    drill_down,
)
from warranty_evidence.reference import build_dossier_from_case, load_case

REVIEWER = Actor("u-01", "质保审核人-林岚")
SUPPLIER = Actor("u-02", "供应商运维-周岭")

# 合同计量条款摘录（参考文件只给了 minimum_availability；
# rated_energy_kwh 引自合同附件 EX-MET-03，需有独立佐证）
EXTRA_TERMS = {
    "2025-A": {"rated_energy_kwh": "500"},
    "2026-B": {"rated_energy_kwh": "500"},
}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    raw = load_case(root / "reference" / "warranty_case.json")
    d = build_dossier_from_case(raw, extra_terms=EXTRA_TERMS,
                                case_end="2026-12-31T23:59:59+08:00")

    # ---- v1：冻结前已掌握的事实 ---------------------------------------
    # 旧表 2025 年末读数（跨合同版本边界的增量将按时间分摊）
    d.import_file(
        "sha256:m88-2025-yearend", "M88-202512.csv", SUPPLIER,
        [{"ts": "2025-12-15T00:00:00+08:00", "value": "17850.00"}],
        meter_id="M-88",
    )
    # 缺口前最后一条旧表读数
    d.import_file(
        "sha256:m88-2026-may", "M88-202605.csv", SUPPLIER,
        [{"ts": "2026-05-01T00:00:00+08:00", "value": "17900.00"}],
        meter_id="M-88",
    )
    # 采集链路中断：显式登记证据缺口，绝不插值
    gap = d.declare_gap(
        "2026-05-10T00:00:00+08:00", "2026-06-10T00:00:00+08:00",
        "通信模块故障，期间无表计导出；换表前未能恢复", REVIEWER,
    )
    # 换表事实由参考资料登记（旧表终值 18420.50 / 新表初值 12.25 / 06-15 10:30）
    # 新表换表后的两条导出读数
    d.import_file(
        "sha256:m104-2026-q3", "M104-2026Q3.csv", SUPPLIER,
        [{"ts": "2026-07-01T00:00:00+08:00", "value": "2012.25"},
         {"ts": "2026-09-01T00:00:00+08:00", "value": "3012.25"}],
        meter_id="M-104",
    )

    v1_snap = d.freeze("v1-submission-2026Q3", REVIEWER)
    v1 = build_report(d, "v1-submission-2026Q3")

    print("=" * 72)
    print("送审版本 v1（冻结后不可变）")
    print("=" * 72)
    print(json.dumps({
        "label": v1["version_label"],
        "frozen_digest": v1["frozen_digest"],
        "totals": v1["totals"],
        "identity_history": v1["identity_history"],
        "open_gaps": [g["seq"] for g in v1["evidence_gaps"] if g["status"] == "open"],
        "anomalies": v1["anomalies"],
    }, ensure_ascii=False, indent=2))

    # ---- v2：供应商补交证据，只能进入子分支 ----------------------------
    child = d.branch()
    child.close_gap(
        gap.seq,
        [{"ts": "2026-05-25T00:00:00+08:00", "value": "18300.00",
          "meter_id": "M-88", "source": "manual_entry",
          "note": "双方现场盘点的手抄表读数，附盘点单 STOCK-8 与照片 PHOTO-7"}],
        SUPPLIER,
        "供应商补交换装前现场盘点手抄表，经审核人与设备团队共同核对",
        refs=["STOCK-8", "PHOTO-7"],
    )
    # 新表 9 月读数经核对时区偏差，更正 3012.25 -> 3112.25（原值保留）
    later = max(seq for seq, r in child.head().readings.items() if r.meter_id == "M-104")
    child.correct_reading(
        later, "3112.25", SUPPLIER,
        "导出文件按 UTC 落库造成偏差，以表计屏幕照片 P-22 为准",
        refs=["P-22"],
    )
    child.freeze("v2-submission-2026Q3", REVIEWER)

    v2 = build_report(child, "v2-submission-2026Q3")
    cmp = compare_versions(child, "v1-submission-2026Q3", "v2-submission-2026Q3")

    print("\n" + "=" * 72)
    print("送审版本 v2（子分支补录与更正后冻结）——两版多算/少算比对")
    print("=" * 72)
    print(json.dumps({
        "old_totals": cmp["old_totals"],
        "new_totals": cmp["new_totals"],
        "observed_delta_kwh": cmp["observed_delta_kwh"],
        "cycles_delta": cmp["cycles_delta"],
        "verdict": cmp["verdict"],
        "by_contract": cmp["by_contract"],
        "line_changes": cmp["line_changes"],
        "newly_closed_gaps": cmp["newly_closed_gaps"],
    }, ensure_ascii=False, indent=2))

    # ---- 从汇总数字下钻到计量记录、身份沿革与修订依据 -------------------
    any_component = next(c for b in v2["by_contract"] for c in b["components"])
    detail = drill_down(v2, any_component["key"])
    print("\n" + "=" * 72)
    print("下钻示例：分量 -> 两端读数 -> 文件摘要 / 修订依据")
    print("=" * 72)
    print(json.dumps(detail, ensure_ascii=False, indent=2))

    # 冻结摘要稳定性自检
    assert build_report(child, "v1-submission-2026Q3")["frozen_digest"] == v1_snap.digest
    print("\n自检通过：v1 冻结摘要在分支补录后保持不变。")


if __name__ == "__main__":
    main()

import json
import unittest
from decimal import Decimal

from warranty_evidence import Actor, Dossier, build_report, compare_versions, drill_down

REVIEWER = Actor("u-01", "质保审核人")
SUPPLIER = Actor("u-02", "供应商运维")

CONTRACTS = [
    {"version": "2025-A", "effective_from": "2025-01-01T00:00:00+08:00",
     "minimum_availability": "0.97", "rated_energy_kwh": "500"},
    {"version": "2026-B", "effective_from": "2026-01-01T00:00:00+08:00",
     "minimum_availability": "0.975", "rated_energy_kwh": "500"},
]


def build_disputed_case():
    """复刻争议：旧表 M-88 累计到换表，新表 M-104 重新起步，中段存在缺口。"""
    d = Dossier(
        asset_id="cabinet-A17", initial_meter_id="M-88",
        case_start="2025-01-01T00:00:00+08:00",
        contracts=CONTRACTS, case_end="2026-12-31T23:59:59+08:00",
    )
    d.import_file("sha256:old-export", "M88-before.csv", SUPPLIER,
                  [{"ts": "2025-12-31T23:59:59+08:00", "value": "18000.00"}], "M-88")
    gap = d.declare_gap("2026-05-01T00:00:00+08:00", "2026-06-10T00:00:00+08:00",
                        "通信模块故障，期间无表计导出", REVIEWER)
    d.record_meter_change("M-88", "18420.50", "M-104", "12.25",
                          "2026-06-15T10:30:00+08:00", SUPPLIER, reason="模块烧毁换表")
    d.import_file("sha256:new-export", "M104-after.csv", SUPPLIER,
                  [{"ts": "2026-08-01T00:00:00+08:00", "value": "1012.25"},
                   {"ts": "2026-09-01T00:00:00+08:00", "value": "1612.25"}], "M-104")
    return d, gap


class ReportTest(unittest.TestCase):
    def test_report_totals_and_identity_history(self):
        d, _ = build_disputed_case()
        report = build_report(d)
        # 旧表 18000->18420.50 跨缺口隔离 420.50；
        # 新表锚点 12.25->1012.25 计 1000，1012.25->1612.25 计 600。
        self.assertEqual(report["totals"]["observed_kwh"], "1600.00")
        self.assertEqual([h["meter_id"] for h in report["identity_history"]],
                         ["M-88", "M-104"])
        change = report["meter_changes"][0]
        self.assertEqual(change["old_final_kwh"], "18420.50")
        self.assertEqual(change["new_initial_kwh"], "12.25")
        self.assertTrue(report["evidence_gaps"][0]["interpolation"] == "forbidden")

    def test_drill_down_reaches_reading_import_and_revision(self):
        d, _ = build_disputed_case()
        report = build_report(d)
        key = next(
            c["key"] for b in report["by_contract"] for c in b["components"]
            if c["basis"] == "observed"
        )
        detail = drill_down(report, key)
        chains = detail["endpoint_readings"]
        self.assertTrue(chains)
        sources = {entry["source"] for chain in chains for entry in chain["provenance_chain"]}
        self.assertIn("meter_export", sources)
        flat = [e for chain in chains for e in chain["provenance_chain"]]
        digests = {e["import_digest"] for e in flat}
        self.assertTrue(digests & {"sha256:old-export", "sha256:new-export"})
        # 换表锚点分量可下钻到换表记录
        anchor_key = next(
            c["key"] for b in report["by_contract"] for c in b["components"]
            if c["basis"] == "meter_change_anchor"
        )
        anchor_detail = drill_down(report, anchor_key)
        self.assertIsNotNone(anchor_detail["meter_change"])

    def test_freeze_then_branch_then_compare_lists_overclaim(self):
        d, gap = build_disputed_case()
        d.freeze("v1-submission", REVIEWER)
        child = d.branch()

        # 供应商在新分支上：补录缺口（少算的 420.5 中部分恢复）并更正新表读数（多算方向）
        child.close_gap(
            gap.seq,
            [{"ts": "2026-05-20T00:00:00+08:00", "value": "18210.00",
              "meter_id": "M-88", "source": "manual_entry",
              "note": "现场手抄表，附换装前盘点单"}],
            SUPPLIER, "依据双方盘点的手抄表补录", refs=["STOCK-8"],
        )
        # 更正新表末条读数 1612.25 -> 2112.25：新分支主张更多吞吐量
        later_reading_seq = max(
            seq for seq, r in child.head().readings.items() if r.meter_id == "M-104"
        )
        child.correct_reading(later_reading_seq, "2112.25", SUPPLIER,
                              "导出文件时区偏差，按表计屏幕照片更正", refs=["P-22"])
        child.freeze("v2-submission", REVIEWER)

        v1 = build_report(child, "v1-submission")
        v2 = build_report(child, "v2-submission")
        # 两版冻结摘要都存在且不同
        self.assertNotEqual(v1["frozen_digest"], v2["frozen_digest"])
        self.assertEqual(v1["version_label"], "v1-submission")
        self.assertEqual(v2["parent_version"], "v1-submission")

        cmp = compare_versions(child, "v1-submission", "v2-submission")
        # 缺口闭合恢复 420.50，末条读数更正增加 500，新版本相对旧版本多算 920.50
        self.assertEqual(cmp["new_version"], "v2-submission")
        self.assertEqual(cmp["verdict"], "新版本多算")
        self.assertEqual(cmp["observed_delta_kwh"], "920.50")
        reasons = {line["reason"] for line in cmp["line_changes"]}
        self.assertIn("gap_closed", reasons)
        self.assertIn("correction", reasons)
        self.assertTrue(any(line["delta_kwh"] == "500.00" for line in cmp["line_changes"]))
        # 旧版报告不泄漏分支事件：v1 中没有任何修订与补录读数
        self.assertEqual(v1["revisions"], [])
        self.assertEqual(
            len(v1["readings"]), len(v2["readings"]) - 2
        )  # 1 条缺口补录 + 1 条更正新读数

    def test_report_is_json_serializable(self):
        d, _ = build_disputed_case()
        report = build_report(d)
        json.dumps(report, ensure_ascii=False)  # 不抛异常即可


if __name__ == "__main__":
    unittest.main()

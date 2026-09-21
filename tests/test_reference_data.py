"""资料口径自检：规则摘录、身份映射、争议案卷与读数文件必须互相印证。"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

import support
from warranty_evidence.reference_case import (
    load_dispute_case,
    load_identity,
    load_rules,
    reference_dir,
)


class ReferenceConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dispute = load_dispute_case()
        cls.rules = load_rules()
        cls.identity = load_identity()

    def test_rules_match_dispute_contract_versions(self):
        dispute_versions = {v["version"]: v for v in self.dispute["contract_versions"]}
        self.assertEqual(set(dispute_versions),
                         {v.version for v in self.rules.versions})
        for ver in self.rules.versions:
            agreed = dispute_versions[ver.version]
            self.assertEqual(ver.effective_from, support.ts(agreed["effective_from"]))
            self.assertEqual(ver.minimum_availability,
                             support.as_decimal(agreed["minimum_availability"]))

    def test_identity_matches_meter_change_record(self):
        change = self.dispute["meter_change"]
        old = self.identity.service_of(change["old_meter"]["id"])
        new = self.identity.service_of(change["new_meter"]["id"])
        self.assertIsNotNone(old)
        self.assertIsNotNone(new)
        self.assertEqual(old.service_until, support.ts(change["changed_at"]))
        self.assertEqual(new.service_from, support.ts(change["changed_at"]))
        self.assertEqual(old.final_kwh,
                         support.as_decimal(change["old_meter"]["final_kwh"]))
        self.assertEqual(new.initial_kwh,
                         support.as_decimal(change["new_meter"]["initial_kwh"]))

    def test_telemetry_file_anchors_meter_change_endpoints(self):
        path = reference_dir() / "readings" / "2025-2026_telemetry.jsonl"
        rows = [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]
        change = self.dispute["meter_change"]
        at = change["changed_at"]
        endpoints = {(r["meter_id"], r["observed_at"]): r["cumulative_kwh"] for r in rows}
        self.assertEqual(
            endpoints[(change["old_meter"]["id"], at)],
            change["old_meter"]["final_kwh"],
            "旧表终值读数必须与换表记录一致",
        )
        self.assertEqual(
            endpoints[(change["new_meter"]["id"], at)],
            change["new_meter"]["initial_kwh"],
            "新表初值读数必须与换表记录一致",
        )
        # 合同边界时刻必须有读数，严格策略下期间归属才解得开
        self.assertIn(("M-88", "2026-01-01T00:00:00+08:00"), endpoints)

    def test_readings_are_monotonic_per_meter(self):
        for name in ("2025-2026_telemetry.jsonl", "2026-09_supplement.jsonl"):
            path = reference_dir() / "readings" / name
            rows = [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines() if line.strip()]
            by_meter = {}
            for row in rows:
                by_meter.setdefault(row["meter_id"], []).append(row)
            for meter_id, meter_rows in by_meter.items():
                meter_rows.sort(key=lambda r: r["observed_at"])
                values = [Decimal(r["cumulative_kwh"]) for r in meter_rows]
                self.assertEqual(values, sorted(values),
                                 f"{name} 中 {meter_id} 的表码出现回退")

    def test_supplement_readings_fill_coverage_gap(self):
        path = reference_dir() / "readings" / "2026-09_supplement.jsonl"
        rows = [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(r["observed_at"] == "2025-01-01T00:00:00+08:00"
                            and r["source"] == "manual" for r in rows),
                        "调档补录必须覆盖窗口起点")
        for row in rows:
            if row["source"] == "manual":
                self.assertTrue(row.get("operator"), "人工补录必须注明操作者")
                self.assertTrue(row.get("reason"), "人工补录必须注明理由")


if __name__ == "__main__":
    unittest.main()

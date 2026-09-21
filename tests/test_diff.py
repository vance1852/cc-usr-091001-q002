"""参考案卷的版本差异：V1 → V2 多算/少算清单逐条对账。"""
import unittest
from decimal import Decimal

from warranty_evidence.reference_case import build_reference_case


class ReferenceDiffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = build_reference_case("V2")
        cls.diff = cls.case.diff("V1", "V2")

    def test_total_and_period_deltas(self):
        total = self.diff["total"]
        self.assertEqual(total["throughput_old"], Decimal("19392.00"))
        self.assertEqual(total["throughput_new"], Decimal("19434.25"))
        self.assertEqual(total["throughput_delta"], Decimal("42.25"))
        periods = {p["version"]: p for p in self.diff["period_deltas"]}
        # 年末错位更正把 306.00 从 2025-A 挪到 2026-B
        self.assertEqual(periods["2025-A"]["throughput_delta"], Decimal("-263.75"))
        self.assertEqual(periods["2026-B"]["throughput_delta"], Decimal("306.00"))
        # 循环次数随吞吐与各自版本额定能量变化
        self.assertEqual(periods["2025-A"]["cycles_delta"],
                         Decimal("-263.75") / Decimal("200.0"))
        self.assertAlmostEqual(
            float(periods["2026-B"]["cycles_delta"]),
            float(Decimal("306.00") / Decimal("215.0")),
            places=5,
        )

    def test_over_and_under_counted_lists(self):
        over = self.diff["over_counted"]
        under = self.diff["under_counted"]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["version"], "2025-A")
        self.assertEqual(over[0]["kwh"], Decimal("263.75"))
        self.assertEqual(len(under), 1)
        self.assertEqual(under[0]["version"], "2026-B")
        self.assertEqual(under[0]["kwh"], Decimal("306.00"))

    def test_corrections_and_additions_are_listed(self):
        corrections = self.diff["corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["new_cumulative_kwh"], Decimal("13462.00"))
        self.assertEqual(corrections[0]["operator"], "auditor-wang")
        self.assertIn("WX-221", corrections[0]["reason"])
        additions = self.diff["additions"]
        self.assertEqual(len(additions), 2)
        self.assertTrue(all(a["source"] == "manual" for a in additions))
        self.assertTrue(all(a["operator"] == "op-liu" for a in additions))

    def test_coverage_gap_closed_by_supplement(self):
        closed = self.diff["gaps_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["kind"], "coverage")
        self.assertTrue(closed[0]["start"].startswith("2025-01-01"))
        self.assertEqual(self.diff["gaps_opened"], [])

    def test_segment_deltas_carry_causes(self):
        deltas = self.diff["segment_deltas"]
        self.assertTrue(len(deltas) > 0)
        # 每一条分段差异都能指到成因（更正或补录）
        for delta in deltas:
            self.assertTrue(delta["causes"], f"分段 {delta['start']}→{delta['end']} 缺成因")
        # 年末边界两侧的分段是更正的直接现场
        boundary = [d for d in deltas
                    if d["start"] == "2025-12-01T00:00:00+08:00"
                    and d["end"] == "2026-01-01T00:00:00+08:00"]
        self.assertEqual(len(boundary), 1)
        self.assertEqual(boundary[0]["delta_kwh"], Decimal("-306.00"))
        self.assertTrue(any("更正" in c for c in boundary[0]["causes"]))
        # 窗口起点的新增分段来自调档补录
        opening = [d for d in deltas
                   if d["start"] == "2025-01-01T00:00:00+08:00"]
        self.assertEqual(len(opening), 1)
        self.assertEqual(opening[0]["delta_kwh"], Decimal("42.25"))
        self.assertTrue(any("补录" in c for c in opening[0]["causes"]))

    def test_gap_closed_by_supplement_is_reported(self):
        closed = self.diff["gaps_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["kind"], "coverage")
        self.assertEqual(closed[0]["start"], "2025-01-01T00:00:00+08:00")
        self.assertEqual(self.diff["gaps_opened"], [])

    def test_summary_lines_mention_both_directions(self):
        text = "\n".join(self.diff["summary_lines"])
        self.assertIn("多计", text)
        self.assertIn("少计", text)
        self.assertIn("42.25", text)


if __name__ == "__main__":
    unittest.main()

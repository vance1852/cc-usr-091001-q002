"""计量口径：换表衔接、合同版本归属、缺口不插值、等效循环。"""
import unittest
from decimal import Decimal

import support
from warranty_evidence.metrics import (
    BOUNDARY_APPORTION,
    BOUNDARY_STRICT,
    compute_metrics,
)
from warranty_evidence.model import Window


def metrics_for(revisions, changes=(), window=None, rules=None,
                two_meters=True, policy=BOUNDARY_STRICT):
    ledger = support.ledger_with(revisions, changes=changes)
    view = ledger.view()
    return compute_metrics(
        view,
        support.make_identity(two_meters),
        rules or support.make_rules(),
        window or support.make_window(),
        policy,
    )


class MeterChangeSpliceTest(unittest.TestCase):
    """换表衔接：旧表终值 + 新表初值 + 生效时刻共同定义连续累计。"""

    def test_asset_cumulative_continues_across_change(self):
        result = metrics_for([
            support.rev("M-88", "2026-06-01T00:00:00+08:00", "18120.00"),
            support.rev("M-88", "2026-06-15T10:30:00+08:00", "18420.50"),
            support.rev("M-104", "2026-06-15T10:30:00+08:00", "12.25"),
            support.rev("M-104", "2026-07-01T00:00:00+08:00", "525.50"),
        ], changes=[support.make_change()])
        # 新表读数换算为资产累计：525.50 - 12.25 + 18420.50
        point = [p for p in result.points
                 if p.at == support.ts("2026-07-01T00:00:00+08:00")][0]
        self.assertEqual(point.asset_cum_kwh, Decimal("18933.75"))
        # 换表生效时刻的衔接点：旧表终值即资产累计
        splice = [p for p in result.points
                  if p.at == support.ts("2026-06-15T10:30:00+08:00")][0]
        self.assertEqual(splice.asset_cum_kwh, Decimal("18420.50"))
        self.assertTrue(any(b.kind == "meter_change" for b in splice.basis))
        # 跨表分段：06-01 → 06-15 → 07-01 的吞吐连续可算
        kwh_by_segment = {(s.start, s.end): s.kwh for s in result.segments}
        self.assertEqual(
            kwh_by_segment[(support.ts("2026-06-15T10:30:00+08:00"),
                            support.ts("2026-07-01T00:00:00+08:00"))],
            Decimal("513.25"),
        )

    def test_change_record_disagreeing_with_boundary_reading_is_flagged(self):
        change = support.make_change()
        # 边界读数与换表记录终值不一致：时间线必须报警而不是悄悄取一个
        result = metrics_for([
            support.rev("M-88", "2026-06-15T10:30:00+08:00", "18400.00"),
            support.rev("M-104", "2026-06-15T10:30:00+08:00", "12.25"),
        ], changes=[change])
        self.assertTrue(any(f.code == "SPLICE_MISMATCH" for f in result.findings))


class ContractBoundaryTest(unittest.TestCase):
    """依据当时生效的合同版本归属吞吐、计算循环。"""

    def _boundary_readings(self):
        return [
            support.rev("M-88", "2025-12-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-01-01T00:00:00+08:00", "200.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "300.00"),
        ]

    def test_segments_attribute_to_version_in_effect(self):
        window = Window(start=support.ts("2025-12-01T00:00:00+08:00"),
                        end=support.ts("2026-02-01T00:00:00+08:00"))
        result = metrics_for(self._boundary_readings(), window=window, two_meters=False)
        periods = {p.version: p for p in result.periods}
        self.assertEqual(periods["2025-A"].throughput_kwh, Decimal("100.00"))
        self.assertEqual(periods["2026-B"].throughput_kwh, Decimal("100.00"))
        # 等效循环用各自版本的额定能量
        self.assertEqual(periods["2025-A"].cycles, Decimal("0.500000"))  # 100 / 200
        self.assertEqual(periods["2026-B"].cycles,
                         (Decimal("100") / Decimal("215")).quantize(Decimal("0.000001")))
        self.assertEqual(result.unattributed, [])

    def test_crossing_segment_stays_unattributed_under_strict_policy(self):
        window = Window(start=support.ts("2025-12-01T00:00:00+08:00"),
                        end=support.ts("2026-02-01T00:00:00+08:00"))
        readings = [
            support.rev("M-88", "2025-12-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "300.00"),
        ]
        result = metrics_for(readings, window=window, two_meters=False,
                             policy=BOUNDARY_STRICT)
        # 吞吐计入窗口合计，但不归属任何期间，也绝不按时间摊派
        self.assertEqual(result.total_kwh, Decimal("200.00"))
        self.assertEqual(len(result.unattributed), 1)
        for period in result.periods:
            self.assertEqual(period.throughput_kwh, Decimal("0"))
        self.assertTrue(any(f.code == "BOUNDARY_UNATTRIBUTED" for f in result.findings))

    def test_apportion_policy_splits_and_labels(self):
        window = Window(start=support.ts("2025-12-15T00:00:00+08:00"),
                        end=support.ts("2026-02-01T00:00:00+08:00"))
        readings = [
            support.rev("M-88", "2025-12-15T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "300.00"),
        ]
        result = metrics_for(readings, window=window, two_meters=False,
                             policy=BOUNDARY_APPORTION)
        periods = {p.version: p for p in result.periods}
        # 17 天归 2025-A，31 天归 2026-B，总量不变且显式标注 apportioned
        self.assertEqual(periods["2025-A"].throughput_kwh,
                         (Decimal("200") * 17 / 48).quantize(Decimal("0.000001")))
        self.assertEqual(periods["2026-B"].throughput_kwh,
                         (Decimal("200") * 31 / 48).quantize(Decimal("0.000001")))
        self.assertEqual(periods["2025-A"].throughput_kwh
                         + periods["2026-B"].throughput_kwh, Decimal("200.000000"))
        self.assertTrue(all(s.apportioned for s in result.segments))


class GapTest(unittest.TestCase):
    """缺测区间是证据缺口，不是待估的数。"""

    def test_window_edges_are_coverage_gaps(self):
        window = Window(start=support.ts("2025-01-01T00:00:00+08:00"),
                        end=support.ts("2026-01-01T00:00:00+08:00"))
        result = metrics_for([
            support.rev("M-88", "2025-06-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2025-07-01T00:00:00+08:00", "200.00"),
        ], window=window, two_meters=False)
        coverage = [g for g in result.gaps if g.kind == "coverage"]
        self.assertEqual(len(coverage), 2)
        self.assertEqual(coverage[0].start, window.start)
        self.assertEqual(coverage[1].end, window.end)
        # 只有被证据夹住的一段计入吞吐
        self.assertEqual(result.total_kwh, Decimal("100.00"))

    def test_counter_regression_becomes_gap_not_negative_throughput(self):
        result = metrics_for([
            support.rev("M-88", "2026-01-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "95.00"),
            support.rev("M-88", "2026-03-01T00:00:00+08:00", "150.00"),
        ], two_meters=False)
        regression = [g for g in result.gaps if g.kind == "counter_regression"]
        self.assertEqual(len(regression), 1)
        self.assertEqual(regression[0].start, support.ts("2026-01-01T00:00:00+08:00"))
        # 回退区间不计吞吐，也不出现负分段；其后的正常分段不受影响
        self.assertEqual(result.total_kwh, Decimal("55.00"))
        self.assertTrue(all(s.kwh >= 0 for s in result.segments))
        self.assertTrue(any(f.code == "COUNTER_REGRESSION" for f in result.findings))

    def test_conflicting_readings_break_timeline_into_gap(self):
        ledger = support.ledger_with([
            support.rev("M-88", "2026-01-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "150.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "155.00"),  # 同刻矛盾
            support.rev("M-88", "2026-03-01T00:00:00+08:00", "200.00"),
        ])
        result = compute_metrics(ledger.view(), support.make_identity(False),
                                 support.make_rules(), support.make_window())
        conflict_gaps = [g for g in result.gaps if g.kind == "reading_conflict"]
        self.assertEqual(len(conflict_gaps), 1)
        self.assertEqual(conflict_gaps[0].start, support.ts("2026-01-01T00:00:00+08:00"))
        self.assertEqual(conflict_gaps[0].end, support.ts("2026-03-01T00:00:00+08:00"))
        self.assertEqual(result.total_kwh, Decimal("0"))
        self.assertTrue(any(f.code == "READING_CONFLICT" for f in result.findings))

    def test_no_interpolation_anywhere(self):
        # 中间静默三个月：累计表码下区间吞吐仍可精确结算，不算缺口
        result = metrics_for([
            support.rev("M-88", "2026-01-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-04-01T00:00:00+08:00", "400.00"),
        ], two_meters=False)
        self.assertEqual(result.total_kwh, Decimal("300.00"))
        self.assertFalse([g for g in result.gaps if g.kind == "counter_regression"])


class CorrectionEffectTest(unittest.TestCase):
    """更正改变计量结果，且只通过新修订生效。"""

    def test_corrected_boundary_reading_moves_throughput_between_periods(self):
        readings = [
            support.rev("M-88", "2025-12-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-01-01T00:00:00+08:00", "200.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "300.00"),
        ]
        window = Window(start=support.ts("2025-12-01T00:00:00+08:00"),
                        end=support.ts("2026-02-01T00:00:00+08:00"))
        case = support.make_case(two_meters=False, with_change=False, window=window)
        ledger = support.ledger_with(readings)
        case.ledger = ledger
        before = compute_metrics(ledger.view(), case.identity, case.rules, window)
        rid = case.find_reading("M-88", support.ts("2026-01-01T00:00:00+08:00"))
        case.correct_reading(rid, operator="auditor", reason="年末抄表错位",
                             at=support.T0, cumulative_kwh="180.00")
        after = compute_metrics(case.ledger.view(), case.identity, case.rules, window)
        p_before = {p.version: p for p in before.periods}
        p_after = {p.version: p for p in after.periods}
        self.assertEqual(p_before["2025-A"].throughput_kwh, Decimal("100.00"))
        self.assertEqual(p_after["2025-A"].throughput_kwh, Decimal("80.00"))
        self.assertEqual(p_after["2026-B"].throughput_kwh, Decimal("120.00"))
        # 窗口合计不受中间读数更正影响（端点未动）
        self.assertEqual(before.total_kwh, after.total_kwh)


if __name__ == "__main__":
    unittest.main()

import unittest
from decimal import Decimal

from warranty_evidence import (
    Actor,
    Dossier,
    ReadingSource,
    compute,
)
from warranty_evidence.computation import (
    BASIS_ANCHOR,
    BASIS_OBSERVED,
    BASIS_SPANS_OPEN_GAP,
    BASIS_TIME_APPORTIONED,
)
from warranty_evidence.reference import build_dossier_from_case, load_case

REVIEWER = Actor("u-01", "质保审核人")
SUPPLIER = Actor("u-02", "供应商运维")

CONTRACTS = [
    {"version": "2025-A", "effective_from": "2025-01-01T00:00:00+08:00",
     "minimum_availability": "0.97", "rated_energy_kwh": "500"},
    {"version": "2026-B", "effective_from": "2026-01-01T00:00:00+08:00",
     "minimum_availability": "0.975", "rated_energy_kwh": "500"},
]


def dossier_with_meter_change():
    d = Dossier(
        asset_id="cabinet-A17",
        initial_meter_id="M-88",
        case_start="2025-01-01T00:00:00+08:00",
        contracts=CONTRACTS,
        case_end="2026-12-31T23:59:59+08:00",
    )
    d.record_meter_change("M-88", "1000.00", "M-104", "10.00",
                          "2026-06-15T10:30:00+08:00", SUPPLIER)
    return d


class ComputationTest(unittest.TestCase):
    def test_throughput_continuous_across_meter_change(self):
        d = dossier_with_meter_change()
        # 旧表累计：200 -> 1000（终值锚点）；新表：10（初值锚点）-> 310 -> 510
        d.import_file("sha256:old", "old.csv", SUPPLIER,
                      [{"ts": "2025-06-01T00:00:00+08:00", "value": "200.00"}], "M-88")
        d.import_file("sha256:new", "new.csv", SUPPLIER,
                      [{"ts": "2026-07-01T00:00:00+08:00", "value": "310.00"},
                       {"ts": "2026-09-01T00:00:00+08:00", "value": "510.00"}], "M-104")
        result = compute(d.head())
        # 旧表段 800 + 新表段 300 + 200 = 1300，新旧表读数绝不相减
        self.assertEqual(result.total_observed_kwh, Decimal("1300.00"))
        bases = {c.basis for c in result.components}
        self.assertIn(BASIS_ANCHOR, bases)
        self.assertIn(BASIS_OBSERVED, bases)
        self.assertEqual(result.total_cycles, Decimal("2.600000"))

    def test_open_gap_quarantines_delta_without_interpolation(self):
        d = dossier_with_meter_change()
        d.declare_gap("2026-03-01T00:00:00+08:00", "2026-04-01T00:00:00+08:00",
                      "通信中断，整月无导出", REVIEWER)
        d.import_file("sha256:before", "b.csv", SUPPLIER,
                      [{"ts": "2026-02-01T00:00:00+08:00", "value": "500.00"}], "M-88")
        d.import_file("sha256:after", "a.csv", SUPPLIER,
                      [{"ts": "2026-05-01T00:00:00+08:00", "value": "900.00"}], "M-88")
        result = compute(d.head())
        # 500->900 的增量跨过缺口，整体隔离；观察吞吐量不得偷偷按时间插值计入。
        # 但 900->旧表终值锚点 1000 这一段不跨缺口，仍计 100。
        self.assertEqual(result.total_observed_kwh, Decimal("100.00"))
        self.assertEqual(result.total_quarantined_kwh, Decimal("400.00"))
        self.assertTrue(any(a["kind"] == "spans_open_gap" for a in result.anomalies))
        non_gap = [c for c in result.components if c.basis != BASIS_SPANS_OPEN_GAP]
        self.assertTrue(all(not c.quarantined for c in non_gap))

    def test_gap_closure_on_branch_recovers_delta(self):
        d = dossier_with_meter_change()
        gap = d.declare_gap("2026-03-01T00:00:00+08:00", "2026-04-01T00:00:00+08:00",
                            "通信中断", REVIEWER)
        d.import_file("sha256:b", "b.csv", SUPPLIER,
                      [{"ts": "2026-02-01T00:00:00+08:00", "value": "500.00"}], "M-88")
        d.import_file("sha256:a", "a.csv", SUPPLIER,
                      [{"ts": "2026-05-01T00:00:00+08:00", "value": "900.00"}], "M-88")
        d.freeze("v1", REVIEWER)
        before = compute(d.state_at("v1"))
        # 400 跨缺口隔离；锚点段 900->1000 计 100
        self.assertEqual(before.total_observed_kwh, Decimal("100.00"))
        self.assertEqual(before.total_quarantined_kwh, Decimal("400.00"))

        child = d.branch()
        child.close_gap(
            gap.seq,
            [{"ts": "2026-03-15T00:00:00+08:00", "value": "680.00",
              "meter_id": "M-88", "source": "manual_entry",
              "note": "供应商提供现场手抄表与检定照片，经双方核对"}],
            SUPPLIER, "依据手抄表与现场照片补录", refs=["WO-99", "PHOTO-7"],
        )
        after = compute(child.head())
        # 长增量被补录点切成两段：500->680、680->900，连同锚点段共 500 全部可计量
        self.assertEqual(after.total_observed_kwh, Decimal("500.00"))
        self.assertEqual(after.total_quarantined_kwh, Decimal("0"))
        # 旧版本视图保持不变
        self.assertEqual(compute(child.state_at("v1")).total_observed_kwh, Decimal("100.00"))

    def test_register_regression_is_quarantined(self):
        d = dossier_with_meter_change()
        d.import_file("sha256:r1", "r1.csv", SUPPLIER,
                      [{"ts": "2026-07-01T00:00:00+08:00", "value": "500.00"},
                       {"ts": "2026-07-02T00:00:00+08:00", "value": "400.00"}],
                      "M-104")
        result = compute(d.head())
        # 初值锚点 10 -> 500 计入；500 -> 400 回退隔离，不得抵减累计量
        self.assertEqual(result.total_observed_kwh, Decimal("490.00"))
        self.assertTrue(any(a["kind"] == "register_regression" for a in result.anomalies))

    def test_cross_contract_version_time_apportionment_is_exact(self):
        d = Dossier(
            asset_id="cabinet-X", initial_meter_id="M-1",
            case_start="2025-12-31T12:00:00+08:00",
            contracts=CONTRACTS,
            case_end="2026-12-31T23:59:59+08:00",
        )
        # 增量 480 千瓦时，恰好跨越 2026-01-01 00:00（+08:00），前后各 12 小时
        d.import_file("sha256:x1", "x1.csv", SUPPLIER,
                      [{"ts": "2025-12-31T12:00:00+08:00", "value": "0.00"},
                       {"ts": "2026-01-01T12:00:00+08:00", "value": "480.00"}], "M-1")
        result = compute(d.head())
        comps = [c for c in result.components if c.basis == BASIS_TIME_APPORTIONED]
        self.assertEqual(len(comps), 2)
        self.assertEqual({c.contract_version for c in comps}, {"2025-A", "2026-B"})
        # 各一半，且分量之和严格等于原始增量（无舍入尾差）
        self.assertEqual(sum((c.kwh for c in comps), Decimal(0)), Decimal("480"))
        by_version = {c.contract_version: c.kwh for c in comps}
        self.assertEqual(by_version["2025-A"], Decimal("240.000000"))
        self.assertEqual(by_version["2026-B"], Decimal("240.000000"))

    def test_correction_changes_only_revision_value(self):
        d = dossier_with_meter_change()
        rec = d.import_file("sha256:m1", "m1.csv", SUPPLIER,
                            [{"ts": "2026-07-01T00:00:00+08:00", "value": "310.00"}],
                            "M-104")
        self.assertEqual(compute(d.head()).total_observed_kwh, Decimal("300.00"))
        d.correct_reading(rec.reading_seqs[0], "210.00", REVIEWER, "倍率错误")
        result = compute(d.head())
        self.assertEqual(result.total_observed_kwh, Decimal("200.00"))

    def test_reference_case_loads_and_endpoints_anchor(self):
        raw = load_case("reference/warranty_case.json")
        d = build_dossier_from_case(raw, extra_terms={
            "2025-A": {"rated_energy_kwh": "500"},
            "2026-B": {"rated_energy_kwh": "500"},
        })
        st = d.head()
        ch = st.active_changes()[0]
        self.assertEqual(ch.old_meter_id, "M-88")
        self.assertEqual(ch.new_meter_id, "M-104")
        tenures = st.meter_tenures()
        self.assertEqual([t.meter_id for t in tenures], ["M-88", "M-104"])
        self.assertEqual(tenures[1].initial, Decimal("12.25"))
        # 无读数时观察吞吐量为 0，且不产生任何凭空数字
        self.assertEqual(compute(st).total_observed_kwh, Decimal("0"))


if __name__ == "__main__":
    unittest.main()

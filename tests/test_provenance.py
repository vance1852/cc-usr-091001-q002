"""来源溯源：每个读数都要讲清楚是谁量的、哪只表、哪次导入、谁更正过。"""
import tempfile
import unittest

import support
from warranty_evidence.model import SourceKind
from warranty_evidence.provenance import identity_lineage, source_kind_of, source_label_of


class SourceLabelTest(unittest.TestCase):
    def setUp(self):
        self.identity = support.make_identity()

    def test_old_and_new_meter_labels(self):
        old_rev = support.rev("M-88", "2026-01-01T00:00:00+08:00", "100.00")
        new_rev = support.rev("M-104", "2026-07-01T00:00:00+08:00", "500.00")
        self.assertEqual(source_kind_of(old_rev, self.identity), "old_meter")
        self.assertEqual(source_kind_of(new_rev, self.identity), "new_meter")
        self.assertEqual(source_label_of(old_rev, self.identity), "原表 M-88 直读")
        self.assertEqual(source_label_of(new_rev, self.identity), "新表 M-104 直读")

    def test_manual_label_carries_operator(self):
        manual = support.rev("M-104", "2026-07-15T00:00:00+08:00", "980.00",
                             source=SourceKind.MANUAL, operator="op-liu",
                             reason="遥测中断人工抄表")
        self.assertEqual(source_kind_of(manual, self.identity), "manual")
        self.assertEqual(source_label_of(manual, self.identity),
                         "人工补录（op-liu，对应新表 M-104）")

    def test_identity_lineage_orders_meters(self):
        lineage = identity_lineage(self.identity, [support.make_change()])
        self.assertEqual([row["meter_id"] for row in lineage], ["M-88", "M-104"])
        self.assertEqual(lineage[0]["label"], "原表 M-88")
        self.assertEqual(lineage[1]["label"], "新表 M-104")
        # 新表的服役起点由换表记录锚定
        self.assertIsNotNone(lineage[1]["installed_by_change"])
        self.assertIsNone(lineage[0]["installed_by_change"])


class ExplainReadingTest(unittest.TestCase):
    def test_explain_card_traces_to_import_and_revisions(self):
        case = support.make_case()
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_readings_file(tmp, [
                {"meter_id": "M-88", "observed_at": "2026-01-01T00:00:00+08:00",
                 "cumulative_kwh": "100.00"},
            ])
            result = case.import_readings_file(path, operator="importer", at=support.T0)
            rid = result.reading_ids[0]
            case.correct_reading(rid, operator="auditor-wang", reason="抄表错位",
                                 at=support.T0, cumulative_kwh="110.00")
            digest = result.digest

        card = case.explain(rid)
        self.assertEqual(card["source_kind"], "old_meter")
        self.assertEqual(card["cumulative_kwh"], support.as_decimal("110.00"))
        self.assertEqual(card["import_digest"], digest)
        self.assertEqual(card["contract_version_at_reading"], "2026-B")
        self.assertEqual(card["meter_service"]["service_from"], "2025-01-01T00:00:00+08:00")
        self.assertEqual(len(card["revision_chain"]), 2)
        first, second = card["revision_chain"]
        self.assertEqual(first["cumulative_kwh"], support.as_decimal("100.00"))
        self.assertFalse(first["is_current"])
        self.assertEqual(second["operator"], "auditor-wang")
        self.assertEqual(second["reason"], "抄表错位")
        self.assertTrue(second["is_current"])
        # 换表记录与读数关联可查
        self.assertEqual(len(card["related_meter_changes"]), 1)

    def test_explain_at_frozen_version_hides_later_corrections(self):
        case = support.make_case()
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_readings_file(tmp, [
                {"meter_id": "M-88", "observed_at": "2026-01-01T00:00:00+08:00",
                 "cumulative_kwh": "100.00"},
            ])
            result = case.import_readings_file(path, operator="importer", at=support.T0)
            rid = result.reading_ids[0]
        case.freeze(operator="auditor", reason="送审", at=support.T0)
        case.correct_reading(rid, operator="auditor", reason="事后更正",
                             at=support.T0, cumulative_kwh="110.00")
        frozen_card = case.explain(rid, version="V1")
        self.assertEqual(frozen_card["cumulative_kwh"], support.as_decimal("100.00"))
        self.assertEqual(len(frozen_card["revision_chain"]), 1)
        current_card = case.explain(rid)
        self.assertEqual(current_card["cumulative_kwh"], support.as_decimal("110.00"))


if __name__ == "__main__":
    unittest.main()

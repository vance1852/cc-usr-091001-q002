"""案卷冻结与分支：送审版本逐字封存，补录进入新分支，重放必须对账。"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import support
from warranty_evidence.casefile import CaseFile
from warranty_evidence.ledger import LedgerError, TamperError
from warranty_evidence.report import report_digest


def _case_with_two_readings():
    case = support.make_case()
    tmp = tempfile.TemporaryDirectory()
    path = support.write_readings_file(Path(tmp.name), [
        {"meter_id": "M-88", "observed_at": "2026-01-01T00:00:00+08:00",
         "cumulative_kwh": "100.00"},
        {"meter_id": "M-88", "observed_at": "2026-02-01T00:00:00+08:00",
         "cumulative_kwh": "200.00"},
    ])
    case.import_readings_file(path, operator="importer", at=support.T0)
    return case, tmp


class FreezeTest(unittest.TestCase):
    def test_freeze_seals_version_and_later_supplements_branch_off(self):
        case, tmp = _case_with_two_readings()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(case.current_branch, "B1")
        v1 = case.freeze(operator="auditor", reason="首次送审", at=support.T0)
        self.assertEqual(v1.label, "V1")
        self.assertEqual(case.current_branch, "B2")

        # 冻结后补录进入新分支
        with tempfile.TemporaryDirectory() as tmp2:
            extra = support.write_readings_file(tmp2, [
                {"meter_id": "M-88", "observed_at": "2026-03-01T00:00:00+08:00",
                 "cumulative_kwh": "300.00"},
            ], name="extra.jsonl")
            case.import_readings_file(extra, operator="importer", at=support.T0)
        branches = {e.branch for e in case.ledger.events}
        self.assertIn("B1", branches)
        self.assertIn("B2", branches)
        last_import = [e for e in case.ledger.events
                       if e.event_type == "IMPORT_FILE"][-1]
        self.assertEqual(last_import.branch, "B2")

    def test_frozen_report_is_recomputed_and_verified(self):
        case, tmp = _case_with_two_readings()
        self.addCleanup(tmp.cleanup)
        v1 = case.freeze(operator="auditor", reason="首次送审", at=support.T0)
        frozen = case.report("V1")
        self.assertEqual(report_digest(frozen), v1.report_digest)
        head_at_freeze = v1.head_seq

        # 冻结之后无论补录什么，V1 逐字不变
        with tempfile.TemporaryDirectory() as tmp2:
            extra = support.write_readings_file(tmp2, [
                {"meter_id": "M-88", "observed_at": "2026-03-01T00:00:00+08:00",
                 "cumulative_kwh": "300.00"},
            ], name="extra.jsonl")
            case.import_readings_file(extra, operator="importer", at=support.T0)
        again = case.report("V1")
        self.assertEqual(report_digest(again), v1.report_digest)
        self.assertEqual(v1.head_seq, head_at_freeze)
        # 工作稿则随补录前进
        working = case.report()
        self.assertNotEqual(report_digest(working), v1.report_digest)

    def test_tampered_frozen_digest_raises(self):
        case, tmp = _case_with_two_readings()
        self.addCleanup(tmp.cleanup)
        v1 = case.freeze(operator="auditor", reason="首次送审", at=support.T0)
        object.__setattr__(v1, "report_digest", "sha256:" + "f" * 64)
        case.versions[0] = v1
        with self.assertRaises(TamperError):
            case.report("V1")

    def test_freeze_requires_reason(self):
        case, tmp = _case_with_two_readings()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(LedgerError):
            case.freeze(operator="auditor", reason="", at=support.T0)


class PersistenceTest(unittest.TestCase):
    def test_save_load_roundtrip_replays_all_versions(self):
        case, tmp = _case_with_two_readings()
        self.addCleanup(tmp.cleanup)
        case.freeze(operator="auditor", reason="首次送审", at=support.T0)
        with tempfile.TemporaryDirectory() as tmp2:
            extra = support.write_readings_file(tmp2, [
                {"meter_id": "M-88", "observed_at": "2026-03-01T00:00:00+08:00",
                 "cumulative_kwh": "300.00"},
            ], name="extra.jsonl")
            case.import_readings_file(extra, operator="importer", at=support.T0)
            case.freeze(operator="auditor", reason="复审", at=support.T0)
            with tempfile.TemporaryDirectory() as store:
                case.save(store)
                loaded = CaseFile.load(store)  # 装载即对全部冻结版本重放对账
                self.assertEqual([v.label for v in loaded.versions], ["V1", "V2"])
                self.assertEqual(
                    report_digest(loaded.report("V1")),
                    report_digest(case.report("V1")),
                )
                self.assertEqual(
                    report_digest(loaded.report("V2")),
                    report_digest(case.report("V2")),
                )
                self.assertEqual(loaded.current_branch, "B3")


class ReferenceCaseTest(unittest.TestCase):
    """参考案卷：两个版本的冻结值与故事线一致。"""

    def test_reference_case_freezes_expected_numbers(self):
        from warranty_evidence.reference_case import build_reference_case

        case = build_reference_case("V2")
        v1 = {p["version"]: p for p in case.report("V1")["periods"]}
        v2 = {p["version"]: p for p in case.report("V2")["periods"]}
        self.assertEqual(v1["2025-A"]["throughput_kwh"], Decimal("12237.75"))
        self.assertEqual(v1["2026-B"]["throughput_kwh"], Decimal("7154.25"))
        self.assertEqual(v2["2025-A"]["throughput_kwh"], Decimal("11974.00"))
        self.assertEqual(v2["2026-B"]["throughput_kwh"], Decimal("7460.25"))
        # V1 有窗口起点覆盖缺口，V2 由调档补录补齐
        self.assertEqual(case.report("V1")["summary"]["gap_count"], 1)
        self.assertEqual(case.report("V2")["summary"]["gap_count"], 0)

    def test_reference_case_is_deterministic(self):
        from warranty_evidence.reference_case import build_reference_case

        first = build_reference_case("V2")
        second = build_reference_case("V2")
        self.assertEqual(report_digest(first.report("V2")),
                         report_digest(second.report("V2")))


if __name__ == "__main__":
    unittest.main()

"""台账纪律：只增不改、哈希链防篡改、摘要去重、更正留痕。"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import support
from warranty_evidence.ledger import (
    DuplicateImportError,
    EvidenceLedger,
    LedgerError,
    TamperError,
    UnknownReadingError,
)
from warranty_evidence.model import SourceKind


class HashChainTest(unittest.TestCase):
    def test_chain_verifies_and_tamper_is_detected(self):
        ledger = support.ledger_with([
            support.rev("M-88", "2026-01-01T00:00:00+08:00", "100.00"),
            support.rev("M-88", "2026-02-01T00:00:00+08:00", "200.00"),
        ])
        self.assertTrue(ledger.verify())
        self.assertEqual(ledger.head_seq, 2)
        # 篡改任一历史事件的负载，哈希链必须报警
        tampered = ledger.events[0]
        object.__setattr__(tampered, "payload", dict(tampered.payload, note="篡改"))
        with self.assertRaises(TamperError):
            ledger.verify()

    def test_append_requires_operator_and_aware_time(self):
        ledger = EvidenceLedger()
        with self.assertRaises(LedgerError):
            ledger.append("X", {}, operator="", at=support.T0, branch="B1")
        naive = support.T0.replace(tzinfo=None)
        with self.assertRaises(LedgerError):
            ledger.append("X", {}, operator="tester", at=naive, branch="B1")

    def test_save_load_roundtrip(self):
        ledger = support.ledger_with(
            [support.rev("M-88", "2026-01-01T00:00:00+08:00", "100.00")],
            changes=[support.make_change()],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger.save(path)
            loaded = EvidenceLedger.load(path)
        self.assertEqual([e.digest for e in ledger.events],
                         [e.digest for e in loaded.events])
        self.assertTrue(loaded.verify())


class ImportDedupTest(unittest.TestCase):
    def test_same_file_digest_rejected(self):
        case = support.make_case()
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_readings_file(tmp, [
                {"meter_id": "M-88", "observed_at": "2026-01-01T00:00:00+08:00",
                 "cumulative_kwh": "100.00"},
            ])
            result = case.import_readings_file(path, operator="tester", at=support.T0)
            self.assertEqual(result.imported, 1)
            with self.assertRaises(DuplicateImportError) as ctx:
                case.import_readings_file(path, operator="tester", at=support.T0)
            self.assertIn("拒绝重复导入", str(ctx.exception))

    def test_same_reading_in_another_file_skipped_idempotently(self):
        case = support.make_case()
        with tempfile.TemporaryDirectory() as tmp:
            first = support.write_readings_file(tmp, [
                {"meter_id": "M-88", "observed_at": "2026-01-01T00:00:00+08:00",
                 "cumulative_kwh": "100.00"},
            ], name="a.jsonl")
            second = support.write_readings_file(tmp, [
                {"meter_id": "M-88", "observed_at": "2026-01-01T00:00:00+08:00",
                 "cumulative_kwh": "100.00"},
                {"meter_id": "M-88", "observed_at": "2026-02-01T00:00:00+08:00",
                 "cumulative_kwh": "200.00"},
            ], name="b.jsonl")
            case.import_readings_file(first, operator="tester", at=support.T0)
            result = case.import_readings_file(second, operator="tester", at=support.T0)
            self.assertEqual(result.imported, 1)
            self.assertEqual(result.skipped_duplicates, 1)

    def test_manual_reading_requires_operator_and_reason(self):
        case = support.make_case()
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_readings_file(tmp, [
                {"meter_id": "M-88", "source": "manual",
                 "observed_at": "2026-01-01T00:00:00+08:00", "cumulative_kwh": "100.00"},
            ])
            with self.assertRaises(LedgerError):
                case.import_readings_file(path, operator="tester", at=support.T0)

    def test_unknown_meter_rejected(self):
        case = support.make_case()
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_readings_file(tmp, [
                {"meter_id": "M-999", "observed_at": "2026-01-01T00:00:00+08:00",
                 "cumulative_kwh": "100.00"},
            ])
            with self.assertRaises(LedgerError):
                case.import_readings_file(path, operator="tester", at=support.T0)


class CorrectionTest(unittest.TestCase):
    def _case_with_reading(self):
        case = support.make_case()
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_readings_file(tmp, [
                {"meter_id": "M-88", "observed_at": "2026-01-01T00:00:00+08:00",
                 "cumulative_kwh": "100.00"},
            ])
            result = case.import_readings_file(path, operator="tester", at=support.T0)
        return case, result.reading_ids[0]

    def test_correction_keeps_original_and_chains(self):
        case, rid = self._case_with_reading()
        new_rev = case.correct_reading(
            rid, operator="auditor", reason="抄表错位修正",
            at=support.T0, cumulative_kwh="110.50",
        )
        view = case.ledger.view()
        chain = view.revision_chain(rid)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].cumulative_kwh, Decimal("100.00"))  # 原版仍在
        self.assertEqual(chain[1].cumulative_kwh, Decimal("110.50"))
        self.assertEqual(chain[1].supersedes, chain[0].revision_id)
        self.assertEqual(view.current_of(rid).revision_id, new_rev.revision_id)

    def test_correction_requires_reason_and_known_reading(self):
        case, rid = self._case_with_reading()
        with self.assertRaises(LedgerError):
            case.correct_reading(rid, operator="auditor", reason="",
                                 at=support.T0, cumulative_kwh="1")
        with self.assertRaises(UnknownReadingError):
            case.correct_reading("rdg:nonexistent", operator="auditor",
                                 reason="x", at=support.T0, cumulative_kwh="1")

    def test_correction_records_operator_and_reason(self):
        case, rid = self._case_with_reading()
        case.correct_reading(rid, operator="auditor-wang", reason="供应商确认错位",
                             at=support.T0, cumulative_kwh="110.50")
        current = case.ledger.view().current_of(rid)
        self.assertEqual(current.operator, "auditor-wang")
        self.assertEqual(current.reason, "供应商确认错位")
        self.assertEqual(current.source, SourceKind.METER)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from warranty_evidence import (
    Actor,
    Dossier,
    DossierFrozenError,
    LedgerError,
    ReadingSource,
)

REVIEWER = Actor("u-01", "质保审核人")
SUPPLIER = Actor("u-02", "供应商运维")

CONTRACTS = [
    {"version": "2025-A", "effective_from": "2025-01-01T00:00:00+08:00",
     "minimum_availability": "0.97", "rated_energy_kwh": "500"},
    {"version": "2026-B", "effective_from": "2026-01-01T00:00:00+08:00",
     "minimum_availability": "0.975", "rated_energy_kwh": "500"},
]


def make_dossier():
    return Dossier(
        asset_id="cabinet-A17",
        initial_meter_id="M-88",
        case_start="2025-01-01T00:00:00+08:00",
        contracts=CONTRACTS,
        case_end="2026-12-31T23:59:59+08:00",
    )


class LedgerRuleTest(unittest.TestCase):
    def test_reading_carries_full_provenance(self):
        d = make_dossier()
        rec = d.import_file(
            "sha256:abc123", "M88-2025Q1.csv", SUPPLIER,
            [{"ts": "2025-01-05T08:00:00+08:00", "value": "120.5"}],
            meter_id="M-88",
        )
        st = d.head()
        r = st.readings[rec.reading_seqs[0]]
        self.assertEqual(r.source, ReadingSource.METER_EXPORT)
        self.assertEqual(r.import_digest, "sha256:abc123")
        self.assertEqual(r.actor, SUPPLIER)
        self.assertEqual(r.value, Decimal("120.5"))

    def test_duplicate_digest_is_rejected(self):
        d = make_dossier()
        d.import_file("sha256:aa", "f1.csv", SUPPLIER,
                      [{"ts": "2025-01-05T08:00:00+08:00", "value": "1"}], "M-88")
        with self.assertRaises(LedgerError):
            d.import_file("sha256:aa", "f2.csv", SUPPLIER,
                          [{"ts": "2025-01-06T08:00:00+08:00", "value": "2"}], "M-88")

    def test_manual_entry_requires_reason_per_row(self):
        d = make_dossier()
        with self.assertRaises(LedgerError):
            d.import_file("sha256:bb", "manual.csv", REVIEWER,
                          [{"ts": "2025-02-01T08:00:00+08:00", "value": "9",
                            "source": "manual_entry"}],
                          meter_id="M-88", source=ReadingSource.MANUAL_ENTRY)
        rec = d.import_file(
            "sha256:bb", "manual.csv", REVIEWER,
            [{"ts": "2025-02-01T08:00:00+08:00", "value": "9",
              "source": "manual_entry", "note": "现场手抄表，附工单 WO-77"}],
            meter_id="M-88", source=ReadingSource.MANUAL_ENTRY,
        )
        self.assertEqual(st_reading(d, rec.reading_seqs[0]).source,
                         ReadingSource.MANUAL_ENTRY)

    def test_meter_change_requires_three_elements(self):
        d = make_dossier()
        with self.assertRaises(LedgerError):
            d.record_meter_change("M-88", "18420.50", "M-88", "12.25",
                                  "2026-06-15T10:30:00+08:00", SUPPLIER)
        ch = d.record_meter_change("M-88", "18420.50", "M-104", "12.25",
                                   "2026-06-15T10:30:00+08:00", SUPPLIER,
                                   reason="通信模块烧毁换表")
        self.assertEqual(ch.old_final, Decimal("18420.50"))
        self.assertEqual(ch.new_initial, Decimal("12.25"))

    def test_readings_cross_meter_boundaries_rejected(self):
        d = make_dossier()
        d.record_meter_change("M-88", "100.00", "M-104", "0.00",
                              "2026-06-15T10:30:00+08:00", SUPPLIER)
        # 新表不得有早于换表时刻的读数
        with self.assertRaises(LedgerError):
            d.import_file("sha256:e1", "new-early.csv", SUPPLIER,
                          [{"ts": "2026-06-14T10:00:00+08:00", "value": "5"}], "M-104")
        # 旧表不得有晚于换表时刻的读数
        with self.assertRaises(LedgerError):
            d.import_file("sha256:e2", "old-late.csv", SUPPLIER,
                          [{"ts": "2026-06-16T10:00:00+08:00", "value": "120"}], "M-88")

    def test_gap_is_explicit_and_blocks_plain_import(self):
        d = make_dossier()
        g = d.declare_gap("2026-03-01T00:00:00+08:00", "2026-03-10T00:00:00+08:00",
                          "采集链路中断，无表计导出", REVIEWER)
        with self.assertRaises(LedgerError):
            d.import_file("sha256:g1", "inside.csv", SUPPLIER,
                          [{"ts": "2026-03-05T00:00:00+08:00", "value": "3"}], "M-88")
        self.assertEqual([gap.seq for gap in d.head().open_gaps()], [g.seq])

    def test_correction_never_overwrites_original(self):
        d = make_dossier()
        rec = d.import_file("sha256:c1", "f.csv", SUPPLIER,
                            [{"ts": "2025-05-01T00:00:00+08:00", "value": "100.00"}], "M-88")
        old_seq = rec.reading_seqs[0]
        new = d.correct_reading(old_seq, "90.00", REVIEWER,
                                "导出倍率错误，按检定证书 CT-55 更正", refs=["CT-55"])
        st = d.head()
        self.assertEqual(st.readings[old_seq].value, Decimal("100.00"))  # 原值保留
        self.assertEqual(st.readings[new.seq].value, Decimal("90.00"))
        self.assertEqual(st.superseded[old_seq], new.seq)
        rv = st.revisions[0]
        self.assertEqual(rv.reason, "导出倍率错误，按检定证书 CT-55 更正")
        self.assertEqual(rv.actor, REVIEWER)
        self.assertEqual(rv.refs, ("CT-55",))
        with self.assertRaises(LedgerError):
            d.correct_reading(old_seq, "80", REVIEWER, "再次改原值")  # 不能修订原值

    def test_meter_change_amendment_chains_but_keeps_original(self):
        d = make_dossier()
        ch = d.record_meter_change("M-88", "18420.50", "M-104", "12.25",
                                   "2026-06-15T10:30:00+08:00", SUPPLIER)
        new_ch = d.amend_meter_change(ch.seq, REVIEWER,
                                      "旧表终值抄录有误，以换装现场照片为准",
                                      old_final_kwh="18410.50", refs=["PHOTO-3"])
        st = d.head()
        self.assertEqual(st.changes[ch.seq].old_final, Decimal("18420.50"))  # 原记录保留
        active = st.active_changes()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].seq, new_ch.seq)
        self.assertEqual(active[0].old_final, Decimal("18410.50"))

    def test_freeze_blocks_writes_and_branch_continues(self):
        d = make_dossier()
        d.import_file("sha256:f1", "f.csv", SUPPLIER,
                      [{"ts": "2025-01-05T08:00:00+08:00", "value": "1"}], "M-88")
        snap = d.freeze("v1-submission", REVIEWER)
        self.assertTrue(snap.digest.startswith("sha256:"))
        with self.assertRaises(DossierFrozenError):
            d.import_file("sha256:f2", "g.csv", SUPPLIER,
                          [{"ts": "2025-01-06T08:00:00+08:00", "value": "2"}], "M-88")
        child = d.branch()
        child.import_file("sha256:f2", "g.csv", SUPPLIER,
                          [{"ts": "2025-01-06T08:00:00+08:00", "value": "2"}], "M-88")
        # 冻结版本视图不含分支事件
        v1 = child.state_at("v1-submission")
        self.assertEqual(len(v1.imports), 1)
        self.assertEqual(len(child.head().imports), 2)
        snap2 = child.freeze("v2-submission", REVIEWER)
        self.assertEqual(snap2.parent_label, "v1-submission")

    def test_frozen_version_digest_is_stable(self):
        d = make_dossier()
        d.import_file("sha256:f1", "f.csv", SUPPLIER,
                      [{"ts": "2025-01-05T08:00:00+08:00", "value": "1.5"}], "M-88")
        digest1 = d.freeze("v1", REVIEWER).digest
        child = d.branch()
        # 分支追加事件不改变已冻结版本的摘要
        self.assertEqual(child.state_at("v1").snapshot("v1").digest, digest1)

    def test_naive_timestamp_rejected(self):
        d = make_dossier()
        with self.assertRaises(ValueError):
            d.import_file("sha256:n1", "naive.csv", SUPPLIER,
                          [{"ts": "2025-01-05T08:00:00", "value": "1"}], "M-88")

    def test_contract_versions_must_be_sorted_and_unique(self):
        with self.assertRaises(LedgerError):
            Dossier("c", "M-1", "2025-01-01T00:00:00+08:00",
                    [{"version": "B", "effective_from": "2026-01-01T00:00:00+08:00"},
                     {"version": "A", "effective_from": "2025-01-01T00:00:00+08:00"}])
        with self.assertRaises(LedgerError):
            Dossier("c", "M-1", "2025-01-01T00:00:00+08:00",
                    [{"version": "A", "effective_from": "2025-01-01T00:00:00+08:00"},
                     {"version": "A", "effective_from": "2026-01-01T00:00:00+08:00"}])

    def test_gap_and_meter_change_cannot_contradict(self):
        d = make_dossier()
        d.declare_gap("2026-06-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00",
                      "无导出", REVIEWER)
        with self.assertRaises(LedgerError):
            d.record_meter_change("M-88", "100", "M-104", "0",
                                  "2026-06-15T10:30:00+08:00", SUPPLIER)

    def test_meter_change_amendment_into_gap_rejected(self):
        d = make_dossier()
        ch = d.record_meter_change("M-88", "100", "M-104", "0",
                                   "2026-06-15T10:30:00+08:00", SUPPLIER)
        d.declare_gap("2026-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00",
                      "无导出", REVIEWER)
        with self.assertRaises(LedgerError):
            d.amend_meter_change(ch.seq, REVIEWER, "错误改时刻",
                                 changed_ts="2026-08-15T10:30:00+08:00")


def st_reading(dossier, seq):
    return dossier.head().readings[seq]


if __name__ == "__main__":
    unittest.main()

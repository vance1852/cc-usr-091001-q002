"""送审报告：汇总数字可逐笔下钻，并支持两个送审版本之间的多算/少算比对。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import computation as comp_mod
from .ledger import Dossier, StateView
from .model import ReadingSource, RevisionKind


def _ts(v: Any) -> str:
    return v.isoformat() if v is not None else None


def _num(d: Decimal | None) -> str | None:
    """格式化计算值：内部精度 6 位小数，展示时去掉多余尾零但至少保留 2 位。

    注意：只用于**计算产物**；读数、换表端点等登记值必须逐字展示原始字符串。
    """
    if d is None:
        return None
    s = f"{d:.6f}".rstrip("0")
    if s.endswith("."):
        return s + "00"
    head, _, frac = s.partition(".")
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"{head}.{frac}"


def _reading_line(r: Any, state: StateView) -> dict[str, Any]:
    """单条读数的完整来源说明（下钻终点之一）。"""
    superseded_by = state.superseded.get(r.seq)
    imp = next(
        (i for i in state.imports if r.seq in i.reading_seqs),
        None,
    )
    revision = next(
        (rv for rv in state.revisions if rv.seq == r.revision_seq),
        None,
    )
    return {
        "seq": r.seq,
        "meter_id": r.meter_id,
        "ts": _ts(r.ts),
        "kwh": str(r.value),
        "source": r.source.value,
        "actor": f"{r.actor.user_id} {r.actor.display_name}".strip(),
        "note": r.note,
        "import_digest": r.import_digest or None,
        "import_filename": imp.filename if imp else None,
        "revision_seq": r.revision_seq,
        "revision_reason": revision.reason if revision else None,
        "superseded_by_reading_seq": superseded_by,
        "status": "superseded" if superseded_by else "active",
    }


def _revision_line(rv: Any, state: StateView) -> dict[str, Any]:
    line = {
        "seq": rv.seq,
        "kind": rv.kind.value,
        "target_seq": rv.target_seq,
        "new_reading_seq": rv.new_reading_seq,
        "actor": f"{rv.actor.user_id} {rv.actor.display_name}".strip(),
        "reason": rv.reason,
        "created_ts": _ts(rv.created_ts),
        "refs": list(rv.refs),
    }
    if rv.kind is RevisionKind.METER_CHANGE_AMEND:
        original = state.changes.get(rv.target_seq)
        replacement = state.changes.get(rv.new_reading_seq)
        line["amendment"] = {
            "old_record": _change_line(original) if original else None,
            "new_record": _change_line(replacement) if replacement else None,
        }
    return line


def _change_line(ch: Any) -> dict[str, Any]:
    return {
        "seq": ch.seq,
        "old_meter_id": ch.old_meter_id,
        "old_final_kwh": str(ch.old_final),
        "new_meter_id": ch.new_meter_id,
        "new_initial_kwh": str(ch.new_initial),
        "changed_ts": _ts(ch.changed_ts),
        "actor": f"{ch.actor.user_id} {ch.actor.display_name}".strip(),
        "reason": ch.reason,
        "amends_seq": ch.amends_seq,
    }


def _gap_line(g: Any, state: StateView) -> dict[str, Any]:
    closed_by = state.closed_gaps.get(g.seq)
    return {
        "seq": g.seq,
        "start_ts": _ts(g.start_ts),
        "end_ts": _ts(g.end_ts),
        "reason": g.reason,
        "declared_by": f"{g.declared_by.user_id} {g.declared_by.display_name}".strip(),
        "status": "closed" if closed_by else "open",
        "closed_by_revision_seq": closed_by,
        "interpolation": "forbidden",
    }


def build_report(dossier: Dossier, label: str | None = None) -> dict[str, Any]:
    """生成某个送审版本（label=None 取 HEAD）的完整质保证据报告。

    报告层次：合同桶汇总 -> 增量分量 -> 两端计量记录 -> 文件摘要 / 修订依据，
    同时附设备身份沿革、证据缺口、异常增量与全部修订留痕。
    """
    state = dossier.state_at(label) if label else dossier.head()
    result = comp_mod.compute(state)
    snap = state.snapshot(label) if label else None

    bucket_reports = []
    for b in result.buckets:
        component_lines = []
        for c in b.components:
            line = {
                "key": c.key,
                "meter_id": c.meter_id,
                "start_ts": _ts(c.start_ts),
                "end_ts": _ts(c.end_ts),
                "contract_version": c.contract_version,
                "kwh": _num(c.kwh),
                "basis": c.basis,
                "counted": not c.quarantined,
                "evidence": c.evidence(),
            }
            component_lines.append(line)
        bucket_reports.append(
            {
                "contract_version": b.contract.version,
                "effective_from": _ts(b.contract.effective_from),
                "window_end": _ts(b.window_end),
                "terms": b.contract.terms,
                "observed_kwh": _num(b.observed_kwh),
                "quarantined_kwh": _num(b.quarantined_kwh),
                "equivalent_full_cycles": _num(b.cycles),
                "time_apportioned_components": b.apportioned_component_count,
                "components": component_lines,
            }
        )

    report = {
        "asset_id": state.asset_id,
        "version_label": label or "HEAD",
        "frozen_digest": snap.digest if snap else None,
        "frozen_ts": _ts(snap.frozen_ts) if snap else None,
        "parent_version": snap.parent_label if snap else None,
        "contracts_used": [
            {"version": cv.version, "effective_from": _ts(cv.effective_from), "terms": cv.terms}
            for cv in state.contracts
        ],
        "identity_history": [
            {
                "meter_id": t.meter_id,
                "from_ts": _ts(t.from_ts),
                "to_ts": _ts(t.to_ts),
                "end_reason": t.end_reason,
                "initial_kwh": str(t.initial) if t.initial is not None else None,
                "final_kwh": str(t.final) if t.final is not None else None,
                "meter_change_seq": t.change_seq,
            }
            for t in state.meter_tenures()
        ],
        "meter_changes": [_change_line(ch) for ch in state.active_changes()],
        "superseded_meter_changes": [
            _change_line(ch)
            for seq, ch in state.changes.items()
            if ch.amends_seq is not None
        ],
        "totals": {
            "observed_kwh": _num(result.total_observed_kwh),
            "quarantined_kwh": _num(result.total_quarantined_kwh),
            "equivalent_full_cycles": _num(result.total_cycles),
        },
        "by_contract": bucket_reports,
        "evidence_gaps": [_gap_line(g, state) for g in sorted(state.gaps.values(), key=lambda x: x.seq)],
        "anomalies": result.anomalies,
        "readings": [
            _reading_line(r, state)
            for r in sorted(state.readings.values(), key=lambda x: (x.ts, x.seq))
        ],
        "revisions": [_revision_line(rv, state) for rv in state.revisions],
        "imports": [
            {
                "digest": str(i.digest),
                "filename": i.filename,
                "meter_id": i.meter_id,
                "source": i.source.value,
                "imported_ts": _ts(i.imported_ts),
                "actor": f"{i.actor.user_id} {i.actor.display_name}".strip(),
                "reading_seqs": list(i.reading_seqs),
            }
            for i in state.imports
        ],
        "audit_note": (
            "minimum_availability 等可用性指标需要运行时段证据，"
            "无法仅凭计量累计量计算；本报告仅重算吞吐量与等效满充满放循环次数。"
        ),
    }
    return report


def drill_down(report: dict[str, Any], component_key: str) -> dict[str, Any]:
    """从汇总数字中的一条增量分量，下钻到两端读数、换表锚点与修订依据。"""
    component = None
    for bucket in report["by_contract"]:
        for c in bucket["components"]:
            if c["key"] == component_key:
                component = c
                break
    if component is None:
        raise KeyError(f"分量 {component_key!r} 不在本版本报告中")
    ev = component["evidence"]
    readings_by_seq = {r["seq"]: r for r in report["readings"]}
    changes_by_seq = {c["seq"]: c for c in report["meter_changes"]}
    revisions_by_seq = {r["seq"]: r for r in report["revisions"]}

    chain: list[dict[str, Any]] = []
    for endpoint_seq in (ev["start_reading_seq"], ev["end_reading_seq"]):
        if endpoint_seq is None:
            continue
        current = readings_by_seq.get(endpoint_seq)
        endpoint_chain = []
        seen = set()
        while current and current["seq"] not in seen:
            seen.add(current["seq"])
            entry = dict(current)
            if current["revision_seq"]:
                entry["revision"] = revisions_by_seq.get(current["revision_seq"])
            endpoint_chain.append(entry)
            if current["superseded_by_reading_seq"]:
                current = readings_by_seq.get(current["superseded_by_reading_seq"])
            else:
                break
        chain.append({"endpoint_reading_seq": endpoint_seq, "provenance_chain": endpoint_chain})

    return {
        "component": component,
        "endpoint_readings": chain,
        "meter_change": changes_by_seq.get(ev["meter_change_seq"])
        if ev["meter_change_seq"]
        else None,
    }


@dataclass
class _LineDiff:
    key: str
    contract_version: str
    old_kwh: Decimal
    new_kwh: Decimal
    delta: Decimal
    counted_old: bool
    counted_new: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "contract_version": self.contract_version,
            "old_kwh": _num(self.old_kwh),
            "new_kwh": _num(self.new_kwh),
            "delta_kwh": _num(self.delta),
            "counted_in_old": self.counted_old,
            "counted_in_new": self.counted_new,
            "reason": self.reason,
        }


def compare_versions(dossier: Dossier, old_label: str, new_label: str) -> dict[str, Any]:
    """逐分量比对两个送审版本，列出新版本相对旧版本多算/少算了什么。"""
    old_state = dossier.state_at(old_label)
    new_state = dossier.state_at(new_label)
    old_result = comp_mod.compute(old_state)
    new_result = comp_mod.compute(new_state)

    def index(result: Any) -> dict[str, Any]:
        return {c.key: c for c in result.components}

    old_idx, new_idx = index(old_result), index(new_result)
    old_open_gaps = {(g.seq) for g in old_state.open_gaps()}
    diffs: list[_LineDiff] = []

    for key in sorted(set(old_idx) | set(new_idx)):
        co, cn = old_idx.get(key), new_idx.get(key)
        old_v = co.kwh if co else Decimal(0)
        new_v = cn.kwh if cn else Decimal(0)
        counted_old = bool(co and not co.quarantined)
        counted_new = bool(cn and not cn.quarantined)
        if old_v == new_v and counted_old == counted_new:
            continue
        reason = _classify_diff(key, co, cn, old_state, new_state, old_open_gaps)
        diffs.append(
            _LineDiff(
                key=key,
                contract_version=(cn or co).contract_version,
                old_kwh=old_v,
                new_kwh=new_v,
                delta=new_v - old_v,
                counted_old=counted_old,
                counted_new=counted_new,
                reason=reason,
            )
        )

    def bucket_delta(version: str) -> dict[str, Decimal]:
        obs_old = old_result.bucket_of(version).observed_kwh if _has_bucket(old_result, version) else Decimal(0)
        obs_new = new_result.bucket_of(version).observed_kwh if _has_bucket(new_result, version) else Decimal(0)
        q_old = old_result.bucket_of(version).quarantined_kwh if _has_bucket(old_result, version) else Decimal(0)
        q_new = new_result.bucket_of(version).quarantined_kwh if _has_bucket(new_result, version) else Decimal(0)
        return {"observed": obs_new - obs_old, "quarantined": q_new - q_old}

    versions = sorted({b.contract.version for b in old_result.buckets} |
                      {b.contract.version for b in new_result.buckets})
    by_contract = []
    for v in versions:
        d = bucket_delta(v)
        signed = d["observed"]
        by_contract.append(
            {
                "contract_version": v,
                "observed_delta_kwh": _num(signed),
                "quarantined_delta_kwh": _num(d["quarantined"]),
                "verdict": "over_claimed" if signed > 0 else "under_claimed" if signed < 0 else "unchanged",
            }
        )

    total_delta = new_result.total_observed_kwh - old_result.total_observed_kwh
    return {
        "asset_id": dossier.asset_id,
        "old_version": old_label,
        "new_version": new_label,
        "old_frozen_digest": old_state.snapshot(old_label).digest,
        "new_frozen_digest": new_state.snapshot(new_label).digest,
        "old_totals": {
            "observed_kwh": _num(old_result.total_observed_kwh),
            "quarantined_kwh": _num(old_result.total_quarantined_kwh),
            "equivalent_full_cycles": _num(old_result.total_cycles),
        },
        "new_totals": {
            "observed_kwh": _num(new_result.total_observed_kwh),
            "quarantined_kwh": _num(new_result.total_quarantined_kwh),
            "equivalent_full_cycles": _num(new_result.total_cycles),
        },
        "observed_delta_kwh": _num(total_delta),
        "cycles_delta": _num(
            (new_result.total_cycles or Decimal(0)) - (old_result.total_cycles or Decimal(0))
        ),
        "verdict": "新版本多算" if total_delta > 0 else "新版本少算" if total_delta < 0 else "总量持平",
        "by_contract": by_contract,
        "line_changes": [d.as_dict() for d in diffs],
        "newly_closed_gaps": [
            g.seq for g in old_state.gaps.values() if g.seq in old_open_gaps and g.seq in new_state.closed_gaps
        ],
        "reason_legend": {
            "new_evidence": "分支上新增了有效读数（如冻结后的补录）",
            "gap_closed": "原证据缺口已由缺口闭合修订补录，隔离增量转为可计量",
            "correction": "端点读数被修订更正（原值仍保留可查）",
            "anchor_amended": "换表端点（旧表终值/新表初值）经修订更正",
            "superseded_removed": "该增量所依读数已被取代，增量不再成立",
            "quarantine_change": "隔离状态变化（缺口/回退口径）",
        },
    }


def _has_bucket(result: Any, version: str) -> bool:
    return any(b.contract.version == version for b in result.buckets)


def _classify_diff(
    key: str,
    co: Any,
    cn: Any,
    old_state: StateView,
    new_state: StateView,
    old_open_gaps: set[int],
) -> str:
    if co is None:
        # 新分量落在旧版本的开放缺口区间内 → 缺口闭合补录产生；否则为全新证据
        if cn is not None and cn.basis != comp_mod.BASIS_SPANS_OPEN_GAP and _span_was_gap(cn, old_state):
            return "gap_closed"
        return "new_evidence"
    if cn is None:
        # 旧的跨缺口长增量因缺口内出现补录点而拆分为更小增量
        if co.quarantined and _span_was_gap(co, old_state):
            return "gap_closed"
        return "superseded_removed"
    if co.quarantined != cn.quarantined:
        if _span_was_gap(co, old_state) and not _span_was_gap(cn, new_state):
            return "gap_closed"
        return "quarantine_change"
    # 值变化：先看端点读数是否被修订取代，再看换表锚点是否被修订
    for comp in (cn, co):
        for seq in (comp.start_seq, comp.end_seq):
            if seq is not None and (seq in new_state.superseded or seq in old_state.superseded):
                return "correction"
    ch_new = new_state.changes.get(cn.change_seq) if cn.change_seq else None
    ch_old = old_state.changes.get(co.change_seq) if co.change_seq else None
    if ch_new is not None and ch_new.amends_seq is not None:
        return "anchor_amended"
    if ch_new is not None and ch_old is not None and ch_new.seq != ch_old.seq:
        return "anchor_amended"
    return "correction"


def _span_was_gap(component: Any, old_state: StateView) -> bool:
    for g in old_state.open_gaps():
        if component.start_ts < g.end_ts and g.start_ts < component.end_ts:
            return True
    return False

"""审核报告：从汇总数字逐笔下钻到计量记录、身份沿革与修订依据。

报告是纯函数输出：同一台账截点永远生成同一份报告（摘要稳定可校验），
供应商拿到任一版本都能沿 汇总 → 期间 → 分段 → 读数来源卡 → 修订链
逐级下钻，不依赖任何页面之外的口头解释。
"""
from __future__ import annotations

from .ledger import LedgerView
from .metrics import MetricsResult, compute_metrics
from .model import (
    IdentityMap,
    RuleSet,
    Window,
    canonical,
    digest_text,
    fmt_ts,
)
from .provenance import explain_reading, identity_lineage, source_label_of


def report_digest(report: dict) -> str:
    """报告内容摘要：冻结版本以它锚定，事后重算必须逐字一致。"""
    return digest_text(canonical(report))


def build_report(
    view: LedgerView,
    *,
    case_id: str,
    asset_id: str,
    window: Window,
    rules: RuleSet,
    identity: IdentityMap,
    boundary_policy: str,
    generated_from: dict,
    description: str = "",
) -> dict:
    """基于台账某一截点生成完整审核报告（dict，可直接 JSON 化）。"""
    metrics = compute_metrics(view, identity, rules, window, boundary_policy)

    # 收集报告涉及的全部读数，逐一生成来源卡
    reading_ids: list[str] = []
    for seg in metrics.segments:
        for basis in list(seg.start_basis) + list(seg.end_basis):
            if basis.kind == "reading" and basis.ref_id not in reading_ids:
                reading_ids.append(basis.ref_id)
    for gap in metrics.gaps:
        for ref in gap.evidence_refs:
            if ref.startswith("rdg:") and ref not in reading_ids:
                reading_ids.append(ref)

    cards = {}
    for rid in reading_ids:
        rev = view.current_of(rid)
        if rev is None:
            continue
        cards[rid] = explain_reading(rev, view, identity, rules, view.meter_changes)

    # 分段下钻：每个分段标注两端证据与来源称谓
    segment_rows = []
    for seg in metrics.segments:
        segment_rows.append({
            **seg.to_dict(),
            "start_basis": [_enrich_basis(b, view, identity) for b in seg.start_basis],
            "end_basis": [_enrich_basis(b, view, identity) for b in seg.end_basis],
        })

    revision_index = {
        rid: card["revision_chain"] for rid, card in cards.items()
    }

    return {
        "case_id": case_id,
        "asset_id": asset_id,
        "description": description,
        "generated_from": generated_from,
        "window": window.to_dict(),
        "boundary_policy": boundary_policy,
        "summary": {
            "total_throughput_kwh": metrics.total_kwh,
            "total_equivalent_cycles": metrics.total_cycles,
            "period_count": len(metrics.periods),
            "segment_count": len(metrics.segments),
            "gap_count": len(metrics.gaps),
            "unattributed_segment_count": len(metrics.unattributed),
            "finding_count": len(metrics.findings),
        },
        "periods": [p.to_dict() for p in metrics.periods],
        "segments": segment_rows,
        "gaps": [g.to_dict() for g in metrics.gaps],
        "unattributed_segment_indexes": list(metrics.unattributed),
        "findings": [f.to_dict() for f in metrics.findings],
        "readings": cards,
        "revision_index": revision_index,
        "identity_lineage": identity_lineage(identity, view.meter_changes),
        "meter_changes": [c.to_payload() for c in view.meter_changes],
        "imports": list(view.imports.values()),
    }


def _enrich_basis(basis, view: LedgerView, identity: IdentityMap) -> dict:
    """给证据引用补上来历称谓，让分段两端直接可读。"""
    row = basis.to_dict()
    if basis.kind == "reading":
        rev = view.current_of(basis.ref_id)
        if rev is not None:
            row["label"] = source_label_of(rev, identity)
            row["observed_at"] = fmt_ts(rev.observed_at)
            row["cumulative_kwh"] = rev.cumulative_kwh
    return row


def render_text(report: dict) -> str:
    """报告的纯文本摘要，供命令行与日志使用。"""
    lines = []
    src = report["generated_from"]
    lines.append(f"案卷 {report['case_id']}（资产 {report['asset_id']}）"
                 f" 版本 {src.get('version', '工作稿')} @ 事件#{src.get('head_seq')}")
    lines.append(f"窗口 {report['window']['start']} → {report['window']['end']}"
                 f"（边界策略 {report['boundary_policy']}）")
    summary = report["summary"]
    lines.append(f"合计吞吐 {summary['total_throughput_kwh']} kWh，"
                 f"等效循环 {summary['total_equivalent_cycles']} 次，"
                 f"缺口 {summary['gap_count']} 处，发现项 {summary['finding_count']} 条")
    for period in report["periods"]:
        lines.append(
            f"  [{period['version']}] {period['start']} → {period['end']}"
            f"  吞吐 {period['throughput_kwh']} kWh"
            f"  循环 {period['equivalent_cycles']} 次"
            f"（额定能量 {period['rated_energy_kwh']} kWh，"
            f"最低可用率 {period['minimum_availability']}）"
        )
    for gap in report["gaps"]:
        lines.append(f"  缺口[{gap['kind']}] {gap['start']} → {gap['end']}：{gap['reason']}")
    for finding in report["findings"]:
        lines.append(f"  发现[{finding['code']}] {finding['message']}")
    return "\n".join(lines)

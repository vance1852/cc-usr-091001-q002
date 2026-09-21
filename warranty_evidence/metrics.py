"""吞吐与循环指标：按当时生效的合同版本计算。

原则：
- 换表衔接只认换表记录：旧表终值与新表初值共同定义资产级累计量的偏移，
  衔接点本身作为时间线上的证据点，与边界读数互相校验；
- 缺测区间是证据缺口，不是待估的数：窗口边缘未覆盖、计数回退、
  同刻矛盾读数都形成显式缺口，绝不做插值；
- 合同版本按分段起点归属；跨越版本边界的分段在严格策略下不归属任何期间
  （吞吐仍计入窗口合计），并明确指出需要哪一时刻的边界读数来解开归属；
- 等效循环次数 = 期间吞吐 ÷ 该版本额定能量，逐版本分别计算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from .ledger import LedgerView
from .model import (
    Finding,
    IdentityMap,
    MeterChange,
    ReadingRevision,
    RuleSet,
    Window,
    fmt_ts,
)

CYCLES_QUANT = Decimal("0.000001")  # 循环次数保留 6 位小数，保证报告摘要稳定

BOUNDARY_STRICT = "strict"  # 跨版本边界的分段不归属，列为待解
BOUNDARY_APPORTION = "apportion"  # 按时间占比分摊并显式标注（归属层面的分摊，非缺测插值）


@dataclass(frozen=True)
class BasisRef:
    """时间线上一个点的证据支撑。"""

    kind: str  # "reading" | "meter_change"
    ref_id: str  # reading_id 或 change_id
    revision_id: Optional[str] = None
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "revision_id": self.revision_id,
            "label": self.label,
        }


@dataclass(frozen=True)
class TimelinePoint:
    at: datetime
    asset_cum_kwh: Decimal
    basis: tuple  # tuple[BasisRef, ...]


@dataclass(frozen=True)
class Segment:
    """相邻两个证据点之间的吞吐分段（两端都有实证，差值即吞吐）。"""

    index: int
    start: datetime
    end: datetime
    kwh: Decimal
    start_basis: tuple
    end_basis: tuple
    period_version: Optional[str] = None  # 归属的合同版本；未归属为 None
    apportioned: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": fmt_ts(self.start),
            "end": fmt_ts(self.end),
            "kwh": self.kwh,
            "period_version": self.period_version,
            "apportioned": self.apportioned,
            "start_basis": [b.to_dict() for b in self.start_basis],
            "end_basis": [b.to_dict() for b in self.end_basis],
        }


@dataclass(frozen=True)
class Gap:
    """证据缺口：明确标注区间与成因，不以任何估计值填充。"""

    kind: str  # coverage / counter_regression / reading_conflict / no_contract
    start: datetime
    end: datetime
    reason: str
    evidence_refs: tuple = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start": fmt_ts(self.start),
            "end": fmt_ts(self.end),
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class PeriodMetrics:
    version: str
    start: datetime
    end: datetime
    throughput_kwh: Decimal
    cycles: Decimal
    rated_energy_kwh: Decimal
    minimum_availability: Decimal
    segment_indexes: tuple

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "start": fmt_ts(self.start),
            "end": fmt_ts(self.end),
            "throughput_kwh": self.throughput_kwh,
            "equivalent_cycles": self.cycles,
            "rated_energy_kwh": self.rated_energy_kwh,
            "minimum_availability": self.minimum_availability,
            "segment_indexes": list(self.segment_indexes),
        }


@dataclass
class MetricsResult:
    window: Window
    points: list = field(default_factory=list)  # list[TimelinePoint]
    segments: list = field(default_factory=list)  # list[Segment]
    periods: list = field(default_factory=list)  # list[PeriodMetrics]
    gaps: list = field(default_factory=list)  # list[Gap]
    unattributed: list = field(default_factory=list)  # 跨边界待归属的分段 index
    findings: list = field(default_factory=list)  # list[Finding]
    total_kwh: Decimal = Decimal("0")
    total_cycles: Decimal = Decimal("0")

    def to_dict(self) -> dict:
        return {
            "window": self.window.to_dict(),
            "points": [
                {"at": fmt_ts(p.at), "asset_cum_kwh": p.asset_cum_kwh,
                 "basis": [b.to_dict() for b in p.basis]}
                for p in self.points
            ],
            "segments": [s.to_dict() for s in self.segments],
            "periods": [p.to_dict() for p in self.periods],
            "gaps": [g.to_dict() for g in self.gaps],
            "unattributed_segment_indexes": list(self.unattributed),
            "findings": [f.to_dict() for f in self.findings],
            "total_kwh": self.total_kwh,
            "total_cycles": self.total_cycles,
        }


# ---------------------------------------------------------------------------
# 时间线装配
# ---------------------------------------------------------------------------


def _meter_offsets(identity: IdentityMap, changes: list[MeterChange],
                   findings: list) -> dict:
    """由换表记录推导各表计的资产级累计偏移。

    首任表偏移为 0；每次换表：新表偏移 = 旧表偏移 + 旧表终值 - 新表初值。
    """
    offsets: dict[str, Decimal] = {}
    if not identity.meters:
        return offsets
    offsets[identity.meters[0].meter_id] = Decimal("0")
    for change in sorted(changes, key=lambda c: c.changed_at):
        base = offsets.get(change.old_meter_id)
        if base is None:
            findings.append(Finding(
                "METER_CHANGE_ORPHAN",
                f"换表记录 {change.change_id} 的旧表 {change.old_meter_id} 不在身份沿革中，按零偏移处理",
                (change.change_id,),
            ))
            base = Decimal("0")
        offsets[change.new_meter_id] = base + change.old_final_kwh - change.new_initial_kwh
    return offsets


def build_timeline(view: LedgerView, identity: IdentityMap) -> tuple:
    """把当前有效读数与换表记录装配成资产级累计时间线。

    返回 (points, gaps, findings)。矛盾读数与计数回退都形成缺口而非猜测。
    """
    findings: list[Finding] = []
    gaps: list[Gap] = []
    changes = sorted(view.meter_changes, key=lambda c: c.changed_at)
    offsets = _meter_offsets(identity, changes, findings)

    # 1) 同表同时刻取值检查：一致则去重，矛盾则整段留缺
    by_key: dict[tuple, list[ReadingRevision]] = {}
    for rev in view.current_revisions():
        by_key.setdefault((rev.meter_id, rev.observed_at), []).append(rev)

    effective: list[ReadingRevision] = []
    conflicted: dict = {}  # 矛盾时刻 -> 涉及读数 ID
    for (meter_id, observed_at), revs in sorted(by_key.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        values = {r.cumulative_kwh for r in revs}
        if len(values) > 1:
            ids = tuple(r.reading_id for r in revs)
            conflicted[observed_at] = ids
            findings.append(Finding(
                "READING_CONFLICT",
                f"表计 {meter_id} 在 {fmt_ts(observed_at)} 存在 {len(values)} 个矛盾取值，"
                f"相关区间留作证据缺口，待更正流程裁决",
                ids,
            ))
        else:
            if len(revs) > 1:
                findings.append(Finding(
                    "DUPLICATE_READING",
                    f"表计 {meter_id} 在 {fmt_ts(observed_at)} 的读数被重复报告（取值一致，已去重）",
                    tuple(r.reading_id for r in revs),
                ))
            effective.append(revs[0])

    # 2) 读数 -> 资产级累计点
    points_by_at: dict[datetime, dict] = {}

    def _merge_point(at: datetime, asset_cum: Decimal, basis: BasisRef) -> None:
        slot = points_by_at.setdefault(at, {"values": [], "basis": []})
        slot["values"].append(asset_cum)
        slot["basis"].append(basis)

    for rev in effective:
        if rev.meter_id is None:
            asset_cum = rev.cumulative_kwh  # 人工资产级表码
        else:
            if rev.meter_id not in offsets:
                findings.append(Finding(
                    "UNKNOWN_METER",
                    f"读数 {rev.reading_id} 的表计 {rev.meter_id} 不在身份沿革中，未纳入时间线",
                    (rev.reading_id,),
                ))
                continue
            asset_cum = rev.cumulative_kwh + offsets[rev.meter_id]
            service = identity.service_of(rev.meter_id)
            if service and not (service.service_from <= rev.observed_at
                                and (service.service_until is None or rev.observed_at <= service.service_until)):
                findings.append(Finding(
                    "READING_OUT_OF_SERVICE",
                    f"读数 {rev.reading_id} 的时刻超出表计 {rev.meter_id} 的服役区间",
                    (rev.reading_id,),
                ))
        _merge_point(rev.observed_at, asset_cum, BasisRef(
            kind="reading", ref_id=rev.reading_id, revision_id=rev.revision_id,
        ))

    # 3) 换表衔接点：旧表终值（= 新表初值 + 偏移）是同一资产累计的两个表达
    for change in changes:
        splice_cum = offsets[change.old_meter_id] + change.old_final_kwh
        cross_check = offsets[change.new_meter_id] + change.new_initial_kwh
        if splice_cum != cross_check:  # 数学上不应发生，防御性校验
            findings.append(Finding(
                "SPLICE_INCONSISTENT",
                f"换表记录 {change.change_id} 两侧衔接值不一致",
                (change.change_id,),
            ))
        _merge_point(change.changed_at, splice_cum, BasisRef(
            kind="meter_change", ref_id=change.change_id,
            label=f"换表衔接（{change.old_meter_id} 终值 {change.old_final_kwh} → "
                  f"{change.new_meter_id} 初值 {change.new_initial_kwh}）",
        ))

    # 4) 同一时刻多个来源的取值必须一致，否则是完整性发现
    points: list[TimelinePoint] = []
    for at in sorted(points_by_at):
        slot = points_by_at[at]
        distinct = set(slot["values"])
        if len(distinct) > 1:
            findings.append(Finding(
                "SPLICE_MISMATCH",
                f"{fmt_ts(at)} 的多个证据来源给出不同资产累计值 {sorted(str(v) for v in distinct)}，"
                f"以换表记录/读数中的首个取值为准",
                tuple(b.ref_id for b in slot["basis"]),
            ))
        points.append(TimelinePoint(at=at, asset_cum_kwh=slot["values"][0], basis=tuple(slot["basis"])))

    # 5) 计数回退与矛盾读数都形成缺口（不做任何修补）。
    #    矛盾时刻本身没有任何有效点落座，它打断的是横跨它的相邻分段；
    #    单侧无邻点的矛盾由窗口覆盖缺口兜底，此处只记录发现项。
    for prev, curr in zip(points, points[1:]):
        between = [c for c in conflicted if prev.at < c < curr.at]
        if between:
            refs = tuple(b.ref_id for b in prev.basis + curr.basis)
            refs += tuple(rid for c in between for rid in conflicted[c])
            gaps.append(Gap(
                kind="reading_conflict",
                start=prev.at,
                end=curr.at,
                reason="区间内存在同一时刻的矛盾读数，吞吐不可计量，待更正流程裁决",
                evidence_refs=refs,
            ))
            continue
        if curr.asset_cum_kwh < prev.asset_cum_kwh:
            refs = tuple(b.ref_id for b in prev.basis + curr.basis)
            findings.append(Finding(
                "COUNTER_REGRESSION",
                f"{fmt_ts(prev.at)} 至 {fmt_ts(curr.at)} 资产累计由 {prev.asset_cum_kwh} "
                f"降为 {curr.asset_cum_kwh}，无换表记录可解释，区间留作证据缺口",
                refs,
            ))
            gaps.append(Gap(
                kind="counter_regression",
                start=prev.at,
                end=curr.at,
                reason="累计量回退且无换表记录支撑，吞吐不可计量",
                evidence_refs=refs,
            ))
    return points, gaps, findings


# ---------------------------------------------------------------------------
# 合同期间归属与指标
# ---------------------------------------------------------------------------


def _contract_periods(rules: RuleSet, window: Window) -> list:
    """评估窗口内的合同期间序列：[版本, 期间起点, 期间终点)，半开区间。"""
    periods = []
    versions = rules.versions
    for idx, ver in enumerate(versions):
        start = max(ver.effective_from, window.start)
        next_from = versions[idx + 1].effective_from if idx + 1 < len(versions) else None
        end = min(next_from, window.end) if next_from else window.end
        if start < end:
            periods.append((ver, start, end))
    return periods


def compute_metrics(view: LedgerView, identity: IdentityMap, rules: RuleSet,
                    window: Window, boundary_policy: str = BOUNDARY_STRICT) -> MetricsResult:
    if boundary_policy not in (BOUNDARY_STRICT, BOUNDARY_APPORTION):
        raise ValueError(f"未知边界策略: {boundary_policy}")

    result = MetricsResult(window=window)
    points, gaps, findings = build_timeline(view, identity)
    result.points = points
    result.gaps.extend(gaps)
    result.findings.extend(findings)

    # 窗口边缘覆盖缺口：宁可留白，不可插值
    if points:
        if window.start < points[0].at:
            result.gaps.append(Gap(
                kind="coverage",
                start=window.start,
                end=points[0].at,
                reason="窗口起点至首个证据点之间无任何读数，吞吐不可计量",
            ))
        if points[-1].at < window.end:
            result.gaps.append(Gap(
                kind="coverage",
                start=points[-1].at,
                end=window.end,
                reason="末个证据点至窗口终点之间无任何读数，吞吐不可计量",
            ))
    else:
        result.gaps.append(Gap(
            kind="coverage", start=window.start, end=window.end,
            reason="窗口内无任何读数",
        ))

    # 缺口区间集合（回退类缺口会吞掉其间的分段）
    gap_spans = [(g.start, g.end) for g in result.gaps if g.start < g.end]

    def _in_gap(start: datetime, end: datetime) -> bool:
        return any(start < g_end and end > g_start for g_start, g_end in gap_spans)

    # 原始分段（相邻证据点）
    raw_segments: list[Segment] = []
    for prev, curr in zip(points, points[1:]):
        if _in_gap(prev.at, curr.at):
            continue  # 该区间已被判为缺口
        raw_segments.append(Segment(
            index=len(raw_segments),
            start=prev.at,
            end=curr.at,
            kwh=curr.asset_cum_kwh - prev.asset_cum_kwh,
            start_basis=prev.basis,
            end_basis=curr.basis,
        ))

    # 合同期间归属
    periods = _contract_periods(rules, window)
    if not periods:
        result.findings.append(Finding("NO_CONTRACT_VERSION", "评估窗口不在任何合同版本生效期内", ()))

    def _period_of(moment: datetime):
        for ver, p_start, p_end in periods:
            if p_start <= moment < p_end:
                return ver, p_start, p_end
        return None

    final_segments: list[Segment] = []
    for seg in raw_segments:
        home = _period_of(seg.start)
        if home and seg.end <= home[2]:
            final_segments.append(Segment(**{**seg.__dict__, "index": len(final_segments),
                                             "period_version": home[0].version}))
            continue
        # 跨越期间边界
        if boundary_policy == BOUNDARY_APPORTION and home:
            final_segments.extend(_apportion_segment(seg, periods, final_segments))
        else:
            result.unattributed.append(len(final_segments))
            candidates = [p[0].version for p in periods if p[1] < seg.end and p[2] > seg.start]
            result.findings.append(Finding(
                "BOUNDARY_UNATTRIBUTED",
                f"分段 {fmt_ts(seg.start)} → {fmt_ts(seg.end)} 跨越合同版本边界"
                f"（候选版本 {candidates}），严格策略下不归属任何期间；"
                f"请在边界时刻补充读数以解开归属",
                tuple(b.ref_id for b in seg.start_basis + seg.end_basis),
            ))
            final_segments.append(Segment(**{**seg.__dict__, "index": len(final_segments)}))

    result.segments = final_segments
    result.total_kwh = sum((s.kwh for s in final_segments), Decimal("0"))

    # 期间指标与等效循环
    total_cycles = Decimal("0")
    for ver, p_start, p_end in periods:
        members = [s for s in final_segments if s.period_version == ver.version]
        kwh = sum((s.kwh for s in members), Decimal("0"))
        cycles = (kwh / ver.rated_energy_kwh).quantize(CYCLES_QUANT, rounding=ROUND_HALF_UP)
        total_cycles += cycles
        result.periods.append(PeriodMetrics(
            version=ver.version,
            start=p_start,
            end=p_end,
            throughput_kwh=kwh,
            cycles=cycles,
            rated_energy_kwh=ver.rated_energy_kwh,
            minimum_availability=ver.minimum_availability,
            segment_indexes=tuple(s.index for s in members),
        ))
    result.total_cycles = total_cycles
    if result.unattributed:
        result.findings.append(Finding(
            "CYCLES_PARTIAL",
            "存在未归属分段，等效循环次数仅为已归属部分合计",
            (),
        ))
    return result


def _apportion_segment(seg: Segment, periods: list, final_segments: list) -> list:
    """按时间占比把跨界分段分摊到各期间（归属层面的分摊，显式标注）。

    注意：这只是把已实测的吞吐在版本期间之间分配，总量不变；
    缺测区间永远不走这条路。
    """
    total_seconds = Decimal(str((seg.end - seg.start).total_seconds()))
    pieces = []
    for ver, p_start, p_end in periods:
        overlap_start = max(seg.start, p_start)
        overlap_end = min(seg.end, p_end)
        if overlap_start >= overlap_end:
            continue
        share = Decimal(str((overlap_end - overlap_start).total_seconds())) / total_seconds
        pieces.append(Segment(
            index=len(final_segments) + len(pieces),
            start=overlap_start,
            end=overlap_end,
            kwh=(seg.kwh * share).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            start_basis=seg.start_basis,
            end_basis=seg.end_basis,
            period_version=ver.version,
            apportioned=True,
        ))
    return pieces

"""依据账本事实重算吞吐量与循环次数。

口径（与供应商共同认可的计算规则）：

1. 每块表在自己的服役段内独立求累计量增量；换表点不做新旧表读数相减
   （旧表终值承载历史累计，新表从初值重新起步，两者只做身份锚定）。
2. 两个有效读数之间的增量必须满足单调不减；出现回退的增量一律隔离，
   不参与达标计算，也不允许被"修平"。
3. 增量区间若与**未闭合证据缺口**相交，整条增量隔离（宁可少算，绝不插值）。
4. 增量跨越合同版本边界时，按生效时长做时间分摊，每个分摊分量都带
   ``time_apportioned`` 标记；合同也可声明 ``cross_version_basis=exclude``
   要求把跨边界增量整体隔离。
5. 等效满充满放循环次数 = 观察吞吐量 / 合同项 ``rated_energy_kwh``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Any

from .ledger import StateView
from .model import ContractVersion, MeterChange, Reading, ReadingSource

getcontext().prec = 28

QUANT = Decimal("0.000001")  # 内部展示统一保留 6 位小数，比较时仍用精确值

# 增量分量的事实依据类型
BASIS_OBSERVED = "observed"  # 两端均为有效读数
BASIS_ANCHOR = "meter_change_anchor"  # 一端为换表登记端点
BASIS_TIME_APPORTIONED = "time_apportioned"  # 跨合同版本边界的时间分摊
BASIS_SPANS_OPEN_GAP = "spans_open_gap"  # 跨未闭合缺口：隔离，不计
BASIS_REGRESSION = "register_regression"  # 表计回退：隔离，不计
BASIS_VERSION_EXCLUDED = "cross_version_excluded"  # 合同要求跨边界增量不计

ISOLATED_BASES = {BASIS_SPANS_OPEN_GAP, BASIS_REGRESSION, BASIS_VERSION_EXCLUDED}


@dataclass(frozen=True)
class Point:
    """服役段内累计量序列上的一个点（真实读数或换表端点）。"""

    ts: Any  # datetime
    value: Decimal
    meter_id: str
    reading_seq: int | None = None
    source: ReadingSource | None = None
    import_digest: str = ""
    change_seq: int | None = None  # 换表端点锚点时指向换表记录
    revision_seq: int | None = None
    is_anchor: bool = False


@dataclass(frozen=True)
class DeltaComponent:
    """一条原始增量在某个合同版本桶内的分摊分量（下钻最小单元）。"""

    key: str  # (表, 起点, 终点, 合同版本)，版本间差异比对用
    meter_id: str
    start_ts: Any
    end_ts: Any
    contract_version: str
    kwh: Decimal
    basis: str
    start_seq: int | None
    end_seq: int | None
    change_seq: int | None
    start_source: str | None
    end_source: str | None
    quarantined: bool

    def evidence(self) -> dict[str, Any]:
        """该分量的证据指针，审核人据此逐笔下钻。"""
        return {
            "meter_id": self.meter_id,
            "start_reading_seq": self.start_seq,
            "end_reading_seq": self.end_seq,
            "meter_change_seq": self.change_seq,
            "start_source": self.start_source,
            "end_source": self.end_source,
            "basis": self.basis,
        }


@dataclass
class ContractBucket:
    """一个合同版本生效窗口内的累计结果。"""

    contract: ContractVersion
    window_start: Any
    window_end: Any | None
    observed_kwh: Decimal = Decimal(0)
    quarantined_kwh: Decimal = Decimal(0)
    cycles: Decimal | None = None
    components: list[DeltaComponent] = field(default_factory=list)
    apportioned_component_count: int = 0

    def threshold(self, name: str) -> Decimal | None:
        raw = self.contract.terms.get(name)
        return Decimal(str(raw)) if raw is not None else None


@dataclass
class ComputationResult:
    """整案卷的重算结果。"""

    asset_id: str
    buckets: list[ContractBucket]
    components: list[DeltaComponent]
    anomalies: list[dict[str, Any]]
    total_observed_kwh: Decimal
    total_quarantined_kwh: Decimal
    total_cycles: Decimal | None
    open_gaps: list[Any]

    def bucket_of(self, version: str) -> ContractBucket:
        for b in self.buckets:
            if b.contract.version == version:
                return b
        raise KeyError(version)


def _overlaps_gap(p: Point, q: Point, gaps: list[Any]) -> Any | None:
    """增量区间 (p.ts, q.ts) 是否与任一未闭合缺口相交。"""
    for g in gaps:
        if p.ts < g.end_ts and g.start_ts < q.ts:
            return g
    return None


def _tenure_series(
    meter_id: str,
    tenure_from: Any,
    tenure_to: Any | None,
    readings: list[Reading],
    start_anchor: Point | None,
    end_anchor: Point | None,
) -> list[Point]:
    """组装一块表服役段内的累计量点序列（读数 + 换表端点，去重后按时间排序）。"""
    pts: dict[Any, Point] = {}
    for r in readings:
        if r.meter_id != meter_id:
            continue
        if r.ts < tenure_from:
            continue
        if tenure_to is not None and r.ts > tenure_to:
            continue
        pts[r.ts] = Point(
            ts=r.ts,
            value=r.value,
            meter_id=meter_id,
            reading_seq=r.seq,
            source=r.source,
            import_digest=r.import_digest,
            revision_seq=r.revision_seq,
        )
    for anchor in (start_anchor, end_anchor):
        if anchor is None:
            continue
        existing = pts.get(anchor.ts)
        if existing is not None:
            # 换表端点与真实读数同时刻：账本已保证二者相等，以真实读数为准
            continue
        pts[anchor.ts] = anchor
    return [pts[k] for k in sorted(pts)]


def _anchor_points(change: MeterChange) -> tuple[Point, Point]:
    old_anchor = Point(
        ts=change.changed_ts,
        value=change.old_final,
        meter_id=change.old_meter_id,
        change_seq=change.seq,
        is_anchor=True,
    )
    new_anchor = Point(
        ts=change.changed_ts,
        value=change.new_initial,
        meter_id=change.new_meter_id,
        change_seq=change.seq,
        is_anchor=True,
    )
    return old_anchor, new_anchor


def _contract_windows(contracts: tuple[ContractVersion, ...]) -> list[tuple[ContractVersion, Any, Any | None]]:
    windows = []
    ordered = sorted(contracts, key=lambda c: c.effective_from)
    for i, cv in enumerate(ordered):
        end = ordered[i + 1].effective_from if i + 1 < len(ordered) else None
        windows.append((cv, cv.effective_from, end))
    return windows


def compute(state: StateView) -> ComputationResult:
    """对账本视图重算全部指标。视图可指向 HEAD 或任一已冻结送审版本。"""
    readings = state.active_readings()
    changes = state.active_changes()
    open_gaps = state.open_gaps()
    tenures = state.meter_tenures()

    # 组装每段服役期的点序列
    series_by_tenure: list[list[Point]] = []
    for idx, tenure in enumerate(tenures):
        start_anchor = end_anchor = None
        if idx > 0:
            _, new_anchor = _anchor_points(changes[idx - 1])
            start_anchor = new_anchor
        if idx < len(changes):
            old_anchor, _ = _anchor_points(changes[idx])
            end_anchor = old_anchor
        series_by_tenure.append(
            _tenure_series(
                tenure.meter_id,
                tenure.from_ts,
                tenure.to_ts,
                readings,
                start_anchor,
                end_anchor,
            )
        )

    # 合同版本桶
    windows = _contract_windows(state.contracts)
    buckets = [
        ContractBucket(contract=cv, window_start=w_start, window_end=w_end)
        for cv, w_start, w_end in windows
    ]

    components: list[DeltaComponent] = []
    anomalies: list[dict[str, Any]] = []

    def alloc_component(p: Point, q: Point, delta: Decimal, basis: str) -> None:
        """把原始增量分配进一个或多个合同版本桶。"""
        nonlocal components
        # 与增量 (p.ts, q.ts] 相交的合同窗口
        hit = [
            (b, w_start, w_end)
            for b, (_, w_start, w_end) in zip(buckets, windows)
            if p.ts < (w_end or q.ts) and (w_start < q.ts)
        ]
        if not hit:
            return
        quarantined = basis in ISOLATED_BASES
        if len(hit) == 1 or quarantined:
            # 隔离增量不做精细分摊，整体挂到起点所在版本，仅在报告中列示
            bucket = hit[0][0]
            comp = _component(p, q, bucket.contract.version, delta, basis)
            components.append(comp)
            bucket.components.append(comp)
            if quarantined:
                bucket.quarantined_kwh += delta
            else:
                bucket.observed_kwh += delta
            return

        # 跨多个合同版本：按生效时长分摊（或按合同口径整体排除）
        basis_term = str(buckets[0].contract.terms.get("cross_version_basis", "time_weighted"))
        total_seconds = Decimal(int((q.ts - p.ts).total_seconds()))
        if basis_term == "exclude":
            # 合同明确要求跨边界增量整体不计：挂首桶隔离
            bucket = hit[0][0]
            comp = _component(p, q, bucket.contract.version, delta, BASIS_VERSION_EXCLUDED)
            components.append(comp)
            bucket.components.append(comp)
            bucket.quarantined_kwh += delta
            return

        # 时间分摊：前 n-1 段按比例量化，末段取余量，保证分量之和严格等于原增量
        allocated = Decimal(0)
        for index, (bucket, w_start, w_end) in enumerate(hit):
            seg_start = max(p.ts, w_start)
            seg_end = min(q.ts, w_end) if w_end else q.ts
            seconds = Decimal(int((seg_end - seg_start).total_seconds()))
            last_segment = index == len(hit) - 1
            share = (delta - allocated) if last_segment else (delta * seconds / total_seconds).quantize(QUANT)
            allocated += share
            comp = _component(p, q, bucket.contract.version, share, BASIS_TIME_APPORTIONED)
            components.append(comp)
            bucket.components.append(comp)
            bucket.observed_kwh += share
            bucket.apportioned_component_count += 1

    def _component(p: Point, q: Point, version: str, kwh: Decimal, basis: str) -> DeltaComponent:
        return DeltaComponent(
            key=f"{p.meter_id}|{p.ts.isoformat()}|{q.ts.isoformat()}|{version}",
            meter_id=p.meter_id,
            start_ts=p.ts,
            end_ts=q.ts,
            contract_version=version,
            kwh=kwh,
            basis=basis,
            start_seq=p.reading_seq,
            end_seq=q.reading_seq,
            change_seq=q.change_seq or p.change_seq,
            start_source=p.source.value if p.source else ("anchor" if p.is_anchor else None),
            end_source=q.source.value if q.source else ("anchor" if q.is_anchor else None),
            quarantined=basis in ISOLATED_BASES,
        )

    # 逐服役段求增量
    for series in series_by_tenure:
        for p, q in zip(series, series[1:]):
            delta = q.value - p.value
            gap = _overlaps_gap(p, q, open_gaps)
            if gap is not None:
                basis = BASIS_SPANS_OPEN_GAP
                anomalies.append(
                    {
                        "kind": "spans_open_gap",
                        "gap_seq": gap.seq,
                        "meter_id": p.meter_id,
                        "start_ts": p.ts.isoformat(),
                        "end_ts": q.ts.isoformat(),
                        "delta_kwh": str(delta),
                        "reason": gap.reason,
                    }
                )
                # 跨缺口增量整体隔离：按合同桶挂账展示，但不计入观察吞吐量
                alloc_component(p, q, max(delta, Decimal(0)), basis)
            elif delta < 0:
                # 表计回退：不允许负增量抵减累计量，生成 0 值隔离分量供下钻
                basis = BASIS_REGRESSION
                anomalies.append(
                    {
                        "kind": "register_regression",
                        "meter_id": p.meter_id,
                        "start_ts": p.ts.isoformat(),
                        "end_ts": q.ts.isoformat(),
                        "start_value": str(p.value),
                        "end_value": str(q.value),
                        "regression_kwh": str(delta),
                    }
                )
                hit0 = [
                    b for b, (_, w_start, w_end) in zip(buckets, windows)
                    if p.ts < (w_end or q.ts) and w_start < q.ts
                ]
                if hit0:
                    comp = _component(p, q, hit0[0].contract.version, Decimal(0), basis)
                    components.append(comp)
                    hit0[0].components.append(comp)
            else:
                basis = BASIS_ANCHOR if (p.is_anchor or q.is_anchor) else BASIS_OBSERVED
                alloc_component(p, q, delta, basis)

    total_observed = sum((b.observed_kwh for b in buckets), Decimal(0))
    total_quarantined = sum((b.quarantined_kwh for b in buckets), Decimal(0))

    total_cycles = None
    for b in buckets:
        rated = b.contract.terms.get("rated_energy_kwh")
        if rated is not None:
            b.cycles = (b.observed_kwh / Decimal(str(rated))).quantize(QUANT)
    rated_any = [b.cycles for b in buckets if b.cycles is not None]
    if rated_any:
        total_cycles = sum(rated_any, Decimal(0))

    return ComputationResult(
        asset_id=state.asset_id,
        buckets=buckets,
        components=components,
        anomalies=anomalies,
        total_observed_kwh=total_observed,
        total_quarantined_kwh=total_quarantined,
        total_cycles=total_cycles,
        open_gaps=open_gaps,
    )

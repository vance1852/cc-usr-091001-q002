"""版本差异：两个送审版本之间究竟多算、少算了什么。

对比两个版本冻结时的报告与其间的台账事件，产出：
- 期间级吞吐/循环差异（新版 - 旧版）；
- 分段级差异及其成因（更正、补录、换表登记），逐条挂到证据；
- 缺口开合清单；
- 人读的中文小结。
"""
from __future__ import annotations

from decimal import Decimal

from .ledger import EVT_ADD_READING, EVT_CORRECT_READING, EVT_IMPORT_FILE, EVT_METER_CHANGE, Event
from .model import as_decimal


def _period_map(report: dict) -> dict:
    return {p["version"]: p for p in report["periods"]}


def _segment_key(seg: dict) -> tuple:
    return (seg["start"], seg["end"])


def _basis_reading_ids(seg: dict) -> set:
    ids = set()
    for basis in seg.get("start_basis", []) + seg.get("end_basis", []):
        if basis.get("kind") == "reading":
            ids.add(basis["ref_id"])
    return ids


def diff_reports(
    old_report: dict,
    new_report: dict,
    events_between: list[Event],
    *,
    old_label: str,
    new_label: str,
) -> dict:
    """生成 old → new 的差异清单。delta 一律为 新版 - 旧版。"""
    # 1) 期间级差异
    old_periods = _period_map(old_report)
    new_periods = _period_map(new_report)
    period_deltas = []
    for version in sorted(set(old_periods) | set(new_periods)):
        old_p = old_periods.get(version)
        new_p = new_periods.get(version)
        old_kwh = as_decimal(old_p["throughput_kwh"]) if old_p else Decimal("0")
        new_kwh = as_decimal(new_p["throughput_kwh"]) if new_p else Decimal("0")
        old_cyc = as_decimal(old_p["equivalent_cycles"]) if old_p else Decimal("0")
        new_cyc = as_decimal(new_p["equivalent_cycles"]) if new_p else Decimal("0")
        period_deltas.append({
            "version": version,
            "throughput_old": old_kwh,
            "throughput_new": new_kwh,
            "throughput_delta": new_kwh - old_kwh,
            "cycles_old": old_cyc,
            "cycles_new": new_cyc,
            "cycles_delta": new_cyc - old_cyc,
        })

    old_total = as_decimal(old_report["summary"]["total_throughput_kwh"])
    new_total = as_decimal(new_report["summary"]["total_throughput_kwh"])
    old_cycles = as_decimal(old_report["summary"]["total_equivalent_cycles"])
    new_cycles = as_decimal(new_report["summary"]["total_equivalent_cycles"])

    # 2) 两版之间发生的台账事件（更正/补录/换表登记）
    corrections = []
    additions = []
    imports = []
    meter_changes = []
    reading_events: dict[str, list[Event]] = {}
    for event in events_between:
        payload = event.payload
        if event.event_type == EVT_CORRECT_READING:
            rev = payload["revision"]
            corrections.append({
                "reading_id": rev["reading_id"],
                "revision_id": rev["revision_id"],
                "supersedes": rev["supersedes"],
                "new_cumulative_kwh": as_decimal(rev["cumulative_kwh"]),
                "observed_at": rev["observed_at"],
                "operator": rev["operator"],
                "reason": rev["reason"],
                "recorded_at": event.recorded_at,
            })
            reading_events.setdefault(rev["reading_id"], []).append(event)
        elif event.event_type == EVT_ADD_READING:
            rev = payload["revision"]
            additions.append({
                "reading_id": rev["reading_id"],
                "meter_id": rev["meter_id"],
                "source": rev["source"],
                "observed_at": rev["observed_at"],
                "cumulative_kwh": as_decimal(rev["cumulative_kwh"]),
                "operator": rev["operator"],
                "reason": rev["reason"],
            })
            reading_events.setdefault(rev["reading_id"], []).append(event)
        elif event.event_type == EVT_IMPORT_FILE:
            imports.append({"digest": payload["digest"], "file_name": payload.get("file_name"),
                            "operator": event.operator, "recorded_at": event.recorded_at})
        elif event.event_type == EVT_METER_CHANGE:
            meter_changes.append(payload["meter_change"])

    # 3) 分段级差异与成因归因
    old_segments = {_segment_key(s): s for s in old_report["segments"]}
    new_segments = {_segment_key(s): s for s in new_report["segments"]}
    segment_deltas = []
    for key in sorted(set(old_segments) | set(new_segments)):
        old_seg = old_segments.get(key)
        new_seg = new_segments.get(key)
        old_kwh = as_decimal(old_seg["kwh"]) if old_seg else Decimal("0")
        new_kwh = as_decimal(new_seg["kwh"]) if new_seg else Decimal("0")
        delta = new_kwh - old_kwh
        if delta == 0 and old_seg and new_seg:
            continue
        involved = set()
        if old_seg:
            involved |= _basis_reading_ids(old_seg)
        if new_seg:
            involved |= _basis_reading_ids(new_seg)
        causes = []
        for rid in sorted(involved):
            for event in reading_events.get(rid, []):
                rev = event.payload["revision"]
                if event.event_type == EVT_CORRECT_READING:
                    causes.append(
                        f"更正读数 {rid}（{rev['observed_at']} 改为 {rev['cumulative_kwh']}，"
                        f"{rev['operator']}：{rev['reason']}）"
                    )
                else:
                    causes.append(
                        f"补录读数 {rid}（{rev['observed_at']} = {rev['cumulative_kwh']}，"
                        f"{rev['operator']}）"
                    )
        segment_deltas.append({
            "start": key[0],
            "end": key[1],
            "old_kwh": old_kwh if old_seg else None,
            "new_kwh": new_kwh if new_seg else None,
            "delta_kwh": delta,
            "old_period": old_seg["period_version"] if old_seg else None,
            "new_period": new_seg["period_version"] if new_seg else None,
            "causes": causes,
        })

    # 3b) 粒度调和：旧分段若被新版中若干更细的分段完整拼接，说明只是区间内
    #     补录了中间读数、区间吞吐并未变化，成因标注为"分段细化"
    new_seg_list = sorted(new_segments.values(), key=lambda s: (s["start"], s["end"]))
    for delta in segment_deltas:
        if delta["causes"] or delta["new_kwh"] is not None:
            continue  # 已有成因，或并非"旧分段消失"
        tiles = [s for s in new_seg_list
                 if s["start"] >= delta["start"] and s["end"] <= delta["end"]]
        if not tiles:
            continue
        contiguous = (tiles[0]["start"] == delta["start"]
                      and tiles[-1]["end"] == delta["end"]
                      and all(tiles[i + 1]["start"] == tiles[i]["end"]
                              for i in range(len(tiles) - 1)))
        if not contiguous:
            continue
        inner_ids = set()
        for tile in tiles:
            inner_ids |= _basis_reading_ids(tile)
        inner_ids -= _basis_reading_ids(old_segments[(delta["start"], delta["end"])])
        tile_causes = []
        for rid in sorted(inner_ids):
            for event in reading_events.get(rid, []):
                rev = event.payload["revision"]
                tile_causes.append(
                    f"补录读数 {rid}（{rev['observed_at']} = {rev['cumulative_kwh']}，"
                    f"{rev['operator']}）"
                )
        delta["causes"] = [
            f"分段细化：区间内补录读数使该分段拆为 {len(tiles)} 段，区间吞吐不变"
        ] + tile_causes
        delta["granularity_only"] = True

    # 4) 多算/少算清单（以旧版为基准陈述）
    over_counted = []   # 旧版多计（新版更少）
    under_counted = []  # 旧版少计（新版更多）
    for pd in period_deltas:
        delta = pd["throughput_delta"]
        if delta < 0:
            over_counted.append({
                "version": pd["version"],
                "kwh": -delta,
                "cycles": -pd["cycles_delta"],
                "statement": f"{pd['version']} 期间：{old_label} 多计 {-delta} kWh",
            })
        elif delta > 0:
            under_counted.append({
                "version": pd["version"],
                "kwh": delta,
                "cycles": pd["cycles_delta"],
                "statement": f"{pd['version']} 期间：{old_label} 少计 {delta} kWh",
            })

    # 5) 缺口开合
    def gap_keys(report):
        return {(g["kind"], g["start"], g["end"]): g for g in report["gaps"]}

    old_gaps = gap_keys(old_report)
    new_gaps = gap_keys(new_report)
    gaps_closed = [g for k, g in old_gaps.items() if k not in new_gaps]
    gaps_opened = [g for k, g in new_gaps.items() if k not in old_gaps]

    # 6) 人读小结
    summary_lines = []
    total_delta = new_total - old_total
    if total_delta != 0:
        direction = "多计" if total_delta > 0 else "少计"
        summary_lines.append(
            f"窗口合计：{new_label} 比 {old_label} {direction} {abs(total_delta)} kWh"
            f"（{old_total} → {new_total}）"
        )
    else:
        summary_lines.append(f"窗口合计不变（{old_total} kWh），期间归属发生变化" if period_deltas
                             else "两版计量结果一致")
    for item in over_counted + under_counted:
        summary_lines.append(item["statement"])
    for cause in corrections:
        summary_lines.append(
            f"更正：{cause['observed_at']} 读数改为 {cause['new_cumulative_kwh']}"
            f"（{cause['operator']}：{cause['reason']}）"
        )
    for add in additions:
        summary_lines.append(
            f"补录：{add['observed_at']} = {add['cumulative_kwh']}"
            f"（{add['source']}，{add['operator']}）"
        )

    return {
        "from_version": old_label,
        "to_version": new_label,
        "total": {
            "throughput_old": old_total,
            "throughput_new": new_total,
            "throughput_delta": total_delta,
            "cycles_old": old_cycles,
            "cycles_new": new_cycles,
            "cycles_delta": new_cycles - old_cycles,
        },
        "period_deltas": period_deltas,
        "segment_deltas": segment_deltas,
        "over_counted": over_counted,
        "under_counted": under_counted,
        "corrections": corrections,
        "additions": additions,
        "imports": imports,
        "meter_changes_recorded": meter_changes,
        "gaps_closed": gaps_closed,
        "gaps_opened": gaps_opened,
        "summary_lines": summary_lines,
    }

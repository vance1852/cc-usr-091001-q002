"""只追加的质保证据账本。

账本中的事实只有新增、没有覆盖：

* 文件导入先登记摘要，同一摘要在案卷谱系内不可重复导入；
* 读数挂在导入事件下，带来源、操作者与文件摘要；
* 换表必须同时给出旧表终值、新表初值与生效时刻；
* 缺口被显式登记为证据缺口，普通导入不得落入开放缺口；
* 任何更正都是一条修订事件 + 一条新读数，原值保留并标记被取代；
* 冻结对截至当时的全部事件计算哈希，此后案卷不可再写，只能开子分支。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from .model import (
    Actor,
    ContractVersion,
    EventType,
    FileDigest,
    FrozenSnapshot,
    Gap,
    ImportRecord,
    MeterChange,
    MeterTenure,
    Reading,
    ReadingSource,
    Revision,
    RevisionKind,
    parse_ts,
    to_decimal,
)


class LedgerError(ValueError):
    """账本规则被违反（缺要素、越界写入、重复导入等）。"""


class DossierFrozenError(LedgerError):
    """案卷已冻结为送审版本；新补录只能进入子分支。"""


@dataclass
class StateView:
    """账本在某一时刻（或某一冻结点）的只读投影，供计算与出报告使用。"""

    asset_id: str
    initial_meter_id: str
    case_start: datetime
    case_end: datetime | None
    contracts: tuple[ContractVersion, ...]
    readings: dict[int, Reading] = field(default_factory=dict)
    revisions: list[Revision] = field(default_factory=list)
    changes: dict[int, MeterChange] = field(default_factory=dict)
    change_order: list[int] = field(default_factory=list)
    amendments: dict[int, int] = field(default_factory=dict)
    gaps: dict[int, Gap] = field(default_factory=dict)
    imports: list[ImportRecord] = field(default_factory=list)
    superseded: dict[int, int] = field(default_factory=dict)  # 原读数序号 -> 新读数序号
    closed_gaps: dict[int, int] = field(default_factory=dict)  # 缺口序号 -> 闭合修订序号
    freezes: list[FrozenSnapshot] = field(default_factory=list)
    cutoff_seq: int | None = None

    # ---- 查询辅助 -------------------------------------------------------

    def snapshot(self, label: str) -> FrozenSnapshot:
        for snap in self.freezes:
            if snap.label == label:
                return snap
        raise LedgerError(f"未知的送审版本: {label!r}")

    def active_changes(self) -> list[MeterChange]:
        """按生效时刻排序的换表记录（修订链取最新版本）。"""
        result = []
        for seq in self.change_order:
            latest = seq
            while latest in self.amendments:
                latest = self.amendments[latest]
            result.append(self.changes[latest])
        result.sort(key=lambda c: c.changed_ts)
        return result

    def active_readings(self) -> list[Reading]:
        return sorted(
            (r for seq, r in self.readings.items() if seq not in self.superseded),
            key=lambda r: (r.ts, r.seq),
        )

    def open_gaps(self) -> list[Gap]:
        return sorted(
            (g for seq, g in self.gaps.items() if seq not in self.closed_gaps),
            key=lambda g: (g.start_ts, g.seq),
        )

    def meter_tenures(self) -> list[MeterTenure]:
        """设备身份沿革：每块表的服役区间与起止读数。"""
        changes = self.active_changes()
        tenures: list[MeterTenure] = []
        meter_id = self.initial_meter_id
        from_ts = self.case_start
        pending_initial: Decimal | None = None  # 上一段换表给出的新表初值
        for ch in changes:
            tenures.append(
                MeterTenure(
                    meter_id=meter_id,
                    from_ts=from_ts,
                    to_ts=ch.changed_ts,
                    end_reason="meter_change",
                    initial=pending_initial,
                    final=ch.old_final,
                    change_seq=ch.seq,
                )
            )
            meter_id = ch.new_meter_id
            from_ts = ch.changed_ts
            pending_initial = ch.new_initial
        tenures.append(
            MeterTenure(
                meter_id=meter_id,
                from_ts=from_ts,
                to_ts=self.case_end,
                end_reason="case_end" if self.case_end else "open",
                initial=pending_initial,
            )
        )
        return tenures

    def meter_at(self, ts: datetime) -> str:
        meter_id = self.initial_meter_id
        for ch in self.active_changes():
            if ts >= ch.changed_ts:
                meter_id = ch.new_meter_id
            else:
                break
        return meter_id

    def contracts_effective_at(self, ts: datetime) -> ContractVersion:
        chosen = self.contracts[0]
        for cv in self.contracts:
            if ts >= cv.effective_from:
                chosen = cv
            else:
                break
        return chosen


def _canonical_json(obj: Any) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, datetime):
            return o.astimezone().isoformat()
        if isinstance(o, Decimal):
            return str(o)
        if hasattr(o, "value"):
            return o.value
        if hasattr(o, "__dict__"):
            return o.__dict__
        raise TypeError(f"无法规范化 {type(o)!r}")

    return json.dumps(
        obj, default=default, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


class Dossier:
    """一个设备的质保证据案卷（只追加账本 + 冻结/分支能力）。"""

    def __init__(
        self,
        asset_id: str,
        initial_meter_id: str,
        case_start: str | datetime,
        contracts: Iterable[ContractVersion | dict[str, Any]],
        case_end: str | datetime | None = None,
    ) -> None:
        self._events: list[tuple[EventType, Any]] = []
        self.asset_id = asset_id
        self.initial_meter_id = initial_meter_id
        self.case_start = parse_ts(case_start)
        self.case_end = parse_ts(case_end) if case_end else None
        self.contracts = tuple(self._normalize_contract(c) for c in contracts)
        if not self.contracts:
            raise LedgerError("至少需要一个合同版本")
        versions = [cv.version for cv in self.contracts]
        if len(set(versions)) != len(versions):
            raise LedgerError("合同版本号必须唯一")
        if sorted(self.contracts, key=lambda c: c.effective_from) != list(self.contracts):
            raise LedgerError("合同版本必须按 effective_from 升序提供")
        if self.case_start < self.contracts[0].effective_from:
            raise LedgerError("案卷起点早于首版合同生效时间，无法确定适用规则")
        if self.case_end and self.case_end < self.case_start:
            raise LedgerError("案卷结束时间早于开始时间")
        self._locked = False
        self._state = self._replay()

    # ---- 内部机制 -------------------------------------------------------

    @staticmethod
    def _normalize_contract(raw: ContractVersion | dict[str, Any]) -> ContractVersion:
        if isinstance(raw, ContractVersion):
            return raw
        # 除版本标识与生效时间外的键一律视为合同条款，与参考资料加载口径一致
        terms = {
            k: v
            for k, v in raw.items()
            if k not in ("version", "effective_from", "terms")
        }
        terms.update(raw.get("terms", {}))
        return ContractVersion(
            version=raw["version"],
            effective_from=parse_ts(raw["effective_from"]),
            terms=terms,
        )

    @property
    def _seq(self) -> int:
        return len(self._events) + 1  # 序号从 1 开始

    def _replay(self, cutoff: int | None = None) -> StateView:
        st = StateView(
            asset_id=self.asset_id,
            initial_meter_id=self.initial_meter_id,
            case_start=self.case_start,
            case_end=self.case_end,
            contracts=self.contracts,
            cutoff_seq=cutoff,
        )
        for etype, obj in self._events:
            # 冻结点是版本元数据而非事实：只要其覆盖的事实范围不超过 cutoff 就保留，
            # 这样历史视图既能定位送审版本，又不会泄漏之后分支上的补录事件。
            if etype is EventType.FREEZE:
                if cutoff is None or obj.last_event_seq <= cutoff:
                    st.freezes.append(obj)
                continue
            if cutoff is not None and getattr(obj, "seq", 0) > cutoff:
                break
            if etype is EventType.READING:
                st.readings[obj.seq] = obj
            elif etype is EventType.REVISION:
                st.revisions.append(obj)
                if obj.kind is RevisionKind.CORRECTION and obj.new_reading_seq is not None:
                    st.superseded[obj.target_seq] = obj.new_reading_seq
                elif obj.kind is RevisionKind.GAP_CLOSURE and obj.target_seq is not None:
                    st.closed_gaps[obj.target_seq] = obj.seq
                elif obj.kind is RevisionKind.METER_CHANGE_AMEND:
                    st.amendments[obj.target_seq] = obj.new_reading_seq  # 指向新换表事件
            elif etype is EventType.METER_CHANGE:
                st.changes[obj.seq] = obj
                if obj.amends_seq is None:
                    # 只有原始换表进入身份沿革链；修订衍生的新版本通过 amendments 取代它
                    st.change_order.append(obj.seq)
            elif etype is EventType.GAP_DECLARED:
                st.gaps[obj.seq] = obj
            elif etype is EventType.FILE_IMPORT:
                st.imports.append(obj)
        return st

    def _check_writable(self) -> None:
        if self._locked:
            raise DossierFrozenError(
                "案卷已冻结为送审版本；请先 branch() 开出子分支再补录"
            )

    def _append(self, etype: EventType, obj: Any) -> None:
        self._events.append((etype, obj))
        self._state = self._replay()

    def _in_window(self, ts: datetime) -> None:
        if ts < self.case_start:
            raise LedgerError(f"读数时间 {ts.isoformat()} 早于案卷起点")
        if self.case_end and ts > self.case_end:
            raise LedgerError(f"读数时间 {ts.isoformat()} 晚于案卷终点")

    def _meter_allowed(self, meter_id: str, ts: datetime) -> None:
        """读数时间必须落在该表的服役区间内（换表时刻新旧表均可写端点）。"""
        active_meter = self._state.meter_at(ts)
        if meter_id == active_meter:
            return
        # 换表生效瞬间允许旧表写终值
        for ch in self._state.active_changes():
            if ts == ch.changed_ts and meter_id == ch.old_meter_id:
                return
        raise LedgerError(
            f"表 {meter_id} 在 {ts.isoformat()} 不属设备 {self.asset_id} 的在役表"
            f"（在役表为 {active_meter}）"
        )

    def _check_change_endpoint(self, ch: MeterChange) -> None:
        """换表时刻若已有点读数，必须与登记的端点一致，否则事实互相矛盾。"""
        for r in self._state.active_readings():
            if r.ts != ch.changed_ts:
                continue
            if r.meter_id == ch.old_meter_id and r.value != ch.old_final:
                raise LedgerError(
                    f"旧表终值 {ch.old_final} 与同时刻既有读数 {r.value} 不一致"
                )
            if r.meter_id == ch.new_meter_id and r.value != ch.new_initial:
                raise LedgerError(
                    f"新表初值 {ch.new_initial} 与同时刻既有读数 {r.value} 不一致"
                )

    # ---- 写入 API ------------------------------------------------------

    def import_file(
        self,
        digest: str,
        filename: str,
        actor: Actor,
        rows: Iterable[dict[str, Any]],
        meter_id: str,
        source: ReadingSource = ReadingSource.METER_EXPORT,
        imported_ts: str | datetime | None = None,
    ) -> ImportRecord:
        """导入一份表计导出/补录文件。

        每行形如 ``{"ts": ..., "value": "123.45", "note": "..."}``。
        人工补录必须逐行写明理由；同一文件摘要不可重复导入。
        """
        self._check_writable()
        fd = FileDigest.parse(digest)
        if any(imp.digest == fd for imp in self._state.imports):
            raise LedgerError(f"文件 {fd} 已导入过，禁止重复导入")

        rows = list(rows)
        if not rows:
            raise LedgerError("空文件不允许登记导入")
        ts_now = parse_ts(imported_ts) if imported_ts else datetime.now().astimezone()

        reading_seqs: list[int] = []
        # 先做全部校验，再统一落账，避免半份文件污染账本
        prepared: list[Reading] = []
        last_ts: datetime | None = None
        for row in rows:
            ts = parse_ts(row["ts"])
            value = to_decimal(row["value"])
            note = str(row.get("note", ""))
            row_source = row.get("source", source)
            if not isinstance(row_source, ReadingSource):
                row_source = ReadingSource(row_source)
            if row_source is ReadingSource.MANUAL_ENTRY and not note:
                raise LedgerError("人工补录必须注明理由（note）")
            self._in_window(ts)
            self._meter_allowed(meter_id, ts)
            if last_ts and ts < last_ts:
                raise LedgerError("导入读数必须按时间递增排列")
            last_ts = ts
            # 时间戳冲突：同一表同一时刻只能有一条事实，更正请走修订
            for existing in self._state.readings.values():
                if (
                    existing.meter_id == meter_id
                    and existing.ts == ts
                    and existing.seq not in self._state.superseded
                ):
                    raise LedgerError(
                        f"表 {meter_id} 在 {ts.isoformat()} 已有读数 "
                        f"#{existing.seq}；更正必须以修订方式提出"
                    )
            # 普通导入不得覆盖证据缺口：缺口内补录只能走 close_gap
            for g in self._state.open_gaps():
                if g.start_ts < ts < g.end_ts:
                    raise LedgerError(
                        f"{ts.isoformat()} 落在未闭合证据缺口 #{g.seq} 内，"
                        "请以缺口闭合修订补录，禁止直接导入"
                    )
            prepared.append(
                Reading(
                    seq=self._seq + len(prepared),
                    asset_id=self.asset_id,
                    meter_id=meter_id,
                    ts=ts,
                    value=value,
                    source=row_source,
                    import_digest=str(fd),
                    actor=actor,
                    note=note,
                )
            )

        for r in prepared:
            self._append(EventType.READING, r)
            reading_seqs.append(r.seq)
        record = ImportRecord(
            seq=self._seq,
            digest=fd,
            filename=filename,
            imported_ts=ts_now,
            actor=actor,
            reading_seqs=tuple(reading_seqs),
            meter_id=meter_id,
            source=source,
        )
        self._append(EventType.FILE_IMPORT, record)
        return record

    def declare_gap(
        self,
        start_ts: str | datetime,
        end_ts: str | datetime,
        reason: str,
        declared_by: Actor,
    ) -> Gap:
        """显式登记证据缺口。缺口区间留空，不做任何插值。"""
        self._check_writable()
        start, end = parse_ts(start_ts), parse_ts(end_ts)
        if end <= start:
            raise LedgerError("缺口结束时间必须晚于开始时间")
        self._in_window(start)
        self._in_window(end)
        if not reason:
            raise LedgerError("登记缺口必须说明原因")
        for g in self._state.gaps.values():
            if start < g.end_ts and g.start_ts < end:
                raise LedgerError(f"与既有缺口 #{g.seq} 区间重叠")
        # 已有读数落在缺口内部时，不能再把该区间宣称为缺口
        for r in self._state.active_readings():
            if start < r.ts < end:
                raise LedgerError(
                    f"区间内已存在读数 #{r.seq}（{r.ts.isoformat()}），不能宣称为缺口"
                )
        # 换表时刻是已登记的确切事实，不得落入"无事实"的缺口内部
        for ch in self._state.active_changes():
            if start < ch.changed_ts < end:
                raise LedgerError(
                    f"换表 #{ch.seq} 生效时刻 {ch.changed_ts.isoformat()} 落在缺口内部，"
                    "换表事实与证据缺口不能互相矛盾"
                )
        gap = Gap(
            seq=self._seq,
            asset_id=self.asset_id,
            start_ts=start,
            end_ts=end,
            reason=reason,
            declared_by=declared_by,
        )
        self._append(EventType.GAP_DECLARED, gap)
        return gap

    def record_meter_change(
        self,
        old_meter_id: str,
        old_final_kwh: str | Decimal,
        new_meter_id: str,
        new_initial_kwh: str | Decimal,
        changed_ts: str | datetime,
        actor: Actor,
        reason: str = "",
    ) -> MeterChange:
        """登记换表：旧表终值、新表初值、生效时刻三要素同时保存。"""
        self._check_writable()
        ts = parse_ts(changed_ts)
        old_final, new_initial = to_decimal(old_final_kwh), to_decimal(new_initial_kwh)
        if old_meter_id == new_meter_id:
            raise LedgerError("新旧表必须是不同的计量设备")
        if old_final < 0 or new_initial < 0:
            raise LedgerError("表计端点读数不能为负")
        self._in_window(ts)

        expected_old = self._state.meter_at(ts)
        if old_meter_id != expected_old:
            raise LedgerError(
                f"{ts.isoformat()} 的在役表是 {expected_old}，不能以 {old_meter_id} 作为旧表"
            )
        for ch in self._state.active_changes():
            if new_meter_id == ch.new_meter_id:
                raise LedgerError(f"新表 {new_meter_id} 已在更早的换表中使用过")
            if ch.changed_ts >= ts:
                raise LedgerError("换表时间顺序与既有换表记录冲突")
        for g in self._state.open_gaps():
            if g.start_ts < ts < g.end_ts:
                raise LedgerError(
                    f"换表时刻落在未闭合证据缺口 #{g.seq} 内部，事实互相矛盾"
                )
        change = MeterChange(
            seq=self._seq,
            asset_id=self.asset_id,
            old_meter_id=old_meter_id,
            old_final=old_final,
            new_meter_id=new_meter_id,
            new_initial=new_initial,
            changed_ts=ts,
            actor=actor,
            reason=reason,
        )
        self._check_change_endpoint(change)
        # 旧表在换表之后、新表在换表之前不得存在读数
        for r in self._state.active_readings():
            if r.meter_id == old_meter_id and r.ts > ts:
                raise LedgerError(f"旧表 {old_meter_id} 存在晚于换表时刻的读数 #{r.seq}")
            if r.meter_id == new_meter_id and r.ts < ts:
                raise LedgerError(f"新表 {new_meter_id} 存在早于换表时刻的读数 #{r.seq}")
        self._append(EventType.METER_CHANGE, change)
        return change

    def amend_meter_change(
        self,
        change_seq: int,
        actor: Actor,
        reason: str,
        old_final_kwh: str | Decimal | None = None,
        new_initial_kwh: str | Decimal | None = None,
        changed_ts: str | datetime | None = None,
        refs: Iterable[str] = (),
    ) -> MeterChange:
        """更正换表端点：以新修订出现，原换表记录保留可查。"""
        self._check_writable()
        original = self._state.changes.get(change_seq)
        if original is None:
            raise LedgerError(f"换表记录 #{change_seq} 不存在")
        if not reason:
            raise LedgerError("修订必须注明理由")
        new_change = MeterChange(
            seq=self._seq,
            asset_id=original.asset_id,
            old_meter_id=original.old_meter_id,
            old_final=to_decimal(old_final_kwh) if old_final_kwh is not None else original.old_final,
            new_meter_id=original.new_meter_id,
            new_initial=to_decimal(new_initial_kwh)
            if new_initial_kwh is not None
            else original.new_initial,
            changed_ts=parse_ts(changed_ts) if changed_ts else original.changed_ts,
            actor=actor,
            reason=reason,
            amends_seq=change_seq,
        )
        self._check_change_endpoint(new_change)
        # 修订后的时刻仍须满足换表时序，且新旧表读数边界不得越界
        for other in self._state.active_changes():
            if other.seq == change_seq:
                continue
            if other.changed_ts >= new_change.changed_ts:
                raise LedgerError(
                    f"修订后的换表时刻与换表 #{other.seq} 的时间顺序冲突"
                )
        for g in self._state.open_gaps():
            if g.start_ts < new_change.changed_ts < g.end_ts:
                raise LedgerError(
                    f"修订后的换表时刻落在未闭合证据缺口 #{g.seq} 内部，事实互相矛盾"
                )
        for r in self._state.active_readings():
            if r.meter_id == new_change.old_meter_id and r.ts > new_change.changed_ts:
                raise LedgerError(
                    f"旧表 {new_change.old_meter_id} 存在晚于修订后换表时刻的读数 #{r.seq}"
                )
            if r.meter_id == new_change.new_meter_id and r.ts < new_change.changed_ts:
                raise LedgerError(
                    f"新表 {new_change.new_meter_id} 存在早于修订后换表时刻的读数 #{r.seq}"
                )
        revision = Revision(
            seq=self._seq + 1,
            kind=RevisionKind.METER_CHANGE_AMEND,
            target_seq=change_seq,
            actor=actor,
            reason=reason,
            created_ts=new_change.changed_ts,
            new_reading_seq=new_change.seq,
            refs=tuple(refs),
        )
        self._append(EventType.METER_CHANGE, new_change)
        self._append(EventType.REVISION, revision)
        return new_change

    def correct_reading(
        self,
        target_seq: int,
        new_value: str | Decimal,
        actor: Actor,
        reason: str,
        note: str = "",
        refs: Iterable[str] = (),
    ) -> Reading:
        """更正一条读数：原值原样保留，新值以新读数承载并串起修订依据。"""
        self._check_writable()
        target = self._state.readings.get(target_seq)
        if target is None:
            raise LedgerError(f"读数 #{target_seq} 不存在")
        if target_seq in self._state.superseded:
            raise LedgerError(f"读数 #{target_seq} 已被修订，不能再次修订原值")
        if not reason:
            raise LedgerError("修订必须注明理由")
        value = to_decimal(new_value)
        new_reading = Reading(
            seq=self._seq,
            asset_id=target.asset_id,
            meter_id=target.meter_id,
            ts=target.ts,
            value=value,
            source=target.source,
            import_digest=target.import_digest,
            actor=actor,
            note=note or f"更正读数 #{target_seq}",
            revision_seq=self._seq + 1,
        )
        revision = Revision(
            seq=self._seq + 1,
            kind=RevisionKind.CORRECTION,
            target_seq=target_seq,
            actor=actor,
            reason=reason,
            created_ts=datetime.now().astimezone(),
            new_reading_seq=new_reading.seq,
            refs=tuple(refs),
        )
        self._append(EventType.READING, new_reading)
        self._append(EventType.REVISION, revision)
        return new_reading

    def close_gap(
        self,
        gap_seq: int,
        rows: Iterable[dict[str, Any]],
        actor: Actor,
        reason: str,
        refs: Iterable[str] = (),
    ) -> Revision:
        """以补录证据闭合缺口；补录读数挂在修订下，缺口本身保留为已闭合历史。"""
        self._check_writable()
        gap = self._state.gaps.get(gap_seq)
        if gap is None:
            raise LedgerError(f"缺口 #{gap_seq} 不存在")
        if gap_seq in self._state.closed_gaps:
            raise LedgerError(f"缺口 #{gap_seq} 已闭合")
        if not reason:
            raise LedgerError("缺口闭合必须注明理由")
        rows = list(rows)
        if not rows:
            raise LedgerError("缺口闭合至少需要一条补录读数")

        revision_seq = self._seq
        prepared: list[Reading] = []
        last_ts: datetime | None = None
        for i, row in enumerate(rows):
            ts = parse_ts(row["ts"])
            if not (gap.start_ts <= ts <= gap.end_ts):
                raise LedgerError(
                    f"补录时间 {ts.isoformat()} 超出缺口 #{gap_seq} 区间"
                )
            meter_id = row["meter_id"]
            self._meter_allowed(meter_id, ts)
            for existing in self._state.readings.values():
                if existing.meter_id == meter_id and existing.ts == ts:
                    raise LedgerError(
                        f"表 {meter_id} 在 {ts.isoformat()} 已有读数 #{existing.seq}"
                    )
            if last_ts and ts < last_ts:
                raise LedgerError("补录读数必须按时间递增排列")
            last_ts = ts
            note = str(row.get("note", ""))
            row_source = row.get("source", ReadingSource.MANUAL_ENTRY)
            if not isinstance(row_source, ReadingSource):
                row_source = ReadingSource(row_source)
            if row_source is ReadingSource.MANUAL_ENTRY and not note:
                raise LedgerError("人工补录必须逐行注明理由")
            prepared.append(
                Reading(
                    seq=revision_seq + 1 + i,
                    asset_id=self.asset_id,
                    meter_id=meter_id,
                    ts=ts,
                    value=to_decimal(row["value"]),
                    source=row_source,
                    import_digest="",  # 缺口补录挂修订依据，不伪造文件来源
                    actor=actor,
                    note=note,
                    revision_seq=revision_seq,
                )
            )

        revision = Revision(
            seq=revision_seq,
            kind=RevisionKind.GAP_CLOSURE,
            target_seq=gap_seq,
            actor=actor,
            reason=reason,
            created_ts=datetime.now().astimezone(),
            refs=tuple(refs),
        )
        # 先落修订（占序号），再落读数；缺口经重放被标记为已闭合
        self._append(EventType.REVISION, revision)
        for r in prepared:
            self._append(EventType.READING, r)
        return revision

    # ---- 冻结与分支 ----------------------------------------------------

    def freeze(self, label: str, actor: Actor) -> FrozenSnapshot:
        """把当前案卷冻结为送审版本；冻结后本对象只读。"""
        self._check_writable()
        if not label:
            raise LedgerError("送审版本必须有标签")
        if any(s.label == label for s in self._state.freezes):
            raise LedgerError(f"送审版本标签 {label!r} 已存在")
        payload = [
            {"seq": getattr(obj, "seq", i + 1), "type": etype.value, "data": obj}
            for i, (etype, obj) in enumerate(self._events)
            if etype is not EventType.FREEZE
        ]
        digest = "sha256:" + hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
        last_seq = max(
            (getattr(obj, "seq", 0) for etype, obj in self._events if etype is not EventType.FREEZE),
            default=0,
        )
        parent = self._state.freezes[-1].label if self._state.freezes else None
        snap = FrozenSnapshot(
            seq=self._seq,
            label=label,
            frozen_ts=datetime.now().astimezone(),
            parent_label=parent,
            last_event_seq=last_seq,
            digest=digest,
            actor=actor,
        )
        self._append(EventType.FREEZE, snap)
        self._locked = True
        return snap

    def branch(self) -> "Dossier":
        """基于已冻结案卷开出子分支：继承全部历史与版本，可继续补录。"""
        if not self._state.freezes:
            raise LedgerError("只有已冻结的送审案卷才能开出分支")
        child = Dossier.__new__(Dossier)
        child._events = list(self._events)
        child.asset_id = self.asset_id
        child.initial_meter_id = self.initial_meter_id
        child.case_start = self.case_start
        child.case_end = self.case_end
        child.contracts = self.contracts
        child._locked = False
        child._state = child._replay()
        return child

    # ---- 读取视图 ------------------------------------------------------

    def head(self) -> StateView:
        return self._state

    def state_at(self, label: str) -> StateView:
        snap = self._state.snapshot(label)
        return self._replay(cutoff=snap.last_event_seq)

    def labels(self) -> list[str]:
        return [s.label for s in self._state.freezes]

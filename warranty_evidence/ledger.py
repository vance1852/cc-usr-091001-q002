"""证据台账：append-only 事件日志，哈希链防篡改。

每条事件携带前一事件摘要，任何对历史事件的改动都会在 verify() 时暴露。
台账只增不改：更正读数、撤销结论都以新事件表达，原始事件永久保留。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Union

from .model import (
    MeterChange,
    ReadingRevision,
    canonical,
    digest_text,
    fmt_ts,
    parse_ts,
)

# 事件类型
EVT_IMPORT_FILE = "IMPORT_FILE"  # 文件导入登记（含摘要，阻止重复导入）
EVT_ADD_READING = "ADD_READING"  # 读数首版
EVT_CORRECT_READING = "CORRECT_READING"  # 读数更正（新修订，注明操作者与理由）
EVT_METER_CHANGE = "METER_CHANGE"  # 换表记录
EVT_FREEZE = "FREEZE"  # 案卷冻结（送审版本）

GENESIS_DIGEST = "sha256:" + "0" * 64


class LedgerError(Exception):
    """台账操作被拒绝（违反证据纪律）。"""


class DuplicateImportError(LedgerError):
    """同一文件摘要已导入过，拒绝重复导入。"""


class UnknownReadingError(LedgerError):
    """目标读数不存在。"""


class TamperError(LedgerError):
    """哈希链校验失败：台账可能被篡改。"""


@dataclass(frozen=True)
class Event:
    """台账事件。digest 覆盖 seq/类型/负载/前序摘要，形成哈希链。"""

    seq: int
    event_type: str
    operator: str
    recorded_at: datetime
    branch: str
    payload: dict
    prev_digest: str
    digest: str

    @property
    def event_id(self) -> str:
        return self.digest

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "operator": self.operator,
            "recorded_at": fmt_ts(self.recorded_at),
            "branch": self.branch,
            "payload": self.payload,
            "prev_digest": self.prev_digest,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            seq=int(data["seq"]),
            event_type=data["event_type"],
            operator=data["operator"],
            recorded_at=parse_ts(data["recorded_at"]),
            branch=data["branch"],
            payload=data["payload"],
            prev_digest=data["prev_digest"],
            digest=data["digest"],
        )


def _event_digest(seq: int, event_type: str, operator: str, recorded_at: datetime,
                  branch: str, payload: dict, prev_digest: str) -> str:
    body = {
        "seq": seq,
        "event_type": event_type,
        "operator": operator,
        "recorded_at": fmt_ts(recorded_at),
        "branch": branch,
        "payload": payload,
        "prev_digest": prev_digest,
    }
    return digest_text(canonical(body))


class EvidenceLedger:
    """只增不改的证据台账。"""

    def __init__(self) -> None:
        self._events: list[Event] = []

    # -- 写入 --

    def append(self, event_type: str, payload: dict, *, operator: str,
               at: datetime, branch: str) -> Event:
        if not operator:
            raise LedgerError("任何台账写入都必须注明操作者")
        if at.tzinfo is None:
            raise LedgerError("事件时间必须携带时区")
        seq = len(self._events) + 1
        prev_digest = self._events[-1].digest if self._events else GENESIS_DIGEST
        digest = _event_digest(seq, event_type, operator, at, branch, payload, prev_digest)
        event = Event(
            seq=seq,
            event_type=event_type,
            operator=operator,
            recorded_at=at,
            branch=branch,
            payload=payload,
            prev_digest=prev_digest,
            digest=digest,
        )
        self._events.append(event)
        return event

    # -- 读取 --

    @property
    def events(self) -> tuple:
        return tuple(self._events)

    @property
    def head_seq(self) -> int:
        return len(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def verify(self) -> bool:
        """重放哈希链；任何历史改动都会在此暴露。"""
        prev = GENESIS_DIGEST
        for expect_seq, event in enumerate(self._events, start=1):
            if event.seq != expect_seq:
                raise TamperError(f"事件序号断裂: 期望 {expect_seq}, 实际 {event.seq}")
            if event.prev_digest != prev:
                raise TamperError(f"事件 #{event.seq} 前序摘要不符")
            recomputed = _event_digest(
                event.seq, event.event_type, event.operator, event.recorded_at,
                event.branch, event.payload, event.prev_digest,
            )
            if recomputed != event.digest:
                raise TamperError(f"事件 #{event.seq} 摘要不符，台账可能被篡改")
            prev = event.digest
        return True

    def view(self, upto_seq: Optional[int] = None) -> "LedgerView":
        """重建某一事件序号之前（含）的台账状态，用于"截至某版本"的回放。"""
        cutoff = self.head_seq if upto_seq is None else upto_seq
        if cutoff < 0 or cutoff > self.head_seq:
            raise LedgerError(f"回放序号越界: {cutoff}")
        return LedgerView([e for e in self._events if e.seq <= cutoff])

    # -- 持久化 --

    def save(self, path: Union[str, Path]) -> None:
        lines = [canonical(e.to_dict()) for e in self._events]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @classmethod
    def load(cls, path: Union[str, Path]) -> "EvidenceLedger":
        import json

        ledger = cls()
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line:
                ledger._events.append(Event.from_dict(json.loads(line)))
        ledger.verify()
        return ledger


class LedgerView:
    """台账在某一截点的状态：当前有效修订、换表记录、导入登记、冲突。"""

    def __init__(self, events: list[Event]) -> None:
        self.events = events
        self.revisions: dict[str, list[ReadingRevision]] = {}
        self.meter_changes: list[MeterChange] = []
        self.imports: dict[str, dict] = {}  # 文件摘要 -> 导入登记
        self.freezes: list[dict] = []
        for event in events:
            self._apply(event)

    def _apply(self, event: Event) -> None:
        payload = event.payload
        if event.event_type == EVT_ADD_READING or event.event_type == EVT_CORRECT_READING:
            rev = ReadingRevision.from_payload(payload["revision"])
            self.revisions.setdefault(rev.reading_id, []).append(rev)
        elif event.event_type == EVT_METER_CHANGE:
            self.meter_changes.append(MeterChange.from_payload(payload["meter_change"]))
        elif event.event_type == EVT_IMPORT_FILE:
            self.imports[payload["digest"]] = payload
        elif event.event_type == EVT_FREEZE:
            self.freezes.append(payload)

    # -- 读数访问 --

    def current_revisions(self) -> list[ReadingRevision]:
        """每条读数的当前有效修订（同一截点下，被取代的旧版不在其列）。"""
        return [revs[-1] for _rid, revs in sorted(self.revisions.items())]

    def current_of(self, reading_id: str) -> Optional[ReadingRevision]:
        revs = self.revisions.get(reading_id)
        return revs[-1] if revs else None

    def revision_chain(self, reading_id: str) -> list[ReadingRevision]:
        """一条读数的完整修订沿革（首版在前）。"""
        return list(self.revisions.get(reading_id, []))

    def conflicts(self) -> list[dict]:
        """同表同时刻但取值矛盾的读数组（双方均保留，等待更正流程裁决）。"""
        by_key: dict[tuple, list[ReadingRevision]] = {}
        for rev in self.current_revisions():
            by_key.setdefault((rev.meter_id, rev.observed_at), []).append(rev)
        conflicts = []
        for (meter_id, observed_at), revs in sorted(by_key.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            values = {r.cumulative_kwh for r in revs}
            if len(values) > 1:
                conflicts.append({
                    "meter_id": meter_id,
                    "observed_at": observed_at,
                    "reading_ids": [r.reading_id for r in revs],
                    "values": sorted(values),
                })
        return conflicts

    @property
    def head_seq(self) -> int:
        return self.events[-1].seq if self.events else 0

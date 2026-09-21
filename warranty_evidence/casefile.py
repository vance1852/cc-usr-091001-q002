"""质保案卷：把台账、合同口径、身份沿革组织成一份可冻结、可分支的卷宗。

工作方式：
- 证据持续进入台账（导入、换表登记、更正），当前工作分支随之前进；
- freeze() 把当前状态封存为送审版本（V1、V2…），报告摘要写入哈希链；
- 冻结之后发生的补录自动进入新的案卷分支，已冻结版本逐字不变；
- report(version=…) 按冻结截点重放台账并校验摘要，任何事后改动都会报警。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from .diff import diff_reports
from .ledger import (
    EVT_ADD_READING,
    EVT_CORRECT_READING,
    EVT_FREEZE,
    EVT_IMPORT_FILE,
    EVT_METER_CHANGE,
    DuplicateImportError,
    EvidenceLedger,
    LedgerError,
    TamperError,
    UnknownReadingError,
)
from .metrics import BOUNDARY_STRICT
from .model import (
    IdentityMap,
    MeterChange,
    ReadingRevision,
    RuleSet,
    SourceKind,
    Window,
    as_decimal,
    digest_file,
    dump_json,
    fmt_ts,
    make_reading_id,
    parse_ts,
)
from .provenance import explain_reading
from .report import build_report, report_digest


@dataclass(frozen=True)
class CaseVersion:
    """一次冻结形成的送审版本。"""

    label: str  # V1、V2…
    frozen_at: datetime
    operator: str
    reason: str
    head_seq: int  # 冻结截点（含）之前的事件属于本版本
    branch: str  # 冻结时所在分支
    report_digest: str

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "frozen_at": fmt_ts(self.frozen_at),
            "operator": self.operator,
            "reason": self.reason,
            "head_seq": self.head_seq,
            "branch": self.branch,
            "report_digest": self.report_digest,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaseVersion":
        return cls(
            label=data["label"],
            frozen_at=parse_ts(data["frozen_at"]),
            operator=data["operator"],
            reason=data["reason"],
            head_seq=int(data["head_seq"]),
            branch=data["branch"],
            report_digest=data["report_digest"],
        )


@dataclass(frozen=True)
class ImportResult:
    digest: str
    file_name: str
    imported: int
    skipped_duplicates: int
    reading_ids: tuple


class CaseFile:
    """一份质保争议案卷。"""

    def __init__(
        self,
        case_id: str,
        asset_id: str,
        window: Window,
        rules: RuleSet,
        identity: IdentityMap,
        *,
        description: str = "",
        boundary_policy: str = BOUNDARY_STRICT,
        ledger: Optional[EvidenceLedger] = None,
    ) -> None:
        if identity.asset_id != asset_id:
            raise ValueError("身份映射与案卷资产不一致")
        self.case_id = case_id
        self.asset_id = asset_id
        self.window = window
        self.rules = rules
        self.identity = identity
        self.description = description
        self.boundary_policy = boundary_policy
        self.ledger = ledger or EvidenceLedger()
        self.versions: list[CaseVersion] = []
        self._frozen_reports: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 分支与版本
    # ------------------------------------------------------------------

    @property
    def current_branch(self) -> str:
        """当前工作分支：B1 为首版前的干流，每次冻结后进入下一分支。"""
        return f"B{len(self.versions) + 1}"

    def version_of(self, label: str) -> CaseVersion:
        for version in self.versions:
            if version.label == label:
                return version
        raise LedgerError(f"未知版本: {label}")

    # ------------------------------------------------------------------
    # 证据写入
    # ------------------------------------------------------------------

    def record_meter_change(self, change: MeterChange, *, operator: str,
                            at: datetime) -> MeterChange:
        if change.asset_id != self.asset_id:
            raise LedgerError("换表记录的资产与案卷不符")
        self.ledger.append(
            EVT_METER_CHANGE, {"meter_change": change.to_payload()},
            operator=operator, at=at, branch=self.current_branch,
        )
        return change

    def import_readings_file(self, path: Union[str, Path], *, operator: str,
                             at: datetime) -> ImportResult:
        """导入读数文件（JSONL）。同一文件摘要重复导入一律拒绝。"""
        path = Path(path)
        digest = digest_file(path)
        view = self.ledger.view()
        if digest in view.imports:
            first = view.imports[digest]
            raise DuplicateImportError(
                f"文件摘要 {digest} 已于 {first['imported_at']} 由 "
                f"{first['operator']} 导入（{first.get('file_name')}），拒绝重复导入"
            )

        records = _parse_readings_jsonl(path)
        # 先完成全部解析与校验，再落事件：导入要么整体成功，要么不留痕迹
        pending: list[ReadingRevision] = []
        skipped = 0
        known_ids = set(view.revisions.keys())
        for record in records:
            rid = make_reading_id(record["meter_id"], record["observed_at"],
                                  record["cumulative_kwh"], record["source"])
            if rid in known_ids or any(r.reading_id == rid for r in pending):
                skipped += 1  # 内容相同的读数幂等跳过，不产生新事件
                continue
            rev = ReadingRevision(
                reading_id=rid,
                revision_no=1,
                meter_id=record["meter_id"],
                source=record["source"],
                observed_at=record["observed_at"],
                cumulative_kwh=record["cumulative_kwh"],
                operator=record.get("operator") or operator,
                reason=record.get("reason"),
                recorded_at=at,
                import_digest=digest,
                note=record.get("note"),
            )
            self._validate_new_reading(rev)
            pending.append(rev)

        imported_ids = [r.reading_id for r in pending]
        self.ledger.append(
            EVT_IMPORT_FILE,
            {
                "digest": digest,
                "file_name": path.name,
                "imported_at": fmt_ts(at),
                "operator": operator,
                "imported_count": len(imported_ids),
                "skipped_duplicates": skipped,
                "reading_ids": list(imported_ids),
            },
            operator=operator, at=at, branch=self.current_branch,
        )
        for rev in pending:
            self.ledger.append(
                EVT_ADD_READING, {"revision": rev.to_payload()},
                operator=operator, at=at, branch=self.current_branch,
            )
        return ImportResult(
            digest=digest, file_name=path.name, imported=len(imported_ids),
            skipped_duplicates=skipped, reading_ids=tuple(imported_ids),
        )

    def _validate_new_reading(self, rev: ReadingRevision) -> None:
        if rev.source is SourceKind.METER and rev.meter_id is None:
            raise LedgerError("表计直读必须指明表计")
        if rev.meter_id is not None and self.identity.service_of(rev.meter_id) is None:
            raise LedgerError(f"表计 {rev.meter_id} 不在资产 {self.asset_id} 的身份沿革中")

    def correct_reading(self, reading_id: str, *, operator: str, reason: str,
                        at: datetime, cumulative_kwh=None, observed_at=None,
                        meter_id=None, note: Optional[str] = None) -> ReadingRevision:
        """更正读数：产生注明操作者与理由的新修订，原修订永久保留。"""
        if not reason:
            raise LedgerError("更正必须注明理由")
        view = self.ledger.view()
        current = view.current_of(reading_id)
        if current is None:
            raise UnknownReadingError(f"读数不存在: {reading_id}")
        new_rev = ReadingRevision(
            reading_id=reading_id,
            revision_no=current.revision_no + 1,
            meter_id=meter_id if meter_id is not None else current.meter_id,
            source=current.source,
            observed_at=parse_ts(observed_at) if isinstance(observed_at, str)
                        else (observed_at or current.observed_at),
            cumulative_kwh=as_decimal(cumulative_kwh) if cumulative_kwh is not None
                           else current.cumulative_kwh,
            operator=operator,
            reason=reason,
            recorded_at=at,
            import_digest=current.import_digest,
            note=note if note is not None else current.note,
            supersedes=current.revision_id,
        )
        self._validate_new_reading(new_rev)
        self.ledger.append(
            EVT_CORRECT_READING, {"revision": new_rev.to_payload()},
            operator=operator, at=at, branch=self.current_branch,
        )
        return new_rev

    def find_reading(self, meter_id: Optional[str], observed_at: Union[str, datetime]) -> Optional[str]:
        """按表计+时刻定位读数（审核页面下钻入口）。"""
        moment = parse_ts(observed_at) if isinstance(observed_at, str) else observed_at
        for rev in self.ledger.view().current_revisions():
            if rev.meter_id == meter_id and rev.observed_at == moment:
                return rev.reading_id
        return None

    # ------------------------------------------------------------------
    # 冻结与回放
    # ------------------------------------------------------------------

    def freeze(self, *, operator: str, reason: str, at: datetime) -> CaseVersion:
        """把当前状态封存为送审版本；此后的补录自动落在新的分支。"""
        if not reason:
            raise LedgerError("冻结必须注明理由")
        label = f"V{len(self.versions) + 1}"
        head_seq = self.ledger.head_seq
        report = self._build_report(head_seq=head_seq, version_label=label)
        digest = report_digest(report)
        version = CaseVersion(
            label=label,
            frozen_at=at,
            operator=operator,
            reason=reason,
            head_seq=head_seq,
            branch=self.current_branch,
            report_digest=digest,
        )
        self.ledger.append(
            EVT_FREEZE,
            {
                "label": label,
                "reason": reason,
                "head_seq": head_seq,
                "report_digest": digest,
                "parent_version": self.versions[-1].label if self.versions else None,
            },
            operator=operator, at=at, branch=self.current_branch,
        )
        self.versions.append(version)
        self._frozen_reports[label] = report
        return version

    def _build_report(self, *, head_seq: int, version_label: Optional[str]) -> dict:
        view = self.ledger.view(upto_seq=head_seq)
        return build_report(
            view,
            case_id=self.case_id,
            asset_id=self.asset_id,
            window=self.window,
            rules=self.rules,
            identity=self.identity,
            boundary_policy=self.boundary_policy,
            generated_from={
                "version": version_label or "WORKING",
                "head_seq": head_seq,
                "branch": self.current_branch if version_label is None else
                          next((v.branch for v in self.versions if v.label == version_label),
                               self.current_branch),
            },
            description=self.description,
        )

    def report(self, version: Optional[str] = None) -> dict:
        """生成报告。指定版本时按冻结截点重放，并与冻结摘要对账。"""
        if version is None:
            return self._build_report(head_seq=self.ledger.head_seq, version_label=None)
        ver = self.version_of(version)
        report = self._build_report(head_seq=ver.head_seq, version_label=ver.label)
        if report_digest(report) != ver.report_digest:
            raise TamperError(f"版本 {version} 重放摘要与冻结摘要不符，台账可能被篡改")
        return report

    def diff(self, old_label: str, new_label: str) -> dict:
        """两个送审版本之间的多算/少算清单。"""
        old_ver = self.version_of(old_label)
        new_ver = self.version_of(new_label)
        if old_ver.head_seq >= new_ver.head_seq:
            raise LedgerError("diff 要求旧版本早于新版本")
        old_report = self.report(old_label)
        new_report = self.report(new_label)
        events_between = [
            e for e in self.ledger.events
            if old_ver.head_seq < e.seq <= new_ver.head_seq
        ]
        return diff_reports(old_report, new_report, events_between,
                            old_label=old_label, new_label=new_label)

    def explain(self, reading_id: str, version: Optional[str] = None) -> dict:
        """一条读数的来源卡（可限定在某一版本的截点上）。"""
        head_seq = self.ledger.head_seq if version is None else self.version_of(version).head_seq
        view = self.ledger.view(upto_seq=head_seq)
        rev = view.current_of(reading_id)
        if rev is None:
            raise UnknownReadingError(f"读数不存在: {reading_id}")
        return explain_reading(rev, view, self.identity, self.rules, view.meter_changes)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self, directory: Union[str, Path]) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "versions").mkdir(exist_ok=True)
        meta = {
            "case_id": self.case_id,
            "asset_id": self.asset_id,
            "description": self.description,
            "window": self.window.to_dict(),
            "boundary_policy": self.boundary_policy,
            "rules": self.rules.to_dict(),
            "identity": self.identity.to_dict(),
            "versions": [v.to_dict() for v in self.versions],
        }
        (directory / "case.json").write_text(dump_json(meta) + "\n", encoding="utf-8")
        self.ledger.save(directory / "ledger.jsonl")
        for label, report in self._frozen_reports.items():
            (directory / "versions" / f"{label}.json").write_text(
                dump_json(report) + "\n", encoding="utf-8"
            )

    @classmethod
    def load(cls, directory: Union[str, Path]) -> "CaseFile":
        directory = Path(directory)
        meta = json.loads((directory / "case.json").read_text(encoding="utf-8"))
        case = cls(
            case_id=meta["case_id"],
            asset_id=meta["asset_id"],
            window=Window(start=parse_ts(meta["window"]["start"]),
                          end=parse_ts(meta["window"]["end"])),
            rules=RuleSet.from_dict(meta["rules"]),
            identity=IdentityMap.from_dict(meta["identity"]),
            description=meta.get("description", ""),
            boundary_policy=meta.get("boundary_policy", BOUNDARY_STRICT),
            ledger=EvidenceLedger.load(directory / "ledger.jsonl"),
        )
        case.versions = [CaseVersion.from_dict(v) for v in meta.get("versions", [])]
        versions_dir = directory / "versions"
        if versions_dir.is_dir():
            for path in sorted(versions_dir.glob("*.json")):
                case._frozen_reports[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        # 装戴即对账：每个冻结版本都必须能逐字重放
        for version in case.versions:
            case.report(version.label)
        return case


# ---------------------------------------------------------------------------
# 读数文件解析
# ---------------------------------------------------------------------------


def _parse_readings_jsonl(path: Path) -> list[dict]:
    """解析读数文件：每行一个 JSON 对象。

    字段：meter_id, source(meter/manual), observed_at, cumulative_kwh,
    人工补录另需 operator 与 reason，可选 note。
    """
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
            record = {
                "meter_id": raw.get("meter_id"),
                "source": SourceKind(raw["source"]),
                "observed_at": parse_ts(raw["observed_at"]),
                "cumulative_kwh": as_decimal(raw["cumulative_kwh"]),
                "operator": raw.get("operator"),
                "reason": raw.get("reason"),
                "note": raw.get("note"),
            }
        except (KeyError, ValueError) as exc:
            raise LedgerError(f"{path.name} 第 {lineno} 行解析失败: {exc}") from exc
        if record["source"] is SourceKind.MANUAL and not record.get("operator"):
            raise LedgerError(f"{path.name} 第 {lineno} 行：人工补录必须注明操作者")
        if record["source"] is SourceKind.MANUAL and not record.get("reason"):
            raise LedgerError(f"{path.name} 第 {lineno} 行：人工补录必须注明理由")
        records.append(record)
    return records

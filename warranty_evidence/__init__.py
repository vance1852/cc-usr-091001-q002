"""电池质保证据模块：计量读数的来源、计量、冻结与版本差异。

公开入口：
- CaseFile：案卷（导入、更正、冻结、报告、差异）
- build_reference_case：用 reference/ 资料完整走一遍参考流程
"""
from .casefile import CaseFile, CaseVersion, ImportResult
from .diff import diff_reports
from .ledger import (
    DuplicateImportError,
    EvidenceLedger,
    LedgerError,
    TamperError,
    UnknownReadingError,
)
from .metrics import BOUNDARY_APPORTION, BOUNDARY_STRICT, compute_metrics
from .model import (
    ContractVersion,
    IdentityMap,
    MeterChange,
    MeterService,
    ReadingRevision,
    RuleSet,
    SourceKind,
    Window,
)
from .provenance import explain_reading, identity_lineage
from .reference_case import build_reference_case
from .report import build_report, render_text, report_digest

__version__ = "0.1.0"

__all__ = [
    "BOUNDARY_APPORTION",
    "BOUNDARY_STRICT",
    "CaseFile",
    "CaseVersion",
    "ContractVersion",
    "DuplicateImportError",
    "EvidenceLedger",
    "IdentityMap",
    "ImportResult",
    "LedgerError",
    "MeterChange",
    "MeterService",
    "ReadingRevision",
    "RuleSet",
    "SourceKind",
    "TamperError",
    "UnknownReadingError",
    "Window",
    "build_reference_case",
    "build_report",
    "compute_metrics",
    "diff_reports",
    "explain_reading",
    "identity_lineage",
    "render_text",
    "report_digest",
]

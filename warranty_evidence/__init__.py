"""电池质保证据模块。

公开 API：

* :class:`Dossier` —— 只追加证据账本，负责读数、换表、缺口、修订、冻结与分支；
* :func:`compute` —— 按当时生效合同版本重算吞吐量与等效满充满放循环次数；
* :func:`build_report` / :func:`drill_down` / :func:`compare_versions` ——
  送审报告、逐笔下钻与两版多算/少算比对。
"""

from .ledger import Dossier, DossierFrozenError, LedgerError, StateView
from .model import (
    Actor,
    ContractVersion,
    FileDigest,
    Gap,
    MeterChange,
    MeterTenure,
    Reading,
    ReadingSource,
    Revision,
    RevisionKind,
)
from .computation import ComputationResult, compute
from .report import build_report, compare_versions, drill_down

__all__ = [
    "Dossier",
    "DossierFrozenError",
    "LedgerError",
    "StateView",
    "Actor",
    "ContractVersion",
    "FileDigest",
    "Gap",
    "MeterChange",
    "MeterTenure",
    "Reading",
    "ReadingSource",
    "Revision",
    "RevisionKind",
    "ComputationResult",
    "compute",
    "build_report",
    "compare_versions",
    "drill_down",
]

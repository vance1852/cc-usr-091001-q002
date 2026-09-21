"""双方认可的参考资料（reference/warranty_case.json）加载与校验。

参考文件是争议的资料口径锚点：本模块只做结构校验与领域映射，
不补齐文件里没有的事实。合同中未提供的计量条款（如额定能量）需由
调用方以独立的合同摘录补充，不允许在这里凭空捏造。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ledger import Dossier, LedgerError
from .model import Actor, ContractVersion, FileDigest, parse_ts, to_decimal

REFERENCE_ACTOR = Actor(user_id="reference", display_name="双方认可资料口径")


def load_case(path: str | Path) -> dict[str, Any]:
    """读取并校验争议案例文件，返回规范化后的字典。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        asset_id = raw["asset_id"]
        versions = raw["contract_versions"]
        change = raw["meter_change"]
    except KeyError as exc:
        raise LedgerError(f"参考资料缺少字段: {exc.args[0]}") from exc

    if not versions:
        raise LedgerError("参考资料至少包含一个合同版本")
    for v in versions:
        parse_ts(v["effective_from"])  # 必须是带时区的合法时间
        if "version" not in v:
            raise LedgerError("合同版本缺少 version 标识")
    if sorted(versions, key=lambda v: parse_ts(v["effective_from"])) != versions:
        raise LedgerError("合同版本必须按 effective_from 升序排列")

    FileDigest.parse(raw["source_digest"])  # 摘要格式必须为 算法:值
    ts = parse_ts(change["changed_at"])
    old_meter, new_meter = change["old_meter"], change["new_meter"]
    if old_meter["id"] == new_meter["id"]:
        raise LedgerError("换表记录的新旧表必须不同")
    old_final = to_decimal(old_meter["final_kwh"])
    new_initial = to_decimal(new_meter["initial_kwh"])
    if old_final < 0 or new_initial < 0:
        raise LedgerError("换表端点读数不能为负")

    # 换表时刻必须落在某个合同版本生效之后
    earliest = parse_ts(versions[0]["effective_from"])
    if ts < earliest:
        raise LedgerError("换表时刻早于首版合同生效时间")

    return raw


def build_dossier_from_case(
    raw: dict[str, Any],
    extra_terms: dict[str, dict[str, Any]] | None = None,
    case_end: str | None = None,
) -> Dossier:
    """把参考案例映射为空读数案卷，并登记其中的换表事实。

    ``extra_terms`` 用于补充参考文件未包含、但有独立合同摘录支撑的
    计量条款（例如 ``{"2026-B": {"rated_energy_kwh": "500"}}``）。
    """
    extra_terms = extra_terms or {}
    contracts = []
    for v in raw["contract_versions"]:
        terms = {k: val for k, val in v.items() if k not in ("version", "effective_from")}
        terms.update(extra_terms.get(v["version"], {}))
        contracts.append(
            ContractVersion(
                version=v["version"],
                effective_from=parse_ts(v["effective_from"]),
                terms=terms,
            )
        )

    change = raw["meter_change"]
    dossier = Dossier(
        asset_id=raw["asset_id"],
        initial_meter_id=change["old_meter"]["id"],
        case_start=contracts[0].effective_from,
        contracts=contracts,
        case_end=case_end,
    )
    dossier.record_meter_change(
        old_meter_id=change["old_meter"]["id"],
        old_final_kwh=change["old_meter"]["final_kwh"],
        new_meter_id=change["new_meter"]["id"],
        new_initial_kwh=change["new_meter"]["initial_kwh"],
        changed_ts=change["changed_at"],
        actor=REFERENCE_ACTOR,
        reason="参考资料登记的换表事实",
    )
    return dossier

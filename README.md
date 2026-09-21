# 电池质保证据归档

质保审核页面背后的证据模块。目标是回答一个问题：**设备换表造成累计量断层后，每一个吞吐量数字究竟以哪份事实为准。**

`reference/warranty_case.json` 是双方认可的争议资料口径（柜体 A17、两版合同、
M-88 → M-104 换表端点、来源摘要）。计量设备更换时，旧表终值与新表初值共同锚定
连续吞吐量；任何来源修订都只追加新记录，原始读数永不覆盖。

## 设计原则

| 争议点 | 模块口径 |
| --- | --- |
| 原表 / 新表 / 人工补录哪份有效 | 每条读数强制带来源（`meter_export` / `manual_entry`）、操作者、导入文件摘要；事实状态（有效 / 已被取代）可逐笔下钻 |
| 换表断层 | 换表记录必须**同时**保存旧表终值、新表初值、生效时刻；各表在自己的服役段内独立累计，新旧表读数绝不相减 |
| 缺测区间 | 显式登记为证据缺口，跨缺口增量整体隔离，**不插值**；补录只能以注明操作者与理由的缺口闭合修订进入 |
| 重复导入 | 文件摘要（如 `sha256:...`）在案卷谱系内唯一 |
| 任何更正 | 新修订 + 新读数，原值原样保留并标记 `superseded`；修订必须注明操作者、理由与佐证编号 |
| 送审版本 | 冻结即对全部事实事件做哈希快照；冻结后案卷只读，补录只能 `branch()` 进入子分支 |
| 供应商对账 | 汇总 → 合同桶 → 增量分量 → 两端计量记录 → 文件摘要 / 身份沿革 / 修订依据，逐层下钻；两版之间多算/少算逐笔列出 |

合同版本按 `effective_from` 选择：每个增量归入其发生时生效的版本；跨版本边界的增量
按生效时长**时间分摊**（分量标记 `time_apportioned`，分量之和严格等于原增量），合同
也可声明 `cross_version_basis: "exclude"` 要求跨边界增量整体不计。

缺口隔离与表计回退的增量只列示、不达标；可用性（`minimum_availability`）等需要运行
时段证据的指标不会仅凭累计量编造，报告中明确注明。

## 包结构

- `warranty_evidence/model.py` —— 领域值对象（读数、修订、换表、缺口、合同、冻结点）
- `warranty_evidence/ledger.py` —— 只追加证据账本：导入、修订、换表、缺口、冻结、分支
- `warranty_evidence/computation.py` —— 按生效合同版本重算吞吐量与等效满充满放循环次数
- `warranty_evidence/report.py` —— 送审报告、逐笔下钻、两版多算/少算比对
- `warranty_evidence/reference.py` —— 双方认可资料口径（`reference/warranty_case.json`）的校验与映射
- `examples/audit_walkthrough.py` —— 争议案例的完整审核走查

## 审核操作流

```python
from warranty_evidence import Dossier, Actor, compute, build_report, compare_versions

reviewer = Actor("u-01", "质保审核人")
dossier = Dossier("cabinet-A17", "M-88", "2025-01-01T00:00:00+08:00", contracts=[...])

# 1) 表计导出（摘要防重复导入）；人工补录必须逐行写明理由
dossier.import_file("sha256:...", "M88.csv", supplier, rows, meter_id="M-88")

# 2) 缺测区间显式留作证据缺口，不插值
gap = dossier.declare_gap(start, end, "通信模块故障，无表计导出", reviewer)

# 3) 换表：旧表终值、新表初值、生效时刻三者同时保存
dossier.record_meter_change("M-88", "18420.50", "M-104", "12.25",
                            "2026-06-15T10:30:00+08:00", supplier, reason="模块烧毁")

# 4) 冻结为送审版本（此后只读）
dossier.freeze("v1-submission", reviewer)

# 5) 供应商补交证据 -> 子分支，旧版本哈希不变
branch = dossier.branch()
branch.close_gap(gap.seq, manual_rows, supplier, "依据现场手抄表补录", refs=["STOCK-8"])
branch.correct_reading(seq, "3112.25", supplier, "时区偏差，以屏幕照片为准", refs=["P-22"])
branch.freeze("v2-submission", reviewer)

# 6) 报告、下钻、两版比对
report = build_report(branch, "v2-submission")
detail = drill_down(report, component_key)          # 汇总数字 -> 计量记录/修订依据
diff   = compare_versions(branch, "v1-submission", "v2-submission")
```

## 运行

```shell
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 examples/audit_walkthrough.py
```

代码运行环境为 Python 3.11 或更高版本，全部金额/千瓦时数值使用 `Decimal`，
所有时间戳必须带时区。

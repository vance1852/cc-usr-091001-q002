# 电池质保证据归档

这里维护质保案卷所依赖的合同口径与争议资料。计量设备更换时，旧表终值和新表初值共同确定连续的吞吐量；任何来源修订都应留下新的记录，原始读数不作覆盖。

`reference/warranty_case.json` 描述一台储能柜在合同有效期内的换表过程，读数单位为千瓦时。合同版本以生效时间选择，案卷冻结时间使用带时区的 ISO 8601 字符串。

代码运行环境为 Python 3.11 或更高版本。资料自检命令是 `python -m unittest discover -s tests -v`。

## 质保证据模块（warranty_evidence）

审核页面背后的完整证据模块，围绕一条**只增不改的哈希链台账**组织：

- **来源溯源**：每条读数都能讲清来历——原表直读 / 新表直读 / 人工补录（含操作者与理由），并挂接设备身份沿革与当时生效的合同版本（`provenance.py`）。
- **换表衔接**：换表记录同时保存旧表终值、新表初值与生效时刻，三者共同定义资产级累计量的拼接偏移；衔接点与边界读数互相校验，不一致即报警（`metrics.py`）。
- **缺口不插值**：窗口边缘未覆盖、计数回退、同刻矛盾读数都形成显式证据缺口；跨越合同版本边界的分段在严格策略下不归属任何期间（吞吐仍计入窗口合计），绝不按时间摊派——除非显式选择 `apportion` 策略，且分摊结果显式标注。
- **合同口径**：吞吐按分段归属到当时生效的合同版本，等效循环次数 = 期间吞吐 ÷ 该版本额定能量，逐版本分别计算（`metrics.py`）。
- **文件摘要去重**：导入文件按 SHA-256 登记，同一文件重复导入一律拒绝；同一读数经不同文件重复到达时幂等跳过（`casefile.py`）。
- **更正留痕**：任何更正产生注明操作者与理由的新修订，原修订永久保留，可回放任一历史截点（`ledger.py`）。
- **冻结与分支**：`freeze()` 把案卷封存为送审版本（V1、V2…），报告摘要写入哈希链；此后的补录自动进入新分支，已冻结版本逐字不变，重放时逐字对账（`casefile.py`）。
- **逐笔下钻**：报告从汇总数字 → 合同期间 → 计量分段 → 读数来源卡 → 修订链 / 导入摘要 / 身份沿革逐级可追（`report.py`）。
- **版本差异**：任意两个送审版本可对比出期间级与分段级的多算/少算清单，每条差异都指到成因事件（更正、补录、换表登记）（`diff.py`）。

### 资料口径（reference/）

| 文件 | 内容 |
| --- | --- |
| `warranty_case.json` | 争议案卷：合同版本、换表记录（旧表终值/新表初值/生效时刻）、来源摘要 |
| `warranty_rules.json` | 质保规则摘录：各合同版本的生效时刻、额定能量、最低可用率 |
| `device_identity.json` | 设备身份映射：资产与历任表计的服役区间，与换表记录互为印证 |
| `readings/2025-2026_telemetry.jsonl` | 供应商遥测导出（含一处年末抄表错位，留待更正流程演示） |
| `readings/2026-09_supplement.jsonl` | 冻结后的补录文件：调档投运读数 + 遥测中断期人工抄表 |

读数文件为 JSONL，每行一条：`meter_id`、`source`（`meter`/`manual`）、`observed_at`（带时区）、`cumulative_kwh`；人工补录另需 `operator` 与 `reason`，可选 `note`。

### 参考流程

`reference_case.build_reference_case()` 用上述资料完整走一遍争议故事线：立案登记换表 → 导入遥测 → 冻结送审版 V1 → 补录进入新分支 → 按工单更正年末错位读数 → 冻结复审版 V2 → 输出两版多算/少算清单。

```bash
python -m warranty_evidence.cli demo            # 完整演示（V1/V2 报告 + 差异清单）
python -m warranty_evidence.cli demo --dir case # 并把案卷落盘
python -m warranty_evidence.cli report case --version V1   # 重放送审版 V1（重算并对账摘要）
python -m warranty_evidence.cli diff case V1 V2            # 两版差异
python -m unittest discover -s tests -v                    # 全部自检
```

案卷落盘结构：`case.json`（案卷元数据与口径）、`ledger.jsonl`（哈希链事件日志）、`versions/V*.json`（冻结报告）。装载时自动重放全部冻结版本并校验摘要，任何事后改动都会以 `TamperError` 暴露。

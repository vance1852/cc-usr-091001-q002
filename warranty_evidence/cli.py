"""命令行：python -m warranty_evidence.cli <command>

- demo   用 reference/ 资料完整走一遍 立案→导入→冻结→补录→更正→复审→差异
- report 从已保存的案卷目录重放某一版本报告
- diff   从已保存的案卷目录对比两个版本
"""
from __future__ import annotations

import argparse
import sys

from .casefile import CaseFile
from .model import dump_json
from .reference_case import build_reference_case
from .report import render_text


def _cmd_demo(args) -> int:
    case = build_reference_case("V2")
    if args.dir:
        case.save(args.dir)
        print(f"案卷已保存到 {args.dir}")

    print("=" * 72)
    print("送审版 V1（首次冻结，仅遥测导出）")
    print("=" * 72)
    print(render_text(case.report("V1")))
    print()
    print("=" * 72)
    print("复审版 V2（补录 + 年末读数更正之后）")
    print("=" * 72)
    print(render_text(case.report("V2")))
    print()
    print("=" * 72)
    print("版本差异 V1 → V2（多算/少算清单）")
    print("=" * 72)
    diff = case.diff("V1", "V2")
    for line in diff["summary_lines"]:
        print(f"  {line}")
    if args.json:
        print()
        print(dump_json(diff))
    return 0


def _cmd_report(args) -> int:
    case = CaseFile.load(args.dir)
    report = case.report(args.version)
    if args.json:
        print(dump_json(report))
    else:
        print(render_text(report))
    return 0


def _cmd_diff(args) -> int:
    case = CaseFile.load(args.dir)
    diff = case.diff(args.old, args.new)
    if args.json:
        print(dump_json(diff))
    else:
        for line in diff["summary_lines"]:
            print(line)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="warranty_evidence", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="走一遍参考争议案卷")
    p_demo.add_argument("--dir", help="把案卷保存到该目录")
    p_demo.add_argument("--json", action="store_true", help="附带差异 JSON")
    p_demo.set_defaults(func=_cmd_demo)

    p_report = sub.add_parser("report", help="重放案卷报告")
    p_report.add_argument("dir", help="案卷目录")
    p_report.add_argument("--version", help="版本标签（如 V1）；缺省为当前工作稿")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=_cmd_report)

    p_diff = sub.add_parser("diff", help="对比两个送审版本")
    p_diff.add_argument("dir", help="案卷目录")
    p_diff.add_argument("old")
    p_diff.add_argument("new")
    p_diff.add_argument("--json", action="store_true")
    p_diff.set_defaults(func=_cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

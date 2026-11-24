"""命令行入口：python3 -m costwh <命令>。所有口径为合成内部管理约定。"""
from __future__ import annotations

import argparse
import sys

from . import db as db_mod
from . import ingest, queries, report
from .allocate import close_period


def _cmd_receive(con, args):
    result = ingest.receive(con, args.file, table=args.table, received_at=args.received_at)
    print(f"接收 #{result['receipt_seq']}：{result['status']}（{result['rows']} 行）{result['message']}")
    return 0 if result["status"] in ("posted", "duplicate") else 1


def _cmd_receipts(con, _args):
    rows = queries._rows(con, "SELECT * FROM receipt ORDER BY receipt_seq")
    for r in rows:
        print(f"#{r['receipt_seq']} [{r['status']}] {r['source_table']} "
              f"{r['rows_staged']} 行 {r['received_at']} {r['source_label']}"
              + (f" — {r['message']}" if r["message"] else ""))
    return 0


def _cmd_close(con, args):
    round_id = close_period(con, args.period, note=args.note or "")
    rows = queries._rows(con, "SELECT * FROM alloc_round WHERE round_id=?", [round_id])
    print(f"{args.period} 结转：{report.round_title(rows[0])}")
    blocking = queries._rows(
        con, "SELECT COUNT(*) AS n FROM round_exception WHERE round_id=? AND severity='fatal'",
        [round_id])[0]["n"]
    open_n = queries._rows(
        con, "SELECT COUNT(*) AS n FROM round_exception WHERE round_id=? AND severity='open'",
        [round_id])[0]["n"]
    if blocking:
        print(f"存在 {blocking} 项阻断性例外，本轮不可闭合；请用 exceptions 查看。")
        return 1
    if open_n:
        print(f"存在 {open_n} 项未核实服务量，本轮为暂结，不能当作完整期间结转确认。")
    return 0


def _cmd_rounds(con, args):
    for r in queries.rounds(con, args.period):
        print(report.round_title(r) + (f" {r['note']}" if r["note"] else ""))
    return 0


def _cmd_total(con, args):
    print(report.render_total(queries.hospital_total(con, args.period, args.round)))
    return 0


def _cmd_report(con, args):
    print(report.render_center(queries.center_report(con, args.period, args.center, args.round)))
    return 0


def _cmd_trace(con, args):
    print(report.render_trace(queries.trace_cost(con, args.cost)))
    return 0


def _cmd_exceptions(con, args):
    print(report.render_exceptions(queries.exceptions(con, args.period, args.round)))
    return 0


def _cmd_transfers(con, args):
    print(report.render_transfers(queries.transfers_view(con, args.period, args.round)))
    return 0


def _cmd_diff(con, args):
    print(report.render_diff(queries.diff_rounds(con, args.period, args.from_id, args.to_id)))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="costwh", description="医院互供服务的科室责任成本仓（合成内部管理约定）")
    parser.add_argument("--db", default="warehouse.duckdb", help="仓库文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("receive", help="分次装载：接收一个资料文件")
    p.add_argument("file")
    p.add_argument("--table", help="资料表名（默认按文件名识别）")
    p.add_argument("--received-at", required=True, help="接收时刻 ISO，须带时区")
    p.set_defaults(func=_cmd_receive)

    sub.add_parser("receipts", help="接收历史").set_defaults(func=_cmd_receipts)

    p = sub.add_parser("close", help="期间费用形成与结转确认（新一轮）")
    p.add_argument("--period", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=_cmd_close)

    p = sub.add_parser("rounds", help="轮次列表")
    p.add_argument("--period", required=True)
    p.set_defaults(func=_cmd_rounds)

    p = sub.add_parser("total", help="全院总额核对")
    p.add_argument("--period", required=True)
    p.add_argument("--round", type=int)
    p.set_defaults(func=_cmd_total)

    p = sub.add_parser("report", help="按中心下钻（临床/保障/闲置）")
    p.add_argument("--period", required=True)
    p.add_argument("--center", required=True)
    p.add_argument("--round", type=int)
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("trace", help="按原始支出查最终去向")
    p.add_argument("--cost", required=True)
    p.set_defaults(func=_cmd_trace)

    p = sub.add_parser("exceptions", help="未达回执/待核费用/不可吸收等例外")
    p.add_argument("--period", required=True)
    p.add_argument("--round", type=int)
    p.set_defaults(func=_cmd_exceptions)

    p = sub.add_parser("transfers", help="内部服务双方核实一览")
    p.add_argument("--period", required=True)
    p.add_argument("--round", type=int)
    p.set_defaults(func=_cmd_transfers)

    p = sub.add_parser("diff", help="新旧轮次差异与受牵连范围")
    p.add_argument("--period", required=True)
    p.add_argument("--from-id", type=int)
    p.add_argument("--to-id", type=int)
    p.set_defaults(func=_cmd_diff)

    p = sub.add_parser("demo", help="用合成资料跑通全流程")
    p.set_defaults(func=lambda con, args: _cmd_demo(con, args))
    return parser


def _cmd_demo(con, _args):
    from .demo import run
    run(con)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    con = db_mod.connect(args.db)
    try:
        return args.func(con, args)
    except ValueError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())

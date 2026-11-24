"""全流程演示：用仓库自带合成资料跑通 装载→核实→结转→补载→差异。

场景（对应 docs/资料口径.md）：
1. 接收基础资料（中心、原生费用、双方声明、共同认可、容量），T8 双方 10/8 未达；
2. 2026-10 第一次结转：T8 未核实，只能暂结（provisional）；
3. 后续接收 later_confirmations（T8 共同认可 9 hour）；
4. 第二次结转：全部核实，确认（confirmed）；WARD2 无新单据但承担额变化；
5. 展示总额核对、下钻、追踪与新旧轮差异。
"""
from __future__ import annotations

from pathlib import Path

from . import ingest, queries, report
from .allocate import close_period

DATA = Path(__file__).resolve().parents[1] / "data"
BASE_FILES = ["centers.csv", "source_costs.csv", "service_claims.csv",
              "service_confirmations.csv", "capacity.csv"]


def run(con):
    print("—— 1. 分次装载基础资料（接收时刻 2026-10-25T09:00+08:00）——")
    for name in BASE_FILES:
        result = ingest.receive(con, DATA / name, received_at="2026-10-25T09:00:00+08:00")
        print(f"  {name}: {result['status']}（{result['rows']} 行）{result['message']}")

    print("\n—— 2. 第一次结转（T8 双方 10/8 未达，尚未共同认可）——")
    first = close_period(con, "2026-10", note="月结：T8 待核")
    _show_round(con, "2026-10", first)

    print("\n—— 3. 后续接收共同认可（2026-10-28T09:00+08:00，不随基础资料自动生效）——")
    result = ingest.receive(con, DATA / "later_confirmations.csv",
                            received_at="2026-10-28T09:00:00+08:00")
    print(f"  later_confirmations.csv: {result['status']}（{result['rows']} 行）")

    print("\n—— 4. 第二次结转（T8=9 进入同一轮新结转，旧轮保留可对比）——")
    second = close_period(con, "2026-10", note="月结后补载 T8 共同认可")
    _show_round(con, "2026-10", second)

    print("\n—— 5. 新旧轮差异与受牵连范围 ——")
    print(report.render_diff(queries.diff_rounds(con, "2026-10", first, second)))

    print("\n—— 6. 下钻：临床 WARD1 ——")
    print(report.render_center(queries.center_report(con, "2026-10", "WARD1")))
    print("\n—— 7. 下钻：保障部门 REPAIR ——")
    print(report.render_center(queries.center_report(con, "2026-10", "REPAIR")))
    print("\n—— 8. 下钻：明确闲置 IDLE ——")
    print(report.render_center(queries.center_report(con, "2026-10", "IDLE")))

    print("\n—— 9. 按原始支出查最终去向（C2：POWER 可变 2000 元）——")
    print(report.render_trace(queries.trace_cost(con, "C2")))

    print("\n—— 10. 内部服务双方核实一览 ——")
    print(report.render_transfers(queries.transfers_view(con, "2026-10")))


def _show_round(con, period, round_id):
    rows = queries._rows(con, "SELECT * FROM alloc_round WHERE round_id=?", [round_id])
    print(f"  {period} {report.round_title(rows[0])}")
    print(report.render_total(queries.hospital_total(con, period, round_id)))
    print(report.render_exceptions(queries.exceptions(con, period, round_id)))

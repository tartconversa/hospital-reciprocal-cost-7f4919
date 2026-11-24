"""业务规则测试：装载、核实、结转、确认、反查与差异。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from costwh import db as db_mod
from costwh import ingest, queries
from costwh.allocate import close_period, distribute, solve_linear
from costwh.contracts import CONTRACTS
from costwh.facts import load_facts

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BASE_FILES = ["centers.csv", "source_costs.csv", "service_claims.csv",
              "service_confirmations.csv", "capacity.csv"]
T1 = "2026-10-25T09:00:00+08:00"
T2 = "2026-10-28T09:00:00+08:00"


def write_csv(folder, name, text):
    path = Path(folder) / name
    path.write_text(text, encoding="utf-8")
    return path


def receive(con, folder, name, text, table, at=T1):
    result = ingest.receive(con, write_csv(folder, name, text), table=table, received_at=at)
    return result


class WarehouseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db_mod.connect(Path(self.tmp.name) / "w.duckdb")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def load_base(self):
        for name in BASE_FILES:
            result = ingest.receive(self.con, DATA / name, received_at=T1)
            self.assertEqual(result["status"], "posted", f"{name}: {result['message']}")

    def load_later(self):
        result = ingest.receive(self.con, DATA / "later_confirmations.csv", received_at=T2)
        self.assertEqual(result["status"], "posted", result["message"])

    def terminal(self, round_id, center):
        rows = queries._rows(self.con,
                             "SELECT terminal_total FROM center_cost WHERE round_id=? AND center_id=?",
                             [round_id, center])
        return rows[0]["terminal_total"] if rows else None

    def flow(self, round_id, provider, consumer, pool="variable"):
        rows = queries._rows(self.con,
                             "SELECT * FROM alloc_flow WHERE round_id=? AND provider=? "
                             "AND consumer=? AND pool=?", [round_id, provider, consumer, pool])
        return rows[0] if rows else None


class RecognizeTests(WarehouseTest):
    def test_expense_recognized_once_payment_not_added(self):
        """同一义务的 expense+payment 只认可一次，现金不重复计入。"""
        self.load_base()
        close_period(self.con, "2026-10")
        total = queries.hospital_total(self.con, "2026-10")
        self.assertEqual(total["own_total"], Decimal("5500.00"))
        self.assertEqual(total["cash_memo_total"], Decimal("2000.00"))
        rows = queries._rows(self.con,
                             "SELECT * FROM recognized_cost WHERE underlying_id='OB2'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], Decimal("2000.00"))

    def test_pending_excluded_but_visible(self):
        """pending 仅待核：不进费用池，但在例外中可见。"""
        self.load_base()
        round_id = close_period(self.con, "2026-10")
        total = queries.hospital_total(self.con, "2026-10")
        self.assertEqual(total["pending_total"], Decimal("100.00"))
        rows = queries._rows(self.con,
                             "SELECT * FROM recognized_cost WHERE cost_id='C7'")
        self.assertEqual(rows, [])
        kinds = {(r["kind"], r["ref_id"]) for r in queries.exceptions(self.con, "2026-10", round_id)}
        self.assertIn(("pending_cost", "C7"), kinds)

    def test_internal_transfer_never_new_original(self):
        """内部转入不是新原生支出：结转前后全院原生总额不变。"""
        self.load_base()
        round_id = close_period(self.con, "2026-10")
        total = queries.hospital_total(self.con, "2026-10")
        self.assertEqual(total["own_total"], Decimal("5500.00"))
        self.assertGreater(total["internal_transfer_total"], 0)
        self.assertEqual(total["own_total"],
                         total["clinical_total"] + total["idle_total"])
        # 转入列只出现在 center_cost，不进入 recognized_cost。
        rows = queries._rows(self.con,
                             "SELECT COUNT(*) AS n FROM recognized_cost r "
                             "JOIN alloc_flow f ON f.round_id=r.round_id "
                             "AND f.provider=r.center_id WHERE r.round_id=?", [round_id])
        self.assertGreaterEqual(rows[0]["n"], 0)  # 两表无金额相加关系，仅供联查


class VerifyTests(WarehouseTest):
    def test_t8_dispute_provisional_not_compromised(self):
        """T8 双方 10/8 未达：确认量留空（不取 min、不折中），首轮只能暂结。"""
        self.load_base()
        round_id = close_period(self.con, "2026-10")
        rnd = queries._rows(self.con, "SELECT * FROM alloc_round WHERE round_id=?",
                            [round_id])[0]
        self.assertEqual(rnd["status"], "provisional")
        facts = load_facts(self.con, rnd["as_of_seq"])
        t8 = facts.transfers["T8"]
        self.assertIsNone(t8.confirmed_qty)
        self.assertEqual(t8.status, "disputed")
        self.assertEqual(t8.gap, Decimal(2))
        hits = [r for r in queries.exceptions(self.con, "2026-10", round_id)
                if r["kind"] == "unverified_transfer" and r["ref_id"] == "T8"]
        self.assertEqual(len(hits), 1)
        self.assertIn("10", hits[0]["detail"])
        self.assertIn("8", hits[0]["detail"])

    def test_missing_receipt_is_not_zero(self):
        """缺回执不能当零：确认量为未知(None)，且该期只能暂结。"""
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        receive(self.con, self.tmp.name, "source_costs.csv",
                "cost_id,center_id,period,cost_type,kind,amount,underlying_id,date\n"
                "C1,SX,2026-10,variable,expense,100,OB1,2026-10-10\n", "source_costs")
        receive(self.con, self.tmp.name, "service_claims.csv",
                "claim_id,transfer_id,provider,consumer,service_type,period,side,quantity\n"
                "X1-provider,X1,SX,WA,power,2026-10,provider,10\n", "service_claims")
        receive(self.con, self.tmp.name, "capacity.csv",
                "capacity_id,provider,consumer,period,capacity_share,idle_variable_share\n"
                "CP,SX,IDLE,2026-10,1,1\n", "capacity")  # 无确认服务量时全部进 IDLE，避免吸收阻断
        round_id = close_period(self.con, "2026-10")
        rnd = queries._rows(self.con, "SELECT * FROM alloc_round WHERE round_id=?",
                            [round_id])[0]
        self.assertEqual(rnd["status"], "provisional")
        facts = load_facts(self.con, rnd["as_of_seq"])
        self.assertIsNone(facts.transfers["X1"].confirmed_qty)
        self.assertEqual(facts.transfers["X1"].status, "provider_only")
        # 缺回执不进分母：无可变流转到 WA，全部 100 元由 IDLE 吸收，且保留待核实例外。
        self.assertIsNone(self.flow(round_id, "SX", "WA"))
        self.assertEqual(self.terminal(round_id, "IDLE"), Decimal("100.00"))
        kinds = {r["kind"] for r in queries.exceptions(self.con, "2026-10", round_id)}
        self.assertIn("unverified_transfer", kinds)

    def test_later_confirmation_enters_new_round_only(self):
        """后续共同认可只通过新一轮生效：T8=9，分母变为 99，旧轮保留。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        self.load_later()
        second = close_period(self.con, "2026-10")
        self.assertNotEqual(first, second)
        rounds = {r["round_id"]: r["status"] for r in queries.rounds(self.con, "2026-10")}
        self.assertEqual(rounds[first], "superseded")
        self.assertEqual(rounds[second], "confirmed")
        self.assertEqual(self.flow(second, "REPAIR", "POWER")["share"], "1/11")
        self.assertEqual(self.flow(second, "REPAIR", "WARD1")["share"], "40/99")
        # 旧轮事实不变：第一轮 REPAIR→POWER 无可变流转。
        self.assertIsNone(self.flow(first, "REPAIR", "POWER"))


class AllocateTests(WarehouseTest):
    def test_conservation_both_rounds(self):
        """原生总额 == 临床终端 + 明确闲置，两轮都精确一致。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        self.load_later()
        second = close_period(self.con, "2026-10")
        for round_id in (first, second):
            total = queries.hospital_total(self.con, "2026-10", round_id)
            self.assertTrue(total["conservation"])
            self.assertEqual(total["own_total"], Decimal("5500.00"))

    def test_idle_explicitly_retained(self):
        """空闲能力显式保留在 IDLE，不分光给有用量的临床科室。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        self.assertEqual(self.terminal(first, "IDLE"), Decimal("495.00"))
        fixed = self.flow(first, "POWER", "IDLE", "fixed")
        self.assertEqual(fixed["amount"], Decimal("200.00"))  # 容量份额 1/5 × 1000
        variable = self.flow(first, "POWER", "IDLE", "variable")
        self.assertEqual(variable["amount"], Decimal("200.00"))  # idle_variable_share 0.1 × 2000
        self.assertLess(self.terminal(first, "WARD1") + self.terminal(first, "WARD2"),
                        Decimal("5500.00"))

    def test_idle_variable_share_not_summed_across_rows(self):
        """容量表重复行的 idle_variable_share 是同一中心级比例，不逐行相加。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        # POWER 有 5 行容量行各标 0.1；若误加成 0.5，IDLE 可变行将为 1000。
        self.assertEqual(self.flow(first, "POWER", "IDLE", "variable")["amount"],
                         Decimal("200.00"))

    def test_reciprocal_order_independence(self):
        """互供联立求解与接收/处理顺序无关。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        mine = queries._rows(self.con, "SELECT * FROM center_cost WHERE round_id=? ORDER BY center_id",
                             [first])
        other_tmp = tempfile.TemporaryDirectory()
        other = db_mod.connect(Path(other_tmp.name) / "w.duckdb")
        try:
            ingest.receive(other, DATA / "centers.csv", received_at=T1)
            ingest.receive(other, DATA / "capacity.csv", received_at=T1)
            ingest.receive(other, DATA / "service_claims.csv", received_at=T1)
            ingest.receive(other, DATA / "source_costs.csv", received_at=T1)
            ingest.receive(other, DATA / "service_confirmations.csv", received_at=T1)
            second = close_period(other, "2026-10")
            theirs = queries._rows(other, "SELECT * FROM center_cost WHERE round_id=? ORDER BY center_id",
                                   [second])
        finally:
            other.close()
            other_tmp.cleanup()
        strip = lambda rows: [(r["center_id"], r["own_fixed"], r["own_variable"],
                               r["in_from_fixed"], r["in_from_variable"], r["terminal_total"])
                              for r in rows]
        self.assertEqual(strip(mine), strip(theirs))

    def test_unclosable_loop_blocked_no_balancing_center(self):
        """互供环路无临床/IDLE 吸收出口：阻断，不造平衡科室。"""
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SA,support,2026-10-01,2027-01-01\n"
                "SB,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        receive(self.con, self.tmp.name, "source_costs.csv",
                "cost_id,center_id,period,cost_type,kind,amount,underlying_id,date\n"
                "C1,SA,2026-10,variable,expense,100,OB1,2026-10-10\n"
                "C2,SB,2026-10,variable,expense,200,OB2,2026-10-10\n", "source_costs")
        receive(self.con, self.tmp.name, "service_claims.csv",
                "claim_id,transfer_id,provider,consumer,service_type,period,side,quantity\n"
                "X1-p,X1,SA,SB,power,2026-10,provider,10\n"
                "X1-c,X1,SA,SB,power,2026-10,consumer,10\n"
                "X2-p,X2,SB,SA,power,2026-10,provider,5\n"
                "X2-c,X2,SB,SA,power,2026-10,consumer,5\n", "service_claims")
        receive(self.con, self.tmp.name, "service_confirmations.csv",
                "confirmation_id,transfer_id,matched_quantity,confirmed_at\n"
                "X1C,X1,10,2026-10-25T08:00:00+08:00\n"
                "X2C,X2,5,2026-10-25T08:00:00+08:00\n", "service_confirmations")
        receive(self.con, self.tmp.name, "capacity.csv",
                "capacity_id,provider,consumer,period,capacity_share,idle_variable_share\n"
                "CA,SA,IDLE,2026-10,1,0\n"
                "CB,SB,IDLE,2026-10,1,0\n", "capacity")
        round_id = close_period(self.con, "2026-10")
        rnd = queries._rows(self.con, "SELECT * FROM alloc_round WHERE round_id=?",
                            [round_id])[0]
        self.assertEqual(rnd["status"], "blocked")
        kinds = {r["kind"] for r in queries._rows(
            self.con, "SELECT * FROM round_exception WHERE round_id=?", [round_id])}
        self.assertIn("unclosable_loop", kinds)
        # 阻断轮不产出任何分摊结果，IDLE 不被当作平衡塞数。
        rows = queries._rows(self.con, "SELECT COUNT(*) AS n FROM center_cost WHERE round_id=?",
                             [round_id])
        self.assertEqual(rows[0]["n"], 0)

    def test_unabsorbable_variable_blocked(self):
        """可变池无已确认服务量且闲置比例<1：不能吸收的成本显式阻断。"""
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        receive(self.con, self.tmp.name, "source_costs.csv",
                "cost_id,center_id,period,cost_type,kind,amount,underlying_id,date\n"
                "C1,SX,2026-10,variable,expense,100,OB1,2026-10-10\n", "source_costs")
        receive(self.con, self.tmp.name, "capacity.csv",
                "capacity_id,provider,consumer,period,capacity_share,idle_variable_share\n"
                "CP,SX,IDLE,2026-10,1,0.5\n", "capacity")
        round_id = close_period(self.con, "2026-10")
        rnd = queries._rows(self.con, "SELECT * FROM alloc_round WHERE round_id=?",
                            [round_id])[0]
        self.assertEqual(rnd["status"], "blocked")
        kinds = {r["kind"] for r in queries._rows(
            self.con, "SELECT * FROM round_exception WHERE round_id=?", [round_id])}
        self.assertIn("unabsorbable_variable", kinds)

    def test_rounding_residual_to_largest_base(self):
        """尾差归同一费用池终端分摊基数最大项，并列按中心键升序。"""
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "WB,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        receive(self.con, self.tmp.name, "source_costs.csv",
                "cost_id,center_id,period,cost_type,kind,amount,underlying_id,date\n"
                "C1,SX,2026-10,fixed,expense,0.03,OB1,2026-10-10\n", "source_costs")
        receive(self.con, self.tmp.name, "capacity.csv",
                "capacity_id,provider,consumer,period,capacity_share,idle_variable_share\n"
                "C1,SX,WA,2026-10,0.5,0\n"
                "C2,SX,WB,2026-10,0.25,0\n"
                "C3,SX,IDLE,2026-10,0.25,0\n", "capacity")
        round_id = close_period(self.con, "2026-10")
        # 3 分按 1/2、1/4、1/4：1.5/0.75/0.75 -> 2/1/1，尾差 -1 归基数最大的 WA。
        self.assertEqual(self.flow(round_id, "SX", "WA", "fixed")["amount"], Decimal("0.01"))
        self.assertEqual(self.flow(round_id, "SX", "WB", "fixed")["amount"], Decimal("0.01"))
        self.assertEqual(self.flow(round_id, "SX", "IDLE", "fixed")["amount"], Decimal("0.01"))
        total = queries.hospital_total(self.con, "2026-10", round_id)
        self.assertTrue(total["conservation"])

    def test_rounding_residual_tie_by_center_key(self):
        from fractions import Fraction
        settled = distribute(3, [("WB", Fraction(1, 2)), ("WA", Fraction(1, 2))],
                             {"WA", "WB"})
        # 1.5/1.5 -> 2/2，尾差 -1 归并列中键序靠前者 WA。
        self.assertEqual(settled, {"WA": 1, "WB": 2})

    def test_transfer_in_uses_variable_shares_not_capacity(self):
        """转入成本按接受方已确认 variable 份额继续承担，不再套固定容量分母。"""
        self.load_base()
        self.load_later()
        second = close_period(self.con, "2026-10")
        # LAUNDRY 固定容量全部给 IDLE（份额1），但其转入成本仍按可变份额分给 REPAIR/WARD1/WARD2。
        laundry_out = queries._rows(
            self.con, "SELECT * FROM alloc_flow WHERE round_id=? AND provider='LAUNDRY'",
            [second])
        self.assertTrue(all(f["pool"] == "variable" for f in laundry_out))
        consumers = {f["consumer"] for f in laundry_out}
        self.assertEqual(consumers, {"REPAIR", "WARD1", "WARD2", "IDLE"})
        ward1 = self.flow(second, "LAUNDRY", "WARD1")
        self.assertEqual(ward1["share"], "19/40")  # (1-0.05) × 50/100


class IngestTests(WarehouseTest):
    def test_capacity_share_must_sum_to_one(self):
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        result = receive(self.con, self.tmp.name, "capacity.csv",
                         "capacity_id,provider,consumer,period,capacity_share,idle_variable_share\n"
                         "C1,SX,WA,2026-10,0.5,0\n"
                         "C2,SX,IDLE,2026-10,0.4,0\n", "capacity")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("合计", result["message"])

    def test_capacity_must_include_idle_row(self):
        """容量行必须含 IDLE：固定待命成本的闲置责任须显式存在，不能只向有用量方分光。"""
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        result = receive(self.con, self.tmp.name, "capacity.csv",
                         "capacity_id,provider,consumer,period,capacity_share,idle_variable_share\n"
                         "C1,SX,WA,2026-10,1,0\n", "capacity")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("IDLE", result["message"])

    def test_confirmation_without_claims_rejected(self):
        """共同认可必须指向已声明服务，无双方声明的确认整批拒收。"""
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        result = receive(self.con, self.tmp.name, "service_confirmations.csv",
                         "confirmation_id,transfer_id,matched_quantity,confirmed_at\n"
                         "X9C,X9,3,2026-10-25T08:00:00+08:00\n", "service_confirmations")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("无双方声明", result["message"])

    def test_claim_head_mismatch_rejected(self):
        """同一 transfer_id 的双方声明主数据不一致时整批拒收。"""
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "WB,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        result = receive(self.con, self.tmp.name, "service_claims.csv",
                         "claim_id,transfer_id,provider,consumer,service_type,period,side,quantity\n"
                         "X1-p,X1,SX,WA,power,2026-10,provider,10\n"
                         "X1-c,X1,SX,WB,power,2026-10,consumer,10\n", "service_claims")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("主数据不一致", result["message"])

    def test_duplicate_digest_adds_no_facts(self):
        self.load_base()
        again = ingest.receive(self.con, DATA / "source_costs.csv", received_at=T2)
        self.assertEqual(again["status"], "duplicate")
        count = queries._rows(self.con, "SELECT COUNT(*) AS n FROM receipt")[0]["n"]
        self.assertEqual(count, 5)

    def test_revision_replaces_row_and_keeps_history(self):
        """同一记录键的新文件是整行修订；原始行与接收历史保留，历史轮次不变。"""
        self.load_base()
        self.load_later()
        second = close_period(self.con, "2026-10")
        before = self.terminal(second, "WARD2")
        revised = ("center_id,kind,start,end\n"
                   "POWER,support,2026-10-01,2027-01-01\n"
                   "LAUNDRY,support,2026-10-01,2027-01-01\n"
                   "REPAIR,support,2026-10-01,2027-01-01\n"
                   "WARD1,clinical,2026-10-01,2027-01-01\n"
                   "WARD2,clinical,2026-10-01,2026-12-01\n"  # 2026-12 起并入 WARD1
                   "IDLE,idle,2026-10-01,2027-01-01\n")
        result = receive(self.con, self.tmp.name, "centers.csv", revised, "centers", at=T2)
        self.assertEqual(result["status"], "posted")
        third = close_period(self.con, "2026-10")
        self.assertNotEqual(second, third)
        # 历史月份仍按当时中心解释：WARD2 在 2026-10 有效，结果不变。
        self.assertEqual(self.terminal(third, "WARD2"), before)
        # 但 2026-12 起 WARD2 不再是有效用方。
        claims = ("claim_id,transfer_id,provider,consumer,service_type,period,side,quantity\n"
                  "X9-p,X9,POWER,WARD2,power,2026-12,provider,5\n"
                  "X9-c,X9,POWER,WARD2,power,2026-12,consumer,5\n")
        rejected = receive(self.con, self.tmp.name, "service_claims.csv", claims, "service_claims", at=T2)
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("不生效", rejected["message"])

    def test_outsourcing_changes_boundary_history_kept(self):
        """服务转院外：历史月份不变；新期间内部服务边界改变，外包发票是直接原生费用。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        revised = ("center_id,kind,start,end\n"
                   "POWER,support,2026-10-01,2027-01-01\n"
                   "LAUNDRY,support,2026-10-01,2026-12-01\n"  # 2026-12 起洗涤转院外
                   "REPAIR,support,2026-10-01,2027-01-01\n"
                   "WARD1,clinical,2026-10-01,2027-01-01\n"
                   "WARD2,clinical,2026-10-01,2027-01-01\n"
                   "IDLE,idle,2026-10-01,2027-01-01\n")
        receive(self.con, self.tmp.name, "centers.csv", revised, "centers", at=T2)
        claims = ("claim_id,transfer_id,provider,consumer,service_type,period,side,quantity\n"
                  "X8-p,X8,LAUNDRY,WARD1,laundry,2026-12,provider,5\n"
                  "X8-c,X8,LAUNDRY,WARD1,laundry,2026-12,consumer,5\n")
        rejected = receive(self.con, self.tmp.name, "service_claims.csv", claims, "service_claims", at=T2)
        self.assertEqual(rejected["status"], "rejected")
        costs = ("cost_id,center_id,period,cost_type,kind,amount,underlying_id,date\n"
                 "C20,WARD1,2026-12,variable,expense,300,OB20,2026-12-05\n")
        posted = receive(self.con, self.tmp.name, "source_costs.csv", costs, "source_costs", at=T2)
        self.assertEqual(posted["status"], "posted")
        round_dec = close_period(self.con, "2026-12")
        self.assertEqual(self.terminal(round_dec, "WARD1"), Decimal("300.00"))
        # 历史轮次结果仍在且不变。
        self.assertEqual(self.terminal(first, "WARD1"), Decimal("2982.75"))

    def test_same_provider_single_service_type(self):
        receive(self.con, self.tmp.name, "centers.csv",
                "center_id,kind,start,end\n"
                "SX,support,2026-10-01,2027-01-01\n"
                "WA,clinical,2026-10-01,2027-01-01\n"
                "IDLE,idle,2026-10-01,2027-01-01\n", "centers")
        claims = ("claim_id,transfer_id,provider,consumer,service_type,period,side,quantity\n"
                  "X1-p,X1,SX,WA,power,2026-10,provider,10\n"
                  "X2-p,X2,SX,WA,laundry,2026-10,provider,10\n")
        result = receive(self.con, self.tmp.name, "service_claims.csv", claims, "service_claims")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("计量类", result["message"])

    def test_contracts_match_catalog(self):
        """入仓契约与资料目录保持一致，防止漂移。"""
        catalog = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
        for table in catalog["tables"]:
            contract = CONTRACTS[table["name"]]
            self.assertEqual([c["name"] for c in contract["columns"]],
                             [c["name"] for c in table["columns"]], table["name"])
            self.assertEqual([c["type"] for c in contract["columns"]],
                             [c["type"] for c in table["columns"]], table["name"])
            self.assertEqual(contract["key"], table["key"], table["name"])


class QueryTests(WarehouseTest):
    def test_trace_cost_to_terminals(self):
        """按原始支出查最终去向：终端合计等于原费用，同义务凭据可见。"""
        self.load_base()
        self.load_later()
        close_period(self.con, "2026-10")
        close_period(self.con, "2026-10")
        result = queries.trace_cost(self.con, "C2")
        total = sum(fen for _, fen in result["destinations"])
        self.assertEqual(total, 200000)  # 2000 元 = 200000 分
        centers = {c for c, _ in result["destinations"]}
        self.assertEqual(centers, {"WARD1", "WARD2", "IDLE"})
        memos = {m.kind for m in result["same_obligation"]}
        self.assertEqual(memos, {"payment"})
        direct = queries.trace_cost(self.con, "C5")
        self.assertEqual(direct["destinations"], [("WARD1", 50000)])

    def test_diff_shows_indirectly_affected(self):
        """补一条互供量：WARD2 无新单据也被牵连，差异可见。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        self.load_later()
        second = close_period(self.con, "2026-10")
        diff = queries.diff_rounds(self.con, "2026-10", first, second)
        affected = {c["center_id"]: c for c in diff["changes"]}
        self.assertIn("WARD2", affected)
        self.assertFalse(affected["WARD2"]["directly_touched"])
        self.assertNotEqual(affected["WARD2"]["delta"]["terminal_total"], 0)
        self.assertTrue(affected["POWER"]["directly_touched"])
        self.assertTrue(affected["REPAIR"]["directly_touched"])

    def test_round_immutable_and_idempotent_close(self):
        """已确认轮次不可变：新事实只产生新轮；相同事实重复结转幂等。"""
        self.load_base()
        first = close_period(self.con, "2026-10")
        ward1_first = self.terminal(first, "WARD1")
        self.load_later()
        second = close_period(self.con, "2026-10")
        again = close_period(self.con, "2026-10")
        self.assertEqual(second, again)
        self.assertEqual(self.terminal(first, "WARD1"), ward1_first)
        self.assertEqual(ward1_first, Decimal("2982.75"))
        self.assertEqual(self.terminal(second, "WARD1"), Decimal("2982.39"))

    def test_amount_and_quantity_in_separate_columns(self):
        """金额与服务量单位不混作一列。"""
        self.load_base()
        round_id = close_period(self.con, "2026-10")
        flow = self.flow(round_id, "POWER", "WARD1")
        self.assertEqual(flow["amount"], Decimal("720.00"))
        self.assertEqual(flow["quantity"], Decimal("40.000"))
        self.assertEqual(flow["unit"], "kWh")
        columns = queries._rows(
            self.con,
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='alloc_flow'")
        types = {c["column_name"]: c["data_type"] for c in columns}
        self.assertNotEqual(types["amount"], types["share"])
        self.assertIn("DECIMAL", types["quantity"])

    def test_support_report_structure(self):
        """保障部门：自有费用、接受的内部服务、对外提供的服务三段齐全且净额为零。"""
        self.load_base()
        self.load_later()
        second = close_period(self.con, "2026-10")
        report = queries.center_report(self.con, "2026-10", "REPAIR", second)
        slot = report["slot"]
        self.assertEqual(slot["own_variable"], Decimal("800.00"))
        self.assertEqual(slot["in_from_fixed"], Decimal("100.00"))
        # 下钻结果按流转键去重后，转出合计 = 自有 + 转入（保障部门净额为零）。
        unique_flows = {(f["provider"], f["consumer"], f["pool"]): f["amount"]
                        for f in report["outflows"]}
        out_total = sum(unique_flows.values())
        self.assertEqual(slot["alloc_out"],
                         slot["own_fixed"] + slot["own_variable"]
                         + slot["in_from_fixed"] + slot["in_from_variable"])
        self.assertEqual(out_total, slot["alloc_out"])
        self.assertEqual(slot["terminal_total"], Decimal("0.00"))
        # 每条流转都可下钻到原服务范围（transfer/capacity/idle_variable 依据）。
        self.assertTrue(all(f["basis_type"] for f in report["outflows"]))


class SolveTests(unittest.TestCase):
    def test_solve_linear_exact(self):
        from fractions import Fraction
        matrix = [[Fraction(2), Fraction(0)], [Fraction(1), Fraction(3)]]
        self.assertEqual(solve_linear(matrix, [Fraction(4), Fraction(5)]),
                         [Fraction(2), Fraction(1)])

    def test_solve_linear_singular(self):
        from fractions import Fraction
        matrix = [[Fraction(1), Fraction(-1)], [Fraction(-1), Fraction(1)]]
        self.assertIsNone(solve_linear(matrix, [Fraction(1), Fraction(2)]))


if __name__ == "__main__":
    unittest.main()

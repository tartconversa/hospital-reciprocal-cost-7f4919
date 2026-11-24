"""按接收截点（as_of）还原的可读事实层。

只读取已入账（posted）的接收批次；同一事实流内同一记录键取接收次序最新行。
历史截点不可使用较晚才知的值：round 的 as_of_seq 决定它能看到哪些事实。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .contracts import UNITS


def period_bounds(period):
    """期间 YYYY-MM 的日期区间，左闭右开。"""
    year, month = int(period[:4]), int(period[5:7])
    if not (1 <= month <= 12) or period[4] != "-" or len(period) != 7:
        raise ValueError("期间格式须为 YYYY-MM：" + str(period))
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start, end


def fen(amount):
    """元 -> 分。入仓校验已保证金额不超过两位小数，此处为精确换算。"""
    return int(amount * 100)


@dataclass
class Center:
    center_id: str
    kind: str
    start: date
    end: date

    def valid_in(self, period):
        start, end = period_bounds(period)
        return self.start < end and self.end > start


@dataclass
class CostRow:
    cost_id: str
    center_id: str
    period: str
    cost_type: str
    kind: str
    amount: Decimal
    underlying_id: str
    date: date
    receipt_seq: int


@dataclass
class Transfer:
    """一笔内部服务的双方声明与共同认可。缺回执不是零，确认量仅取共同认可量。"""
    transfer_id: str
    provider: str
    consumer: str
    service_type: str
    period: str
    qty_provider: Decimal | None
    qty_consumer: Decimal | None
    matched: Decimal | None
    confirmation_id: str | None
    confirmed_at: str | None

    @property
    def unit(self):
        return UNITS[self.service_type]

    @property
    def status(self):
        if self.matched is not None:
            return "confirmed"        # 已核实（共同认可量）
        if self.qty_provider is None:
            return "consumer_only"    # 缺供方回执
        if self.qty_consumer is None:
            return "provider_only"    # 缺用方回执
        if self.qty_provider != self.qty_consumer:
            return "disputed"         # 双方不一致，保留未达
        return "declared"             # 双方一致但未经人工核实

    @property
    def gap(self):
        if self.qty_provider is None or self.qty_consumer is None:
            return None
        return self.qty_provider - self.qty_consumer

    @property
    def confirmed_qty(self):
        """确认服务量：仅人工共同认可量；未核实时为 None（未知），不为零。"""
        return self.matched


@dataclass
class Capacity:
    provider: str
    period: str
    shares: dict          # consumer -> Decimal 容量份额（含 IDLE，合计 1）
    idle_variable: Decimal  # 该中心级可变闲置比例（重复行是同一比例，不逐行相加）
    row_ids: dict         # consumer -> [capacity_id]


@dataclass
class Facts:
    as_of: int
    centers: dict         # center_id -> Center
    costs: list           # CostRow
    transfers: dict       # transfer_id -> Transfer
    capacities: dict      # (provider, period) -> Capacity

    def centers_in(self, period):
        return {cid: c for cid, c in self.centers.items() if c.valid_in(period)}

    def recognized(self, period):
        """本期认可原生费用：仅 kind=expense；payment 不重复计入，pending 仅待核。"""
        out = {}
        for row in self.costs:
            if row.period == period and row.kind == "expense":
                slot = out.setdefault(row.center_id, {"fixed": 0, "variable": 0})
                slot[row.cost_type] += fen(row.amount)
        return out

    def expense_rows(self, period):
        return [c for c in self.costs if c.period == period and c.kind == "expense"]

    def cost_memos(self, period):
        """现金/待核备查：不进入费用，但必须分别可见。"""
        by_obligation = {}
        for row in self.costs:
            if row.period == period:
                by_obligation.setdefault(row.underlying_id, []).append(row)
        memos = []
        for obligation, rows in sorted(by_obligation.items()):
            expense = [r for r in rows if r.kind == "expense"]
            expense_total = sum((r.amount for r in expense), Decimal(0))
            for r in rows:
                if r.kind == "payment":
                    if expense and r.amount == expense_total:
                        memos.append(("info", "cash_memo", r.cost_id,
                                      f"{r.cost_id}: 付款 {r.amount} 元，义务 {obligation} 已形成费用，现金不重复计入"))
                    elif expense:
                        memos.append(("warning", "cash_expense_gap", r.cost_id,
                                      f"{r.cost_id}: 付款 {r.amount} 元与义务 {obligation} 已形成费用 {expense_total} 元不一致，"
                                      "以已形成费用为准，付款不重复计入"))
                    else:
                        memos.append(("warning", "cash_without_expense", r.cost_id,
                                      f"{r.cost_id}: 付款 {r.amount} 元（义务 {obligation}）尚无已形成费用，不计入"))
                elif r.kind == "pending":
                    memos.append(("warning", "pending_cost", r.cost_id,
                                  f"{r.cost_id}: 待核 {r.amount} 元（义务 {obligation}），未形成费用，不计入本期"))
        return memos

    def transfers_in(self, period):
        return [t for t in self.transfers.values() if t.period == period]


def _decimal(value):
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _date(value):
    if value is None or value == "":
        return None
    return date.fromisoformat(str(value))


def load_facts(con, as_of=None, extra=None):
    """还原截至 as_of（含）的已入账事实；extra 为正在校验、尚未入账的接收号。"""
    if as_of is None:
        as_of = con.execute(
            "SELECT COALESCE(MAX(receipt_seq),0) FROM receipt WHERE status='posted'"
        ).fetchone()[0]
    rows = con.execute(
        """SELECT s.receipt_seq, r.stream, s.record_key, s.payload
           FROM stg_row s JOIN receipt r USING (receipt_seq)
           WHERE (r.status = 'posted' AND s.receipt_seq <= ?) OR s.receipt_seq = ?
           ORDER BY s.receipt_seq""",
        [as_of, extra if extra is not None else -1],
    ).fetchall()
    latest = {}
    for seq, stream, key, payload in rows:
        latest[(stream, key)] = (seq, json.loads(payload))

    centers, costs = {}, []
    claim_rows, confirmation_rows, capacity_rows = [], [], []
    for (stream, _key), (seq, p) in latest.items():
        if stream == "centers":
            centers[p["center_id"]] = Center(p["center_id"], p["kind"],
                                             _date(p["start"]), _date(p["end"]))
        elif stream == "source_costs":
            costs.append(CostRow(p["cost_id"], p["center_id"], p["period"], p["cost_type"],
                                 p["kind"], _decimal(p["amount"]), p["underlying_id"],
                                 _date(p["date"]), seq))
        elif stream == "claims":
            claim_rows.append((seq, p))
        elif stream == "confirmations":
            confirmation_rows.append((seq, p))
        elif stream == "capacity":
            capacity_rows.append((seq, p))

    transfers = {}
    for _seq, p in claim_rows:
        t = transfers.setdefault(p["transfer_id"], Transfer(
            p["transfer_id"], p["provider"], p["consumer"], p["service_type"], p["period"],
            None, None, None, None, None))
        if p["side"] == "provider":
            t.qty_provider = _decimal(p["quantity"])
        else:
            t.qty_consumer = _decimal(p["quantity"])
    # 同一 transfer 的共同认可量：取接收次序最新的一条人工核实记录。
    best = {}
    for seq, p in confirmation_rows:
        tid = p["transfer_id"]
        if tid not in best or seq > best[tid][0]:
            best[tid] = (seq, p)
    for tid, (_seq, p) in best.items():
        if tid in transfers:
            transfers[tid].matched = _decimal(p["matched_quantity"])
            transfers[tid].confirmation_id = p["confirmation_id"]
            transfers[tid].confirmed_at = p["confirmed_at"]

    capacities = {}
    for _seq, p in capacity_rows:
        key = (p["provider"], p["period"])
        cap = capacities.setdefault(key, Capacity(p["provider"], p["period"], {}, None, {}))
        consumer = p["consumer"]
        share = _decimal(p["capacity_share"])
        cap.shares[consumer] = cap.shares.get(consumer, Decimal(0)) + share
        cap.row_ids.setdefault(consumer, []).append(p["capacity_id"])
        idle = _decimal(p["idle_variable_share"])
        if cap.idle_variable is None:
            cap.idle_variable = idle
        elif idle is not None and cap.idle_variable != idle:
            # 同一 provider 的重复行是同一中心级比例；不一致属资料错误，入账校验应已拦截。
            raise ValueError(f"idle_variable_share 不一致：{p['provider']} {p['period']}")

    return Facts(as_of, centers, costs, transfers, capacities)

"""下钻查询：按原始支出查去向、按保障部门查自有/转入/转出、按临床查直接与共同、
全院总额核对、未达与待核例外、新旧轮次差异。金额与服务量始终分列。"""
from __future__ import annotations

from fractions import Fraction
from decimal import Decimal
import json

from .allocate import distribute, solve_linear
from .facts import load_facts


def _rows(con, sql, params=()):
    cur = con.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def rounds(con, period):
    return _rows(con,
                 "SELECT * FROM alloc_round WHERE period=? ORDER BY round_id", [period])


def current_round(con, period):
    """当前有效轮次：最新一轮未被替代且已产出结果的（confirmed/provisional）。"""
    row = _rows(con,
                """SELECT * FROM alloc_round
                   WHERE period=? AND status IN ('confirmed','provisional')
                   ORDER BY round_id DESC LIMIT 1""", [period])
    return row[0] if row else None


def _round_or_current(con, period, round_id):
    if round_id is not None:
        found = _rows(con, "SELECT * FROM alloc_round WHERE round_id=?", [round_id])
        if not found:
            raise ValueError(f"轮次不存在：{round_id}")
        return found[0]
    current = current_round(con, period)
    if current is None:
        raise ValueError(f"{period} 没有可用的结转轮次")
    return current


def hospital_total(con, period, round_id=None):
    """全院总额核对：原生费用 == 临床终端 + 明确闲置；内部互供不再计入。"""
    rnd = _round_or_current(con, period, round_id)
    rid = rnd["round_id"]
    costs = _rows(con, "SELECT * FROM center_cost WHERE round_id=?", [rid])
    own_total = sum(r["own_fixed"] + r["own_variable"] for r in costs)
    clinical_total = sum(r["terminal_total"] for r in costs if r["kind"] == "clinical")
    idle_total = sum(r["terminal_total"] for r in costs if r["kind"] == "idle")
    internal = con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM alloc_flow WHERE round_id=?", [rid]).fetchone()[0]
    facts = load_facts(con, rnd["as_of_seq"])
    pending = sum((r.amount for r in facts.costs if r.period == period and r.kind == "pending"),
                  Decimal(0))
    cash = sum((r.amount for r in facts.costs if r.period == period and r.kind == "payment"),
               Decimal(0))
    return {
        "round": rnd, "own_total": own_total, "clinical_total": clinical_total,
        "idle_total": idle_total, "internal_transfer_total": internal,
        "pending_total": pending, "cash_memo_total": cash,
        "conservation": own_total == clinical_total + idle_total,
        "centers": costs,
    }


def center_report(con, period, center_id, round_id=None):
    """按中心下钻：临床=直接+共同；保障=自有+接受+提供；IDLE=闲置承担。"""
    rnd = _round_or_current(con, period, round_id)
    rid = rnd["round_id"]
    slot = _rows(con, "SELECT * FROM center_cost WHERE round_id=? AND center_id=?",
                 [rid, center_id])
    if not slot:
        raise ValueError(f"{center_id} 不在 {period} 第 {rid} 轮结果中")
    inflows = _rows(con,
                    """SELECT f.*, b.basis_type, b.ref_id, b.quantity AS basis_qty,
                              b.unit AS basis_unit, b.share AS basis_share
                       FROM alloc_flow f LEFT JOIN alloc_flow_basis b
                         ON b.round_id=f.round_id AND b.provider=f.provider
                        AND b.consumer=f.consumer AND b.pool=f.pool
                       WHERE f.round_id=? AND f.consumer=? ORDER BY f.provider, f.pool""",
                    [rid, center_id])
    outflows = _rows(con,
                     """SELECT f.*, b.basis_type, b.ref_id, b.quantity AS basis_qty,
                              b.unit AS basis_unit, b.share AS basis_share
                       FROM alloc_flow f LEFT JOIN alloc_flow_basis b
                         ON b.round_id=f.round_id AND b.provider=f.provider
                        AND b.consumer=f.consumer AND b.pool=f.pool
                       WHERE f.round_id=? AND f.provider=? ORDER BY f.consumer, f.pool""",
                     [rid, center_id])
    own = _rows(con, "SELECT * FROM recognized_cost WHERE round_id=? AND center_id=?",
                [rid, center_id])
    return {"round": rnd, "slot": slot[0], "inflows": inflows, "outflows": outflows, "own": own}


def _network(flows):
    """从某轮流转事实重建互供网络份额。"""
    var_share, fix_share = {}, {}
    supports = sorted({f["provider"] for f in flows})
    for f in flows:
        target = var_share if f["pool"] == "variable" else fix_share
        target[(f["provider"], f["consumer"])] = Fraction(f["share"])
    terminals = sorted({c for pair in list(var_share) + list(fix_share)
                        for c in pair[1:2]} - set(supports))
    return supports, terminals, var_share, fix_share


def _terminal_distribution(supports, terminals, var_share):
    """每个保障部门可变池 1 元最终落到各终端的份额（精确分数）。"""
    n = len(supports)
    index = {p: i for i, p in enumerate(supports)}
    matrix = [[Fraction(1 if i == j else 0) - var_share.get((supports[i], supports[j]), Fraction(0))
               for j in range(n)] for i in range(n)]
    # matrix·x = e：x_j 为 j 池 1 元落到终端 t 的份额；注意方向为行=来源池。
    dist = {}
    for t in terminals:
        rhs = [var_share.get((supports[i], t), Fraction(0)) for i in range(n)]
        solved = solve_linear(matrix, rhs)
        if solved is None:
            continue
        for j in supports:
            dist[(j, t)] = solved[index[j]]
    return dist


def trace_cost(con, cost_id):
    """按原始支出查最终去向：费用 -> 承担中心费用池 -> 终端（临床/IDLE）。"""
    found = _rows(con,
                  """SELECT r.*, a.period, a.as_of_seq FROM recognized_cost r
                     JOIN alloc_round a ON a.round_id = r.round_id
                     WHERE r.cost_id=? ORDER BY r.round_id DESC LIMIT 1""", [cost_id])
    if not found:
        raise ValueError(f"费用 {cost_id} 未在任何轮次中认可（可能为 payment/pending 或不存在）")
    cost = found[0]
    rid, period = cost["round_id"], cost["period"]
    flows = _rows(con, "SELECT * FROM alloc_flow WHERE round_id=?", [rid])
    if not flows:
        raise ValueError(f"费用 {cost_id} 所在第 {rid} 轮为阻断轮（blocked），"
                         "存在未闭合例外，没有可确认的终端去向，请先查 exceptions")
    supports, terminals, var_share, fix_share = _network(flows)
    var_dist = _terminal_distribution(supports, terminals, var_share)

    center = cost["center_id"]
    amount_fen = int(cost["amount"] * 100)
    if center in supports:
        if cost["cost_type"] == "variable":
            dist = {t: var_dist.get((center, t), Fraction(0)) for t in terminals}
        else:
            dist = {}
            for t in terminals:
                share = fix_share.get((center, t), Fraction(0))
                share += sum(fix_share.get((center, s), Fraction(0)) * var_dist.get((s, t), Fraction(0))
                             for s in supports)
                dist[t] = share
        items = [(t, s) for t, s in dist.items() if s > 0]
        settled = distribute(amount_fen, items, set(terminals)) if items else {}
    else:
        settled = {center: amount_fen}  # 临床直接费用，终端即自身

    facts = load_facts(con, cost["as_of_seq"])
    same_obligation = [r for r in facts.costs
                       if r.underlying_id == cost["underlying_id"] and r.cost_id != cost_id]
    return {"cost": cost, "round_id": rid, "period": period,
            "destinations": sorted(settled.items()), "same_obligation": same_obligation}


def exceptions(con, period, round_id=None):
    """例外事项对所有轮次可见，包括阻断轮（blocked 不产出结果，但例外必须可查）。"""
    if round_id is None:
        found = _rows(con,
                      "SELECT * FROM alloc_round WHERE period=? ORDER BY round_id DESC LIMIT 1",
                      [period])
        if not found:
            raise ValueError(f"{period} 没有任何结转轮次")
        rnd = found[0]
    else:
        found = _rows(con, "SELECT * FROM alloc_round WHERE round_id=?", [round_id])
        if not found:
            raise ValueError(f"轮次不存在：{round_id}")
        rnd = found[0]
    return _rows(con,
                 """SELECT * FROM round_exception WHERE round_id=?
                    ORDER BY CASE severity WHEN 'fatal' THEN 0 WHEN 'open' THEN 1
                             WHEN 'warning' THEN 2 ELSE 3 END, kind, ref_id""",
                 [rnd["round_id"]])


def transfers_view(con, period, round_id=None):
    """内部服务一览：双方声明、共同认可、状态与对应流转金额，量与额分列。"""
    rnd = _round_or_current(con, period, round_id)
    facts = load_facts(con, rnd["as_of_seq"])
    flows = {(f["provider"], f["consumer"], f["pool"]): f
             for f in _rows(con, "SELECT * FROM alloc_flow WHERE round_id=?", [rnd["round_id"]])}
    out = []
    for t in sorted(facts.transfers_in(period), key=lambda x: x.transfer_id):
        flow = flows.get((t.provider, t.consumer, "variable"))
        out.append({"transfer": t, "flow": flow})
    return {"round": rnd, "rows": out}


def diff_rounds(con, period, from_id=None, to_id=None):
    """新旧两轮差异：受影响中心（含无新单据而被牵连的）及金额变化。"""
    all_rounds = rounds(con, period)
    usable = [r for r in all_rounds if r["status"] != "blocked"]
    if to_id is None:
        if not usable:
            raise ValueError(f"{period} 没有可对比的轮次")
        to_id = usable[-1]["round_id"]
    if from_id is None:
        earlier = [r for r in usable if r["round_id"] < to_id]
        if not earlier:
            raise ValueError(f"{period} 第 {to_id} 轮之前没有可对比的旧轮")
        from_id = earlier[-1]["round_id"]
    a = _rows(con, "SELECT * FROM center_cost WHERE round_id=?", [from_id])
    b = _rows(con, "SELECT * FROM center_cost WHERE round_id=?", [to_id])
    ra = _rows(con, "SELECT * FROM alloc_round WHERE round_id=?", [from_id])[0]
    rb = _rows(con, "SELECT * FROM alloc_round WHERE round_id=?", [to_id])[0]

    # 两截点之间新入账的接收直接触及哪些中心。
    touched = set()
    new_receipts = _rows(con,
                         """SELECT * FROM receipt WHERE receipt_seq>? AND receipt_seq<=?
                            AND status='posted' ORDER BY receipt_seq""",
                         [ra["as_of_seq"], rb["as_of_seq"]])
    facts_b = load_facts(con, rb["as_of_seq"])
    for receipt in new_receipts:
        for row in _rows(con, "SELECT payload FROM stg_row WHERE receipt_seq=?",
                         [receipt["receipt_seq"]]):
            p = json.loads(row["payload"])
            for field in ("center_id", "provider", "consumer"):
                if p.get(field):
                    touched.add(p[field])
            if p.get("transfer_id") and p["transfer_id"] in facts_b.transfers:
                t = facts_b.transfers[p["transfer_id"]]
                touched.update([t.provider, t.consumer])

    by_a = {r["center_id"]: r for r in a}
    by_b = {r["center_id"]: r for r in b}
    changes = []
    for cid in sorted(set(by_a) | set(by_b)):
        old = by_a.get(cid, {})
        new = by_b.get(cid, {})
        delta = {k: (new.get(k, 0) or 0) - (old.get(k, 0) or 0)
                 for k in ("own_fixed", "own_variable", "in_from_fixed",
                           "in_from_variable", "terminal_total")}
        if any(v != 0 for v in delta.values()):
            changes.append({"center_id": cid,
                            "kind": (new or old).get("kind"),
                            "old": old, "new": new, "delta": delta,
                            "directly_touched": cid in touched})
    flow_a = {(f["provider"], f["consumer"], f["pool"]): f["amount"]
              for f in _rows(con, "SELECT * FROM alloc_flow WHERE round_id=?", [from_id])}
    flow_b = {(f["provider"], f["consumer"], f["pool"]): f["amount"]
              for f in _rows(con, "SELECT * FROM alloc_flow WHERE round_id=?", [to_id])}
    flow_changes = [{"provider": k[0], "consumer": k[1], "pool": k[2],
                     "old": flow_a.get(k), "new": flow_b.get(k)}
                    for k in sorted(set(flow_a) | set(flow_b))
                    if flow_a.get(k) != flow_b.get(k)]
    return {"period": period, "from_round": ra, "to_round": rb,
            "new_receipts": new_receipts, "changes": changes, "flow_changes": flow_changes}

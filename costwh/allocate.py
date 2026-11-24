"""期间费用形成与结转确认。

口径要点（docs/资料口径.md）：
- 原生 fixed 按容量份额分担；原生 variable 与全部转入成本按该中心已确认
  variable 服务份额及 idle_variable_share 继续承担，转入不是新增 fixed 预算。
- 互供联立求解（同时方程），不因处理顺序让某个部门少承担。
- IDLE 是真实闲置责任；互供没有临床或 IDLE 吸收出口时不能确认成平衡。
- 金额结算到分，尾差归同一费用池终端分摊基数最大项，并列按中心键升序。
- 存在未核实服务量时只能暂结（provisional），不能当作完整期间结转确认。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from fractions import Fraction

from .contracts import IDLE_CENTER, UNITS
from .facts import load_facts

CN_TZ = timezone(timedelta(hours=8))

STATUS_LABEL = {"confirmed": "已确认", "provisional": "暂结", "blocked": "不可闭合", "superseded": "已被替代"}


@dataclass
class Exc:
    severity: str   # fatal / open / warning / info
    kind: str
    ref_id: str | None
    detail: str


@dataclass
class Flow:
    provider: str
    consumer: str
    pool: str                 # fixed / variable
    service_type: str | None
    quantity: object | None   # Decimal 确认服务量；fixed 池与 IDLE 闲置行为 None
    unit: str | None
    amount_fen: int
    share: Fraction
    basis: list = field(default_factory=list)  # (basis_type, ref_id, quantity, unit, share_text)


@dataclass
class CenterCost:
    center_id: str
    kind: str
    own_fixed: int = 0
    own_variable: int = 0
    in_fixed: int = 0
    in_variable: int = 0
    alloc_out: int = 0
    terminal: int = 0


@dataclass
class ComputeResult:
    hint: str                 # ok / open / fatal
    exceptions: list
    flows: list
    center_costs: dict
    recognized_rows: list     # 费用行级（recognized_cost 表）
    supports: list
    input_hash: str


def fen_round(x):
    """Fraction -> 分，四舍五入（半分向上）。"""
    n, d = x.numerator, x.denominator
    if n >= 0:
        return (2 * n + d) // (2 * d)
    return -((-2 * n + d) // (2 * d))


def solve_linear(matrix, rhs):
    """高斯-若尔当精确求解 matrix·x = rhs（Fraction）。奇异返回 None。"""
    n = len(rhs)
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if aug[pivot][col] == 0:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        inv = 1 / aug[col][col]
        aug[col] = [v * inv for v in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [v - factor * w for v, w in zip(aug[r], aug[col])]
    return [aug[i][n] for i in range(n)]


def distribute(pool_fen, items, terminals):
    """把 pool_fen 按份额分到各项（份额合计须恰为 1）。

    尾差归终端分摊基数最大项，并列按中心键升序；尚未吸收的成本不放尾差。
    items: [(consumer, Fraction share)]；terminals: 终端中心集合。
    """
    assert sum((s for _, s in items), Fraction(0)) == 1, "分摊份额合计必须为 1"
    exact = [(key, share, pool_fen * share) for key, share in items]
    rounded = {key: fen_round(e) for key, _share, e in exact}
    residual = pool_fen - sum(rounded.values())
    if residual and exact:
        def rank(item):
            key, _share, amount = item
            return (0 if key in terminals else 1, -amount, key)
        target = min(exact, key=rank)[0]
        rounded[target] += residual
    return rounded


def compute(facts, period):
    """对单个期间做费用形成与互供联解。不触碰数据库，便于测试与复算。"""
    exc = []
    centers = facts.centers_in(period)
    recognized = facts.recognized(period)
    expense_rows = facts.expense_rows(period)
    for severity, kind, ref, detail in facts.cost_memos(period):
        exc.append(Exc(severity, kind, ref, detail))

    transfers = facts.transfers_in(period)
    for t in sorted(transfers, key=lambda x: x.transfer_id):
        if t.status == "confirmed":
            continue
        if t.status == "disputed":
            detail = (f"{t.transfer_id}: {t.provider}→{t.consumer} {t.service_type} "
                      f"供方声明 {t.qty_provider} {t.unit}、用方声明 {t.qty_consumer} {t.unit}，"
                      f"未达 {abs(t.gap)} {t.unit}；无共同认可量，确认量留空（不按零、不折中）")
        elif t.status == "provider_only":
            detail = (f"{t.transfer_id}: {t.provider}→{t.consumer} {t.service_type} "
                      f"缺用方回执，供方声明 {t.qty_provider} {t.unit}；确认量留空（缺回执不当零）")
        elif t.status == "consumer_only":
            detail = (f"{t.transfer_id}: {t.provider}→{t.consumer} {t.service_type} "
                      f"缺供方回执，用方声明 {t.qty_consumer} {t.unit}；确认量留空（缺回执不当零）")
        else:
            detail = (f"{t.transfer_id}: {t.provider}→{t.consumer} {t.service_type} "
                      f"双方声明一致（{t.qty_provider} {t.unit}）但未经人工共同核实，确认量留空")
        exc.append(Exc("open", "unverified_transfer", t.transfer_id, detail))

    providers = sorted({
        cid for cid, c in centers.items() if c.kind == "support" and (
            recognized.get(cid) or (cid, period) in facts.capacities
            or any(t.provider == cid for t in transfers))
    })

    own_fixed = {p: recognized.get(p, {}).get("fixed", 0) for p in providers}
    own_var = {p: recognized.get(p, {}).get("variable", 0) for p in providers}

    # 容量与闲置比例：每个有费用或有服务的保障部门都必须有容量行。
    idle, capshares, cap_rows = {}, {}, {}
    for p in providers:
        cap = facts.capacities.get((p, period))
        if cap is None:
            exc.append(Exc("fatal", "capacity_missing", p,
                           f"{p}: {period} 缺容量行，无法确定固定份额与可变闲置比例"))
            continue
        total = sum(cap.shares.values())
        if total != 1:
            exc.append(Exc("fatal", "capacity_invalid", p,
                           f"{p}: {period} 容量份额合计 {total}，须恰为 1（含 IDLE）"))
        idle[p] = Fraction(cap.idle_variable)
        capshares[p] = {c: Fraction(s) for c, s in cap.shares.items()}
        cap_rows[p] = cap.row_ids

    # 已确认服务量（仅共同认可量进入份额；未核实部分不进分母也不当零）。
    confirmed = {}
    transfer_by_pair = {}
    service_type_of = {}
    for t in transfers:
        service_type_of.setdefault(t.provider, t.service_type)
        if t.status == "confirmed" and t.matched is not None:
            pair = (t.provider, t.consumer)
            confirmed[pair] = confirmed.get(pair, 0) + t.matched
            transfer_by_pair.setdefault(pair, []).append(t)
    total_q = {p: sum(q for (pr, _), q in confirmed.items() if pr == p) for p in providers}

    terminals = {cid for cid, c in centers.items() if c.kind in ("clinical", "idle")}

    def fatal_result():
        return ComputeResult("fatal", exc, [], {}, expense_rows, providers, _input_hash(facts, period))

    if any(e.severity == "fatal" for e in exc):
        return fatal_result()

    # 联立求解：V_i = 自有variable_i + Σ_j 固定转入 + Σ_j (1-idle_j)·qs_{j→i}·V_j
    n = len(providers)
    index = {p: i for i, p in enumerate(providers)}
    matrix = [[Fraction(1 if i == j else 0) for j in range(n)] for i in range(n)]
    for j in providers:
        if total_q[j] == 0:
            continue
        for (pr, consumer), qty in confirmed.items():
            if pr == j and consumer in index and qty > 0:
                matrix[index[consumer]][index[j]] -= (1 - idle[j]) * Fraction(qty) / Fraction(total_q[j])
    rhs = []
    for i in providers:
        value = Fraction(own_var[i])
        for j in providers:
            value += Fraction(own_fixed[j]) * capshares.get(j, {}).get(i, Fraction(0))
        rhs.append(value)
    solved = solve_linear(matrix, rhs) if n else []
    if solved is None:
        loop = "、".join(providers)
        exc.append(Exc("fatal", "unclosable_loop", None,
                       f"互供服务环路没有有效的临床或 IDLE 吸收出口（涉及 {loop}），"
                       "不能确认成平衡，也不设平衡科室"))
        return fatal_result()

    # 无可变服务量且闲置比例小于 1 的池：部分成本无吸收出口。
    for j in providers:
        pool_exact = solved[index[j]]
        if total_q[j] == 0 and idle[j] < 1 and fen_round(pool_exact) != 0:
            exc.append(Exc("fatal", "unabsorbable_variable", j,
                           f"{j}: 可变池 {pool_exact} 元无已确认服务量且 idle_variable_share={idle[j]}<1，"
                           "存在不能吸收的成本，须显式处理，不能丢弃残差"))
    if any(e.severity == "fatal" for e in exc):
        return fatal_result()

    # 固定池：自有 fixed 按容量份额一次分配（含 IDLE 行）。
    fixed_shares = {}
    for j in providers:
        items = [(c, s) for c, s in capshares[j].items() if s > 0]
        fixed_shares[j] = distribute(own_fixed[j], items, terminals) if own_fixed[j] and items else {}

    def variable_items(j):
        items = []
        if total_q[j] > 0:
            for (pr, consumer), qty in sorted(confirmed.items()):
                if pr == j and qty > 0:
                    items.append((consumer, (1 - idle[j]) * Fraction(qty) / Fraction(total_q[j])))
        if idle[j] > 0:
            items.append((IDLE_CENTER, idle[j]))
        return items

    # 分位定点迭代：转入按分入账后再分配，直到各部门池子在分位上自洽。
    var_pools = {j: fen_round(solved[index[j]]) for j in providers}
    var_shares = {}
    for _ in range(200):
        var_shares = {j: (distribute(var_pools[j], variable_items(j), terminals)
                          if var_pools[j] and variable_items(j) else {}) for j in providers}
        new_pools = {}
        for i in providers:
            inflow = own_var[i]
            inflow += sum(fixed_shares[j].get(i, 0) for j in providers)
            inflow += sum(var_shares[j].get(i, 0) for j in providers)
            new_pools[i] = inflow
        if new_pools == var_pools:
            break
        var_pools = new_pools
    else:
        exc.append(Exc("fatal", "conservation", None, "分位舍入迭代不收敛，不能确认"))
        return fatal_result()

    # 组装流转事实（金额与服务量分列，份额保留精确分数文本）。
    flows = []
    for j in providers:
        stype = service_type_of.get(j)
        unit = UNITS.get(stype) if stype else None
        for consumer, amount in sorted(fixed_shares[j].items()):
            if amount == 0 and capshares[j].get(consumer, 0) == 0:
                continue
            basis = [("capacity", cid, None, None, str(capshares[j][consumer]))
                     for cid in cap_rows[j].get(consumer, ["?"])]
            flows.append(Flow(j, consumer, "fixed", None, None, None, amount,
                              capshares[j].get(consumer, Fraction(0)), basis))
        for consumer, amount in sorted(var_shares[j].items()):
            share = dict(variable_items(j)).get(consumer, Fraction(0))
            quantity = confirmed.get((j, consumer))  # IDLE 闲置行无服务量，为 None
            basis = []
            if consumer == IDLE_CENTER:
                basis.append(("idle_variable", "IDLE_VARIABLE", None, None, str(idle[j])))
            else:
                pair_total = confirmed.get((j, consumer))
                for t in sorted(transfer_by_pair.get((j, consumer), []), key=lambda x: x.transfer_id):
                    basis.append(("transfer", t.transfer_id, t.matched, t.unit,
                                  str(Fraction(t.matched) / Fraction(pair_total))))
            flows.append(Flow(j, consumer, "variable", stype, quantity, unit, amount, share, basis))

    # 部门成本汇总与守恒核对：原生总额 == 临床终端 + 明确闲置。
    center_costs = {}
    for cid, c in centers.items():
        slot = CenterCost(cid, c.kind)
        slot.own_fixed = recognized.get(cid, {}).get("fixed", 0)
        slot.own_variable = recognized.get(cid, {}).get("variable", 0)
        for f in flows:
            if f.consumer == cid:
                if f.pool == "fixed":
                    slot.in_fixed += f.amount_fen
                else:
                    slot.in_variable += f.amount_fen
        if c.kind == "support":
            slot.alloc_out = slot.own_fixed + slot.own_variable + slot.in_fixed + slot.in_variable
            slot.terminal = 0
        elif c.kind == "clinical":
            slot.terminal = slot.own_fixed + slot.own_variable + slot.in_fixed + slot.in_variable
        else:
            slot.terminal = slot.in_fixed + slot.in_variable
        center_costs[cid] = slot

    own_total = sum(s.own_fixed + s.own_variable for s in center_costs.values())
    terminal_total = sum(s.terminal for s in center_costs.values())
    if own_total != terminal_total:
        exc.append(Exc("fatal", "conservation", None,
                       f"总额不可核对：原生 {own_total} 分 ≠ 终端吸收 {terminal_total} 分"))
        return fatal_result()

    hint = "open" if any(e.severity == "open" for e in exc) else "ok"
    return ComputeResult(hint, exc, flows, center_costs, expense_rows, providers,
                         _input_hash(facts, period))


def _input_hash(facts, period):
    centers = facts.centers_in(period)
    snapshot = {
        "period": period,
        "as_of": facts.as_of,
        "centers": sorted((c.center_id, c.kind, str(c.start), str(c.end)) for c in centers.values()),
        "costs": sorted((r.cost_id, r.center_id, r.cost_type, r.kind, str(r.amount), r.underlying_id)
                        for r in facts.costs if r.period == period),
        "transfers": sorted((t.transfer_id, t.provider, t.consumer, t.service_type,
                             str(t.qty_provider), str(t.qty_consumer), str(t.matched))
                            for t in facts.transfers_in(period)),
        "capacity": sorted((p, cp, c, str(s), str(cap.idle_variable))
                           for (p, cp), cap in facts.capacities.items() if cp == period
                           for c, s in cap.shares.items()),
    }
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _next_round_id(con):
    return con.execute("SELECT COALESCE(MAX(round_id),0)+1 FROM alloc_round").fetchone()[0]


def close_period(con, period, note=""):
    """为期间创建一轮结转：同一接收截点幂等；新事实只会产生新的一轮，旧轮保留。"""
    as_of = con.execute(
        "SELECT COALESCE(MAX(receipt_seq),0) FROM receipt WHERE status='posted'").fetchone()[0]
    if as_of == 0:
        raise ValueError("尚无已入账资料，不能结转")
    existing = con.execute(
        "SELECT round_id, status FROM alloc_round WHERE period=? AND as_of_seq=? AND status!='superseded'",
        [period, as_of]).fetchone()
    if existing:
        return existing[0]

    facts = load_facts(con, as_of)
    result = compute(facts, period)
    if not result.recognized_rows and not result.exceptions:
        raise ValueError(f"{period} 无任何事实，不能结转")

    round_id = _next_round_id(con)
    status = {"ok": "confirmed", "open": "provisional", "fatal": "blocked"}[result.hint]
    created_at = datetime.now(CN_TZ).isoformat()
    con.execute("INSERT INTO alloc_round VALUES (?,?,?,?,?,?,?)",
                [round_id, period, as_of, status, created_at, result.input_hash, note])

    for row in result.recognized_rows:
        con.execute("INSERT INTO recognized_cost VALUES (?,?,?,?,?,?)",
                    [round_id, row.cost_id, row.center_id, row.cost_type,
                     row.amount, row.underlying_id])
    if result.hint != "fatal":
        for slot in result.center_costs.values():
            con.execute("INSERT INTO center_cost VALUES (?,?,?,?,?,?,?,?,?)",
                        [round_id, slot.center_id, slot.kind,
                         _yuan(slot.own_fixed), _yuan(slot.own_variable),
                         _yuan(slot.in_fixed), _yuan(slot.in_variable),
                         _yuan(slot.alloc_out), _yuan(slot.terminal)])
        for f in result.flows:
            con.execute("INSERT INTO alloc_flow VALUES (?,?,?,?,?,?,?,?,?)",
                        [round_id, f.provider, f.consumer, f.pool, f.service_type,
                         f.quantity, f.unit, _yuan(f.amount_fen), str(f.share)])
            for basis_type, ref_id, qty, unit, share_text in f.basis:
                con.execute("INSERT INTO alloc_flow_basis VALUES (?,?,?,?,?,?,?,?,?)",
                            [round_id, f.provider, f.consumer, f.pool,
                             basis_type, ref_id, qty, unit, share_text])
    for e in result.exceptions:
        con.execute("INSERT INTO round_exception VALUES (?,?,?,?,?)",
                    [round_id, e.severity, e.kind, e.ref_id, e.detail])

    # 新一轮可用解释产生后，旧轮标记为已被替代（结果保留，可对比）；blocked 不替代有效结转。
    if status in ("confirmed", "provisional"):
        con.execute(
            """UPDATE alloc_round SET status='superseded'
               WHERE period=? AND round_id!=? AND status IN ('confirmed','provisional')""",
            [period, round_id])
    return round_id


def _yuan(fen_value):
    from decimal import Decimal
    return Decimal(fen_value) / 100

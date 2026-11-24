"""查询结果的中文可读渲染。金额（元）与服务量（带单位）始终分列。"""
from __future__ import annotations

from decimal import Decimal

from .allocate import STATUS_LABEL

POOL_LABEL = {"fixed": "固定保障", "variable": "可变服务"}
SEVERITY_LABEL = {"fatal": "阻断", "open": "待核实", "warning": "警示", "info": "备查"}


def yuan(value):
    return f"{Decimal(value):,.2f} 元"


def qty(value, unit):
    if value is None:
        return "—"
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {unit}" if unit else text


def round_title(rnd):
    return f"第 {rnd['round_id']} 轮（{STATUS_LABEL.get(rnd['status'], rnd['status'])}，事实截点 #{rnd['as_of_seq']}）"


def render_total(result):
    rnd = result["round"]
    lines = [f"== {rnd['period']} 全院汇总 · {round_title(rnd)} =="]
    lines.append(f"原生费用认可合计      {yuan(result['own_total'])}")
    lines.append(f"  临床终端承担        {yuan(result['clinical_total'])}")
    lines.append(f"  明确闲置(IDLE)承担  {yuan(result['idle_total'])}")
    lines.append(f"内部互供结转(不计入总额) {yuan(result['internal_transfer_total'])}")
    lines.append(f"现金凭据(不重复计入)  {yuan(result['cash_memo_total'])}")
    lines.append(f"待核费用(未形成,不计入) {yuan(result['pending_total'])}")
    ok = "一致" if result["conservation"] else "不一致！"
    lines.append(f"总额核对(原生=临床+闲置)：{ok}")
    return "\n".join(lines)


def render_center(report):
    rnd, slot = report["round"], report["slot"]
    cid = slot["center_id"]
    lines = [f"== {cid}（{slot['kind']}）{rnd['period']} · {round_title(rnd)} =="]
    lines.append(f"自有原生费用：固定 {yuan(slot['own_fixed'])}，可变 {yuan(slot['own_variable'])}")
    if slot["kind"] == "support":
        lines.append(f"接受内部服务(转入,非新原生)：固定源 {yuan(slot['in_from_fixed'])}，"
                     f"可变源 {yuan(slot['in_from_variable'])}")
        lines.append(f"对外提供服务(转出)合计：{yuan(slot['alloc_out'])}")
        lines.append("转入明细：")
        lines += _flow_lines(report["inflows"], "  ← ")
        lines.append("转出明细：")
        lines += _flow_lines(report["outflows"], "  → ")
    elif slot["kind"] == "clinical":
        lines.append(f"共同保障费用(转入)：固定源 {yuan(slot['in_from_fixed'])}，"
                     f"可变源 {yuan(slot['in_from_variable'])}")
        lines.append("共同费用明细：")
        lines += _flow_lines(report["inflows"], "  ← ")
        lines.append(f"本科室合计承担：{yuan(slot['terminal_total'])}")
    else:
        lines.append("闲置能力承担明细：")
        lines += _flow_lines(report["inflows"], "  ← ")
        lines.append(f"闲置合计：{yuan(slot['terminal_total'])}")
    if report["own"]:
        lines.append("自有费用行(可追到凭据)：")
        for row in report["own"]:
            lines.append(f"  {row['cost_id']} {row['cost_type']} {yuan(row['amount'])}"
                         f"（义务 {row['underlying_id']}）")
    return "\n".join(lines)


def _flow_lines(flows, prefix):
    lines = []
    seen = set()
    for f in flows:
        key = (f["provider"], f["consumer"], f["pool"])
        if key not in seen:
            seen.add(key)
            peer = f["provider"] if prefix.strip() == "←" else f["consumer"]
            quantity = qty(f["quantity"], f["unit"]) if f["quantity"] is not None else "—"
            lines.append(f"{prefix}{peer} {POOL_LABEL[f['pool']]} {yuan(f['amount'])}"
                         f"｜服务量 {quantity}｜份额 {f['share']}")
        if f.get("basis_type"):
            lines.append(f"      依据[{f['basis_type']}] {f['ref_id']}"
                         f" 量 {qty(f.get('basis_qty'), f.get('basis_unit'))} 份额 {f['basis_share']}")
    return lines or ["  （无）"]


def render_exceptions(rows):
    if not rows:
        return "无例外事项。"
    lines = ["== 例外事项（分别可见，不以总额相等掩盖） =="]
    for r in rows:
        lines.append(f"[{SEVERITY_LABEL.get(r['severity'], r['severity'])}|{r['kind']}] {r['detail']}")
    return "\n".join(lines)


def render_transfers(view):
    rnd = view["round"]
    lines = [f"== {rnd['period']} 内部服务核实 · {round_title(rnd)} =="]
    status_label = {"confirmed": "已核实", "disputed": "双方不一致", "provider_only": "缺用方回执",
                    "consumer_only": "缺供方回执", "declared": "未共同核实"}
    for row in view["rows"]:
        t, flow = row["transfer"], row["flow"]
        amount = yuan(flow["amount"]) if flow else "—"
        lines.append(
            f"{t.transfer_id} {t.provider}→{t.consumer} {t.service_type}"
            f"｜供方 {qty(t.qty_provider, t.unit)}｜用方 {qty(t.qty_consumer, t.unit)}"
            f"｜确认 {qty(t.matched, t.unit)}｜{status_label[t.status]}｜可变池金额 {amount}")
    return "\n".join(lines)


def render_trace(result):
    cost = result["cost"]
    lines = [f"== 费用去向追踪 {cost['cost_id']}（第 {result['round_id']} 轮，{result['period']}）=="]
    lines.append(f"承担中心 {cost['center_id']}｜{cost['cost_type']}｜{yuan(cost['amount'])}"
                 f"｜义务 {cost['underlying_id']}")
    for memo in result["same_obligation"]:
        note = {"payment": "现金凭据，不重复计入", "pending": "待核，不计入"}.get(memo.kind, memo.kind)
        lines.append(f"  同义务凭据 {memo.cost_id}（{memo.kind} {yuan(memo.amount)}）：{note}")
    lines.append("最终去向：")
    for center, fen_value in result["destinations"]:
        lines.append(f"  → {center} {yuan(Decimal(fen_value) / 100)}")
    return "\n".join(lines)


def render_diff(result):
    a, b = result["from_round"], result["to_round"]
    lines = [f"== {result['period']} 结转差异：第 {a['round_id']} 轮 → 第 {b['round_id']} 轮 =="]
    if result["new_receipts"]:
        lines.append("两轮之间新入账资料：")
        for r in result["new_receipts"]:
            lines.append(f"  #{r['receipt_seq']} {r['source_table']}（{r['source_label']}，{r['received_at']}）")
    if not result["changes"]:
        lines.append("各中心承担金额无变化。")
    for ch in result["changes"]:
        mark = "新资料直接触及" if ch["directly_touched"] else "牵连影响（本轮无新单据）"
        old_t = ch["old"].get("terminal_total")
        new_t = ch["new"].get("terminal_total")
        delta = ch["delta"]["terminal_total"]
        lines.append(f"  {ch['center_id']}（{ch['kind']}，{mark}）")
        lines.append(f"    终端承担 {yuan(old_t or 0)} → {yuan(new_t or 0)}（Δ {yuan(delta)}）")
        if ch["delta"]["in_from_fixed"] or ch["delta"]["in_from_variable"]:
            lines.append(f"    其中转入变化：固定源 Δ{yuan(ch['delta']['in_from_fixed'])}，"
                         f"可变源 Δ{yuan(ch['delta']['in_from_variable'])}")
    if result["flow_changes"]:
        lines.append("变化的内部流转：")
        for f in result["flow_changes"]:
            lines.append(f"  {f['provider']}→{f['consumer']} {f['pool']}"
                         f" {yuan(f['old'] or 0)} → {yuan(f['new'] or 0)}")
    return "\n".join(lines)

"""分次装载：接收 -> 暂存 -> 业务校验 -> 入账或整批拒收。

- 相同文件摘要不增加事实（duplicate）。
- 同一记录键的新文件表示整行修订，接收次序即 receipt_seq；原始行与接收历史保留。
- 每个接收要么整批入账，要么整批拒收：新资料只有在相关依赖都核对后才进入事实层。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sourcepack.catalog import read_table

from . import facts as facts_mod
from .contracts import (BASENAMES, CENTER_KINDS, CONTRACTS, COST_KINDS, COST_TYPES,
                        IDLE_CENTER, SIDES, STREAM, UNITS)


def _next_seq(con):
    return con.execute("SELECT COALESCE(MAX(receipt_seq),0)+1 FROM receipt").fetchone()[0]


def _record_receipt(con, seq, table, label, digest, received_at, rows, status, message):
    con.execute(
        "INSERT INTO receipt VALUES (?,?,?,?,?,?,?,?,?)",
        [seq, table, STREAM.get(table, "?"), label, digest, received_at, rows, status, message],
    )
    return {"receipt_seq": seq, "status": status, "rows": rows, "message": message}


def receive(con, path, table=None, received_at=None):
    """接收一个资料文件。返回接收结果字典；拒收不改变已入账事实。"""
    path = Path(path)
    label = str(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = con.execute(
        "SELECT receipt_seq, status FROM receipt WHERE sha256 = ? ORDER BY receipt_seq LIMIT 1",
        [digest],
    ).fetchone()
    if existing:
        return {"receipt_seq": existing[0], "status": "duplicate", "rows": 0,
                "message": f"相同摘要已接收（第 {existing[0]} 批，{existing[1]}），不增加事实"}

    table = table or BASENAMES.get(path.name)
    if table not in CONTRACTS:
        raise ValueError("未知资料表，请用 --table 指定：" + path.name)
    if received_at is None:
        raise ValueError("缺 received_at：接收时刻必须显式给定（ISO 带时区）")
    stamp = datetime.fromisoformat(received_at)
    if stamp.tzinfo is None:
        raise ValueError("received_at 须带时区，如 2026-10-25T09:00:00+08:00")

    seq = _next_seq(con)
    try:
        rows = read_table(path, CONTRACTS[table])
    except ValueError as exc:
        return _record_receipt(con, seq, table, label, digest, received_at, 0,
                               "rejected", "字段/类型/记录键校验失败：" + str(exc))

    con.execute("INSERT INTO receipt VALUES (?,?,?,?,?,?,?,?,?)",
                [seq, table, STREAM[table], label, digest, received_at, len(rows), "staged", None])
    key_fields = CONTRACTS[table]["key"]
    for row_no, row in enumerate(rows, 1):
        record_key = json.dumps({k: row[k] for k in key_fields}, ensure_ascii=False, sort_keys=True)
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
        con.execute("INSERT INTO stg_row VALUES (?,?,?,?)", [seq, row_no, record_key, payload])

    errors = validate_receipt(con, seq)
    if errors:
        message = "；".join(errors)
        con.execute("UPDATE receipt SET status='rejected', message=? WHERE receipt_seq=?",
                    [message, seq])
        return {"receipt_seq": seq, "status": "rejected", "rows": len(rows), "message": message}
    con.execute("UPDATE receipt SET status='posted' WHERE receipt_seq=?", [seq])
    return {"receipt_seq": seq, "status": "posted", "rows": len(rows), "message": ""}


def _decimal_places(value, max_places):
    return value == value.quantize(Decimal(1).scaleb(-max_places)) if value is not None else True


def validate_receipt(con, seq):
    """对“已入账事实 + 本批”做业务校验；返回错误列表，空列表表示可入账。"""
    try:
        facts = facts_mod.load_facts(con, extra=seq)
    except (ValueError, KeyError, TypeError) as exc:
        return ["事实还原失败：" + str(exc)]
    errors = []

    def check_period(period, ref):
        try:
            facts_mod.period_bounds(period)
        except ValueError:
            errors.append(f"{ref}: 期间格式须为 YYYY-MM（{period}）")
            return False
        return True

    for center in facts.centers.values():
        if center.kind not in CENTER_KINDS:
            errors.append(f"{center.center_id}: 未知中心类型 {center.kind}")
        if center.start is None or center.end is None or not center.start < center.end:
            errors.append(f"{center.center_id}: 起止区间无效（左闭右开且起<止）")

    for row in facts.costs:
        ref = row.cost_id
        if not check_period(row.period, ref):
            continue
        center = facts.centers.get(row.center_id)
        if center is None:
            errors.append(f"{ref}: 承担中心 {row.center_id} 不存在")
        elif not center.valid_in(row.period):
            errors.append(f"{ref}: 承担中心 {row.center_id} 在 {row.period} 不生效")
        elif center.kind == "idle":
            errors.append(f"{ref}: IDLE 是闲置责任，不承担原生费用")
        if row.cost_type not in COST_TYPES:
            errors.append(f"{ref}: 未知 cost_type {row.cost_type}")
        if row.kind not in COST_KINDS:
            errors.append(f"{ref}: 未知 kind {row.kind}")
        if row.amount is None or row.amount < 0:
            errors.append(f"{ref}: 金额缺失或为负")
        elif not _decimal_places(row.amount, 2):
            errors.append(f"{ref}: 金额须结算到分（{row.amount}）")
        if row.date is None:
            errors.append(f"{ref}: 缺日期")
        else:
            start, end = facts_mod.period_bounds(row.period)
            if not (start <= row.date < end):
                errors.append(f"{ref}: 日期 {row.date} 不在期间 {row.period} 内")

    for t in facts.transfers.values():
        ref = t.transfer_id
        if not check_period(t.period, ref):
            continue
        provider = facts.centers.get(t.provider)
        consumer = facts.centers.get(t.consumer)
        if provider is None:
            errors.append(f"{ref}: 供方 {t.provider} 不存在")
        elif provider.kind != "support":
            errors.append(f"{ref}: 供方 {t.provider} 不是保障部门")
        elif not provider.valid_in(t.period):
            errors.append(f"{ref}: 供方 {t.provider} 在 {t.period} 不生效")
        if consumer is None:
            errors.append(f"{ref}: 用方 {t.consumer} 不存在")
        elif not consumer.valid_in(t.period):
            errors.append(f"{ref}: 用方 {t.consumer} 在 {t.period} 不生效")
        elif consumer.kind == "idle":
            errors.append(f"{ref}: IDLE 不消耗服务量，不能作为用方")
        if t.provider == t.consumer:
            errors.append(f"{ref}: 供方对自身服务不重复消耗")
        if t.service_type not in UNITS:
            errors.append(f"{ref}: 未知计量类 {t.service_type}")
        for side, qty in (("provider", t.qty_provider), ("consumer", t.qty_consumer)):
            if qty is not None and (qty < 0 or not _decimal_places(qty, 3)):
                errors.append(f"{ref}: {side} 声明量无效（{qty}）")

    # 同一 transfer 双方各一条；同一供方同期只能有一种计量类（服务量不跨类型相加）。
    claim_sides = {}
    provider_types = {}
    transfer_heads = {}
    for row in con.execute(
            """SELECT s.payload FROM stg_row s JOIN receipt r USING (receipt_seq)
               WHERE r.stream='claims' AND (r.status='posted' OR s.receipt_seq=?)""", [seq]).fetchall():
        p = json.loads(row[0])
        side_key = (p["transfer_id"], p["side"])
        if side_key in claim_sides:
            errors.append(f"{p['transfer_id']}: 同一方重复声明（{p['side']}）")
        claim_sides[side_key] = True
        tkey = (p["provider"], p["period"])
        if tkey in provider_types and provider_types[tkey] != p["service_type"]:
            errors.append(f"{p['provider']} 在 {p['period']} 有多种计量类，服务量不能跨类型相加")
        provider_types[tkey] = p["service_type"]
        head = (p["provider"], p["consumer"], p["service_type"], p["period"])
        if p["transfer_id"] in transfer_heads and transfer_heads[p["transfer_id"]] != head:
            errors.append(f"{p['transfer_id']}: 同一服务的双方声明主数据不一致"
                          f"（供方/用方/类型/期间）")
        transfer_heads[p["transfer_id"]] = head
        if p["side"] not in SIDES:
            errors.append(f"{p['claim_id']}: 未知声明方 {p['side']}")

    for t in facts.transfers.values():
        if t.matched is not None and (t.matched < 0 or not _decimal_places(t.matched, 3)):
            errors.append(f"{t.transfer_id}: 共同确认量无效（{t.matched}）")

    for (provider, period), cap in facts.capacities.items():
        if not check_period(period, f"capacity:{provider}"):
            continue
        center = facts.centers.get(provider)
        if center is None or center.kind != "support":
            errors.append(f"capacity: 供方 {provider} 不是保障部门")
        elif not center.valid_in(period):
            errors.append(f"capacity: 供方 {provider} 在 {period} 不生效")
        total = sum(cap.shares.values(), Decimal(0))
        if total != 1:
            errors.append(f"capacity: {provider} {period} 容量份额合计须为 1（含 IDLE），当前 {total}")
        if IDLE_CENTER not in cap.shares:
            errors.append(f"capacity: {provider} {period} 容量行须含 IDLE 用方，"
                          "固定待命成本的闲置责任须显式存在")
        for consumer, share in cap.shares.items():
            if share < 0 or share > 1:
                errors.append(f"capacity: {provider}->{consumer} 容量份额越界（{share}）")
            target = facts.centers.get(consumer)
            if target is None:
                errors.append(f"capacity: 用方 {consumer} 不存在")
            elif not target.valid_in(period):
                errors.append(f"capacity: 用方 {consumer} 在 {period} 不生效")
        if cap.idle_variable is None or not (0 <= cap.idle_variable <= 1):
            errors.append(f"capacity: {provider} {period} 缺 idle_variable_share 或越界")

    # 共同确认必须指向已声明的服务。
    for row in con.execute(
            """SELECT s.payload FROM stg_row s JOIN receipt r USING (receipt_seq)
               WHERE r.stream='confirmations' AND (r.status='posted' OR s.receipt_seq=?)""",
            [seq]).fetchall():
        p = json.loads(row[0])
        if p["transfer_id"] not in facts.transfers:
            errors.append(f"{p['confirmation_id']}: 确认的服务 {p['transfer_id']} 无双方声明")

    return errors

"""仓表结构：只存放可反查事实与结转结果。

金额一律 DECIMAL(18,2)（结算到分），服务量 DECIMAL(18,3) 并单列单位，
金额与服务量不混作一列。原始行与接收历史永久保留（stg_row / receipt）。
"""
import duckdb

DDL = [
    """
    CREATE TABLE IF NOT EXISTS receipt (
      receipt_seq INTEGER PRIMARY KEY,   -- 接收次序，即事实可知顺序
      source_table VARCHAR NOT NULL,     -- 资料表名（如 later_confirmations）
      stream VARCHAR NOT NULL,           -- 事实流（centers/source_costs/claims/confirmations/capacity）
      source_label VARCHAR NOT NULL,     -- 来源标识（文件路径）
      sha256 VARCHAR NOT NULL,           -- 相同摘要不增加事实
      received_at VARCHAR NOT NULL,      -- 接收时刻 ISO +08:00
      rows_staged INTEGER NOT NULL,
      status VARCHAR NOT NULL,           -- posted / rejected / duplicate
      message VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stg_row (
      receipt_seq INTEGER NOT NULL,      -- 原始暂存行，随接收历史保留
      row_no INTEGER NOT NULL,
      record_key VARCHAR NOT NULL,       -- 记录键 JSON
      payload VARCHAR NOT NULL,          -- 整行 JSON（原文）
      PRIMARY KEY (receipt_seq, row_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_round (
      round_id INTEGER PRIMARY KEY,
      period VARCHAR NOT NULL,
      as_of_seq INTEGER NOT NULL,        -- 本轮所用事实的接收截点
      status VARCHAR NOT NULL,           -- provisional / confirmed / blocked / superseded
      created_at VARCHAR NOT NULL,
      input_hash VARCHAR NOT NULL,       -- 输入事实摘要，可核对
      note VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recognized_cost (
      round_id INTEGER NOT NULL,         -- 本轮认可的原生费用（仅 kind=expense）
      cost_id VARCHAR NOT NULL,
      center_id VARCHAR NOT NULL,
      cost_type VARCHAR NOT NULL,
      amount DECIMAL(18,2) NOT NULL,
      underlying_id VARCHAR NOT NULL,
      PRIMARY KEY (round_id, cost_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS center_cost (
      round_id INTEGER NOT NULL,
      center_id VARCHAR NOT NULL,
      kind VARCHAR NOT NULL,
      own_fixed DECIMAL(18,2) NOT NULL,      -- 自有原生 fixed
      own_variable DECIMAL(18,2) NOT NULL,   -- 自有原生 variable
      in_from_fixed DECIMAL(18,2) NOT NULL,  -- 转入（源自 fixed 池，可追到来源）
      in_from_variable DECIMAL(18,2) NOT NULL,-- 转入（源自 variable 池）
      alloc_out DECIMAL(18,2) NOT NULL,      -- 转出合计（保障部门）
      terminal_total DECIMAL(18,2) NOT NULL, -- 终端承担（临床/闲置）；保障部门为 0
      PRIMARY KEY (round_id, center_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_flow (
      round_id INTEGER NOT NULL,
      provider VARCHAR NOT NULL,
      consumer VARCHAR NOT NULL,         -- 含 IDLE（真实闲置责任，不是平衡塞数）
      pool VARCHAR NOT NULL,             -- fixed / variable
      service_type VARCHAR,              -- variable 池的计量类
      quantity DECIMAL(18,3),            -- 确认服务量；fixed 池与 IDLE 闲置行为 NULL
      unit VARCHAR,
      amount DECIMAL(18,2) NOT NULL,     -- 金额（元），与服务量分列
      share VARCHAR NOT NULL,            -- 精确份额（分数文本，可反查）
      PRIMARY KEY (round_id, provider, consumer, pool)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_flow_basis (
      round_id INTEGER NOT NULL,         -- 每条流转的分摊依据，可追到原服务范围
      provider VARCHAR NOT NULL,
      consumer VARCHAR NOT NULL,
      pool VARCHAR NOT NULL,
      basis_type VARCHAR NOT NULL,       -- transfer / capacity / idle_variable
      ref_id VARCHAR NOT NULL,           -- transfer_id / capacity_id / IDLE_VARIABLE
      quantity DECIMAL(18,3),
      unit VARCHAR,
      share VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS round_exception (
      round_id INTEGER NOT NULL,
      severity VARCHAR NOT NULL,         -- fatal / open / warning / info
      kind VARCHAR NOT NULL,             -- unverified_transfer / pending_cost / unclosable_loop / ...
      ref_id VARCHAR,
      detail VARCHAR NOT NULL            -- 可读说明（含双方声明与未达）
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_flow_consumer ON alloc_flow (round_id, consumer)",
    "CREATE INDEX IF NOT EXISTS idx_exception_round ON round_exception (round_id)",
]


def connect(path):
    """打开（必要时创建）仓库并建表。"""
    con = duckdb.connect(str(path))
    for statement in DDL:
        con.execute(statement)
    return con

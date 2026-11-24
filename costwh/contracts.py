"""资料字段契约：与 data/catalog.json 的表定义一一对应，入仓校验以此为准。

契约是仓库自身的入仓约定；data/catalog.json 描述当前样本文件。
tests 会核对两者保持一致，防止漂移。
"""

CONTRACTS = {
    "centers": {
        "key": ["center_id"],
        "columns": [
            {"name": "center_id", "type": "string", "nullable": True},
            {"name": "kind", "type": "string", "nullable": True},
            {"name": "start", "type": "date", "nullable": True},
            {"name": "end", "type": "date", "nullable": True},
        ],
    },
    "source_costs": {
        "key": ["cost_id"],
        "columns": [
            {"name": "cost_id", "type": "string", "nullable": True},
            {"name": "center_id", "type": "string", "nullable": True},
            {"name": "period", "type": "string", "nullable": True},
            {"name": "cost_type", "type": "string", "nullable": True},
            {"name": "kind", "type": "string", "nullable": True},
            {"name": "amount", "type": "number", "nullable": True},
            {"name": "underlying_id", "type": "string", "nullable": True},
            {"name": "date", "type": "date", "nullable": True},
        ],
    },
    "service_claims": {
        "key": ["claim_id"],
        "columns": [
            {"name": "claim_id", "type": "string", "nullable": True},
            {"name": "transfer_id", "type": "string", "nullable": True},
            {"name": "provider", "type": "string", "nullable": True},
            {"name": "consumer", "type": "string", "nullable": True},
            {"name": "service_type", "type": "string", "nullable": True},
            {"name": "period", "type": "string", "nullable": True},
            {"name": "side", "type": "string", "nullable": True},
            {"name": "quantity", "type": "number", "nullable": True},
        ],
    },
    "service_confirmations": {
        "key": ["confirmation_id"],
        "columns": [
            {"name": "confirmation_id", "type": "string", "nullable": True},
            {"name": "transfer_id", "type": "string", "nullable": True},
            {"name": "matched_quantity", "type": "number", "nullable": True},
            {"name": "confirmed_at", "type": "datetime", "nullable": True},
        ],
    },
    "capacity": {
        "key": ["capacity_id"],
        "columns": [
            {"name": "capacity_id", "type": "string", "nullable": True},
            {"name": "provider", "type": "string", "nullable": True},
            {"name": "consumer", "type": "string", "nullable": True},
            {"name": "period", "type": "string", "nullable": True},
            {"name": "capacity_share", "type": "number", "nullable": True},
            {"name": "idle_variable_share", "type": "number", "nullable": True},
        ],
    },
}
# later_confirmations 与 service_confirmations 同构，是同一事实流的后续接收文件。
CONTRACTS["later_confirmations"] = {
    "key": CONTRACTS["service_confirmations"]["key"],
    "columns": [dict(c) for c in CONTRACTS["service_confirmations"]["columns"]],
}

# 资料表 -> 仓内事实流。同一事实流内按记录键整行修订，接收次序即 receipt_seq。
STREAM = {
    "centers": "centers",
    "source_costs": "source_costs",
    "service_claims": "claims",
    "service_confirmations": "confirmations",
    "later_confirmations": "confirmations",
    "capacity": "capacity",
}

# 已知样本文件名 -> 资料表，用于接收时自动识别；未知文件名须显式 --table。
BASENAMES = {
    "centers.csv": "centers",
    "source_costs.csv": "source_costs",
    "service_claims.csv": "service_claims",
    "service_confirmations.csv": "service_confirmations",
    "later_confirmations.csv": "later_confirmations",
    "capacity.csv": "capacity",
}

# 服务量单位按类型独立，金额（元）与服务量永远不混列、不跨类型相加。
UNITS = {"power": "kWh", "laundry": "kg", "repair": "hour"}
CENTER_KINDS = {"support", "clinical", "idle"}
COST_TYPES = {"fixed", "variable"}
COST_KINDS = {"expense", "payment", "pending"}
SIDES = {"provider", "consumer"}
IDLE_CENTER = "IDLE"

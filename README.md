# 医院互供服务的科室成本结转仓

合成业务资料的入仓、双方服务核实、期间费用形成与结转确认。字段、单位和业务记录的含义见
[docs/资料口径.md](docs/资料口径.md)，仓表与口径实现见 [docs/成本仓架构.md](docs/成本仓架构.md)。
所有口径均为合成的内部管理约定，不作法定会计或医疗定价。

## 环境

Python 3.11.2；分析依赖已在 requirements.txt 固定版本。

    python3 -m pip install -r requirements.txt

## 资料自检（sourcepack，不解释业务）

    python3 -m sourcepack
    python3 -m unittest tests.test_sources -v

检查入口核对文件摘要、字段类型、记录键、引用与行数，不进行估计、推荐、批准或结算。
文件自检成功不等于业务结论成立。

## 成本仓（costwh）

全流程演示（装载 → 暂结 → 月结后补载共同认可 → 确认 → 差异/下钻/追踪）：

    python3 -m costwh --db warehouse.duckdb demo

手工分步：

    python3 -m costwh --db warehouse.duckdb receive data/centers.csv --received-at 2026-10-25T09:00:00+08:00
    python3 -m costwh --db warehouse.duckdb receive data/source_costs.csv --received-at 2026-10-25T09:00:00+08:00
    python3 -m costwh --db warehouse.duckdb receive data/service_claims.csv --received-at 2026-10-25T09:00:00+08:00
    python3 -m costwh --db warehouse.duckdb receive data/service_confirmations.csv --received-at 2026-10-25T09:00:00+08:00
    python3 -m costwh --db warehouse.duckdb receive data/capacity.csv --received-at 2026-10-25T09:00:00+08:00
    python3 -m costwh --db warehouse.duckdb close --period 2026-10            # T8 未核实 → 暂结
    python3 -m costwh --db warehouse.duckdb receive data/later_confirmations.csv --received-at 2026-10-28T09:00:00+08:00
    python3 -m costwh --db warehouse.duckdb close --period 2026-10            # T8=9 共同认可 → 确认（旧轮保留）

查询：

    python3 -m costwh --db warehouse.duckdb total      --period 2026-10       # 全院总额核对
    python3 -m costwh --db warehouse.duckdb report     --period 2026-10 --center WARD1
    python3 -m costwh --db warehouse.duckdb report     --period 2026-10 --center REPAIR
    python3 -m costwh --db warehouse.duckdb trace      --cost C2              # 原始支出 → 终端去向
    python3 -m costwh --db warehouse.duckdb transfers  --period 2026-10       # 双方核实一览
    python3 -m costwh --db warehouse.duckdb exceptions --period 2026-10       # 未达/待核/不可吸收
    python3 -m costwh --db warehouse.duckdb diff       --period 2026-10       # 新旧轮差异与牵连范围
    python3 -m costwh --db warehouse.duckdb receipts                          # 接收历史
    python3 -m costwh --db warehouse.duckdb rounds    --period 2026-10

关键保证：

- 原生费用只在实际承担中心记一次（expense 才入池，payment 不重复加，pending 仅待核且可见）；
  内部转入始终标记为转入，不会成为新的原生支出。
- 双方声明与未达分别保留，缺回执不是零，共同认可量不取 min、不折中；存在未核实服务量时只能暂结。
- 固定待命成本按容量份额（含 IDLE 行）分担；可变成本与全部转入按确认服务量和闲置比例分担，
  空闲能力显式留在 IDLE，不分光给有用量的临床科室。
- 互供服务联立求解，与处理顺序无关；无临床/IDLE 吸收出口的环路阻断，不造平衡科室、不丢残差。
- 金额结算到分且全院总额精确守恒；金额（元）与服务量（带单位）分列，不跨计量类型相加。
- 每轮结转绑定事实接收截点与输入哈希，旧轮不可变；新资料只通过新一轮生效，差异与受牵连中心可查。

## 测试

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack costwh tests

`tests/test_sources.py` 覆盖资料容器与输入诊断；`tests/test_costwh.py` 覆盖装载拒收、双方核实、
互供联解、IDLE 保留、守恒、尾差、阻断、轮次不可变、差异牵连与下钻查询。运行不需要访问外部业务系统。

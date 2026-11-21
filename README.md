# Load Ledger · 清运过磅数据仓

把清运站不同系统导出的过磅票据导入同一个 SQLite 数据仓。过磅单迟报、修订或作废后，日报只重算受影响的 UTC 日期和站点，旧版票据及其来源行仍可查阅。南站更换地磅后，重车联（毛重）与空车联（皮重）由两台设备分别导出，仓库同时接收整行票据与单联记录。样例为合成数据。仓库地址沿用原地址，当前代码不再提供旧版音频矩阵接口。

## 快速开始

需要 Python 3.9+（SQLite 3.24+），无第三方依赖。选用一个新的数据库路径：

```sh
python3 -B -m loadledger --db demo.sqlite ingest --batch opening --source station-export --format csv-kg-v1 examples/first.csv
python3 -B -m loadledger --db demo.sqlite ingest --batch fixes --source audit-desk --format jsonl-g-v2 examples/corrections.jsonl
python3 -B -m loadledger --db demo.sqlite ingest --batch gross --source slip-gross --format jsonl-slip-v1 examples/slips_gross.jsonl
python3 -B -m loadledger --db demo.sqlite ingest --batch tare --source slip-tare --format jsonl-slip-v1 examples/slips_tare.jsonl
python3 -B -m loadledger --db demo.sqlite report --start 2025-06-01 --end 2025-06-03
python3 -B -m loadledger --db demo.sqlite trace --site south --ticket S-101
python3 -B examples/smoke.py
```

`report` 的结束日期不含当天；加 `--site` 可查单站。报告按日期、站点和物料汇总有效票数与净重（整数克），不输出空分组。整行票与成票的单联轮次在同一分区按物料合并。`trace` 返回当前版和历史各版的来源系统、批次、行号、文件摘要；单联票则按称重轮次展开重车联、空车联各自的修订与来源行，并给出当前轮次的成票净重或未成票原因。作废最后一版仍能查阅。

### 单联成票规则

单联记录按 `(站点, 票号, 轮次, 联别, 修订号)` 存储，每联各自以最高修订生效；同修订业务值相同则保留各自的来源设备证据，值不同则整批拒绝。只有同站点、票号、轮次的两张有效联才能成票，且空车时刻不早于重车时刻、两次称量至多相隔六小时、重车重量不小于空车重量。净重为两联之差，归日和物料取重车联。以同站点票号已见的最高轮次为准：新一轮只到一联或不满足条件时，旧轮次的净重移出日报；旧轮次后补也不能顶回现行票。整行模式与单联模式在同一站点票号上互斥，不能混用。

成功时输出 JSON；文件不合合同或版本冲突时退出码为 2，错误输出到标准错误。单批文件上限 16 MiB。该工具面向离线部署，不提供联网采集服务。

详细字段、票据版本规则和存储分层见 [项目说明](PROJECT_SPEC.md)。

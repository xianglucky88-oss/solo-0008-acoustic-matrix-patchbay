# Load Ledger · 清运过磅数据仓

把清运站不同系统导出的过磅票据导入同一个 SQLite 数据仓。过磅单迟报、修订或作废后，日报只重算受影响的 UTC 日期和站点，旧版票据及其来源行仍可查阅。样例为合成数据。仓库地址沿用原地址，当前代码不再提供旧版音频矩阵接口。

## 快速开始

需要 Python 3.9+（SQLite 3.24+），无第三方依赖。选用一个新的数据库路径：

```sh
python3 -B -m loadledger --db demo.sqlite ingest --batch opening --source station-export --format csv-kg-v1 examples/first.csv
python3 -B -m loadledger --db demo.sqlite ingest --batch fixes --source audit-desk --format jsonl-g-v2 examples/corrections.jsonl
python3 -B -m loadledger --db demo.sqlite report --start 2025-06-01 --end 2025-06-03
python3 -B -m loadledger --db demo.sqlite trace --site south --ticket T-41
python3 -B examples/smoke.py
```

`report` 的结束日期不含当天；加 `--site` 可查单站。报告按日期、站点和物料汇总有效票数与净重（整数克），不输出空分组。`trace` 返回当前版和历史各版的来源系统、批次、行号、文件摘要；作废最后一版仍能查阅。

成功时输出 JSON；文件不合合同或版本冲突时退出码为 2，错误输出到标准错误。单批文件上限 16 MiB。该工具面向离线部署，不提供联网采集服务。

详细字段、票据版本规则和存储分层见 [项目说明](PROJECT_SPEC.md)。

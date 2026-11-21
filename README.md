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
python3 -B examples/settlement_smoke.py
```

`report` 的结束日期不含当天；加 `--site` 可查单站。报告按日期、站点和物料汇总有效票数与净重（整数克），不输出空分组。`trace` 返回当前版和历史各版的来源系统、批次、行号、文件摘要；作废最后一版仍能查阅。

## 月底清运结算

先冻结当期合同清册（最低/最高认可克数、允许站点物料、UTC 半开称重区间），再从当前有效票据生成可签收分配单：

```sh
python3 -B -m loadledger --db demo.sqlite roster examples/settlement-roster.json
python3 -B -m loadledger --db demo.sqlite propose --doc s1 --roster roster-2025-06
python3 -B -m loadledger --db demo.sqlite sign --doc s1                 # 或 --plan C-mixed:max
python3 -B -m loadledger --db demo.sqlite doc --doc s1
```

优化先最小化各合同低于最低量的缺口之和，再最大化认可净重；一张票的整数克可拆给多个合格合同，未分配克数也会列出。每个合同给出在所有同样最优分配中的认可克数范围，并附达到两端的可复核分配；始终短缺的合同会显式标注。签收会冻结清册、候选修订指纹和所选方案，候选票据被修订、作废或失去资格后，即使站点物料净重不变，旧提案也只能标记 `mark-expired`，不能签收。

更正或作废后用同一清册另出重核单，旧单只读保留、不会被覆盖：

```sh
python3 -B -m loadledger --db demo.sqlite reevaluate --doc s2 --parent s1
python3 -B -m loadledger --db demo.sqlite doc-diff --doc s2
python3 -B -m loadledger --db demo.sqlite doc-lineage --doc s2
```

重核在满足前两个全局目标后尽可能保留旧单“同站点票号给同一合同”的克数；增减明细区分因票据无效或不再合格而完全不能保留的分配与其余未保留克数，两版克数都能查回有效修订和批次原行。

成功时输出 JSON；文件不合合同或版本冲突时退出码为 2，错误输出到标准错误。单批文件上限 16 MiB。该工具面向离线部署，不提供联网采集服务。

详细字段、票据版本规则和存储分层见 [项目说明](PROJECT_SPEC.md)。

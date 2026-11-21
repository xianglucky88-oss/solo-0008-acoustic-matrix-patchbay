# 清运过磅数据仓：基线契约

站点数据运维导入称重票，日报审核员查看跨站可比的净重与票数，需要时追查某条修订出自哪个批次和哪一行。

## 文件输入

两种输入均为 UTF-8。`csv-kg-v1` 要求固定字段集，列可重排：`site,ticket,revision,operation,weighed_at,material,gross_kg,tare_kg`。公斤值最多三位小数，不经过浮点数。`jsonl-g-v2` 每个非空物理行一个对象，前六字段相同，重量列使用整数 `gross_g,tare_g`。站点、票号、批次号、来源号标识使用 ASCII 字母数字开头，允许点、下划线和连字符，最大 64 字符。修订号为正的 32 位整数。

| 操作 | 字段规则 |
| --- | --- |
| `upsert` | ISO 时间到秒且含显式偏移；物料为 `organics`、`recyclables` 或 `residual`；皮重非负且小于毛重，毛重不超过 200 000 千克。 |
| `void` | 只保留站点、票号、修订号和操作，CSV 其余列留空，JSONL 不传其余字段。 |

## 存储与增量语义

- 来源层 `source_batch` 按批次 ID 记录来源、文件合同、原文件 SHA-256 和实际导入行数；`source_row` 记录每次批次中原始行与标准票据版本的映射。批次 ID 被同一来源、格式和字节再次调用时是空操作，绑定信息不同则拒绝。行号是物理行号，CSV 多行记录用结束行号。
- 标准层 `ticket_version` 使用 `(site, ticket, revision)`，保存 UTC 时间、整数克毛重和皮重。同一版本归一化内容一致时只添加该次来源证据；内容不一致时整批回滚。不同站点同票号不合并。较低修订仍保留，高修订覆盖当前版；高修订作废同时防止迟到的低修订重新生效。
- 当前层 `ticket_head` 和日报 `daily_rollup` 与导入在同一写事务中提交。对每张票只比较批次前后的当前版本；如跨日期修订，旧、新两个站点日分区都重算。按分区聚合物料、票数与净重。没有改动的分区不重建。
- `trace` 在同一个读事务中获取当前版本、历史版本和来源行。数据库不储存原文件正文；来源号、原文件摘要和行号用于回到外部归档核对。

## 纸票票簿核销与交档

- 领用清单合同 `allocation-csv-v1` 为固定列 CSV：`site,booklet,first_print,last_print`，闭区间且 `1 <= first_print <= last_print`。同站点同票簿的区间在批次内或跨批次重叠都会整批拒绝；区间相邻或留空隔都合法，空隔中的号不是预发号、不算缺号。
- 纸票过磅合同 `jsonl-paper-v3` 为 JSONL，操作取 `upsert`、`void` 或 `blank_return`。前两者携带 `ticket,revision,booklet,print_no`，重量与时间沿用 `jsonl-g-v2` 规则；`blank_return`（未使用的作废空白联交回）只允许 `site,operation,booklet,print_no`。纸票过磅行同时写入标准票据层和只追加的 `print_declaration`；空白交回只写声明。印刷号可在不同票簿（含隔月重新编号的票簿）中重复，位置始终由 `(site, booklet, print_no)` 识别。
- 旧合同 `csv-kg-v1`、`jsonl-g-v2` 不含簿号，照常入标准层并参与日报与追溯，但不产生占号声明，不能证明任何票簿齐套。
- 声明永不被后续行覆盖。改重（更高修订）和 `void` 都保留占号；同票重复声明同一原号只算一个位置。以下三类为争议，涉及的**预发**印刷号均不得验收：`contested`（同位置两张不同票）、`ticket_split`（同票在同站点任意票簿占两个或更多位置，跨票簿也算）、`used_vs_blank`（同位置同时有过磅声明和空白交回）。争议条目保留所有相关声明的原始批次与物理行；落在预发区间外的声明单列 `position_not_issued` 异常，不阻塞该簿验收。
- 每个预发号的结论为可靠核销、争议或缺号。缺号仅统计完全无声明的预发号，并压缩成连续段输出，大段缺号不逐号展开。`booklet` 视图返回预发区间、缺号段、争议及越界异常；`booklets` 汇总所有出现过的票簿。
- 交档凭据在单个写事务的同一已提交仓库视图内签发，前提是该册全部预发号可靠核销（无缺号、无区间内争议）。`certificate_evidence` 冻结签发时每个号的核销证据（批次、行、票、操作及 used/blank 归类）。同一票簿重复申请不发第二份，返回原凭据；凭据不存在或当前处于 `pending_review` 时申请失败。
- 任何新批次的导入或新领用区间登记后，已签发凭据会在同一事务内复核：票簿不再满足全量核销时状态转为 `pending_review`，冻结的证据保持不变。`certificate` 命令只读查看凭据及其证据快照。

## 源码职责

- `loadledger/intake.py` 定义来源合同与归一化。
- `loadledger/storage.py` 拥有分层表、索引和连接事务。
- `loadledger/warehouse.py` 管理批次去重、版本冲突与受影响分区。
- `loadledger/mart.py` 重算、查询日报分区。
- `loadledger/lineage.py` 展开来源行和历史版本。
- `loadledger/archive.py` 计算票簿核销、争议与缺号段，签发并复核交档凭据。
- `loadledger/__main__.py` 提供摄取、日报、追溯与交档入口。

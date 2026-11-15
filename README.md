# 工资支付单 · 银行分拆到账回执对账

把银行分拆到账回执归集到工资支付单，生成可入仓的实付依据。
FastAPI + SQLite 单容器应用，含浏览器操作页面与 REST API。

## 业务规则

- 同月最多导入 **8 张支付单、16 条回执**；单据/回执均含唯一编号、员工号、正整数金额（单位：分）。
- 每张支付单可由 **同一员工 1～3 条** 回执组成，回执金额之和必须等于支付单应付额；每条回执全局只能使用一次。
- 建议算法先求「完整匹配支付单数量最多」的最优解（最大基数匹配，记忆化 DFS），
  再用禁止法求所有最优方案的**配对交集**：交集内的标记为「确定」，其余有合法组合的标「待核」，无合法组合的单列展示。
- 网页可勾选确定/待核组合后整组确认，并下载已确认实付依据 CSV（UTF-8 BOM，Excel 可直接打开）。
- 确认后的支付单与回执立即退出匹配池；补录回执后建议按当前开放数据重新计算，其他员工的补录不改变既有确认。
- 确认接口在 `BEGIN IMMEDIATE` 事务内拿锁后重新校验：双窗口并发确认不会重复占用回执，**整组成功或全不改**；失败与重启后已确认依据持久保留。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 浏览器入口
open http://localhost:8000/
```

SQLite 数据文件保存在命名卷 `recon-data`（容器内 `/data/recon.db`），容器重启数据不丢。

## 本地开发启动

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://localhost:8000/
```

环境变量 `RECON_DB_PATH` 可覆盖数据库路径（默认 `data/recon.db`）。

## 页面操作

1. 「载入演示数据（2026-10）」一键导入 5 张支付单 + 11 条回执：
   - 3 个确定配对（含 2 条、1 条、3 条回执三种形态）；
   - E004 存在两种等价 4000 组合 → 全部列为「待核」，不进确定区；
   - E005 无回执（待补录），另有一条 E999 孤立回执。
2. 「演示：补录 E005 回执」可观察建议自动重算，PO20261005 进入确定区，其他员工结论不变。
3. 勾选配对 → 「确认所选配对」；到「已确认实付依据」区下载 CSV。
4. 也可用 `data/sample_orders.csv`、`data/sample_receipts.csv` 走 CSV 导入流程。

## REST API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 浏览器页面 |
| GET | `/api/state` | 当前建议 + 已确认依据 + 全量单据/回执 |
| POST | `/api/import/orders` | 上传支付单 CSV（multipart 字段 `file`） |
| POST | `/api/import/receipts` | 上传回执 CSV（支持补录） |
| POST | `/api/import/orders-json` | JSON 导入支付单 `{"orders":[{order_no,employee_no,amount,period?}]}` |
| POST | `/api/import/receipts-json` | JSON 导入回执 `{"receipts":[{receipt_no,employee_no,amount,period?}]}` |
| POST | `/api/confirm` | 整组确认 `{"items":[{"order_no","receipt_nos":[...]}]}`，冲突返回 409 且全不改 |
| GET | `/api/evidence.csv` | 下载已确认实付依据（无确认记录时 404） |
| POST | `/api/demo` | 载入演示数据（先清空） |
| POST | `/api/demo/supplement` | 演示补录 E005 回执 |
| POST | `/api/reset` | 清空全部数据 |

## 自测

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q
```

- `tests/test_matching.py`：300 组随机数据用全量暴力枚举做 oracle，校验最大匹配数、最优方案交集（确定配对）、待核/不可配分类及确认校验规则。
- `tests/test_api.py`：导入上限与校验、确定/人工确认、冲突整组回滚、补录重算、重启持久化、CSV 导入与依据下载、双线程并发确认不重复占用。

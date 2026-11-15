"""导入数据校验与演示数据。

支付单字段：order_no（唯一编号）、employee_no（员工号）、amount（正整数，分）
回执字段：  receipt_no（唯一编号）、employee_no（员工号）、amount（正整数，分）
另支持可选 period（所属月份，YYYY-MM），缺省落到同一默认月份。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
REQUIRED_ORDER_FIELDS = ("order_no", "employee_no", "amount")
REQUIRED_RECEIPT_FIELDS = ("receipt_no", "employee_no", "amount")


class ImportError_(ValueError):
    """导入数据不合法。"""


@dataclass
class ParsedRecord:
    no: str
    employee_no: str
    amount: int
    period: str


def _coerce_amount(raw, where: str) -> int:
    if isinstance(raw, bool):  # bool 是 int 子类，显式拒绝
        raise ImportError_(f"{where} 金额必须是正整数（分）")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s or not re.fullmatch(r"\d+", s):
            raise ImportError_(f"{where} 金额必须是正整数（分），收到 {raw!r}")
        value = int(s)
    else:
        raise ImportError_(f"{where} 金额必须是正整数（分），收到 {raw!r}")
    if value <= 0:
        raise ImportError_(f"{where} 金额必须为正整数，收到 {value}")
    return value


def _validate_common(rows, required: Sequence[str], no_field: str, kind: str) -> List[ParsedRecord]:
    if not isinstance(rows, list) or not rows:
        raise ImportError_(f"{kind}数据必须是非空数组")
    parsed: List[ParsedRecord] = []
    seen: set = set()
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ImportError_(f"{kind}第 {idx} 行必须是对象")
        missing = [f for f in required if f not in row or row[f] in (None, "")]
        if missing:
            raise ImportError_(f"{kind}第 {idx} 行缺少字段：{', '.join(missing)}")
        no = str(row[no_field]).strip()
        emp = str(row["employee_no"]).strip()
        if not no or not emp:
            raise ImportError_(f"{kind}第 {idx} 行编号/员工号不能为空")
        if no in seen:
            raise ImportError_(f"{kind}编号重复：{no}")
        seen.add(no)
        period = str(row.get("period") or "2026-10").strip()
        if not PERIOD_RE.fullmatch(period):
            raise ImportError_(f"{kind} {no} 的 period 须为 YYYY-MM 格式")
        parsed.append(ParsedRecord(no, emp, _coerce_amount(row["amount"], f"{kind} {no}"), period))
    return parsed


def validate_orders_payload(payload) -> List[ParsedRecord]:
    if not isinstance(payload, dict) or "orders" not in payload:
        raise ImportError_("请求体须为含 orders 数组的对象")
    return _validate_common(payload["orders"], REQUIRED_ORDER_FIELDS, "order_no", "支付单")


def validate_receipts_payload(payload) -> List[ParsedRecord]:
    if not isinstance(payload, dict) or "receipts" not in payload:
        raise ImportError_("请求体须为含 receipts 数组的对象")
    return _validate_common(payload["receipts"], REQUIRED_RECEIPT_FIELDS, "receipt_no", "回执")


def parse_csv(text: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportError_("CSV 内容为空或缺少表头")
    rows: List[Dict[str, str]] = []
    for row in reader:
        rows.append({k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
    return rows


def check_periods(orders: Sequence[ParsedRecord], receipts: Sequence[ParsedRecord]) -> None:
    """同月约束：同一批次导入的数据 period 必须一致；与库内数据也须同月。"""
    periods = {r.period for r in list(orders) + list(receipts)}
    if len(periods) > 1:
        raise ImportError_(f"数据跨月：{sorted(periods)}，同一批次只能处理同一个月的数据")


# ---------------------------------------------------------------- 演示数据

def demo_orders() -> List[Dict]:
    """2026-10 月演示支付单（5 张，未超 8 张上限）。"""
    return [
        {"order_no": "PO20261001", "employee_no": "E001", "amount": 7000, "period": "2026-10"},
        {"order_no": "PO20261002", "employee_no": "E002", "amount": 5000, "period": "2026-10"},
        {"order_no": "PO20261003", "employee_no": "E003", "amount": 6000, "period": "2026-10"},
        {"order_no": "PO20261004", "employee_no": "E004", "amount": 4000, "period": "2026-10"},
        {"order_no": "PO20261005", "employee_no": "E005", "amount": 3000, "period": "2026-10"},
    ]


def demo_receipts() -> List[Dict]:
    """2026-10 月演示回执（9 条，未超 16 条上限）。

    - E001：3000+4000 恰好匹配 PO...001（确定配对）
    - E002：5000 整笔匹配 PO...002（确定配对）
    - E003：2000+2000+2000 三条匹配 PO...003（确定配对）
    - E004：1500+2500 与 1000+3000 两种组合都能匹配 PO...004（歧义 → 待核）
    - RC20261099：E999 孤立回执，无法匹配
    - E005 暂无回执，PO...005 待补录（演示"补录后重算"）
    """
    return [
        {"receipt_no": "RC20261001", "employee_no": "E001", "amount": 3000, "period": "2026-10"},
        {"receipt_no": "RC20261002", "employee_no": "E001", "amount": 4000, "period": "2026-10"},
        {"receipt_no": "RC20261003", "employee_no": "E002", "amount": 5000, "period": "2026-10"},
        {"receipt_no": "RC20261004", "employee_no": "E003", "amount": 2000, "period": "2026-10"},
        {"receipt_no": "RC20261005", "employee_no": "E003", "amount": 2000, "period": "2026-10"},
        {"receipt_no": "RC20261006", "employee_no": "E003", "amount": 2000, "period": "2026-10"},
        {"receipt_no": "RC20261007", "employee_no": "E004", "amount": 1500, "period": "2026-10"},
        {"receipt_no": "RC20261008", "employee_no": "E004", "amount": 2500, "period": "2026-10"},
        {"receipt_no": "RC20261009", "employee_no": "E004", "amount": 1000, "period": "2026-10"},
        {"receipt_no": "RC20261010", "employee_no": "E004", "amount": 3000, "period": "2026-10"},
        {"receipt_no": "RC20261099", "employee_no": "E999", "amount": 999, "period": "2026-10"},
    ]


def demo_supplement_receipt() -> Dict:
    """补录 E005 的回执：3000 整笔，补录后 PO...005 成为新的确定配对。"""
    return {"receipt_no": "RC20261011", "employee_no": "E005", "amount": 3000, "period": "2026-10"}

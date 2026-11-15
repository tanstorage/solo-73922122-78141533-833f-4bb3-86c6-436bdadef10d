"""FastAPI 对账服务入口。"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from . import db
from .matching import (
    Order,
    Receipt,
    suggest,
    validate_confirmation,
)
from .validation import (
    ImportError_,
    demo_orders,
    demo_receipts,
    demo_supplement_receipt,
    parse_csv,
    validate_orders_payload,
    validate_receipts_payload,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(title="工资支付单-银行回执对账", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------- 数据装配

def _open_sets(conn: sqlite3.Connection):
    order_rows = db.fetch_open_orders(conn)
    receipt_rows = db.fetch_open_receipts(conn)
    orders = [Order(r["order_no"], r["employee_no"], r["amount"]) for r in order_rows]
    receipts = [Receipt(r["receipt_no"], r["employee_no"], r["amount"]) for r in receipt_rows]
    return orders, receipts


def _suggestion_payload(conn: sqlite3.Connection) -> dict:
    orders, receipts = _open_sets(conn)
    suggestion = suggest(orders, receipts)
    order_map = {o.order_no: {"employee_no": o.employee_no, "amount": o.amount} for o in orders}
    receipt_map = {r.receipt_no: {"employee_no": r.employee_no, "amount": r.amount} for r in receipts}

    def enrich_order(order_no):
        info = order_map[order_no]
        return {"order_no": order_no, **info}

    def enrich_combo(receipt_nos):
        return [
            {"receipt_no": rn, **receipt_map[rn]} for rn in receipt_nos if rn in receipt_map
        ]

    certain = [
        {
            "order": enrich_order(order_no),
            "receipts": enrich_combo(receipt_nos),
        }
        for order_no, receipt_nos in sorted(suggestion["certain"].items())
    ]
    pending = [
        {
            "order": enrich_order(order_no),
            "combos": [enrich_combo(combo) for combo in combos],
        }
        for order_no, combos in sorted(suggestion["pending"].items())
    ]
    unmatched = [enrich_order(no) for no in suggestion["unmatched"]]
    unused = [
        {"receipt_no": rn, **receipt_map[rn]}
        for rn in suggestion["unused_receipts"]
        if rn in receipt_map
    ]
    return {
        "max_matched": suggestion["max_matched"],
        "certain": certain,
        "pending": pending,
        "unmatched": unmatched,
        "unused_receipts": unused,
        "counts": {
            "open_orders": len(orders),
            "open_receipts": len(receipts),
            "max_orders": db.MAX_ORDERS,
            "max_receipts": db.MAX_RECEIPTS,
        },
    }


def _confirmed_payload(conn: sqlite3.Connection) -> List[dict]:
    evidence = db.fetch_evidence(conn)
    groups: Dict[int, dict] = {}
    for row in evidence:
        g = groups.setdefault(
            row["confirmation_id"],
            {
                "confirmation_id": row["confirmation_id"],
                "confirmed_at": row["confirmed_at"],
                "items": {},
            },
        )
        item = g["items"].setdefault(
            row["order_no"],
            {
                "order_no": row["order_no"],
                "employee_no": row["employee_no"],
                "order_amount": row["order_amount"],
                "receipts": [],
            },
        )
        item["receipts"].append(
            {"receipt_no": row["receipt_no"], "amount": row["receipt_amount"]}
        )
    result = []
    for g in groups.values():
        g["items"] = sorted(g["items"].values(), key=lambda x: x["order_no"])
        for item in g["items"]:
            item["receipts"].sort(key=lambda x: x["receipt_no"])
        result.append(g)
    result.sort(key=lambda g: g["confirmation_id"])
    return result


# --------------------------------------------------------------- 路由：页面

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


# --------------------------------------------------------------- 路由：查询

@app.get("/api/state")
def get_state() -> dict:
    conn = db.connect()
    try:
        return {
            "suggestion": _suggestion_payload(conn),
            "confirmed": _confirmed_payload(conn),
            "orders": [dict(r) for r in db.fetch_all_orders(conn)],
            "receipts": [dict(r) for r in db.fetch_all_receipts(conn)],
        }
    finally:
        conn.close()


# --------------------------------------------------------------- 路由：导入

@app.post("/api/import/orders")
async def import_orders(file: Optional[UploadFile] = File(default=None)) -> dict:
    """导入支付单。支持 JSON（application/json 走 /api/import/orders-json）或 CSV 上传。"""
    if file is None:
        raise HTTPException(status_code=400, detail="请上传 CSV 文件，或调用 JSON 接口")
    raw = (await file.read()).decode("utf-8-sig")
    try:
        payload = {"orders": parse_csv(raw)}
        records = validate_orders_payload(payload)
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_orders(records)


class OrdersJson(BaseModel):
    orders: List[dict] = Field(min_length=1)


@app.post("/api/import/orders-json")
def import_orders_json(body: OrdersJson) -> dict:
    try:
        records = validate_orders_payload(body.model_dump())
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_orders(records)


def _persist_orders(records) -> dict:
    conn = db.connect()
    try:
        existing = db.count_orders(conn)
        if existing + len(records) > db.MAX_ORDERS:
            raise HTTPException(
                status_code=400,
                detail=f"同月支付单上限 {db.MAX_ORDERS} 张：已有 {existing} 张，本次 {len(records)} 张",
            )
        existing_periods = {
            r[0] for r in conn.execute("SELECT DISTINCT period FROM payment_orders")
        }
        incoming_periods = {r.period for r in records}
        if existing_periods and incoming_periods != existing_periods:
            raise HTTPException(
                status_code=400,
                detail=f"月份不一致：库内为 {sorted(existing_periods)}，本次为 {sorted(incoming_periods)}",
            )
        now = time.time()
        try:
            with db.immediate_transaction(conn):
                db.insert_orders(
                    conn, [(r.no, r.employee_no, r.amount, r.period, now) for r in records]
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"支付单编号冲突：{exc}")
        return {"imported": len(records), "state": _state_snapshot(conn)}
    finally:
        conn.close()


@app.post("/api/import/receipts")
async def import_receipts(file: Optional[UploadFile] = File(default=None)) -> dict:
    if file is None:
        raise HTTPException(status_code=400, detail="请上传 CSV 文件，或调用 JSON 接口")
    raw = (await file.read()).decode("utf-8-sig")
    try:
        payload = {"receipts": parse_csv(raw)}
        records = validate_receipts_payload(payload)
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_receipts(records)


class ReceiptsJson(BaseModel):
    receipts: List[dict] = Field(min_length=1)


@app.post("/api/import/receipts-json")
def import_receipts_json(body: ReceiptsJson) -> dict:
    try:
        records = validate_receipts_payload(body.model_dump())
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_receipts(records)


def _persist_receipts(records) -> dict:
    conn = db.connect()
    try:
        existing = db.count_receipts(conn)
        if existing + len(records) > db.MAX_RECEIPTS:
            raise HTTPException(
                status_code=400,
                detail=f"同月回执上限 {db.MAX_RECEIPTS} 条：已有 {existing} 条，本次 {len(records)} 条",
            )
        existing_periods = {
            r[0] for r in conn.execute("SELECT DISTINCT period FROM receipts")
        }
        incoming_periods = {r.period for r in records}
        if existing_periods and incoming_periods != existing_periods:
            raise HTTPException(
                status_code=400,
                detail=f"月份不一致：库内为 {sorted(existing_periods)}，本次为 {sorted(incoming_periods)}",
            )
        now = time.time()
        try:
            with db.immediate_transaction(conn):
                db.insert_receipts(
                    conn, [(r.no, r.employee_no, r.amount, r.period, now) for r in records]
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"回执编号冲突：{exc}")
        return {"imported": len(records), "state": _state_snapshot(conn)}
    finally:
        conn.close()


def _state_snapshot(conn: sqlite3.Connection) -> dict:
    return {"suggestion": _suggestion_payload(conn), "confirmed": _confirmed_payload(conn)}


# --------------------------------------------------------------- 路由：确认

class ConfirmItem(BaseModel):
    order_no: str
    receipt_nos: List[str] = Field(min_length=1, max_length=3)


class ConfirmRequest(BaseModel):
    items: List[ConfirmItem] = Field(min_length=1)


@app.post("/api/confirm")
def confirm(body: ConfirmRequest) -> dict:
    """整组确认：全部合法才落库，任一回执被占用则整体失败、数据不变。"""
    pairing = {item.order_no: item.receipt_nos for item in body.items}
    if len(pairing) != len(body.items):
        raise HTTPException(status_code=400, detail="支付单在请求中重复出现")
    conn = db.connect()
    try:
        with db.immediate_transaction(conn):
            # 拿锁后重新读取，保证并发确认下不重复占用回执
            orders, receipts = _open_sets(conn)
            all_receipts = [
                Receipt(r["receipt_no"], r["employee_no"], r["amount"])
                for r in db.fetch_all_receipts(conn)
            ]
            used = db.used_receipt_nos(conn)
            ok, reason = validate_confirmation(
                pairing, orders, receipts, frozenset(used), all_receipts
            )
            if not ok:
                raise HTTPException(status_code=409, detail=reason)
            confirmation_id = db.save_confirmation(conn, pairing, time.time())
        return {
            "confirmation_id": confirmation_id,
            "confirmed_items": len(pairing),
            "state": _state_snapshot(conn),
        }
    finally:
        conn.close()


# --------------------------------------------------------------- 路由：依据下载

@app.get("/api/evidence.csv")
def download_evidence_csv() -> Response:
    conn = db.connect()
    try:
        rows = db.fetch_evidence(conn)
        if not rows:
            raise HTTPException(status_code=404, detail="尚无已确认依据，请先确认配对")
        buf = io.StringIO()
        buf.write("﻿")  # UTF-8 BOM，便于 Excel 直接打开
        writer = csv.writer(buf)
        writer.writerow(
            [
                "确认批次",
                "确认时间",
                "支付单号",
                "员工号",
                "应付金额(分)",
                "回执编号",
                "回执金额(分)",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["confirmation_id"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["confirmed_at"])),
                    r["order_no"],
                    r["employee_no"],
                    r["order_amount"],
                    r["receipt_no"],
                    r["receipt_amount"],
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=evidence.csv"},
        )
    finally:
        conn.close()


# --------------------------------------------------------------- 路由：演示/重置

@app.post("/api/demo")
def load_demo() -> dict:
    conn = db.connect()
    try:
        db.reset_db(conn)
        now = time.time()
        with db.immediate_transaction(conn):
            db.insert_orders(
                conn,
                [(o["order_no"], o["employee_no"], o["amount"], o["period"], now) for o in demo_orders()],
            )
            db.insert_receipts(
                conn,
                [(r["receipt_no"], r["employee_no"], r["amount"], r["period"], now) for r in demo_receipts()],
            )
        return {"loaded_orders": len(demo_orders()), "loaded_receipts": len(demo_receipts()),
                "state": _state_snapshot(conn)}
    finally:
        conn.close()


@app.post("/api/demo/supplement")
def demo_supplement() -> dict:
    """补录演示：新增 E005 的回执，触发建议重算。"""
    try:
        records = validate_receipts_payload({"receipts": [demo_supplement_receipt()]})
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_receipts(records)


@app.post("/api/reset")
def reset() -> dict:
    conn = db.connect()
    try:
        db.reset_db(conn)
        return {"ok": True, "state": _state_snapshot(conn)}
    finally:
        conn.close()

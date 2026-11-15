"""API 全链路自测（使用临时 SQLite 库）。"""
import os
import sqlite3
import tempfile
import threading

import pytest

# 在导入应用前指定独立测试库
_TMPDIR = tempfile.mkdtemp(prefix="recon-test-")
os.environ["RECON_DB_PATH"] = os.path.join(_TMPDIR, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/reset")
        yield c


def load_demo(client):
    r = client.post("/api/demo")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 基础流程

def test_demo_load_and_suggestion_shape(client):
    data = load_demo(client)
    s = data["state"]["suggestion"]
    assert s["max_matched"] == 4
    certain_orders = {c["order"]["order_no"] for c in s["certain"]}
    assert certain_orders == {"PO20261001", "PO20261002", "PO20261003"}
    pending = {p["order"]["order_no"]: p["combos"] for p in s["pending"]}
    assert len(pending["PO20261004"]) == 2  # 两种 4000 组合
    assert s["unmatched"][0]["order_no"] == "PO20261005"
    assert any(r["receipt_no"] == "RC20261099" for r in s["unused_receipts"])


def test_state_counts_and_limits(client):
    load_demo(client)
    r = client.get("/api/state")
    s = r.json()["suggestion"]
    assert s["counts"] == {
        "open_orders": 5, "open_receipts": 11,
        "max_orders": 8, "max_receipts": 16,
    }
    # 再导入 4 张支付单 → 达到 9 张，超过 8 张上限
    payload = {"orders": [
        {"order_no": f"PX{i}", "employee_no": "E1", "amount": 100, "period": "2026-10"}
        for i in range(4)
    ]}
    r = client.post("/api/import/orders-json", json=payload)
    assert r.status_code == 400 and "上限" in r.json()["detail"]


def test_receipt_limit_16(client):
    load_demo(client)  # 11 条回执
    payload = {"receipts": [
        {"receipt_no": f"RN{i}", "employee_no": "E1", "amount": 100, "period": "2026-10"}
        for i in range(6)  # 11+6=17 > 16
    ]}
    r = client.post("/api/import/receipts-json", json=payload)
    assert r.status_code == 400 and "上限" in r.json()["detail"]


def test_validation_duplicate_and_amount(client):
    r = client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "A1", "employee_no": "E1", "amount": 100},
        {"order_no": "A1", "employee_no": "E2", "amount": 200},
    ]})
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    r = client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "A2", "employee_no": "E1", "amount": 0},
    ]})
    assert r.status_code == 400 and "正整数" in r.json()["detail"]

    r = client.post("/api/import/receipts-json", json={"receipts": [
        {"receipt_no": "B1", "employee_no": "E1", "amount": -5},
    ]})
    assert r.status_code == 400


def test_unique_no_conflict_with_existing(client):
    load_demo(client)
    r = client.post("/api/import/receipts-json", json={"receipts": [
        {"receipt_no": "RC20261001", "employee_no": "E1", "amount": 100, "period": "2026-10"},
    ]})
    assert r.status_code == 409 and "冲突" in r.json()["detail"]


def test_period_must_match(client):
    load_demo(client)
    r = client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "PM1", "employee_no": "E1", "amount": 100, "period": "2026-09"},
    ]})
    assert r.status_code == 400 and "月份不一致" in r.json()["detail"]


# ------------------------------------------------------------- 确认与依据

def test_confirm_certain_group_and_evidence(client):
    load_demo(client)
    payload = {"items": [
        {"order_no": "PO20261001", "receipt_nos": ["RC20261001", "RC20261002"]},
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
    ]}
    r = client.post("/api/confirm", json=payload)
    assert r.status_code == 200, r.text
    cid = r.json()["confirmation_id"]

    state = client.get("/api/state").json()
    open_orders = {o["order_no"] for o in state["orders"] if o["status"] == "open"}
    open_receipts = {rc["receipt_no"] for rc in state["receipts"] if rc["status"] == "open"}
    assert "PO20261001" not in open_orders and "PO20261003" in open_orders
    assert "RC20261001" not in open_receipts and "RC20261004" in open_receipts
    assert state["confirmed"][0]["confirmation_id"] == cid

    r = client.get("/api/evidence.csv")
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "支付单号" in text and "PO20261001" in text and "RC20261002" in text


def test_confirm_rejects_reused_receipt_and_rolls_back(client):
    load_demo(client)
    # 先确认 PO...003（占用 RC...004/005/006）
    r = client.post("/api/confirm", json={"items": [
        {"order_no": "PO20261003", "receipt_nos": ["RC20261004", "RC20261005", "RC20261006"]},
    ]})
    assert r.status_code == 200
    # 再尝试占用已使用回执 RC...004 → 整组失败（只带未确认订单，排除订单状态干扰）
    # RC...004(2000) + RC...007(1500) + RC...009(1000) = 4500 ≠ 4000，
    # 换成 RC...007+RC...008 本身合法，再额外拼入已占用的 RC...004 触发占用冲突
    r = client.post("/api/confirm", json={"items": [
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
        {"order_no": "PO20261004", "receipt_nos": ["RC20261004", "RC20261007", "RC20261009"]},
    ]})
    assert r.status_code == 409 and "已被占用" in r.json()["detail"]
    # 同组里合法的那一项也必须回滚：PO...002、PO...004 仍是 open
    state = client.get("/api/state").json()
    po2 = next(o for o in state["orders"] if o["order_no"] == "PO20261002")
    assert po2["status"] == "open"
    po4 = next(o for o in state["orders"] if o["order_no"] == "PO20261004")
    assert po4["status"] == "open"


def test_confirm_rejects_amount_mismatch(client):
    load_demo(client)
    r = client.post("/api/confirm", json={"items": [
        {"order_no": "PO20261001", "receipt_nos": ["RC20261001"]},  # 3000 != 7000
    ]})
    assert r.status_code == 409 and "金额不平" in r.json()["detail"]


def test_manual_confirm_pending_combo(client):
    """待核组合允许人工选择确认。"""
    load_demo(client)
    r = client.post("/api/confirm", json={"items": [
        {"order_no": "PO20261004", "receipt_nos": ["RC20261007", "RC20261008"]},
    ]})
    assert r.status_code == 200
    state = client.get("/api/state").json()
    used = {rc["receipt_no"] for rc in state["receipts"] if rc["status"] == "used"}
    assert {"RC20261007", "RC20261008"} <= used
    # 另一组待核回执仍开放，但 PO...004 已退出，建议中不再出现
    open_order_nos = {o["order_no"] for o in state["orders"] if o["status"] == "open"}
    assert "PO20261004" not in open_order_nos


# ------------------------------------------------------------- 补录重算

def test_supplement_recalculates_and_other_employee_unchanged(client):
    load_demo(client)
    before = client.get("/api/state").json()["suggestion"]
    before_certain = {c["order"]["order_no"]: [r["receipt_no"] for r in c["receipts"]]
                      for c in before["certain"]}

    r = client.post("/api/demo/supplement")
    assert r.status_code == 200
    after = client.get("/api/state").json()["suggestion"]
    after_certain = {c["order"]["order_no"]: [rc["receipt_no"] for rc in c["receipts"]]
                     for c in after["certain"]}

    # E005 新回执 → PO...005 进入确定配对，最大匹配数 +1
    assert after["max_matched"] == 5
    assert after_certain["PO20261005"] == ["RC20261011"]
    # 其他员工的确定配对保持不变
    for k, v in before_certain.items():
        assert after_certain[k] == v


def test_supplement_other_employee_keeps_confirmation(client):
    """补录其他员工回执不影响已确认依据。"""
    load_demo(client)
    client.post("/api/confirm", json={"items": [
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
    ]})
    client.post("/api/demo/supplement")
    state = client.get("/api/state").json()
    assert len(state["confirmed"]) == 1
    assert state["confirmed"][0]["items"][0]["order_no"] == "PO20261002"


# ------------------------------------------------------------- 持久化

def test_persistence_across_reconnect(client):
    load_demo(client)
    client.post("/api/confirm", json={"items": [
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
    ]})
    # 模拟重启：丢弃连接、重新建连接（lifespan 不会清库）
    conn = db.connect()
    try:
        orders = {r["order_no"]: r["status"] for r in db.fetch_all_orders(conn)}
        receipts = {r["receipt_no"]: r["status"] for r in db.fetch_all_receipts(conn)}
        assert orders["PO20261002"] == "confirmed"
        assert receipts["RC20261003"] == "used"
        assert db.fetch_evidence(conn)
    finally:
        conn.close()
    # 新连接下 API 仍可下载依据
    r = client.get("/api/evidence.csv")
    assert r.status_code == 200


# ------------------------------------------------------------- 并发确认

def test_concurrent_confirm_no_double_occupancy(client):
    """两个线程同时确认、争抢同一回执：只有一个成功，另一个整组失败、全不改。"""
    # K1/K2 同员工同额，R-a/R-b 各 100：
    # 两线程都尝试用 R-a 配平各自的订单（同一回执只能进一个事务）。
    # 失败线程附带独立订单 K3+R-c，用来证明失败组整组回滚。
    client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "K1", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"order_no": "K2", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"order_no": "K3", "employee_no": "E2", "amount": 200, "period": "2026-10"},
    ]})
    client.post("/api/import/receipts-json", json={"receipts": [
        {"receipt_no": "R-a", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"receipt_no": "R-b", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"receipt_no": "R-c", "employee_no": "E2", "amount": 200, "period": "2026-10"},
    ]})
    barrier = threading.Barrier(2)
    results = []

    def worker(payload):
        barrier.wait()
        r = client.post("/api/confirm", json=payload)
        results.append((r.status_code, r.json()))

    t1 = threading.Thread(target=worker, args=({"items": [
        {"order_no": "K1", "receipt_nos": ["R-a"]},
    ]},))
    t2 = threading.Thread(target=worker, args=({"items": [
        {"order_no": "K2", "receipt_nos": ["R-a"]},
        {"order_no": "K3", "receipt_nos": ["R-c"]},
    ]},))
    t1.start(); t2.start(); t1.join(); t2.join()

    ok = [r for r in results if r[0] == 200]
    fail = [r for r in results if r[0] != 200]
    assert len(ok) == 1 and len(fail) == 1
    assert "已被占用" in fail[0][1]["detail"]

    state = client.get("/api/state").json()
    statuses = {o["order_no"]: o["status"] for o in state["orders"]}
    receipt_st = {rc["receipt_no"]: rc["status"] for rc in state["receipts"]}
    winner = ok[0][1]
    if winner["confirmed_items"] == 1:
        # t1 赢：仅 K1 落库；t2 整组回滚，K3/R-c 保持开放
        assert statuses == {"K1": "confirmed", "K2": "open", "K3": "open"}
        assert receipt_st["R-c"] == "open"
    else:
        # t2 赢：K2+K3 作为一个事务整体落库；K1 保持开放
        assert winner["confirmed_items"] == 2
        assert statuses == {"K1": "open", "K2": "confirmed", "K3": "confirmed"}
        assert receipt_st["R-c"] == "used"
    # 无论谁赢，R-a 只被使用一次、R-b 从未使用
    assert receipt_st["R-a"] == "used" and receipt_st["R-b"] == "open"
    # 回执唯一约束兜底：确认明细中每条回执仅一行
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT receipt_no, COUNT(*) c FROM confirmation_items GROUP BY receipt_no HAVING c > 1"
        ).fetchall()
        assert rows == []
        # 成功批次与其明细同生共死：失败线程不得留下任何残数据
        (n_conf,) = conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()
        (n_items,) = conn.execute("SELECT COUNT(*) FROM confirmation_items").fetchone()
        assert n_conf == 1 and n_items == winner["confirmed_items"]
    finally:
        conn.close()


# ------------------------------------------------------------- CSV 导入/下载

def test_csv_import_and_evidence_download(client):
    csv_orders = "order_no,employee_no,amount,period\nC1,E1,500,2026-10\nC2,E2,700,2026-10\n"
    csv_receipts = (
        "receipt_no,employee_no,amount,period\n"
        "CR1,E1,200,2026-10\nCR2,E1,300,2026-10\n"
        "CR3,E2,700,2026-10\n"
    )
    r = client.post("/api/import/orders", files={"file": ("o.csv", csv_orders, "text/csv")})
    assert r.status_code == 200, r.text
    r = client.post("/api/import/receipts", files={"file": ("r.csv", csv_receipts, "text/csv")})
    assert r.status_code == 200, r.text

    state = client.get("/api/state").json()["suggestion"]
    assert state["max_matched"] == 2
    r = client.post("/api/confirm", json={"items": [
        {"order_no": "C1", "receipt_nos": ["CR1", "CR2"]},
        {"order_no": "C2", "receipt_nos": ["CR3"]},
    ]})
    assert r.status_code == 200
    r = client.get("/api/evidence.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert b"\xef\xbb\xbf" in r.content  # BOM


def test_evidence_empty_returns_404(client):
    r = client.get("/api/evidence.csv")
    assert r.status_code == 404

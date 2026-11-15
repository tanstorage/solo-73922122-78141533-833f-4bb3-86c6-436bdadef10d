"""SQLite 持久化层。

表：
- payment_orders  支付单（导入后不可变；确认后 status=confirmed）
- receipts        银行回执（补录进同一批次；确认后被占用）
- confirmations   一次确认动作（整组事务），对应一份可下载实付依据
- confirmation_items  确认明细（支付单 ↔ 回执配对）
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable, Iterator, List, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_orders (
    order_no     TEXT PRIMARY KEY,
    employee_no  TEXT NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount > 0),
    period       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','confirmed')),
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_no   TEXT PRIMARY KEY,
    employee_no  TEXT NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount > 0),
    period       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','used')),
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmed_at REAL NOT NULL,
    item_count   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmation_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_id INTEGER NOT NULL REFERENCES confirmations(id),
    order_no        TEXT NOT NULL REFERENCES payment_orders(order_no),
    receipt_no      TEXT NOT NULL REFERENCES receipts(receipt_no) UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_items_conf ON confirmation_items(confirmation_id);
CREATE INDEX IF NOT EXISTS idx_items_order ON confirmation_items(order_no);
"""

MAX_ORDERS = 8
MAX_RECEIPTS = 16


def default_db_path() -> str:
    return os.environ.get("RECON_DB_PATH", os.path.join("data", "recon.db"))


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即加写锁的事务上下文，配合 busy_timeout 实现整组成功/全不改。

    两个窗口同时确认时，第二个窗口阻塞在 BEGIN IMMEDIATE，
    拿到锁后重新读取状态校验，若回执已被占用则整体回滚。
    """
    last_err: sqlite3.OperationalError | None = None
    for _ in range(5):  # 重试，规避锁等待超时的边界情况
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:  # database is locked
            last_err = exc
            time.sleep(0.1)
    else:
        raise last_err  # type: ignore[misc]
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reset_db(conn: sqlite3.Connection) -> None:
    with immediate_transaction(conn):
        conn.execute("DELETE FROM confirmation_items")
        conn.execute("DELETE FROM confirmations")
        conn.execute("DELETE FROM receipts")
        conn.execute("DELETE FROM payment_orders")


def insert_orders(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        "INSERT INTO payment_orders(order_no, employee_no, amount, period, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def insert_receipts(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        "INSERT INTO receipts(receipt_no, employee_no, amount, period, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def count_orders(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0]


def count_receipts(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]


def fetch_open_orders(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT order_no, employee_no, amount FROM payment_orders"
        " WHERE status='open' ORDER BY order_no"
    ).fetchall()


def fetch_open_receipts(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT receipt_no, employee_no, amount FROM receipts"
        " WHERE status='open' ORDER BY receipt_no"
    ).fetchall()


def fetch_all_orders(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT order_no, employee_no, amount, period, status FROM payment_orders"
        " ORDER BY order_no"
    ).fetchall()


def fetch_all_receipts(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT receipt_no, employee_no, amount, period, status FROM receipts"
        " ORDER BY receipt_no"
    ).fetchall()


def used_receipt_nos(conn: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in conn.execute("SELECT receipt_no FROM confirmation_items")
    }


def save_confirmation(
    conn: sqlite3.Connection, pairing: dict, now: float
) -> int:
    """在已开启的事务内写入确认，返回 confirmation id。"""
    cur = conn.execute(
        "INSERT INTO confirmations(confirmed_at, item_count) VALUES (?, ?)",
        (now, len(pairing)),
    )
    confirmation_id = cur.lastrowid
    items: List[tuple] = []
    for order_no, receipt_nos in pairing.items():
        for rn in receipt_nos:
            items.append((confirmation_id, order_no, rn))
    conn.executemany(
        "INSERT INTO confirmation_items(confirmation_id, order_no, receipt_no)"
        " VALUES (?, ?, ?)",
        items,
    )
    order_nos = list(pairing.keys())
    receipt_nos = [rn for nos in pairing.values() for rn in nos]
    conn.executemany(
        "UPDATE payment_orders SET status='confirmed' WHERE order_no=?",
        [(o,) for o in order_nos],
    )
    conn.executemany(
        "UPDATE receipts SET status='used' WHERE receipt_no=?",
        [(r,) for r in receipt_nos],
    )
    return confirmation_id


def fetch_evidence(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id AS confirmation_id, c.confirmed_at,
               i.order_no, po.employee_no,
               po.amount AS order_amount,
               i.receipt_no, r.amount AS receipt_amount
        FROM confirmations c
        JOIN confirmation_items i ON i.confirmation_id = c.id
        JOIN payment_orders po ON po.order_no = i.order_no
        JOIN receipts r ON r.receipt_no = i.receipt_no
        ORDER BY c.id, i.order_no, i.receipt_no
        """
    ).fetchall()

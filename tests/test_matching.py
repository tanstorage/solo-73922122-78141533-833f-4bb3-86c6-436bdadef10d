"""匹配引擎测试：用全量暴力枚举作为 oracle 交叉验证。"""
import itertools
import random

import pytest

from app.matching import (
    Order,
    Receipt,
    build_candidates,
    max_matching,
    suggest,
    validate_confirmation,
)


def brute_force_optimum(orders, receipts):
    """枚举所有可行匹配，返回 (最大匹配数, 所有最优方案集合)。"""
    candidates = build_candidates(orders, receipts)
    best = -1
    best_solutions = []

    def search(idx, used, chosen):
        nonlocal best, best_solutions
        if idx == len(orders):
            score = len(chosen)
            if score > best:
                best, best_solutions = score, [dict(chosen)]
            elif score == best:
                best_solutions.append(dict(chosen))
            return
        order = orders[idx]
        # 不匹配该单
        search(idx + 1, used, chosen)
        for combo in candidates[order.order_no].combos:
            if not (combo & used):
                chosen[order.order_no] = combo
                search(idx + 1, used | combo, chosen)
                del chosen[order.order_no]

    search(0, frozenset(), {})
    return best, best_solutions


def make_random_case(rng, n_orders=6, n_receipts=12):
    """构造随机但保证有交集的员工数据。"""
    emps = [f"E{rng.randrange(4)}" for _ in range(n_orders)]
    orders = [
        Order(f"PO{i:03d}", emps[i], rng.randrange(50, 500) * 10)
        for i in range(n_orders)
    ]
    receipts = []
    for i in range(n_receipts):
        emp = f"E{rng.randrange(4)}"
        receipts.append(Receipt(f"RC{i:03d}", emp, rng.randrange(10, 400) * 10))
    return orders, receipts


def test_random_against_brute_force():
    rng = random.Random(20261115)
    for trial in range(300):
        orders, receipts = make_random_case(
            rng, n_orders=rng.randrange(1, 7), n_receipts=rng.randrange(1, 13)
        )
        score, solution = max_matching(orders, receipts)
        bf_best, bf_solutions = brute_force_optimum(orders, receipts)
        assert score == bf_best, f"trial {trial}: {score} != {bf_best}"

        # 引擎给出的方案必须是最优方案之一
        assert len(solution) == bf_best
        bf_set = {
            frozenset((o, frozenset(c)) for o, c in sol.items())
            for sol in bf_solutions
        }
        assert frozenset((o, c) for o, c in solution.items()) in bf_set

        # 公共配对 = 所有最优方案的交集
        s = suggest(orders, receipts)
        intersection = dict(bf_solutions[0])
        for sol in bf_solutions[1:]:
            for o in list(intersection):
                if sol.get(o) != intersection[o]:
                    del intersection[o]
        assert s["max_matched"] == bf_best
        assert s["certain"] == {o: sorted(c) for o, c in sorted(intersection.items())}

        # pending 中的每个组合都必须是合法组合
        candidates = build_candidates(orders, receipts)
        for o, combos in s["pending"].items():
            for combo in combos:
                assert frozenset(combo) in candidates[o].combos
        # unmatched 必须确实无合法组合
        for o in s["unmatched"]:
            assert candidates[o].combos == []


def test_demo_scenario_certain_and_ambiguous():
    orders = [
        Order("PO1", "E1", 7000),
        Order("PO2", "E2", 5000),
        Order("PO3", "E3", 6000),
        Order("PO4", "E4", 4000),
        Order("PO5", "E5", 3000),
    ]
    receipts = [
        Receipt("R1", "E1", 3000), Receipt("R2", "E1", 4000),
        Receipt("R3", "E2", 5000),
        Receipt("R4", "E3", 2000), Receipt("R5", "E3", 2000), Receipt("R6", "E3", 2000),
        Receipt("R7", "E4", 1500), Receipt("R8", "E4", 2500),
        Receipt("R9", "E4", 1000), Receipt("R10", "E4", 3000),
        Receipt("R99", "E9", 999),
    ]
    s = suggest(orders, receipts)
    assert s["max_matched"] == 4
    assert s["certain"] == {
        "PO1": ["R1", "R2"],
        "PO2": ["R3"],
        "PO3": ["R4", "R5", "R6"],
    }
    # E4 两种组合都出现在某个最优方案中 → 待核（内部排序不影响集合语义）
    assert {frozenset(c) for c in s["pending"]["PO4"]} == {
        frozenset(("R7", "R8")), frozenset(("R9", "R10"))
    }
    assert s["unmatched"] == ["PO5"]
    assert "R99" in s["unused_receipts"]


def test_supplement_recalculates_suggestion():
    """补录 E5 回执后，PO5 成为新的确定配对；其他员工结论不变。"""
    orders = [Order("PO1", "E1", 7000), Order("PO5", "E5", 3000)]
    receipts = [Receipt("R1", "E1", 3000), Receipt("R2", "E1", 4000)]
    before = suggest(orders, receipts)
    assert before["unmatched"] == ["PO5"]

    receipts.append(Receipt("R5", "E5", 3000))
    after = suggest(orders, receipts)
    assert after["max_matched"] == 2
    assert after["certain"] == {"PO1": ["R1", "R2"], "PO5": ["R5"]}
    assert after["pending"] == {} and after["unmatched"] == []


def test_other_employee_supplement_does_not_change_result():
    orders = [Order("PO1", "E1", 100)]
    receipts = [Receipt("R1", "E1", 100)]
    before = suggest(orders, receipts)
    # 补录别的员工的回执，E1 的建议不受影响
    receipts.append(Receipt("RX", "E2", 100))
    after = suggest(orders, receipts)
    assert after["certain"] == before["certain"] == {"PO1": ["R1"]}


def test_receipt_used_at_most_once_across_orders():
    """同一员工两张同额单 + 一条回执：回执只能被一张单占用。"""
    orders = [Order("PO1", "E1", 100), Order("PO2", "E1", 100)]
    receipts = [Receipt("R1", "E1", 100)]
    s = suggest(orders, receipts)
    assert s["max_matched"] == 1
    # 两种最优方案（R1 给 PO1 或给 PO2）→ 无公共配对
    assert s["certain"] == {}
    assert set(s["pending"].keys()) == {"PO1", "PO2"}


def test_validate_confirmation_rules():
    orders = [Order("PO1", "E1", 100), Order("PO2", "E2", 300)]
    receipts = [
        Receipt("R1", "E1", 100),
        Receipt("R2", "E2", 100), Receipt("R3", "E2", 200),
        Receipt("R4", "E2", 50), Receipt("R5", "E2", 50), Receipt("R6", "E2", 200),
    ]
    ok, _ = validate_confirmation({"PO1": ["R1"]}, orders, receipts, frozenset())
    assert ok
    ok, _ = validate_confirmation({"PO2": ["R2", "R3"]}, orders, receipts, frozenset())
    assert ok
    # 四条回执超上限
    ok, msg = validate_confirmation({"PO2": ["R2", "R4", "R5", "R6"]}, orders, receipts, frozenset())
    assert not ok and "1~3" in msg
    # 金额不平
    ok, msg = validate_confirmation({"PO2": ["R4", "R5"]}, orders, receipts, frozenset())
    assert not ok and "金额不平" in msg
    # 员工不一致（R2 未被占用，才能走到员工校验）
    ok, msg = validate_confirmation({"PO1": ["R2"]}, orders, receipts, frozenset())
    assert not ok and "员工不一致" in msg
    # 回执已占用（优先报占用）
    ok, msg = validate_confirmation({"PO1": ["R1"]}, orders, receipts, frozenset(["R1"]))
    assert not ok and "已被占用" in msg
    # 同组重复使用（同员工，精准命中重复占用分支）
    ok, msg = validate_confirmation(
        {"PO1": ["R1"], "PO2": ["R1", "R2"]},
        [Order("PO1", "E1", 100), Order("PO2", "E1", 200)],
        receipts, frozenset()
    )
    assert not ok and "重复使用" in msg


def test_max_size_problem_runs_fast():
    """上限规模（8 单 / 16 回执）在密集组合下仍快速完成。"""
    orders = [Order(f"PO{i}", f"E{i}", 300) for i in range(8)]
    receipts = []
    for i in range(8):
        for j in range(2):
            receipts.append(Receipt(f"R{i}-{j}", f"E{i}", 150))
    import time

    t0 = time.perf_counter()
    s = suggest(orders, receipts)
    assert time.perf_counter() - t0 < 2.0
    assert s["max_matched"] == 8

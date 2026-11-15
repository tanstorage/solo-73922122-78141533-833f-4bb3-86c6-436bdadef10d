"""对账匹配引擎。

规则：
- 每条支付单可由同一员工的 1~3 条回执组成，回执金额之和须等于支付单应付额；
- 每条回执至多被一个支付单占用；
- 目标：让完整匹配的支付单数量最多（最大基数匹配）；
- 输出：在 *所有* 最优方案中配对完全相同的支付单标记为"确定"，其余为"待核"。

数据规模很小（支付单 ≤ 8、回执 ≤ 16），用记忆化 DFS 求最大匹配，
再用"禁止法"判定每条配对是否为所有最优解的公共配对。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Receipt:
    receipt_no: str
    employee_no: str
    amount: int  # 正整数，单位：分


@dataclass(frozen=True)
class Order:
    order_no: str
    employee_no: str
    amount: int  # 正整数，单位：分


@dataclass
class OrderCandidates:
    """某支付单在当前可用回执下的全部合法回执组合（1~3 条、同员工、金额相等）。"""

    order_no: str
    combos: List[FrozenSet[str]] = field(default_factory=list)


def _all_combos_for_order(
    order: Order,
    receipts_by_emp: Dict[str, List[Receipt]],
    allowed_receipts: FrozenSet[str],
) -> List[FrozenSet[str]]:
    """枚举某支付单的所有合法回执组合。"""
    pool = [r for r in receipts_by_emp.get(order.employee_no, ()) if r.receipt_no in allowed_receipts]
    combos: List[FrozenSet[str]] = []
    n = len(pool)
    # 1~3 条子集
    for i in range(n):
        ri = pool[i]
        if ri.amount == order.amount:
            combos.append(frozenset((ri.receipt_no,)))
        for j in range(i + 1, n):
            rj = pool[j]
            s2 = ri.amount + rj.amount
            if s2 == order.amount:
                combos.append(frozenset((ri.receipt_no, rj.receipt_no)))
            if s2 >= order.amount:
                # 金额均为正，再加只会更大；但顺序未排序，不能直接 break，continue 即可
                pass
            for k in range(j + 1, n):
                rk = pool[k]
                if ri.amount + rj.amount + rk.amount == order.amount:
                    combos.append(
                        frozenset((ri.receipt_no, rj.receipt_no, rk.receipt_no))
                    )
    # 去重（不同枚举路径可能产生相同集合——当前写法不会，但稳妥起见）
    return sorted(set(combos), key=lambda c: tuple(sorted(c)))


def build_candidates(
    orders: Sequence[Order],
    receipts: Sequence[Receipt],
    allowed_receipts: Optional[FrozenSet[str]] = None,
) -> Dict[str, OrderCandidates]:
    """为每张支付单构造合法组合表。"""
    by_emp: Dict[str, List[Receipt]] = {}
    for r in receipts:
        by_emp.setdefault(r.employee_no, []).append(r)
    if allowed_receipts is None:
        allowed_receipts = frozenset(r.receipt_no for r in receipts)
    result: Dict[str, OrderCandidates] = {}
    for o in orders:
        result[o.order_no] = OrderCandidates(
            order_no=o.order_no,
            combos=_all_combos_for_order(o, by_emp, allowed_receipts),
        )
    return result


def max_matching(
    orders: Sequence[Order],
    receipts: Sequence[Receipt],
    allowed_receipts: Optional[FrozenSet[str]] = None,
    forbidden_pairs: Optional[Dict[str, FrozenSet[str]]] = None,
) -> Tuple[int, Dict[str, FrozenSet[str]]]:
    """求最大匹配，返回 (匹配支付单数, {订单号: 回执编号集合})。

    forbidden_pairs: 对指定支付单屏蔽某些组合（判定公共配对用），
                     值为需要排除的回执集合，命中即跳过该组合。
    """
    candidates = build_candidates(orders, receipts, allowed_receipts)
    forbidden_pairs = forbidden_pairs or {}
    # 按组合数从少到多排序，利于剪枝与快速找到较优解
    ordered = sorted(
        candidates.values(), key=lambda c: (len(c.combos), c.order_no)
    )
    memo: Dict[Tuple[int, FrozenSet[str]], int] = {}

    def dfs(idx: int, used: FrozenSet[str]) -> int:
        key = (idx, used)
        if key in memo:
            return memo[key]
        if idx == len(ordered):
            memo[key] = 0
            return 0
        best = dfs(idx + 1, used)  # 跳过本单
        cand = ordered[idx]
        blocked = forbidden_pairs.get(cand.order_no)
        for combo in cand.combos:
            if combo & used:
                continue
            if blocked is not None and combo == blocked:
                continue
            score = 1 + dfs(idx + 1, used | combo)
            if score > best:
                best = score
        memo[key] = best
        return best

    # 回溯出一个具体方案（与 dfs 同序，保证可复现）
    def reconstruct() -> Dict[str, FrozenSet[str]]:
        used: FrozenSet[str] = frozenset()
        chosen: Dict[str, FrozenSet[str]] = {}
        for idx in range(len(ordered)):
            cand = ordered[idx]
            skip_score = dfs(idx + 1, used)
            blocked = forbidden_pairs.get(cand.order_no)
            best_combo: Optional[FrozenSet[str]] = None
            for combo in cand.combos:
                if combo & used:
                    continue
                if blocked is not None and combo == blocked:
                    continue
                if 1 + dfs(idx + 1, used | combo) > skip_score:
                    best_combo = combo
                    break
            if best_combo is not None:
                chosen[cand.order_no] = best_combo
                used = used | best_combo
        return chosen

    total = dfs(0, frozenset())
    return total, reconstruct()


def suggest(
    orders: Sequence[Order],
    receipts: Sequence[Receipt],
    allowed_receipts: Optional[FrozenSet[str]] = None,
) -> dict:
    """生成建议方案。

    返回结构::

        {
          "max_matched": int,
          "certain": {order_no: [receipt_no, ...]},   # 所有最优方案共有的配对
          "pending": {order_no: [[receipt_no, ...]]}, # 其余有合法组合的支付单
          "unmatched": [order_no, ...],               # 无任何合法组合
          "unused_receipts": [receipt_no, ...],
        }
    """
    if allowed_receipts is None:
        allowed_receipts = frozenset(r.receipt_no for r in receipts)
    candidates = build_candidates(orders, receipts, allowed_receipts)
    best_score, one_solution = max_matching(orders, receipts, allowed_receipts)

    certain: Dict[str, List[str]] = {}
    for order_no, combo in one_solution.items():
        # 若"禁止该订单使用这一组合"后最大匹配数下降，
        # 说明该订单在所有最优方案中都必须以该组合出现。
        score, _ = max_matching(
            orders,
            receipts,
            allowed_receipts,
            forbidden_pairs={order_no: combo},
        )
        if score < best_score:
            certain[order_no] = sorted(combo)

    pending: Dict[str, List[List[str]]] = {}
    unmatched: List[str] = []
    for o in orders:
        if o.order_no in certain:
            continue
        combos = [sorted(c) for c in candidates[o.order_no].combos]
        if combos:
            pending[o.order_no] = combos
        else:
            unmatched.append(o.order_no)

    used_in_certain = {r for combo in certain.values() for r in combo}
    unused = sorted(
        r.receipt_no
        for r in receipts
        if r.receipt_no in allowed_receipts and r.receipt_no not in used_in_certain
    )
    return {
        "max_matched": best_score,
        "certain": certain,
        "pending": pending,
        "unmatched": unmatched,
        "unused_receipts": unused,
    }


def validate_confirmation(
    pairing: Dict[str, Sequence[str]],
    orders: Sequence[Order],
    receipts: Sequence[Receipt],
    used_receipts: FrozenSet[str],
    all_receipts: Optional[Sequence[Receipt]] = None,
) -> Tuple[bool, str]:
    """校验一组人工/建议确认是否合法（金额、同员工、1~3 条、回执未占用）。

    receipts 为当前可参与匹配的开放回执；all_receipts 提供全量回执用于
    区分"编号不存在"与"已被占用"两种错误（缺省等同于 receipts）。
    """
    order_map = {o.order_no: o for o in orders}
    receipt_map = {r.receipt_no: r for r in receipts}
    all_receipt_map = {r.receipt_no: r for r in (all_receipts or receipts)}
    seen: set = set()
    for order_no, receipt_nos in pairing.items():
        order = order_map.get(order_no)
        if order is None:
            return False, f"支付单 {order_no} 不存在或已确认"
        if not 1 <= len(receipt_nos) <= 3:
            return False, f"支付单 {order_no} 回执数须在 1~3 条之间"
        total = 0
        for rn in receipt_nos:
            if rn not in all_receipt_map:
                return False, f"回执 {rn} 不存在"
            r = all_receipt_map[rn]
            if rn in used_receipts or rn in seen or rn not in receipt_map:
                return False, f"回执 {rn} 已被占用或重复使用"
            if r.employee_no != order.employee_no:
                return False, f"回执 {rn} 与支付单 {order_no} 员工不一致"
            seen.add(rn)
            total += r.amount
        if total != order.amount:
            return False, (
                f"支付单 {order_no} 金额不平：回执合计 {total} 分，应付 {order.amount} 分"
            )
    return True, ""

"""整次调用口径：不可分割的动作至少做一次。

`plan/supply.py` 的曲线是**连续松弛**（可以调用 0.3 次），那是为批量规划设计的。
但「跑一趟结构」要么进要么不进：拿 0.54 趟去比价，会把沉船算得比杀铁傀儡还便宜。

这里实现同一件事的离散口径：`ACTION_KINDS` 里标了 `discrete` 的动作按
`max(1, ceil(需求 / 每次产出))` 计次，其余动作保持线性，并对前置**递归**取整。
`grade`（评分）与 `plan`（路线）共用这一份递归——同一件事只能有一个答案。

    calls = ceil(demand / 一次调用产出)            # 不可分割的动作至少 1 次
    total = 动作自带固定成本 + 动作自身时间 × calls
          + Σ once 前置的整次成本                   # 只付一次
          + Σ per_use 前置的整次成本(qty × calls)   # 跟着调用次数走

`WholeChoice.children` 在 `want_children=True` 时给出完整决策树，路线图用它按整次口径
直接画；全库评分只取成本，不建树。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .actions import MODE_PER_USE, is_discrete


@dataclass(frozen=True)
class WholeChoice:
    """某个物品在整次口径下的一次取舍。"""

    item: str
    need: float
    cost: float | None
    action_id: str = ""
    label: str = ""
    variance: float = 0.0
    # 这个动作要被完整执行几次（不可分割动作至少 1，其余等于 `need / 每次产出`）。
    calls: float = 0.0
    # 本动作**自身**那部分成本：`overhead + action.cost × calls`。
    # 与 `children` 的 `cost` 相加正好等于 `cost`——图上的算式靠它才能核对。
    own_cost: float = 0.0
    # 本动作自身那部分的方差（不含前置），去重后重算总数要用。
    own_variance: float = 0.0
    # 这个前置是 `once` 模式（工具、站点、状态）。`once` 在整条路线里只该收一次，
    # `dedupe_once_costs()` 靠这个标记做去重。
    once: bool = False
    # 前置的整次方案（`want_children=True` 时才有内容）。
    children: tuple["WholeChoice", ...] = ()
    source: str = ""
    # 这棵 choice 是否带 children：缓存命中时用来判断要不要重算树。
    tree: bool = False
    # 不可行的原因：`cycle` = 依赖回到本链路（这条来源不成立），
    # `missing` = 前置缺来源。两者都返回 `cost=None`，图上的「还有 N 条」
    # 需要靠它区分是「依赖自己」还是「贵/缺数据」。
    blocked: str = ""
    # 这条前置在本链路的上游已经付过（同一个 `once` 只收一次），成本记 0。
    already_paid: bool = False
    # 调用方自带的标注，**不参与定价**：`grade.best_bundle()` 用它记
    # `(action_id, 调用次数, 覆盖的目标)`，把「一次买齐」的树投影回 `BundleStep`。
    # `dedupe_once_costs` 走 `dataclasses.replace`，会原样保留。
    payload: object = None

    @property
    def feasible(self) -> bool:
        return self.cost is not None


def _solve_choice(
    actionset,
    curves,
    item: str,
    demand: float,
    *,
    seen: frozenset[str] = frozenset(),
    memo: dict | None = None,
    actions_by_id: dict | None = None,
    want_children: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> WholeChoice:
    """求最便宜的整次方案，**不做跨分支的 `once` 去重**（去重见 `dedupe_once_costs`）。

    不把「已付过的前置」放进递归状态：那样缓存键得跟着变，深树上会退化成
    指数级枚举（实测 elytra 从 0.4 秒涨到 250 秒）。均摊口径也是这个分工——
    先按边际最优解出一条路线，再对整棵树的 `once` 池去重（`dedupe_once`）。
    """
    if memo is None:
        memo = {}
    key = (item, round(demand, 6))
    cached = memo.get(key)
    if cached is not None and (not want_children or cached.tree):
        return cached
    if item in seen:
        # 回到当前链路已经走过的物品：这条分支是环，无法给出有限成本。
        return WholeChoice(item=item, need=demand, cost=None, blocked="cycle")
    best_cost: float | None = None
    best_var = 0.0
    best_action = ""
    best_label = ""
    best_source = ""
    best_calls = 0.0
    best_own = 0.0
    best_own_var = 0.0
    best_children: tuple[WholeChoice, ...] = ()
    best_tree = False
    curve = curves.get(item)
    by_id = (
        actions_by_id
        if actions_by_id is not None
        else {action.action_id: action for action in actionset.actions}
    )
    for segment in curve.segments if curve else ():
        if segment.action_id in exclude:
            continue
        if segment.members:
            # 并行合成的段：产出无法归到单个动作上，退回线性计价。
            total = segment.fixed_cost + segment.unit_cost * demand
            spread = demand * (segment.unit_cost * segment.cv) ** 2
            if best_cost is None or total < best_cost:
                best_cost, best_var = total, spread
                best_action, best_label = segment.action_id, segment.label
                best_source = ""
                best_calls = demand
                best_own = total
                best_children = ()
            continue
        action = by_id.get(segment.action_id)
        if action is None:
            continue
        effective = action.effective_yield(item)
        if effective <= 0:
            continue
        if is_discrete(segment.action_id):
            # 不可分割：至少跑一趟，余数补齐到整次。
            # 口径定义见 `plan/actions.py::ACTION_KINDS`（`discrete` 属性 + 理由）。
            calls = (
                1.0
                if demand <= effective
                else float(math.ceil(demand / effective - 1e-9))
            )
        else:
            calls = demand / effective
        own = sum(amount for _, amount in action.overhead) + action.cost * calls
        total = own
        # 方差按**每件成本**换算：`cv` 描述的是「每件成本的相对波动」，
        # 已经含了 `1/μ` 的折减（`cv = (s/μ)·√μ`），不能再乘一次调用次数。
        cv = action.yield_cv if item == action.item else 0.0
        own_spread = demand * (action.cost * cv / effective) ** 2
        spread = own_spread
        children: list[WholeChoice] = []
        for requirement in action.requires:
            once = requirement.mode != MODE_PER_USE
            need = requirement.qty if once else requirement.qty * calls
            sub = _solve_choice(
                actionset,
                curves,
                requirement.item,
                need,
                seen=seen | {item},
                memo=memo,
                actions_by_id=by_id,
                want_children=want_children,
            )
            if sub.cost is None:
                total = None
                break
            total += sub.cost
            # 前置的波动也会传下来（独立随机变量，方差可加）。
            spread += sub.variance
            if want_children:
                # 记下这条前置的付费模式：去重只对 `once` 生效。
                children.append(replace(sub, once=once))
        if total is None:
            continue
        if best_cost is None or total < best_cost:
            best_cost, best_var = total, spread
            best_action, best_label = segment.action_id, segment.label
            best_source = action.source
            best_calls = calls
            best_own = own
            best_own_var = own_spread
            best_children = tuple(children) if want_children else ()
            best_tree = want_children
    if best_cost is None:
        # **不能缓存 None**：它往往是「递归撞上 `seen` 里的物品、这条分支被判成环」
        # 的产物，只对当前这条链路成立。缓存后别的调用会命中它，把本来走得通的
        # 路线误判为不可行——实测凋灵之星的整次成本就是这么变成 `None` 的。
        return WholeChoice(item=item, need=demand, cost=None, blocked="missing")
    choice = WholeChoice(
        item=item,
        need=demand,
        cost=best_cost,
        action_id=best_action,
        label=best_label,
        variance=best_var,
        calls=best_calls,
        own_cost=best_own,
        own_variance=best_own_var,
        children=best_children,
        source=best_source,
        tree=best_tree,
    )
    memo[key] = choice
    return choice


def dedupe_once_costs(root: WholeChoice) -> WholeChoice:
    """整棵树里同名的一次性前置只收一次（与 `requires.dedupe_once` 同一条规则）。

    均摊口径把整条路线的固定成本收进 `once_pool` 再按名字去重；整次口径的递归
    天然做不到——`capability:nether_access` 既是 `portal:full_frame` 的前置、
    又出现在黑曜石来源链上，各解一次就会收两遍（`dragon_egg` 实测多收 324 TU）。

    做法：**先按边际最优解出树（缓存友好），再对整棵树做一次前序去重**
    ——第一次出现的 `once` 保留原价，之后同名的置为 0 并标 `already_paid`，
    最后自底向上重算 `cost` / `variance`，保证「本动作 + Σ前置 = 总代价」仍然成立。

    与均摊口径同样的局限：读路线时并不知道「共享能省钱」，所以选出的路线
    未必是共享收益最大那条。这是可接受的近似（只会高估，不会低估）。
    """
    if not root.tree or root.cost is None:
        return root

    paid: set[str] = set()

    def visit(node: WholeChoice) -> WholeChoice:
        if node.once and node.item in paid:
            # 已经付过：成本归零，子树也不必再算（那一份是给这条支路用的）。
            return WholeChoice(
                item=node.item,
                need=node.need,
                cost=0.0,
                own_cost=0.0,
                own_variance=0.0,
                once=True,
                already_paid=True,
                tree=True,
            )
        if node.once:
            paid.add(node.item)
        children = tuple(visit(child) for child in node.children)
        if not children or children == node.children:
            return node
        cost = node.own_cost + sum(child.cost or 0.0 for child in children)
        spread = node.own_variance + sum(child.variance for child in children)
        return replace(node, cost=cost, variance=spread, children=children)

    return visit(root)


def whole_choice(
    actionset,
    curves,
    item: str,
    demand: float,
    *,
    seen: frozenset[str] = frozenset(),
    memo: dict | None = None,
    actions_by_id: dict | None = None,
    want_children: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> WholeChoice:
    """按整次调用口径求「凑够 `demand` 件」最便宜的方案（含 `once` 去重）。

    `curves` 是全部物品的供给曲线（`solved.curves`）。`seen` 防环：递归回到
    当前链路已经出现过的物品时，该分支判为不可行。

    `memo` 可以跨调用共享（全库评分用），键是 `(物品, 件数)`——
    **`once` 去重发生在缓存之外**（`dedupe_once_costs`），否则键得带上
    「已付过哪些」、深树上会退化成指数级枚举。

    去重要求手里有整棵树，所以**无论 `want_children` 都要先把树解出来**：
    只报一个数、不去重的版本会和 `bg plan` 给出两个答案（`elytra` 实测
    1762.91 vs 1530.61）。不需要树时最后把 `children` 摘掉，不影响成本。
    """
    root = dedupe_once_costs(
        _solve_choice(
            actionset,
            curves,
            item,
            demand,
            seen=seen,
            memo=memo,
            actions_by_id=actions_by_id,
            want_children=True,
            exclude=exclude,
        )
    )
    return root if want_children else replace(root, children=())


def whole_choices(
    actionset,
    curves,
    item: str,
    demand: float,
    *,
    limit: int = 3,
) -> list[WholeChoice]:
    """按整次口径列前 `limit` 条路线：每次排除上一条的顶层动作。

    与 `rank_paths` 的语义一致——第 k 条只替换**本物品**的来源，前置各取自己的整次最优。
    每条都用独立的 `memo`：`exclude` 会让同一物品的最优解变化，共享缓存会串味。
    """
    by_id = {action.action_id: action for action in actionset.actions}
    seen_actions: set[str] = set()
    out: list[WholeChoice] = []
    while len(out) < max(1, limit):
        choice = whole_choice(
            actionset,
            curves,
            item,
            demand,
            memo={},
            actions_by_id=by_id,
            want_children=True,
            exclude=frozenset(seen_actions),
        )
        if choice.cost is None:
            break
        out.append(choice)
        if not choice.action_id:
            break
        seen_actions.add(choice.action_id)
    return out


def to_step_tree(choice: WholeChoice, *, depth: int = 32) -> "Step":
    """把整次口径的取舍转成路线图用的 `Step` 树。

    `Step.unit_cost` 取等效单价（整次成本 ÷ 需求），`whole_calls` 记住实际
    要完整执行几次——图据此显示「整次调用 = 52.22 TU」，而不是「9.44 × 0.54」。
    `depth` 只控制**展示**层：超出的节点不展开明细，成本仍然已经计入。
    """
    from .explain import Step

    children = (
        tuple(to_step_tree(child, depth=depth - 1) for child in choice.children)
        if depth > 0
        else ()
    )
    need = choice.need
    cost = choice.cost or 0.0
    return Step(
        item=choice.item,
        action_id=choice.action_id,
        unit_cost=(cost / need) if need > 0 else 0.0,
        need=need,
        calls=choice.calls or need,
        subtotal=cost,
        label=choice.label,
        source=choice.source,
        children=children,
        note=(
            "链上游已付过，这里不再计费"
            if choice.already_paid
            else (
                "展开到深度上限，成本已计入"
                if depth <= 0 and choice.children
                else ""
            )
        ),
        # 本动作自身那部分；`children` 的 `subtotal` 与之相加正好等于 `subtotal`。
        own_unit_cost=(choice.own_cost / need) if need > 0 else 0.0,
        # 整棵树都是整次口径：`whole_calls` 非 0 让展示层统一用「整次调用」措辞，
        # 不再把递归取整后的成本拆成「单价」。
        whole_calls=choice.calls,
    )

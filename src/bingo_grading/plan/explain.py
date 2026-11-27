"""把「某物品的第 k 优路线」摊成一条可读的链。

`solve` 算的是**单条最优方案**，`Solved.curves` 里虽然躺着每个物品的**全部**
候选与单价，但那是一张表，不是一条路。想看「改用 `oak_wood` 造木板会多花多少、
多花在哪一步」，需要把表按依赖关系重新串起来。

```text
minecraft:oak_planks  #1  0.2594 TU
  └ minecraft:oak_log ×1        1.0375   gather
minecraft:oak_planks  #2  0.3458 TU
  └ minecraft:oak_wood ×1       1.3833   craft
      └ minecraft:oak_log ×1.33 1.0375   craft（4 原木 → 3 木）
```

规则：

- 第 k 优只看**本物品**曲线的第 k 段；它下面的原料各取**自己的最优**段
  （局部最优链）。这样「次优差在哪一步」直接体现在第一层分叉上。
- `once` 前置（工具、站点）按 1 份算，`per_use` 前置按调用次数折算。
- `race` 合成段（`race[a|b]`，玩家不知道先碰到哪个）展开成并列成员。
- 递归有深度上限与路径栈，环上截断并标出来，不静默丢。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from bingo_grading.plan.actions import MODE_ONCE, Action, ActionSet, ActionError
from bingo_grading.plan.solve import Solved
from bingo_grading.plan.supply import Segment, SupplyError, merge_fixed

MAX_DEPTH = 10


def _seg_total(seg: Segment, need: float) -> float:
    """用这一段凑够 `need` 件的**总价**（边际 + 这一段自己的固定成本）。

    排序不能用单价：`cast:obsidian` 单价 0.1429 却要 48.4 的固定成本（铁桶），
    拿 4 件时它比 3.2 单价的开箱贵得多。方案层用 `Curve.cost` 真的求最小，
    路径树要跟它一致。
    """
    take = min(need, seg.capacity)
    return seg.unit_cost * take + sum(amount for _, amount in seg.fixed)


@dataclass(frozen=True)
class Step:
    """路径上的一步：一个动作，以及它在这条链上被调用几次。"""

    item: str
    action_id: str
    unit_cost: float
    need: float
    calls: float
    subtotal: float
    label: str = ""
    source: str = ""
    fixed: tuple[tuple[str, float], ...] = ()
    children: tuple["Step", ...] = ()
    note: str = ""
    # 本动作自身的每件耗时（`action.cost ÷ 单次产出`）。`unit_cost` 里已经折进了
    # `per_use` 前置的边际，展示时要能和它们分开，否则读者没法核对单价。
    own_unit_cost: float = 0.0
    # 这一层是 `once` 前置（工具、状态）：整笔进固定成本，不参与单价。
    once: bool = False
    # 整次口径：这个动作实际要完整执行几次（0 = 这棵树来自均摊曲线）。
    whole_calls: float = 0.0

    def as_dict(self) -> dict:
        out = {
            "item": self.item,
            "action_id": self.action_id,
            "unit_cost": round(self.unit_cost, 6),
            "need": round(self.need, 6),
            "calls": round(self.calls, 6),
            "subtotal": round(self.subtotal, 6),
            "label": self.label,
            "source": self.source,
        }
        if self.fixed:
            out["fixed"] = {name: round(amount, 6) for name, amount in self.fixed}
        if self.note:
            out["note"] = self.note
        if self.children:
            out["children"] = [child.as_dict() for child in self.children]
        return out

    def lines(self, depth: int = 0) -> list[str]:
        """人类可读的一行（带缩进），供 CLI 直接打印。"""
        pad = "  " * depth
        head = (
            f"{pad}{self.item} ×{self.need:g}"
            f"  [{self.unit_cost:.4f} TU/个，本步 {self.subtotal:.4f}]"
            f"  {self.action_id or '（无动作）'}"
        )
        out = [head]
        if self.fixed:
            total = sum(amount for _, amount in self.fixed)
            names = "、".join(f"{name} {amount:.3f}" for name, amount in self.fixed)
            out.append(f"{pad}  + 固定 {total:.4f}：{names}")
        if self.note:
            out.append(f"{pad}  ! {self.note}")
        for child in self.children:
            out.extend(child.lines(depth + 1))
        return out


@dataclass(frozen=True)
class PathTree:
    """某物品的第 `rank` 优路线。"""

    item: str
    rank: int
    unit_cost: float
    rank_gap: float
    root: Step
    note: str = ""
    # 这条路线凑够 `need` 件的**总价**（边际 + 固定）。`rank` 就是按它排的。
    total_cost: float = 0.0
    need: float = 1.0

    def as_dict(self) -> dict:
        out = {
            "item": self.item,
            "rank": self.rank,
            "unit_cost": round(self.unit_cost, 6),
            "total_cost": round(self.total_cost, 6),
            "need": self.need,
            "gap_vs_best": round(self.rank_gap, 6),
            "root": self.root.as_dict(),
        }
        if self.note:
            out["note"] = self.note
        return out

    def lines(self) -> list[str]:
        gap = "最优" if self.rank == 1 else f"比最优贵 {self.rank_gap:+.4f}"
        if self.need != 1.0 or self.root.fixed:
            price = f"{self.unit_cost:.4f} TU/个，{self.need:g} 个共 {self.total_cost:.4f} TU"
        else:
            price = f"{self.unit_cost:.4f} TU/个"
        head = f"{self.item} #{self.rank}  {price}（{gap}）"
        out = [head]
        if self.note:
            out.append(f"  ! {self.note}")
        # 根步骤自己也要显示：叶类物品（粘土、竹子）根本没有子节点，
        # 不写出来就只剩一行干巴巴的单价，看不出走的是地表/洞穴/矿道。
        out.append(
            f"  经 {self.root.action_id or '（无动作）'}"
            + (f"  需求 {self.root.need:g} 个" if self.root.need != 1.0 else "")
        )
        if self.root.fixed:
            total = sum(amount for _, amount in self.root.fixed)
            names = "、".join(f"{name} {amount:.3f}" for name, amount in self.root.fixed)
            out.append(f"     + 固定 {total:.4f}：{names}")
        for child in self.root.children:
            out.extend(child.lines(1))
        return out


def has_cycle(step: "Step") -> bool:
    """这条路线里有「要用的东西正是它在造的」那一段（钻石块 ↔ 钻石这类互逆配方）。"""
    for child in step.children:
        if child.note.startswith("环") or has_cycle(child):
            return True
    return False


def _best_segment(solved: Solved, item: str, need: float = 1.0) -> Segment | None:
    """取 `need` 件时方案层实际会走的那一段（按总价，不是按单价）。"""
    curve = solved.curve_of(item)
    if not curve.segments:
        return None
    try:
        return curve.segment_at(need)
    except SupplyError:
        return min(curve.segments, key=lambda seg: seg.unit_cost)


def _step(
    actionset: ActionSet,
    solved: Solved,
    item: str,
    seg: Segment,
    need: float,
    *,
    depth: int,
    stack: frozenset[str],
    seen_once: set[str],
) -> Step:
    """按 `seg` 展开 `item` 这一步，并递归它自己的原料。"""
    if item in stack:
        return Step(
            item=item,
            action_id=seg.action_id,
            unit_cost=seg.unit_cost,
            need=need,
            calls=need,
            subtotal=need * seg.unit_cost,
            label=seg.label,
            fixed=seg.fixed,
            note="环：这一份要绕回本路线自己（按最优价计），多绕一圈不合算",
            own_unit_cost=seg.unit_cost,
        )
    if depth <= 0:
        return Step(
            item=item,
            action_id=seg.action_id,
            unit_cost=seg.unit_cost,
            need=need,
            calls=need,
            subtotal=need * seg.unit_cost,
            label=seg.label,
            fixed=seg.fixed,
            note="递归深度用尽，下游未展开",
            own_unit_cost=seg.unit_cost,
        )
    if seg.members:
        children = []
        for member_id, member_cost in seg.members:
            try:
                action = actionset.get(member_id)
            except ActionError:
                continue
            children.append(
                _from_action(
                    actionset,
                    solved,
                    item,
                    action,
                    member_cost,
                    need,
                    depth=depth,
                    stack=stack | {item},
                    seen_once=seen_once,
                )
            )
        return Step(
            item=item,
            action_id=seg.action_id,
            unit_cost=seg.unit_cost,
            need=need,
            calls=need,
            subtotal=need * seg.unit_cost,
            label=seg.label or "并行来源合成",
            fixed=seg.fixed,
            children=tuple(children),
            note="这些来源玩家不知道会先碰到哪个，按速度相加",
            own_unit_cost=seg.unit_cost,
        )
    try:
        action = actionset.get(seg.action_id)
    except ActionError:
        return Step(
            item=item,
            action_id=seg.action_id,
            unit_cost=seg.unit_cost,
            need=need,
            calls=need,
            subtotal=need * seg.unit_cost,
            label=seg.label,
            fixed=seg.fixed,
            note="这个段不是单个动作（找不到同名动作），下游未展开",
            own_unit_cost=seg.unit_cost,
        )
    return _from_action(
        actionset,
        solved,
        item,
        action,
        seg.unit_cost,
        need,
        depth=depth,
        stack=stack | {item},
        seen_once=seen_once,
        fixed=seg.fixed,
    )


def _from_action(
    actionset: ActionSet,
    solved: Solved,
    item: str,
    action: Action,
    unit_cost: float,
    need: float,
    *,
    depth: int,
    stack: frozenset[str],
    seen_once: set[str],
    fixed: tuple[tuple[str, float], ...] = (),
) -> Step:
    per_call = action.yields.get(item) or 1.0
    calls = need / per_call
    children = []
    for requirement in action.requires:
        # `once` 前置（工具、站点）只要一份；`per_use` 按调用次数折算
        if requirement.mode == MODE_ONCE:
            if requirement.item in seen_once:
                children.append(
                    Step(
                        item=requirement.item,
                        action_id="",
                        unit_cost=0.0,
                        need=requirement.qty,
                        calls=0.0,
                        subtotal=0.0,
                        note="once 前置，链上游已付过",
                        once=True,
                    )
                )
                continue
            seen_once.add(requirement.item)
            sub_need = requirement.qty
        else:
            sub_need = requirement.qty * calls
        sub_seg = _best_segment(solved, requirement.item, sub_need)
        if sub_seg is None:
            children.append(
                Step(
                    item=requirement.item,
                    action_id="",
                    unit_cost=math.inf,
                    need=sub_need,
                    calls=sub_need,
                    subtotal=math.inf,
                    note=f"没有可用来源（{requirement.mode}）",
                    once=requirement.mode == MODE_ONCE,
                )
            )
            continue
        child = _step(
            actionset,
            solved,
            requirement.item,
            sub_seg,
            sub_need,
            depth=depth - 1,
            stack=stack,
            seen_once=seen_once,
        )
        if requirement.mode == MODE_ONCE:
            child = replace(child, once=True)
        children.append(child)
    # `seg.fixed` 是**这一段完整的一次性开销**（含全部上游能力，按名字已去重），
    # 与 `seg.unit_cost`（含直接前置的边际）一起构成这个节点的「整次」价。
    # 不要再扣子节点的同名项：那样父节点的数字就凑不出子节点的合计。
    return Step(
        item=item,
        action_id=action.action_id,
        unit_cost=unit_cost,
        need=need,
        calls=calls,
        subtotal=need * unit_cost,
        label=action.label,
        source=action.source,
        fixed=merge_fixed(tuple(action.overhead), fixed),
        children=tuple(children),
        own_unit_cost=action.cost / per_call,
    )


def rank_paths(
    actionset: ActionSet,
    solved: Solved,
    item: str | None = None,
    *,
    limit: int = 3,
    depth: int = MAX_DEPTH,
) -> list[PathTree]:
    """某物品前 `limit` 条路线，按单价升序。

    第 k 条只替换**本物品**的那一段，下游各自取最优——所以第 2 条与第 1 条
    的差别就是「第一层换了哪个来源」，正是要展示的东西。

    同价的两段共享同一个 `rank`（名次形如 1、1、3），近价不合并。
    """
    target = item or solved.target
    curve = solved.curve_of(target)
    need = solved.demand
    # 按「凑够 need 件的总价」排序：单价最低的那段可能带着很贵的固定成本，
    # 方案层不会选它，路径树排第一就成了假路线。
    ranked = sorted(curve.segments, key=lambda seg: _seg_total(seg, need))
    segments = ranked[: max(limit, 1)]
    if not segments:
        return []
    best = _seg_total(segments[0], need)
    trees: list[PathTree] = []
    rank = 0
    previous: float | None = None
    for position, seg in enumerate(segments, start=1):
        total = _seg_total(seg, need)
        # 同价的两段共享名次（1、1、3）：名次回答「排在哪个档」，不回答「差多少」。
        # 近价（只差千分之几）仍按位置排名，差额由 `rank_gap` 承担。
        if previous is None or not math.isclose(
            total, previous, rel_tol=1e-12, abs_tol=1e-12
        ):
            rank = position
            previous = total
        root = _step(
            actionset,
            solved,
            target,
            seg,
            need,
            depth=depth,
            stack=frozenset(),
            seen_once=set(),
        )
        trees.append(
            PathTree(
                item=target,
                rank=rank,
                unit_cost=seg.unit_cost,
                rank_gap=total - best,
                root=root,
                total_cost=total,
                need=need,
            )
        )
    return trees


def render(trees: list[PathTree]) -> str:
    return "\n".join(line for tree in trees for line in tree.lines())

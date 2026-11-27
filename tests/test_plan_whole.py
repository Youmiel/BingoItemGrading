"""整次调用口径：不可分割的动作至少做一次（`plan` 与 `grade` 共用）。"""

from __future__ import annotations

import pytest

from bingo_grading.plan import (
    MODE_ONCE,
    MODE_PER_USE,
    Action,
    ActionSet,
    Requirement,
    solve,
)
from bingo_grading.plan.whole import whole_choice, whole_choices


def _solved(actions, target: str = "minecraft:t", count: float = 1.0):
    actionset = ActionSet("whole", tuple(actions))
    return actionset, solve(actionset, target, count, prune_dominated=False)


def test_loot_trip_is_billed_once() -> None:
    """一整趟产出 5.53 件、成本 52.22：要 3 件也得付一整趟，不能按 0.54 趟算。"""
    actionset, solved = _solved(
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 3.0, MODE_PER_USE),),
            ),
            Action("loot:trip", "minecraft:p", 52.22, {"minecraft:p": 5.53}),
        )
    )
    choice = whole_choice(actionset, solved.curves, "minecraft:t", 1.0, want_children=True)
    assert choice.cost == pytest.approx(1.0 + 52.22)
    child = choice.children[0]
    assert child.action_id == "loot:trip"
    assert child.calls == 1.0
    # 连续松弛仍是按件的（这就是两套口径的差别，图不再直接展示它）。
    assert solved.curve_of("minecraft:p").cost(3.0) == pytest.approx(52.22 * 3 / 5.53)


def test_divisible_action_still_prorates() -> None:
    """`craft:` 可分割：要 3 件就只付 3 件的钱。"""
    actionset, solved = _solved(
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 3.0, MODE_PER_USE),),
            ),
            Action("craft:p", "minecraft:p", 52.22, {"minecraft:p": 5.53}),
        )
    )
    choice = whole_choice(actionset, solved.curves, "minecraft:t", 1.0, want_children=True)
    child = choice.children[0]
    assert child.calls == pytest.approx(3 / 5.53)
    assert choice.cost == pytest.approx(1.0 + 52.22 * 3 / 5.53)


def test_atomic_quantity_scales_with_calls() -> None:
    """前置要 2 趟就按 2 趟计：整次口径必须递归到原料层。"""
    actionset, solved = _solved(
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 2.0, MODE_PER_USE),),
            ),
            Action("kill:p", "minecraft:p", 10.0, {"minecraft:p": 1.0}),
        )
    )
    choice = whole_choice(actionset, solved.curves, "minecraft:t", 1.0, want_children=True)
    child = choice.children[0]
    assert child.calls == 2.0
    assert choice.cost == pytest.approx(1.0 + 20.0)


def test_once_dedup_applies_without_asking_for_children() -> None:
    """`want_children=False` 也走去重：`bg grade` 只报一个数，口径必须和 `bg plan` 一致。

    反例：去重信息藏在树里，只有 `want_children=True` 才建树时，`bg grade` 会按
    「每处各付一遍」报数（`elytra` 实测 1762.91 vs 1530.61），同一件事两个答案。
    """
    once = Requirement("capability:site", 1.0, MODE_ONCE)
    actionset, solved = _solved(
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(
                    Requirement("minecraft:a", 1.0, MODE_PER_USE),
                    Requirement("minecraft:b", 1.0, MODE_PER_USE),
                ),
            ),
            Action(
                "craft:a",
                "minecraft:a",
                2.0,
                {"minecraft:a": 1.0},
                requires=(once,),
            ),
            Action(
                "craft:b",
                "minecraft:b",
                3.0,
                {"minecraft:b": 1.0},
                requires=(once,),
            ),
            Action("travel:site", "capability:site", 40.0, {"capability:site": 1.0}),
        )
    )
    bare = whole_choice(actionset, solved.curves, "minecraft:t", 1.0)
    tree = whole_choice(
        actionset, solved.curves, "minecraft:t", 1.0, want_children=True
    )
    # 同一趟站点只收一次：1 + 2 + 3 + 40
    assert bare.cost == pytest.approx(46.0)
    assert tree.cost == pytest.approx(46.0)
    # 不要树时不返回 children，成本照样是去重后的
    assert bare.children == ()


def test_whole_choices_lists_routes_by_excluding_previous_top() -> None:
    """第 2 条路线 = 排除第 1 条的顶层来源，前置各取自己的整次最优。"""
    actionset, solved = _solved(
        (
            Action("make", "minecraft:t", 1.0, {"minecraft:t": 1.0}),
            Action("loot:a", "minecraft:t", 10.0, {"minecraft:t": 1.0}),
            Action("loot:b", "minecraft:t", 30.0, {"minecraft:t": 1.0}),
        )
    )
    choices = whole_choices(actionset, solved.curves, "minecraft:t", 1.0, limit=3)
    assert [item.action_id for item in choices] == ["make", "loot:a", "loot:b"]
    assert [item.cost for item in choices] == pytest.approx([1.0, 10.0, 30.0])

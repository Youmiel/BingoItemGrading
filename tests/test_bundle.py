"""组合需求：一次买齐多件物品，共享同一趟的产出。

`loot` 动作的 `yields` 本来就是「跑一趟拿到的一整套」，所以清单里几件如果
落在同一张表上，跑同一趟就都齐了——逐件算完再相加会重复付行程。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bingo_grading import grade
from bingo_grading.case.context import CaseContext
from bingo_grading.plan import solve
from bingo_grading.sources import actions

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "stats" / "26.3"
INDEX = ROOT / "build" / "26.3" / "source_index.jsonl"

needs_data = pytest.mark.skipif(
    not SNAPSHOT.exists() or not INDEX.exists(),
    reason=f"缺少快照 {SNAPSHOT} 或来源索引 {INDEX}",
)


@pytest.fixture(scope="module")
def ctx() -> CaseContext:
    return CaseContext.load(SNAPSHOT)


def _bundle_of(ctx: CaseContext, items: list[str]) -> tuple[object, float]:
    built = None
    for item in items:
        one = actions.build(ctx, item)
        if built is None:
            built = one
            continue
        for action in one.bundle.actions:
            built.emit(action)
    aset = built.actionset()
    bundle = grade.best_bundle(aset, solve(aset, items[0], 1.0).curves, {item: 1.0 for item in items})
    separate = 0.0
    for item in items:
        solved = solve(aset, item, 1.0)
        cost, _, _, _ = grade.best_whole_cost(aset, solved.curves, item, 1.0)
        separate += cost or 0.0
    return bundle, separate


@needs_data
def test_one_trip_covers_several_targets(ctx: CaseContext) -> None:
    """药水与海洋之心同在埋藏宝藏那张表上，跑同一趟就够了。

    原先用的是钻石/金锭/铁锭，但那三件的最优路线后来各自独立了
    （钻石走 `mine:cave:`、铁锭杀铁傀儡、金锭熔炼矿石），
    「同一趟 loot 覆盖三件」的前提不再成立。换两个仍在同一张表上的目标，
    测的仍是同一件事：`best_bundle` 会把共享行程的那趟算成一次。
    """
    items = ["minecraft:potion", "minecraft:heart_of_the_sea"]
    bundle, separate = _bundle_of(ctx, items)
    assert bundle.cost is not None
    assert bundle.cost < separate, (bundle.cost, separate)
    # 一趟 loot 同时覆盖两件，不该拆成两次行程。
    loots = [step for step in bundle.steps if step.action_id.startswith("loot:")]
    assert len(loots) == 1, [step.action_id for step in bundle.steps]
    assert set(loots[0].covers) == set(items)


@needs_data
def test_bundle_never_costs_more_than_separate(ctx: CaseContext) -> None:
    """没有共享产出时组合不等于更贵——分别买本来就该是组合的候选之一。"""
    bundle, separate = _bundle_of(ctx, ["minecraft:iron_pickaxe", "minecraft:bucket"])
    assert bundle.cost is not None
    assert bundle.cost <= separate + 1e-6, (bundle.cost, separate)


@needs_data
def test_bundle_covers_every_target(ctx: CaseContext) -> None:
    """方案必须覆盖清单里的每一件，不能漏。"""
    items = ["minecraft:diamond", "minecraft:bucket"]
    bundle, _ = _bundle_of(ctx, items)
    covered = {item for step in bundle.steps for item in step.covers}
    assert covered == set(items), covered

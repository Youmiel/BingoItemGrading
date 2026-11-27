"""方块来源的多路查找：结构 / 地表 / 地下 / 散点矿并列，由引擎挑最便宜的。

这三条测的是「并列」本身，而不是某个具体价格：

- 同一个方块的多条来源必须**同时**在图上（早先是「谁先出活谁独占」）；
- 同一个采集动作服务两件物品时不能被 ID 去重吞掉一件；
- 结构来源的行程按趟补收，拿得多就得多跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bingo_grading import gamedata, mining_facts, units
from bingo_grading.case.context import CaseContext
from bingo_grading.case.sources import ActionBundle, structure_block
from bingo_grading.case.sources.exposure import mine_surface
from bingo_grading.sources import actions

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "stats" / "26.3"

pytestmark = pytest.mark.skipif(
    not SNAPSHOT.exists(),
    reason=f"缺少快照 {SNAPSHOT}",
)


@pytest.fixture(scope="module")
def ctx() -> CaseContext:
    return CaseContext.load(SNAPSHOT)


def test_block_offers_structure_route_alongside_mining(ctx: CaseContext) -> None:
    """煤矿石同时有挖矿与结构两条来源，不是「有挖矿就不给结构」。

    `structure_content.blocks_per_start` 里确实记着村庄模板有煤矿石（地形混入），
    但那是数据质量的事，不该用「有挖矿就不出结构」这种顺序去掩盖——
    顺序一改，结论就跟着变。
    """
    built = actions.build(ctx, "minecraft:coal")
    ids = {action.action_id for action in built.actionset().actions}
    assert any(name.startswith("mine:cave:coal_ore") for name in ids), "挖矿路线没了"
    assert any(
        name.startswith("loot_block:coal_ore:") for name in ids
    ), "结构路线被挖矿路线挡掉了"


def test_same_block_action_serves_two_items(ctx: CaseContext) -> None:
    """铁矿石掉粗铁，也能被当方块取走——两条都要留在图上。

    `mine:cave:iron_ore` 这个 ID 只由方块与来源拼成，撞车时早先直接丢后一份，
    于是「先被访问到的物品独占该来源」。访问顺序取决于 `max_depth`，
    所以同一件物品在 `max_depth=3` 有 17 条方块来源、`max_depth=6` 只剩 1 条。
    """
    built = actions.build(ctx, "minecraft:raw_iron")
    ids = {action.action_id for action in built.actionset().actions}
    assert any(name.startswith("mine:cave:iron_ore") for name in ids), (
        "铁矿石 → 粗铁的方块来源没进图"
    )
    # 换个深度再看一遍：来源数量不该随递归深度变化。
    shallow = actions.build(ctx, "minecraft:raw_iron", max_depth=3)
    deep = actions.build(ctx, "minecraft:raw_iron", max_depth=6)
    shallow_ids = {
        a.action_id.split("@")[0]
        for a in shallow.actionset().actions
        if "minecraft:raw_iron" in a.yields
    }
    deep_ids = {
        a.action_id.split("@")[0]
        for a in deep.actionset().actions
        if "minecraft:raw_iron" in a.yields
    }
    assert shallow_ids == deep_ids, f"来源随深度变化：{shallow_ids ^ deep_ids}"


def test_structure_trip_amortises_over_per_start() -> None:
    """一处只有 2 个方块时，拿 5 个要跑 3 趟，行程补收 2 趟。

    第一趟由调用方的 `once` 前置付，`cost` 只含超出的部分。全额摊销会把
    第一趟摊成零头（`trial_chambers` 每处 5387 个蜡铜块 → 铜锭 0.05 TU），
    完全不摊又会宣称「一趟结构能无限出方块」。
    """
    bundle = ActionBundle(label="t")
    one = structure_block(
        bundle,
        None,  # type: ignore[arg-type]
        block="minecraft:no_such_block",
        item="minecraft:test_block",
        structure="minecraft:nowhere",
        batch=2.0,
        action_id="loot_block:test:nowhere",
        per_start=2.0,
        travel_per_trip=10.0,
    )
    over = structure_block(
        bundle,
        None,  # type: ignore[arg-type]
        block="minecraft:no_such_block",
        item="minecraft:test_block",
        structure="minecraft:nowhere",
        batch=5.0,
        action_id="loot_block:test:nowhere",
        per_start=2.0,
        travel_per_trip=10.0,
    )
    assert one is not None and over is not None
    # 未知方块按 dirt 的破坏秒数算；拿 2 个正好一趟，行程为零。
    per_block = mining_facts.break_seconds("dirt") / units.SECONDS_PER_TU
    assert one.cost == pytest.approx(2 * per_block)
    # 拿 5 个 = 2.5 趟，超出一趟的 1.5 趟按 10 TU 补收。
    assert over.cost == pytest.approx(5 * per_block + 1.5 * 10.0)


def test_structure_sources_ignore_measured_terrain(ctx: CaseContext) -> None:
    """有模板行的结构不拿「包围盒内全部方块」当结构来源。

    `village_taiga` 的实测行有 17 个 `coal_ore`、1597 块 `stone`（都是地形），
    模板行一个都没有——村庄用的是 `cobblestone` 与 `dirt_path`。
    混用会得出「跑一趟村庄拿 17 个煤矿石」。
    """
    coal_ore = ctx.structure_block_sources("minecraft:coal_ore")
    assert not any(set_id.endswith("villages") for set_id, _, _ in coal_ore), (
        f"村庄仍在提供煤矿石：{coal_ore}"
    )
    # 没有模板行、只有实测的结构（矿井）仍要能提供，否则矿石只剩一条路线。
    assert any(set_id.endswith("mineshafts") for set_id, _, _ in coal_ore)


def test_wax_off_reaches_unwaxed_blocks(ctx: CaseContext) -> None:
    """`waxed_X → X`（斧头刮蜡）要有路线，且原料自己可达。

    试炼大厅放的是打蜡形态，玩家要未打蜡那件。这条变换既不是配方也不是
    战利品表（RA 图里的 `transmutes` 边），缺了它 `oxidized_copper_door`
    直接不可得。
    """
    built = actions.build(ctx, "minecraft:oxidized_copper_door")
    moves = [a for a in built.actionset().actions if a.action_id.startswith("transmute:")]
    assert moves, "刮蜡动作没进动作图"
    requires = {r.item for a in moves for r in a.requires}
    assert "minecraft:waxed_oxidized_copper_door" in requires
    # 工具是**类别**（`tool:minecraft:axe`），要映射成具体物品才可达。
    assert "minecraft:wooden_axe" in requires
    assert all(
        "minecraft:axe" != r.item for a in moves for r in a.requires
    ), "`tool:minecraft:axe` 是类别名，不能直接当前置物品"
    supplied = {
        a.item
        for a in built.actionset().actions
        if "minecraft:waxed_oxidized_copper_door" in a.yields
    }
    assert supplied, "打蜡方块没有来源，刮蜡路线会不可达"


def test_concrete_solidifies_from_powder(ctx: CaseContext) -> None:
    """`*_concrete_powder` + 水 → `*_concrete`：RA 的 `bridge/convention` 边。

    数据包里没有这条规则（是代码行为），RA 用约定变换导出来了。
    以前一条都没接，15 色混凝土直接不可得。
    """
    built = actions.build(ctx, "minecraft:orange_concrete")
    moves = [
        a for a in built.actionset().actions
        if a.action_id.startswith("transmute:orange_concrete")
    ]
    assert moves, "混凝土没有固化路线"
    requires = {r.item for a in moves for r in a.requires}
    assert "minecraft:orange_concrete_powder" in requires
    assert actions.WATER_SOURCE in requires
    # 水源自己要有动作（走到水边装水），否则整条路不可达。
    assert any(
        a.item == actions.WATER_SOURCE for a in built.actionset().actions
    ), "水源能力没有来源动作"


def test_surface_cost_scales_with_blocks_per_item(ctx: CaseContext) -> None:
    """`entries_per_block` 是「一块方块掉几个」：掉落期望减半，凑 1 件要挖两倍块数。

    树叶掉木棍只有 3%（`yield_per_trigger`），漏掉这层折算会把代价压到实际的
    三十三分之一，于是「打树叶」比「原木 → 木板 → 木棍」还便宜。
    """
    full = mine_surface(
        ActionBundle(label="test"),
        ctx,
        block="minecraft:oak_leaves",
        item="minecraft:stick",
        batch=1.0,
        action_id="mine:surface:full",
        entries_per_block=1.0,
    )
    third = mine_surface(
        ActionBundle(label="test"),
        ctx,
        block="minecraft:oak_leaves",
        item="minecraft:stick",
        batch=1.0,
        action_id="mine:surface:third",
        entries_per_block=1 / 3,
    )
    assert full is not None and third is not None
    assert third.cost == pytest.approx(full.cost * 3)
    # 产量与一次性成本不随掉落率变：凑 1 件仍是 1 件，找首片只付一次。
    assert third.yields["minecraft:stick"] == 1.0
    assert third.overhead == full.overhead



def test_naturally_generated_block_with_9_to_1_recipe_is_mined(ctx: CaseContext) -> None:
    """Melon has a 9-to-1 recipe but generates naturally, so it keeps its mining route."""
    built = actions.build(ctx, "minecraft:melon_slice")
    ids = {a.action_id for a in built.actionset().actions if "minecraft:melon_slice" in a.yields}
    assert any(name.startswith("mine:vein:melon") for name in ids), f"melon has no mining route: {ids}"
    assert "minecraft:melon_slice" not in built.missing_prereqs


def test_compressed_block_still_has_no_scatter_route(ctx: CaseContext) -> None:
    """Coal blocks are not placed by any feature and stay compressed."""
    built = actions.build(ctx, "minecraft:coal_block")
    ids = {a.action_id for a in built.actionset().actions}
    assert not any(name.startswith("mine:vein:coal_block") for name in ids)

"""方块来源：地表 / 洞穴 / 矿脉 / 结构内放置，四条并列让引擎挑。"""

from __future__ import annotations

import math
from typing import Mapping
from bingo_grading import copper_facts, gamedata
from bingo_grading.case.context import CaseContext
from bingo_grading.case.sources import loot_site, mine_cave, mine_geode, mine_surface, mine_vein, resin_farm, seconds, structure_block
from bingo_grading.plan import Action, Requirement
from bingo_grading.plan.actions import MODE_ONCE, MODE_PER_USE
from bingo_grading.sources import index as source_index

from .capabilities import _ensure_access, _ensure_capability, _ensure_tool
from .common import DIMENSION_ACCESS, GEODE_SOURCES, RESIN_SOURCES, _short, _structure_maps
from .crops import plant_crop
from .graph import GraphActions




# ---------------------------------------------------------------- 扫表补漏


def _sweep_tables(
    ctx: CaseContext,
    result: GraphActions,
    items: Mapping[str, int],
    once_caps: set[str],
) -> None:
    """把「索引里没有、但战利品表里确实有」的来源补上。

    RA 的来源索引会漏掉部分表（例如 `pots/trial_chambers/corridor` —— 陶罐
    不是方块也不是箱子，`classify()` 没把它归到 `loot_table`）。只要有
    「表 → 结构集合」映射与箱数，下游就能自己算，不该因为上游没列就整条缺席。

    只针对**已经沿配方链走到的物品**，不额外引入新目标，避免把图撞大。
    """
    mapping = _structure_maps()
    for row in ctx.store.rows.values():
        if row.stat_type != "loot_expectation":
            continue
        subject = row.subject
        if f"loot:{_short(subject)}" in result._ids:
            continue
        expected = row.value.get("per_trigger_expected") or {}
        wanted = [item for item in items if item in expected]
        if not wanted:
            continue
        structure = mapping.set_for_table(subject)
        if not structure:
            continue
        # 这个结构已经有「跑一趟」动作把全部表合并了，不再补逐表路线。
        if structure in result.batched_structures:
            continue
        chests = ctx.loot_tables_per_site(
            structure, subject, dimension=ctx.structure_dimension(structure)
        )
        if chests is None:
            chests = ctx.containers_per_site(
                structure, dimension=ctx.structure_dimension(structure)
            )
        if chests is None:
            continue
        cap = _ensure_capability(
            ctx, result, once_caps, structure, route=""
        )
        requires: tuple[Requirement, ...] = ()
        if cap:
            requires = (Requirement(cap, 1.0, MODE_PER_USE),)
        containers = ctx.containers_per_site(
            structure, dimension=ctx.structure_dimension(structure)
        )
        action = loot_site(
            result.bundle,
            ctx,
            subject=subject,
            structure=structure,
            chests_per_site=chests.value,
            open_containers=containers.value if containers else None,
            primary=wanted[0],
            action_id=f"loot:{_short(subject)}",
            with_travel=not cap,
            requires=requires,
            dimension=ctx.structure_dimension(structure),
        )
        if action is None:
            continue
        result.bundle.note(
            f"{subject} 不在来源索引里，按战利品表直接补上（{chests.detail}）"
        )
        result.emit(action)



def _block(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    pack: str,
    once_caps: set[str],
) -> Action | None:
    """方块掉落：四条来源并列，让引擎自己挑最便宜的。

    四条模型的适用范围互不重叠，各按自己的口径定价：

    | 来源 | 代价 | 适用 |
    |---|---|---|
    | 结构放置 | 每处几个 → 要跑几趟 | 只在结构里生成的方块 |
    | 地表露头 | 走过去 + 挖掉 | 贴天空/水、看得见 |
    | 洞穴暴露 | 下潜 + 沿洞走 | 埋在地里但暴露在洞穴 |
    | 3D 挖掘 | 竖井 + 挖穿岩层 | 真正散点分布 |

    **四个都并列，不做优先级**：哪条便宜完全由数据决定，不再用
    `if not made` 这种先后顺序去猜——那个顺序实际上是在猜「这个方块是什么
    方式生成的」，猜错就会把一个便宜来源误判成昂贵（或反之）。
    """
    block = record.subject
    if block in GEODE_SOURCES:
        # 紫水晶洞不能用散点模型：按 `1/ρ` 线性外推要挖几十万块（实测算出 6360 TU）。
        # 洞是球形空腔，挖到任意一块洞壁就能把整窝成熟簇采走。
        return mine_geode(
            result.bundle,
            ctx,
            item=record.item,
            action_id=f"mine:geode:{_short(block)}",
        )
    if block in RESIN_SOURCES:
        # 树脂团方块不自然生成：它是夜里打嘎枝时 heart 长在原木上的。散点/
        # 地表/洞穴/结构四条模型全都返回 None，只剩这一条可重复的农场路线。
        action = resin_farm(
            result.bundle,
            ctx,
            item=record.item,
            action_id="harvest:resin_clump:creaking_heart",
        )
        if action is not None:
            result.emit(action)
        return None
    # 作物方块：不自然生成，四条挖掘模型全返回 None。真实来源是「种子种下去」。
    # 放在四条模型之前：发一条就 return，不给后面留重复的无效尝试。
    if block in gamedata.CROP_SEEDS:
        made_crop = plant_crop(record, ctx, result)
        if made_crop:
            return None
    entries = float(record.yield_per_trigger or 1.0)
    tool_requires = _ensure_tool(ctx, result, once_caps, block)
    made = 0
    surface = mine_surface(
        result.bundle,
        ctx,
        block=block,
        item=record.item,
        batch=1.0,
        action_id=f"mine:surface:{_short(block)}",
        requires=tool_requires,
        entries_per_block=entries,
    )
    if surface is not None:
        result.emit(surface)
        made += 1
    cave = mine_cave(
        result.bundle,
        ctx,
        block=block,
        item=record.item,
        batch=1.0,
        action_id=f"mine:cave:{_short(block)}",
        requires=tool_requires,
        entries_per_block=entries,
    )
    if cave is not None:
        result.emit(cave)
        made += 1
    # 方块在哪个维度有非零密度就在哪挖。下界矿石在主世界群系下只有空行，
    # 按默认维度查会把整条挖矿路线判掉。
    block_dimension = ctx.block_dimension(block)
    cross_requires: tuple[Requirement, ...] = ()
    if block_dimension != "minecraft:overworld":
        access = DIMENSION_ACCESS.get(block_dimension)
        if access is not None:
            access = _ensure_access(ctx, result, once_caps, block_dimension, access)
            if access:
                cross_requires = (Requirement(access, 1.0, MODE_ONCE),)
    # 只有**会自然生成**的方块才有「散点分布」这回事。煤块 / 钻石块 / 铁块
    # 这类 9 合 1 的压缩方块没有 worldgen feature，`block_density` 那几行
    # 只是结构里的几堆；按散点模型算（一维 `1/ρ` 或二维搜索都一样）会得出
    # 「一挖顶九个」，于是 `craft:coal#0`（9 煤/块）把煤拉到 1.35 TU。
    # 它们只该有合成与结构两路来源。
    #
    # 判据用「配方里有 ≥4 个同一原料」，不用 `worldgen.jsonl` 的 feature 引用：
    # 那个表**覆盖不全**——鲜巢是 tree decorator，上游一行都没有，拿它当
    # 「不自然生成」的凭据会把蜂巢的散点路线误删。
    #
    # Blocks placed by biome features are not compressed blocks (melon, packed ice).
    conversions, _layer = ctx.recipe_conversions(block, pack=pack)
    compressed = any(
        qty >= 4 for conversion in conversions for _, qty in conversion.inputs
    ) and not ctx.placed_by_biome_feature(block, pack=pack)
    vein = (
        mine_vein(
            result.bundle,
            ctx,
            block=block,
            item=record.item,
            batch=1.0,
            entries_per_block=entries,
            dimension=block_dimension,
            requires=cross_requires + tool_requires,
            action_id=f"mine:vein:{_short(block)}",
            # 只有真正埋着的方块才用「挖穿岩层」的矿道模型。地表/树上的稀疏
            # 方块（蜂巢 ρ=0.0048/区块、紫颂花、仙人掌花）`1/ρ` 线性外推会
            # 给出五位数——同一处蜂巢取蜜只要 4.85 TU，作为物品却要 111456 TU。
            search_ok=record.route != "underground",
        )
        if not compressed
        else None
    )
    if compressed:
        result.bundle.note(
            f"{block} 是压缩方块（配方里 ≥4 个同一原料），不作散点矿，只留合成与结构来源"
        )
    if vein is not None:
        result.emit(vein)
        made += 1
    # 结构放置也是候选：每处几个 × 跑几趟的线性供给，第一趟由 `once` 前置
    # 付，超出部分按趟补（见 `structure_block`）。
    #
    # **不拿「这个方块会不会自然生成」去筛它**。曾经试过：用 `worldgen.jsonl`
    # 的 feature 引用把「会自然生成」的方块从结构来源里剔掉，理由是对矿石
    # 而言 `blocks_per_start` 里那份是地形混入（`village_taiga` 模板里有 17 个
    # `coal_ore`）。结果剔掉的代价比收益大得多：`glow_lichen` 437→6.61 TU、
    # `sculk_shrieker` 167212→1790 TU 都是靠结构来源救回来的，而它们同样
    # 「会自然生成」。真正让混入不致命的是**定价**：一趟结构只摊到能装的
    # 那几个方块，污染项（每处 17 个煤矿石）算下来反而比挖矿贵，引擎不会选。
    made += _structure_placed(record, ctx, result, once_caps)
    # 铜质方块的自然氧化：不是挖出来也不是合成，是「等它锈」。
    made += _oxidation_source(record, result, pack)
    # 铁砧的损坏链：anvil → chipped_anvil → damaged_anvil，用着用着就掉一级。
    made += _anvil_wear_source(record, result)
    # 眼眸花的昼夜开合：睁开的形态是闭合的那一种等到天黑。
    made += _eyeblossom_source(record, result)
    if not made:
        # 压块类（铁块/金块/钻石块）没有密度也没有结构放置，但它们 9 合 1，
        # 合成路线已经在图上。分开记账，免得和「真的没路」混在一起数。
        conversions, _ = ctx.recipe_conversions(block, pack=pack)
        if conversions:
            result.uncovered[f"缺方块密度（有合成替代）:{block}"] += 1
        else:
            result.uncovered[f"缺方块密度与暴露:{block}"] += 1
        return None
    return None



def _eyeblossom_source(
    record: source_index.SourceRecord,
    result: GraphActions,
) -> int:
    """眼眸花随昼夜开合：睁开的形态 = 闭合的那一种等到天黑。

    两种形态是**同一个方块的方块状态**（`EYEBLOSSOM_OPEN`），上游按「挖了掉
    自己」各登记一条 `block_drop`，于是 `open_eyeblossom` 只剩「挖它自己」
    ——而它只存在于苍白之园，密度模型四条全算不出。真实来源是挖一朵闭合的、
    等到天黑（它自己会睁眼），所以成本 = 闭合花 + 半个昼夜的等待。
    """
    if record.item != "minecraft:open_eyeblossom":
        return 0
    closed = "minecraft:closed_eyeblossom"
    result.emit(
        Action(
            action_id=f"bloom:{_short(record.item)}",
            item=record.item,
            cost=seconds(gamedata.EYEBLOSSOM_WAIT_SECONDS),
            yields={record.item: 1.0},
            requires=(Requirement(closed, 1.0, MODE_PER_USE),),
            label=(
                f"挖一朵 {_short(closed)} 放到天黑，它自己睁开变成 "
                f"{_short(record.item)}（等 {gamedata.EYEBLOSSOM_WAIT_SECONDS / 60:.0f} 分钟）"
            ),
            source="eyeblossom:daynight",
        )
    )
    return 1



def _anvil_wear_source(
    record: source_index.SourceRecord,
    result: GraphActions,
) -> int:
    """铁砧的降级链：`anvil` 用着用到坏，变成 `chipped_anvil` 再变 `damaged_anvil`。

    降一级**不是挖掘也不是合成**：上游按挖了掉自己把它登记成 `block_drop`，
    于是两条损坏形态只剩「结构里挖一个」（林地府邸那 1067 TU），而没有
    密度自然就算不出别的东西。真实来源是「拿上一级的铁砧做一次操作」，
    每次 12% 概率掉级（`ANVIL_DEGRADE_CHANCE`），所以期望 1/0.12 次操作。
    """
    try:
        stage = gamedata.ANVIL_STAGES.index(record.item)
    except ValueError:
        return 0
    if stage == 0:
        return 0
    previous = gamedata.ANVIL_STAGES[stage - 1]
    uses = 1.0 / gamedata.ANVIL_DEGRADE_CHANCE
    cost = seconds(uses * gamedata.ANVIL_USE_SECONDS)
    result.emit(
        Action(
            action_id=f"wear:{_short(record.item)}",
            item=record.item,
            cost=cost,
            yields={record.item: 1.0},
            requires=(Requirement(previous, 1.0, MODE_PER_USE),),
            label=(
                f"拿 {_short(previous)} 不断修东西，直到掉一级变成 "
                f"{_short(record.item)}（每次 {gamedata.ANVIL_DEGRADE_CHANCE:.0%} 概率，"
                f"期望 {uses:.1f} 次 × {gamedata.ANVIL_USE_SECONDS:g}s）"
            ),
            source=f"anvil_wear:{record.item}",
        )
    )
    return 1



def _oxidation_source(
    record: source_index.SourceRecord,
    result: GraphActions,
    pack: str,
) -> int:
    """铜质方块的自然氧化：从同族更不锈的那一级等过来。

    上游把这些变种登记成 `block_drop`（挖了掉自己），但它们只由氧化生成、
    没有密度也不在结构里，四条挖掘模型全返回 0。真实来源是「放一块更不锈的、
    等随机刻把它氧化」，时间由 `gamedata.copper_oxidize_seconds` 给出。

    氧化链本身是**数据推出**的（`copper_facts` 从 `block_transform` 的除锈对
    反向取），所以铜格栅/铜链/避雷针/铜傀儡雕像这些族自动全覆盖。
    """
    entry = copper_facts.load(pack).get(record.item)
    if entry is None:
        return 0
    seconds = gamedata.copper_oxidize_seconds(entry.stage)
    result.emit(
        Action(
            action_id=f"oxidize:{_short(record.item)}",
            item=record.item,
            cost=seconds,
            yields={record.item: 1.0},
            requires=(Requirement(entry.previous, 1.0, MODE_PER_USE),),
            label=(
                f"等 {_short(entry.previous)} 自然氧化成 {_short(record.item)}"
                f"（第 {entry.stage} 级，孤立放置约 {seconds / 60:.0f} 分钟）"
            ),
            source=f"copper_oxidation:{record.item}",
        )
    )
    return 1


def _structure_placed(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
) -> int:
    """按「结构里每处放几个」造方块来源。返回造了几个动作。"""
    block = record.subject
    placed = ctx.structure_block_sources(block)
    made = 0
    for structure, per_start, variant in placed:
        # 第一趟行程挂成 `once` 前置：跑一趟结构是真的要跑，不能把「走到
        # 试炼大厅」按「一处 5387 个铜块」摊成零头（那会把铜锭算成
        # 0.05 TU，半个多世纪前的「跑过去」变成了 0.35 秒）。超过一处的
        # 需求量由 `travel_per_trip` 按趟补收。
        cap = _ensure_capability(ctx, result, once_caps, structure, route="")
        travel = result.bundle.provenance.get(f"travel:{structure}")
        travel_cost = travel.value if travel is not None else 0.0
        reqs: list[Requirement] = []
        if cap is not None:
            reqs.append(Requirement(cap, 1.0, MODE_ONCE))
        # 跨维度开门也是 `once`：门开一次就一直在，不随趟数重付。
        access = DIMENSION_ACCESS.get(ctx.structure_dimension(structure))
        if access is not None:
            resolved = _ensure_access(
                ctx, result, once_caps, ctx.structure_dimension(structure), access
            )
            reqs.append(Requirement(resolved or access, 1.0, MODE_ONCE))
        requires: tuple[Requirement, ...] = tuple(reqs)
        # 结构里的方块也要挖：废弃传送门的黑曜石一样要钻石镐才能拿走。
        # `loot_block:*` 以前只挂「跑到结构」的前置，挖的代价里没有任何
        # 工具门槛，于是「跑一趟传送门」连带把黑曜石定价成 0.16 TU/块。
        requires = requires + _ensure_tool(ctx, result, once_caps, block)
        action = structure_block(
            result.bundle,
            ctx,
            block=block,
            item=record.item,
            structure=structure,
            batch=1.0,
            action_id=f"loot_block:{_short(block)}:{_short(structure)}",
            find_seconds=gamedata.STRUCTURE_FIND_SECONDS,
            requires=requires,
            per_start=per_start,
            entries_per_block=float(record.yield_per_trigger or 1.0),
            capacity=math.inf,
            travel_per_trip=travel_cost,
        )
        if action is not None:
            result.bundle.note(
                f"{block} 结构来源：{variant} 每处 {per_start:.2f} 个，"
                f"拿 1 个平均跑 {1.0 / per_start:.2f} 趟（每趟 {travel_cost:.1f} TU）"
                if per_start > 0
                else f"{block} 结构来源：{variant} 每处 {per_start:.2f} 个"
            )
            result.emit(action)
            made += 1
    return made

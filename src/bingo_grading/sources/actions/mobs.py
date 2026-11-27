"""实体来源：击杀、剪毛、交互，以及刷怪权重与袭击。"""

from __future__ import annotations

from bingo_grading import gamedata
from bingo_grading.case.context import CaseContext
from bingo_grading.case.context.model import FISH_WATER_BIOMES
from bingo_grading.case.sources import seconds
from bingo_grading.plan import Action, Requirement
from bingo_grading.plan.actions import MODE_ONCE, MODE_PER_USE
from bingo_grading.sources import index as source_index

from .capabilities import _ensure_access, _ensure_capability
from .common import DIMENSION_ACCESS, _short, cost_cv
from .graph import GraphActions




def _shear(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    pack: str,
    once_caps: set[str],
) -> Action | None:
    """剪羊毛：找到羊、拿剪刀剪一次。

    与「打死它」是两条不同的路：羊活着能反复剪，掉落表是 `shear` 触发而不是
    `kill`。`loot_expectation` 里已经按 trigger 存了期望（白羊毛 2.0/次），
    所以不用自己算概率。

    剪刀是 `once`：一把用到底，不消耗。
    """
    subject = record.subject
    mob = subject.split("/")[0]
    found = ctx.mob_search_cost(mob)
    if found is None:
        result.uncovered[f"缺生物密度({_short(mob)}):{record.item}"] += 1
        return None
    got = ctx.yield_per_trigger(
        source_type="shear",
        subject=subject,
        item=record.item,
        condition=record.yield_condition or "",
    )
    if got is None or got.value <= 0:
        result.uncovered[f"缺剪毛期望:{subject}"] += 1
        return None
    travel, density = found
    result.bundle.record(f"mob:{mob}", density)
    result.visit("minecraft:shears", 0)
    return Action(
        action_id=f"shear:{_short(record.item)}:{_short(mob)}",
        item=record.item,
        cost=travel.value + seconds(gamedata.SHEAR_SECONDS),
        yields={record.item: got.value},
        requires=(Requirement("minecraft:shears", 1.0, MODE_ONCE),),
        label=(
            f"找到一只 {_short(mob)} 剪一次毛：每次 {got.value:g} 个"
            f"（搜索 {travel.value:.2f} TU + 剪 {gamedata.SHEAR_SECONDS:g}s）"
        ),
        source="loot_expectation:shear",
        # 不传 condition：剪毛行按 `tools=present` 存，`yield_condition` 是空串时
        # 归一化之后反而对不上。查不到就是 0，不猜。
        yield_cv=cost_cv(ctx, subject, record.item, got.value),
    )



def _mob_interact(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
) -> Action | None:
    """拿容器对生物用一下：挤奶。来自 `bridge/entity_behaviour`。

    这不是击杀也不是「用桶舀」：生物活着、容器被消耗成结果物品。
    前置从 `notes` 里读（`item:minecraft:bucket`），不写死。
    """
    subject = record.subject
    containers = [
        note.split(":", 1)[1]
        for note in record.notes or ()
        if note.startswith("item:")
    ]
    if subject.startswith("bridge:"):
        # 取蜜：蜂巢的区块密度 → 搜索距离，玻璃瓶对蜂巢。
        #
        # **只对这一类走方块交互**。`bridge/entity_behaviour` 里还有
        # `filled_map`（制图师给地图）、`written_book`（书与笔）、
        # `player_head`（玩家头颅，根本不是交互）——它们共用
        # `bridge:behaviour:<name>:0` 这个 subject 形状，早先一律转去取蜜，
        # 于是「玩家头颅」被算成「找蜂巢取一瓶蜂蜜」3.10 TU。
        if "honey" in subject or "beehive" in subject:
            return _block_interact(record, ctx, result, containers)
        result.uncovered[f"未建模的行为:{subject}"] += 1
        return None
    found = ctx.mob_search_cost(subject)
    if found is None:
        result.uncovered[f"缺生物密度({_short(subject)}):{record.item}"] += 1
        return None
    travel, density = found
    result.bundle.record(f"mob:{subject}", density)
    requires: list[Requirement] = []
    for name in containers:
        result.visit(name, 0)
        # 桶被消耗成奶桶，所以是 per_use 而不是 once。
        requires.append(Requirement(name, 1.0, MODE_PER_USE))
    return Action(
        action_id=f"interact:{_short(record.item)}:{_short(subject)}",
        item=record.item,
        cost=travel.value + seconds(gamedata.MILK_SECONDS),
        yields={record.item: 1.0},
        requires=tuple(requires),
        label=(
            f"找到一只 {_short(subject)} 用{_short(containers[0]) if containers else '容器'}"
            f"取一次（搜索 {travel.value:.2f} TU + {gamedata.MILK_SECONDS:g}s）"
        ),
        source="bridge/entity_behaviour",
    )



def _block_interact(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    containers: list[str],
) -> Action | None:
    """对**方块**用容器：取蜜（玻璃瓶对蜂巢）。

    `bridge:behaviour:honey_bottle_from_beehive` 的 `notes` 里写的是
    `item:minecraft:beehive`——那是玩家合成的蜂巢，而自然生成的是 `bee_nest`。
    两者都能取蜜，按密度更高的那个找：自然蜂巢才是玩家真正会去找的。
    """
    candidates = ("minecraft:bee_nest", "minecraft:beehive")
    best: tuple[float, str, str] | None = None
    for block in candidates:
        got = ctx.block_search_cost(block)
        if got is None:
            continue
        if best is None or got[0].value < best[0]:
            best = (got[0].value, got[1], block)
    if best is None:
        result.uncovered[f"缺方块密度（交互）:{record.subject}"] += 1
        return None
    cost, detail, block = best
    requires: list[Requirement] = []
    for name in containers:
        if name == "minecraft:beehive":
            continue  # 那是目标方块，不是要付出的工具
        result.visit(name, 0)
        # 玻璃瓶被消耗成蜂蜜瓶。
        requires.append(Requirement(name, 1.0, MODE_PER_USE))
    return Action(
        action_id=f"interact:{_short(record.item)}:{_short(block)}",
        item=record.item,
        cost=cost + seconds(gamedata.HONEY_SECONDS),
        yields={record.item: 1.0},
        requires=tuple(requires),
        label=(
            f"找到一处 {_short(block)} 取一瓶蜂蜜"
            f"（{detail} + 取 {gamedata.HONEY_SECONDS:g}s）"
        ),
        source="bridge/entity_behaviour",
    )



def _biome_mob_weighted(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
    mob: str,
    per_kill: float,
) -> int:
    """群系自然刷怪：到那个群系、等它刷出来、打死。

    这是**结构类与事件类之外的退路**：`ghast` 不在任何结构里，只在
    `biome:minecraft:nether_wastes` 之类的地方有权重。做法与结构权重一致
    （`w / W` 是刷到它的机率，平均打死 `W / w` 只），
    差别只在「等一只」的周期来自 `gamedata.NATURAL_SPAWN_SECONDS`。

    **这是全仓唯一一处靠拍的时间常量**，所以只在没有结构/事件来源时才用：
    `_structure_mob_placed` → `_structure_mob_weighted` → `_raid_mob` → 这里。
    """
    made = 0
    # 等一只的周期与击杀并行：等待计时不会因为你在打怪而暂停，
    # 所以整只耗时是两者的**大者**，不是和。
    cycle = seconds(max(gamedata.NATURAL_SPAWN_SECONDS, gamedata.KILL_SECONDS))
    for biome, weight, total, category in ctx.biome_spawn_weights(mob):
        found = ctx.biome_search_cost(biome)
        if found is None:
            continue
        travel, dimension = found
        requires: list[Requirement] = []
        access = DIMENSION_ACCESS.get(dimension)
        if access is not None:
            resolved = _ensure_access(ctx, result, once_caps, dimension, access)
            # `None` = 这个能力没有构造可展开（`uncovered` 里已记），此时不挂前置，
            # 与 `_structure_mob_weighted` 同一口径。
            if resolved is not None:
                requires.append(Requirement(resolved, 1.0, MODE_ONCE))
        share = weight / total
        kills = max(1.0, round(1.0 / share, 2))
        cost = travel.value + kills * cycle
        # 打死 `kills` 只里只有**一只**是目标生物，所以产出是 `per_kill`，
        # 不是 `kills × per_kill`（后者等于假设每只都掉，把单价压低了 1/share 倍）。
        yields = per_kill
        action = Action(
            action_id=f"roam:{_short(record.item)}:{_short(mob)}:{_short(biome)}",
            item=record.item,
            cost=cost,
            yields={record.item: yields},
            requires=tuple(requires),
            label=(
                f"在 {_short(biome)} 等 {_short(mob)} 刷出来"
                f"（占 {category or '同类'} 权重 {share * 100:.0f}%，"
                f"平均打死 {kills:.1f} 只才出 1 只；{travel.detail} + "
                f"{kills * max(gamedata.NATURAL_SPAWN_SECONDS, gamedata.KILL_SECONDS):g}s/只"
                f"（等待与击杀取大者）），"
                f"这只掉 {per_kill:g} 个"
            ),
            source="mob_spawn:biome",
        )
        result.emit(action)
        made += 1
    return made



def _per_kill(record: source_index.SourceRecord) -> float:
    """打死一只给多少个目标物品。

    优先用战利品表算出来的期望（`loot_expectation`）；数据层没有时退到
    `gamedata.PROBABILITY_ROWS` 登记的「触发一次掉 1 个的概率」——对 `mob_drop`
    来说两者是同一个量。

    走第二条路的是**装备掉落**：MC 1.21 起「手持/穿戴的装备在死亡时掉落」
    从战利品表移进了实体类的 `dropCustomDeathLoot`，`entities/<mob>.json`
    里只剩独立掉落池。上游只能给出「哪个槽位的装备会掉」（`drops_source`
    的 `meta.slot`），概率拿不到，所以三叉戟这类只能登记。
    """
    for value in (record.yield_per_trigger, record.probability):
        if value is not None and value > 0:
            return float(value)
    return 0.0



def _mob(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    pack: str,
    once_caps: set[str],
) -> Action | None:
    """生物掉落：找到一只、打死、拿走它的掉落。"""
    if record.family == "code/mob_buckets":
        # 「用桶舀」不是「打死」：掉落表里没有、也不需要掉落期望。
        # 两条来源**并列竞争**，不做优先级（与 `_block` 的四条模型一致）：
        #
        # - 群系刷怪：美西鰭/硫磺立方在 `lush_caves`/`sulfur_caves` 有刷怪权重，
        #   和普通怪一样周期刷新，只是收获方式改成舀；
        # - 走到最近水域舀一条：普通鱼（鳕鱼/鲑鱼/河豚）就是这样拿的。
        #
        # 两条各自会自己判「适不适用」（`_mob_bucket` 会拒绝不在任何地表水域
        # 刷的生物），所以这里直接两边都造，让引擎挑便宜的。
        mob = record.stat_subject or record.subject
        made = _bucket_mob_weighted(record, ctx, result, once_caps, mob)
        # `_mob_bucket()` 返回动作而不自己 emit（它原来是被 `_from_record` 直接
        # 返回给上层的），所以这里要显式发出去，否则那条路线的动作会凭空消失。
        scoop = _mob_bucket(record, ctx, result)
        if scoop is not None:
            result.emit(scoop)
            made += 1
        if not made:
            result.uncovered[f"缺生物密度({_short(mob)}):{record.item}"] += 1
        return None
    if record.family == "bridge/entity_behaviour":
        # 挤奶/取蜜：容器对生物或方块用一下，不是击杀。
        return _mob_interact(record, ctx, result)
    per_kill = _per_kill(record)
    if per_kill <= 0:
        result.uncovered[f"缺每次掉落期望({_short(record.subject)}):{record.item}"] += 1
        return None
    # `charged_creeper/<mob>` 的子表名就是实体 ID，但**表路径必须留着**——
    # 剥成 `creeper` 会和 `entities/creeper` 撞名（头颅因此查错表）。
    # 所以查「找到一只要多久」时单独取路径末段；`stat_subject` 那边是集合名，
    # 不能用（`base_entity` 会把它截成 `minecraft:charged_creeper`）。
    raw = record.subject
    if raw.startswith("minecraft:charged_creeper/"):
        lookup = f"minecraft:{raw.partition('/')[2]}"
    else:
        lookup = record.stat_subject or raw
    found = ctx.mob_search_cost(lookup)
    if found is None:
        # 冇敌生物没有自然密度数据（依赖玩家活动，C17 已判定不可测），
        # 但「哪个结构有它的刷怪笼」是结构事实，走那条路。
        return _structure_mob(record, ctx, result, once_caps, lookup)
    travel, density = found
    result.bundle.record(f"mob:{lookup}", density)
    # `loot_expectation` 的 `kill` 行用裸实体 ID（`minecraft:blaze`），而
    # `record.subject` 是表路径（`minecraft:entities/blaze`），直接查一定落空。
    loot_subject = record.stat_subject or record.subject
    if "/" in loot_subject:
        loot_subject = f"minecraft:{loot_subject.rpartition('/')[2]}"
    return Action(
        action_id=f"kill:{_short(record.item)}:{_short(lookup)}",
        item=record.item,
        cost=travel.value + seconds(gamedata.KILL_SECONDS),
        yields={record.item: float(per_kill)},
        label=(
            f"找到并打死一只 {_short(lookup)}：搜索 {travel.value:.2f} TU"
            f"（{density.detail}）+ 击杀 {gamedata.KILL_SECONDS:g}s，"
            f"每次给 {per_kill:g} 个"
        ),
        source="mob_density+loot_expectation",
        # 稀有掉落（凋灵骷髅头 2.5%）的 `per_trigger_cv` 能到 6 以上，
        # 不接方差的话这类来源的尾巴完全看不见。
        yield_cv=cost_cv(ctx, loot_subject, record.item, per_kill),
    )



def _bucket_mob_weighted(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
    mob: str,
) -> int:
    """用桶舀**周期刷怪**的生物：繁茂洞穴的美西鰭、硫磺洞穴的硫磺立方。

    它们与普通怪一样按群系刷怪表周期性刷新（`mob_spawn` 里的 `biome:` holder + 权重），
    所以「等一只」的时长与 `_biome_mob_weighted` 同一口径；差别只在收获方式——
    不用打死，拿桶舀一下就行（`SCOOP_SECONDS`）。

    与 `_mob_bucket`（走到最近地表水域舀鱼）是**并列候选**：
    哪里便宜由数据决定，不在这里判先后。
    """
    made = 0
    cycle = seconds(gamedata.NATURAL_SPAWN_SECONDS + gamedata.SCOOP_SECONDS)
    for biome, weight, total, category in ctx.biome_spawn_weights(mob):
        found = ctx.biome_search_cost(biome)
        if found is None:
            continue
        travel, dimension = found
        requires: list[Requirement] = []
        access = DIMENSION_ACCESS.get(dimension)
        if access is not None:
            resolved = _ensure_access(ctx, result, once_caps, dimension, access)
            if resolved is not None:
                requires.append(Requirement(resolved, 1.0, MODE_ONCE))
        result.visit("minecraft:bucket", 0)
        requires.append(Requirement("minecraft:bucket", 1.0, MODE_PER_USE))
        share = weight / total
        tries = max(1.0, round(1.0 / share, 2))
        cost = travel.value + tries * cycle
        result.emit(
            Action(
                action_id=f"scoop_spawn:{_short(record.item)}:{_short(mob)}:{_short(biome)}",
                item=record.item,
                cost=cost,
                yields={record.item: 1.0},
                requires=tuple(requires),
                label=(
                    f"去 {_short(biome)} 等 {_short(mob)} 刷出来再舀：{travel.detail}，"
                    f"占{category or '同类'}权重 {share * 100:.0f}%"
                    f"（平均等 {tries:.1f} 只 = {tries * gamedata.NATURAL_SPAWN_SECONDS:g}s）"
                    f" + 舀 {tries * gamedata.SCOOP_SECONDS:g}s"
                ),
                source="mob_spawn:biome+code/mob_buckets",
            )
        )
        made += 1
    return made


def _mob_bucket(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
) -> Action | None:
    """用桶舀水生生物：`cod` → `cod_bucket` 这类，来自 `code/mob_buckets`。

    和「打死」不是一回事：战利品表里**没有**这个条目（`loot_expectation` 里
    `minecraft:cod` 的 `expected` 是空的），它来自 `Items.java` 的注册。
    所以不能按掉落期望算，也不能因为「缺每次掉落期望」就把整条路线判掉——
    鱼桶在此之前只能靠「跟渔夫买」，那是把「捞一条鱼」估成了「跑一趟村庄」。

    成本 = 走到最近水域 + 舀一次；前置是**空桶**（被消耗，变成鱼桶）。
    鱼类没有 `mob_density`（C17 撤回后不再采生物密度），所以「水里有没有鱼」
    按「水域到处都有」处理，只把「走到水边」计入。
    """
    mob = record.stat_subject or record.subject
    # 「走到最近地表水域舀一条」只对**真的在地表水里刷**的生物成立。
    # 美西鰭只在繁茂洞穴、硫磺立方只在硫磺洞穴，地表水里根本没有它们——
    # 这条并列候选对它们不成立（由 `_bucket_mob_weighted` 出价）。
    habitats = {biome for biome, *_rest in ctx.biome_spawn_weights(mob)}
    if habitats and not (habitats & FISH_WATER_BIOMES):
        result.uncovered[f"不在地表水域刷:{mob}"] += 1
        return None
    water = ctx.water_search_cost(record.biomes)
    if water is None:
        # 美西螈、硫磺方块生活在洞穴水体里，表面积的占比法算不出「怎么下去」。
        result.uncovered[f"缺洞穴水体定位（抓生物）:{mob}"] += 1
        return None
    travel, biome = water
    result.visit("minecraft:bucket", 0)
    return Action(
        action_id=f"scoop:{_short(record.item)}:{_short(mob)}",
        item=record.item,
        cost=travel.value + seconds(gamedata.SCOOP_SECONDS),
        yields={record.item: 1.0},
        requires=(Requirement("minecraft:bucket", 1.0, MODE_PER_USE),),
        label=(
            f"拿空桶舀一只 {_short(mob)}：{travel.detail}，"
            f"舀 {gamedata.SCOOP_SECONDS:g}s（水里鱼不稀，不计找鱼）"
        ),
        source="code/mob_buckets",
    )



def _structure_mob(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
    mob: str,
) -> Action | None:
    """结构里的生物：两种来源，成本模型不同。

    - **刷怪笼**（`mob_spawner` / `trial_spawner`）：无限刷，在那儿等着就行。
      单价 = (等一只刷新 + 打死一只) ÷ 每次击杀的掉落数。
    - **结构自带**（`entities`，末地城每处 21 只潜影贝）：一趟打完就没了。
      按「跑一趟打完全部」计价：单价 = 一趟成本 ÷ 一趟产出。
      行程进 `cost`，所以引擎拿 N 份就调用 N 次、付 N 次行程。
    """
    per_kill = _per_kill(record)
    if per_kill <= 0:
        result.uncovered[f"缺每次掉落期望({_short(mob)}):{record.item}"] += 1
        return None
    made = _structure_mob_placed(record, ctx, result, once_caps, mob, per_kill)
    made += _structure_mob_weighted(record, ctx, result, once_caps, mob, per_kill)
    made += _raid_mob(record, ctx, result, once_caps, mob, per_kill)
    if not made:
        # 前面三条都是有数据支撑的（结构内容、刷怪表权重、袭击波次）。
        # 都没有才退到「群系自然刷怪」——那条的等待周期是拍的。
        made += _biome_mob_weighted(record, ctx, result, once_caps, mob, per_kill)
    if not made:
        # 最后退到「条件刷怪」：不在群系刷怪表里、也不属于结构，
        # 要玩家满足某个状态才会出现（幻翼）。
        made += _special_mob(record, result, mob, per_kill)
    if not made:
        result.uncovered[f"缺生物密度({_short(mob)}):{record.item}"] += 1
    return None


# 条件刷怪的生物：不在任何群系刷怪表里（`mob_spawn` 没有它们的行），也不在
# 结构的实体清单里，要靠玩家自己的状态才会出现。
#
# 目前只有幻翼：玩家**连续 3 个游戏日没睡**之后，夜里开始在高空刷新。
# 所以成本 = 熬这 3 天 + 打一只，而不是「去某个群系找」。它不能走
# `_biome_mob_weighted`，因为那条要的是「群系 + 权重」。
SPECIAL_SPAWNS: dict[str, tuple[float, str]] = {
    "minecraft:phantom": (
        gamedata.PHANTOM_DAYS_WITHOUT_SLEEP * gamedata.SECONDS_PER_GAME_DAY,
        "连续 {days} 个游戏日不睡，第 {days} 天夜里在高空开始刷幻翼",
    ),
}


def _special_mob(
    record: source_index.SourceRecord,
    result: GraphActions,
    mob: str,
    per_kill: float,
) -> int:
    """靠玩家状态触发的刷怪（幻翼）。"""
    entry = SPECIAL_SPAWNS.get(mob)
    if entry is None:
        return 0
    wait, template = entry
    result.emit(
        Action(
            action_id=f"special:{_short(record.item)}:{_short(mob)}",
            item=record.item,
            cost=seconds(wait + gamedata.KILL_SECONDS),
            yields={record.item: per_kill},
            label=(
                template.format(days=gamedata.PHANTOM_DAYS_WITHOUT_SLEEP)
                + f"：等 {wait / 60:.0f} 分钟，再打一只（每次掉 {per_kill:g} 个）"
            ),
            source="mob_spawn:special",
        )
    )
    return 1




def _raid_mob(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
    mob: str,
    per_kill: float,
) -> int:
    """袭击的怪物：按**波次**累加，打到目标出现为止就收工。

    袭击不是随机刷怪，是**事件**：不祥之瓶 → 村庄触发 → 按波次成批出怪。
    波次有顺序，想要第 7 波的唤魔者就得把前 6 波也打完；但反过来，
    只想要第 5 波那 1 只唤魔者时**不必**陪跑到第 7 波。

    所以这里不为「整场」造一个动作，而是**在目标数量变化的每一波各造一个**：
    成本 = 前几波全部怪物的战斗时间 + 波间等待，产出 = 累计到该波的目标数。
    引擎按需求挑最省的截断点，`bg grade` 的整次口径再按取整约束重新选。
    """
    raids = ctx.raid_spawns(mob)
    if not raids:
        return 0
    # 不祥之瓶自己就是触发袭击的前置，从袭击里拿它会自锁。
    # 它的真实来源是袭击队长（前哨站/巡逻队），走 `kill:` 或刷怪权重那条路。
    if record.item == "minecraft:ominous_bottle":
        return 0
    travel = result.bundle.provenance.get("travel:minecraft:villages")
    travel_cost = travel.value if travel is not None else 0.0
    if ctx.item_registered("minecraft:ominous_bottle") is False:
        # Without ominous bottles, Bad Omen comes from killing an outpost captain.
        trigger = _ensure_capability(
            ctx, result, once_caps, "minecraft:pillager_outposts", route=""
        )
        if trigger is None:
            return 0
    else:
        trigger = "minecraft:ominous_bottle"
        result.visit(trigger, 0)
    requires: tuple[Requirement, ...] = (Requirement(trigger, 1.0, MODE_ONCE),)
    kill = seconds(gamedata.KILL_SECONDS)
    gap = seconds(gamedata.RAID_WAVE_SECONDS)
    made = 0
    for holder, target, per_wave_total, per_wave_target in raids:
        stop_at = {wave for wave, _ in per_wave_target}
        cum_monsters = 0.0
        cum_target = 0.0
        for wave, amount in per_wave_total:
            cum_monsters += amount
            cum_target += dict(per_wave_target).get(wave, 0.0)
            if wave not in stop_at:
                continue
            cost = travel_cost + cum_monsters * kill + (wave - 1) * gap
            yields = cum_target * per_kill
            action = Action(
                action_id=f"raid:{_short(record.item)}:{_short(mob)}:{_short(holder)}:w{wave}",
                item=record.item,
                cost=cost,
                yields={record.item: yields},
                requires=requires,
                label=(
                    f"打袭击到第 {wave} 波收工（{_short(holder)}，"
                    f"前 {wave} 波共 {cum_monsters:g} 只，累计 {_short(mob)} {cum_target:g} 只；"
                    f"行程 {travel_cost:.2f} TU + 战斗 {cum_monsters * gamedata.KILL_SECONDS:g}s"
                    f" + 波间 {(wave - 1) * gamedata.RAID_WAVE_SECONDS:g}s），"
                    f"每只掉 {per_kill:g} 个"
                ),
                source="mob_spawn:raid",
            )
            result.emit(action)
            made += 1
    return made



def _structure_mob_placed(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
    mob: str,
    per_kill: float,
) -> int:
    """结构里实测到的生物：刷怪笼（无限刷）或已生成的实体（一趟打完）。"""
    placed = ctx.structure_mob_sources(mob)
    if not placed:
        return 0
    waiting = seconds(max(gamedata.SPAWNER_WAIT_SECONDS, gamedata.KILL_SECONDS))
    trial_waiting = seconds(max(gamedata.TRIAL_SPAWNER_WAIT_SECONDS, gamedata.KILL_SECONDS))
    made = 0
    for structure, per_start, variant, kind in placed:
        cap = _ensure_capability(ctx, result, once_caps, structure, route="")
        requires: tuple[Requirement, ...] = ()
        if cap:
            requires = (Requirement(cap, 1.0, MODE_ONCE),)
        travel = result.bundle.provenance.get(f"travel:{structure}")
        travel_cost = travel.value if travel is not None else 0.0
        if kind in ("entities", "spawned"):
            # 一趟打完：成本是「跑过去 + 把这一处的都打死」，产出是全部掉落。
            # `spawned` 是采集侧从结构定义推出的固定生成量（每座神殿 3 只
            # 远古守卫者），与实测 `entities` 同样的计价口径——都不是无限刷。
            rounds = max(1.0, per_start)
            cost = travel_cost + seconds(gamedata.KILL_SECONDS) * rounds
            yields = rounds * per_kill
            label = (
                f"跑一趟 {_short(structure)}，把里面 {rounds:.1f} 只 {_short(mob)} 都打死"
                f"（每只 {per_kill:g} 个；行程 {travel_cost:.2f} TU + 打 {rounds * gamedata.KILL_SECONDS:g}s）"
            )
        else:
            # 刷怪笼：无限刷，单价按「等一只」的周期（击杀并行在等待窗口里）。
            # 两种笼子的周期差一个数量级，不能共用一个常量：`mob_spawner` 是
            # 200–800 tick（25s），`trial_spawner` 是 20–160 tick（每次都补一批）。
            if kind == "trial_spawner":
                cost = trial_waiting / per_kill
                cadence = f"每 {gamedata.TRIAL_SPAWNER_WAIT_SECONDS:g}s 补一批"
            else:
                cost = waiting / per_kill
                cadence = f"每 {gamedata.SPAWNER_WAIT_SECONDS:g}s 刷一只"
            yields = 1.0
            label = (
                f"在 {_short(variant)} 的刷怪笼边刷（{kind}，每处 {per_start:.2f} 个）"
                f"：{cadence}（击杀并行在里面），"
                f"每次掉 {per_kill:g} 个"
            )
        action = Action(
            action_id=f"{'haunt' if kind in ('entities', 'spawned') else 'spawner'}:{_short(record.item)}:{_short(mob)}:{_short(structure)}",
            item=record.item,
            cost=cost,
            yields={record.item: yields},
            requires=requires,
            label=label,
            source=(
                "structure_content:entities_per_start"
                if kind == "entities"
                else (
                    "structure_content:spawned_per_start"
                    if kind == "spawned"
                    else "structure_content:spawners_per_start"
                )
            ),
        )
        result.emit(action)
        made += 1
    return made



def _structure_mob_weighted(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
    mob: str,
    per_kill: float,
) -> int:
    """结构刷怪表的权重：结构内部无限刷，但要先刷到目标生物。

    RA 的刷怪表只给相对权重，不给刷怪速率。权重占比 `w / W`（同 `category` 内）
    就是「刷出来的是它」的机率，所以要拿一只平均得打死 `W / w` 只。
    每只按「等一只 + 打一只」计。

    **这里的等待不是刷怪笼**：结构刷怪表覆盖的是区域自然刷怪（下界要塞的
    凋灵骷髅、海底神殿的远古守卫者、堡垒的岩浆怪），没有 200–800 tick 的
    确定性延迟，受 mobcap 与光照影响，所以周期用 `NATURAL_SPAWN_SECONDS`，
    与 `_biome_mob_weighted` 同一口径。

    这是 `monument` → 守卫者、`fortress` → 凋灵骷髅这类来源唯一的入口：
    它们的容器内容全空（H3），只能靠刷怪权重算。
    """
    made = 0
    # 等一只的周期与击杀并行，取大者；与群系刷怪退路同一口径。
    cycle = seconds(max(gamedata.NATURAL_SPAWN_SECONDS, gamedata.KILL_SECONDS))
    for set_id, weight, total, category in ctx.structure_spawn_weights(mob):
        cap = _ensure_capability(ctx, result, once_caps, set_id, route="")
        requires: tuple[Requirement, ...] = ()
        if cap:
            requires = (Requirement(cap, 1.0, MODE_ONCE),)
        travel = result.bundle.provenance.get(f"travel:{set_id}")
        travel_cost = travel.value if travel is not None else 0.0
        share = weight / total
        kills = max(1.0, round(1.0 / share, 2))
        cost = travel_cost + kills * cycle
        # 同 `_biome_mob_weighted`：打死 `kills` 只里只有一只是目标生物。
        yields = per_kill
        action = Action(
            action_id=f"haunt:{_short(record.item)}:{_short(mob)}:{_short(set_id)}:pool",
            item=record.item,
            cost=cost,
            yields={record.item: yields},
            requires=requires,
            label=(
                f"在 {_short(set_id)} 里等刷怪（{_short(mob)} 占 {category or '同类'}"
                f" 权重的 {share * 100:.0f}%，平均打死 {kills:.2f} 只才出 1 只；"
                f"行程 {travel_cost:.2f} TU + {kills * gamedata.NATURAL_SPAWN_SECONDS:g}s 等待"
                f" + {kills * gamedata.KILL_SECONDS:g}s 击杀），"
                f"这只掉 {per_kill:g} 个"
            ),
            source="mob_spawn:structure",
        )
        result.emit(action)
        made += 1
    return made

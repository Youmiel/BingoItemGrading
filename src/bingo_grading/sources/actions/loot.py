"""战利品与节点型来源：开箱、钓鱼、以物易物、赠礼。"""

from __future__ import annotations

import math
from dataclasses import replace

from bingo_grading import gamedata
from bingo_grading.case.context import CaseContext
from bingo_grading.case.sources import loot_site, seconds, structure_block
from bingo_grading.plan import Action, Requirement
from bingo_grading.plan.actions import MODE_ONCE, MODE_PER_USE
from bingo_grading.sources import index as source_index
from bingo_grading.sources import template_items

from .capabilities import _ensure_capability, _ensure_travel
from .common import (
    BINDING_PREFIX,
    NodeTable,
    _gadget_for,
    _is_non_structure,
    _node_table,
    _short,
    _structure_maps,
    cost_cv,
)
from .graph import GraphActions




def _loot(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    pack: str,
    once_caps: set[str],
) -> Action | None:
    """箱子来源：跑一趟结构，开 `containers_per_start` 个箱。"""
    structure = record.structure_set
    if not structure:
        node = _node_table(record.subject)
        if node is not None:
            return _node_source(record, ctx, result, node)
        if _gadget_for(record.subject) is not None:
            # 代码里直接塞进结构的物品（末地船的鞘翅挂在展示框里）。`GADGET_SOURCES`
            # 有登记，但还没有生成动作的路径——记账，不静默跳过。
            result.uncovered[f"结构装置未接入:{_short(record.item)}"] += 1
            return None
        if _is_non_structure(record.subject):
            result.uncovered[f"非结构来源（未建模）:{record.subject}"] += 1
        elif record.subject.startswith(BINDING_PREFIX):
            # `binding:template-item:<容器>:<物品>`：上游只说「某个模板里放了这个容器、
            # 容器里有这件东西」，没有战利品表名——因为陶片根本不在表里，
            # 是模板 NBT 写进装饰罐 `sherds` 槽位的。走模板索引那一路。
            return _binding_items(record, ctx, result, pack, once_caps)
        else:
            result.uncovered[f"表未映射到结构集合:{record.subject}"] += 1
        return None
    if structure in result.batched_structures:
        # 这一结构已经有「跑一趟」动作，它把该处所有表合并了。
        return None
    # 每处**这张表**的箱数（堡垒遗迹 7.2 个容器里只有 0.1 个是宝藏箱）。
    # 拿容器总数乘一张表的每箱期望，会把「跑一趟能拿到多少」放大几十倍。
    chests = ctx.loot_tables_per_site(
        structure, record.subject, dimension=ctx.structure_dimension(structure)
    )
    if chests is None:
        # 模板行的 `loot_tables` 可能为空，退回容器总数（偏乐观，已在标签里标注）。
        chests = ctx.containers_per_site(
            structure, dimension=ctx.structure_dimension(structure)
        )
    if chests is None:
        chests = ctx.code_placed_tables_per_site(structure, record.subject, pack=pack)
    if chests is None:
        result.uncovered[f"缺每处箱数:{structure}"] += 1
        return None
    cap = _ensure_travel(record, ctx, result, once_caps, structure)
    if cap is None:
        return None
    # **每趟**付一次行程，不是 once：一趟只能开掉这张表的
    # `loot_tables[table]` 个箱（宝藏箱每处只有 0.1 个），拿更多就要再跑一趟。
    # 写成 once 会让「一趟的产出」被当成无限供给，稀有表单价被低估一两个量级。
    requires = (Requirement(cap, 1.0, MODE_PER_USE),)
    containers = ctx.containers_per_site(
        structure, dimension=ctx.structure_dimension(structure)
    )
    action = loot_site(
        result.bundle,
        ctx,
        subject=record.subject,
        structure=structure,
        chests_per_site=chests.value,
        open_containers=containers.value if containers else None,
        primary=record.item,
        action_id=f"loot:{_short(record.subject)}",
        with_travel=False,
        requires=requires,
        # 下界/末地的箱子表带的是那个维度的行，不传维度会一条也查不到。
        dimension=ctx.structure_dimension(structure),
    )
    if action is None:
        result.uncovered[f"缺每箱期望:{record.subject}"] += 1
        return None
    # 方差按**每箱**期望算，不按每趟：一趟开 `chests` 个箱时，趟级 cv 是
    # `单箱 cv / √chests`，再乘 √(每趟件数) 化简回来正好是 `单箱 cv · √p`，
    # 与趟数无关。所以这里只要每箱的 p。
    per_chest = ctx.loot_per_chest(
        record.subject,
        action.item,
        dimension=ctx.structure_dimension(structure),
        condition="tools=present",
    )
    if per_chest is None:
        return action
    return replace(
        action, yield_cv=cost_cv(ctx, record.subject, action.item, per_chest.value)
    )



def _binding_items(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    pack: str,
    once_caps: set[str],
) -> Action | None:
    """模板 NBT 里直接放的物品：装饰罐里的陶片。

    数据分两处：采集侧的「每个结构每处用几次某个模板」（`templates[].perStart`）
    与解析侧的「这个模板的某个 block entity 里放了什么」（`item_refs`）。
    两者相乘就是跑一趟能拿到几个。成本模型与「结构里放的方块」一致：
    第一趟由 `once` 前置付，超出部分按趟补（`travel_per_trip`）。

    陶片不在任何战利品表里（它是 NBT），所以不能用 `loot_expectation` 查——
    这也是它以前整条缺席的原因。
    """
    rest = record.subject[len(BINDING_PREFIX):]
    suffix = f":{record.item}"
    if not rest.endswith(suffix) or len(rest) <= len(suffix):
        result.uncovered[f"绑定主体无法解析:{record.subject}"] += 1
        return None
    container = rest[: -len(suffix)]
    sites = template_items.load(pack).sites(container, record.item)
    if not sites:
        result.uncovered[f"模板里找不到该物品:{record.subject}"] += 1
        return None
    made = 0
    for site in sites:
        # 模板绑定给的是**结构变体**（`minecraft:ancient_city`），而密度行按
        # **结构集**记（`minecraft:ancient_cities`）。不反查就会查不到间距，
        # `_ensure_capability` 返回 `None`，行程静默归零。
        site_set = _structure_maps().set_of(site.structure) or site.structure
        cap = _ensure_capability(ctx, result, once_caps, site_set, route="")
        travel = result.bundle.provenance.get(f"travel:{site_set}")
        travel_cost = travel.value if travel is not None else 0.0
        requires: tuple[Requirement, ...] = ()
        if cap:
            requires = (Requirement(cap, 1.0, MODE_ONCE),)
        action = structure_block(
            result.bundle,
            ctx,
            block=container,
            item=record.item,
            structure=site.structure,
            batch=1.0,
            action_id=f"bind:{_short(record.item)}:{_short(site.structure)}",
            find_seconds=gamedata.STRUCTURE_FIND_SECONDS,
            requires=requires,
            per_start=site.per_start,
            capacity=float("inf"),
            travel_per_trip=travel_cost,
        )
        if action is None:
            continue
        result.bundle.note(
            f"{record.item} 模板绑定：{site.structure} 每处 {site.per_start:.2f} 个"
            f"（模板 NBT 的 item_refs × 模板 perStart）"
        )
        result.emit(
            replace(
                action,
                label=(
                    f"跑一趟 {_short(site.structure)}，挖装好物品的 "
                    f"{_short(container)}：每处 {site.per_start:.2f} 个"
                    f"（行程 {travel_cost:.1f} TU + 挖一个容器）"
                ),
            )
        )
        made += 1
    if not made:
        result.uncovered[f"模板绑定未造出动作:{record.subject}"] += 1
    return None


def _node_source(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    node: NodeTable,
) -> Action | None:
    """节点型来源：满足 `node.once` 之后反复触发，每次给一份整表期望。

    单价 = 单次耗时分摊到「凑够 1 个目标物品需要的次数」。
    `yields` 写 1 份而不是整表，引擎需要多份时会自己按单价累乘。
    """
    got = ctx.loot_per_chest_all(record.subject)
    if got is None:
        result.uncovered[f"缺每箱期望:{record.subject}"] += 1
        return None
    table, _detail = got
    per_item = float(table.get(record.item, 0.0))
    if per_item <= 0:
        result.uncovered[f"该表不含目标物品:{record.subject}"] += 1
        return None
    requires = [Requirement(name, 1.0, MODE_ONCE) for name in node.once]
    requires.extend(
        Requirement(name, qty, MODE_PER_USE) for name, qty in node.per_use
    )
    # `capability:` 前置要显式展开：它只由构造表产出、不在来源索引里，
    # `build()` 末尾按 `missing_prereqs` 补齐时又会把 `capability:` 排除，
    # 于是 `capability:adult_sniffer` 这类状态永远不会有产出动作，
    # 整条来源在分层时被判 `no_rule` 丢掉。
    #
    # **只展开 capability**：普通物品（如 `turtle_egg`）由补齐循环处理，
    # 在这里 visit 会把「同表能产出该物品」的情况也拉进来（钓鱼能钓到钓鱼竿），
    # 凭空给节点表加一条无前置的动作。
    for name in node.once:
        if name.startswith("capability:") and result.visit is not None:
            result.visit(name, 1)
    # 「钓上来的垃圾里有钓鱼竿」这类：表本身能产出 node 的前置物品，
    # 于是动作既要求又生产同一个物品，`Action` 会判为自相矛盾。
    # 语义上这是真实的循环（手里有钓鱼竿才钓得到第二把），但前置本来就
    # 由外层供给，这一条自产自用的需求直接丢掉即可。
    requires = [item for item in requires if item.item != record.item]
    return Action(
        action_id=f"{node.action_prefix}:{_short(record.item)}:{_short(record.subject)}",
        item=record.item,
        cost=seconds(node.seconds) / per_item,
        yields={record.item: 1.0},
        requires=tuple(requires),
        label=(
            f"{node.note}（每次给 {per_item:.3f} 个 {_short(record.item)}，"
            f"凑 1 个要 {1.0 / per_item:.1f} 次）"
        ),
        source=f"loot_expectation:{record.subject}",
        # `cost` 已经把「平均 1/per_item 次触发」折成一次调用、`yields` 写 1.0，
        # 所以要的是**凑够 1 件的成本 cv**（≈√(1-p)），不是产出件数的 cv
        # （≈√(1-p)/√p）。两者差 1/√p 倍：陶片 p=0.067 时会多算 3.7 倍。
        yield_cv=ctx.loot_cv(record.subject, record.item) * math.sqrt(per_item),
    )



def _suspicious(
    record: source_index.SourceRecord,
    ctx: CaseContext,
    result: GraphActions,
    pack: str,
    once_caps: set[str],
) -> Action | None:
    """考古点：跑一趟结构，刷扫其中的可疑方块。"""
    structure = record.structure_set
    if not structure:
        result.uncovered[f"考古点未映射到结构集合:{record.subject}"] += 1
        return None
    got = ctx.loot_per_chest_all(
        record.subject, dimension=ctx.structure_dimension(structure)
    )
    if got is None:
        result.uncovered[f"缺每点期望:{record.subject}"] += 1
        return None
    table, _detail = got
    per_item = float(table.get(record.item, 0.0))
    if per_item <= 0:
        result.uncovered[f"该表不含目标物品:{record.subject}"] += 1
        return None
    cap = _ensure_travel(record, ctx, result, once_caps, structure)
    cost = seconds(gamedata.SUSPICIOUS_SECONDS) / per_item
    requires: tuple[Requirement, ...] = ()
    if cap:
        requires = (Requirement(cap, 1.0, MODE_ONCE),)
    return Action(
        action_id=f"brush:{_short(record.item)}:{_short(record.subject)}",
        item=record.item,
        cost=cost,
        yields={record.item: 1.0},
        requires=requires,
        label=f"在 {_short(structure)} 刷可疑方块（每 {per_item:.2f} 个出一条）",
        source=f"loot_expectation:{record.subject}",
        # 同 `_node_source`：`cost` 已是「凑 1 条」的全价，cv 要的是成本口径。
        yield_cv=ctx.loot_cv(
            record.subject, record.item, dimension=ctx.structure_dimension(structure)
        )
        * math.sqrt(per_item),
    )

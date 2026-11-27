"""结构：按「跑一趟」批量覆盖，避免逐表重复计价。"""

from __future__ import annotations

import math

from bingo_grading import gamedata
from bingo_grading.case.context import CaseContext
from bingo_grading.case.sources import seconds
from bingo_grading.plan import Action, Requirement
from bingo_grading.plan.actions import MODE_ONCE, MODE_PER_USE

from .capabilities import _ensure_access, _ensure_capability, access_capability
from .common import GADGET_SOURCES, _short, _structure_maps
from .graph import GraphActions




# ---------------------------------------------------------------- 跑一趟结构


def _batch_targets(ctx: CaseContext) -> set[str]:
    """哪些结构集合会被「跑一趟」覆盖。

    必须**先**算出来并登记到 `result.batched_structures`，再开始 emit：
    `_structure_batches` 里挂跨维度开门时会间接触发 `visit`，那时
    `_loot` 要靠这个集合判断「该结构已有批量动作，不再走逐表路线」。
    登记得晚了，逐表路线会先生成一遍，两套动作并存、重复计价。
    """
    mapping = _structure_maps()
    found: set[str] = set()
    for row in ctx.store.rows.values():
        if row.stat_type != "loot_expectation":
            continue
        structure = mapping.set_for_table(row.subject)
        if structure and (row.value.get("per_trigger_expected") or {}):
            found.add(structure)
    return found



def _structure_batches(
    ctx: CaseContext,
    result: GraphActions,
    once_caps: set[str],
) -> set[str]:
    """每个结构集合一条动作：跑一趟，开掉该处的全部箱子。

    与「每张表一个动作」相比，这是玩家真实做的事：进一趟村庄/堡垒，
    看到箱子就开，不会挑着只开 `village_toolsmith` 而绕开 `village_temple`。
    收益合并之后，同一趟顺带拿到的物品**共享**那一趟的行程与开箱时间。

    `cost` 含行程，所以引擎拿 N 件时会调用 N 次、付 N 次行程——
    去下一处结构自然要累加。**跨维度的开门仍然是 `once`**：门开一次就一直在。

    返回被覆盖的结构集合，`_loot` 据此跳过逐表路线，避免重复计价。
    """
    mapping = _structure_maps()
    groups: dict[str, dict[str, dict]] = {}
    for row in ctx.store.rows.values():
        if row.stat_type != "loot_expectation":
            continue
        structure = mapping.set_for_table(row.subject)
        if not structure:
            continue
        expected = row.value.get("per_trigger_expected") or {}
        if not expected:
            continue
        groups.setdefault(structure, {})[row.subject] = {
            "expected": {
                item: float(amount) for item, amount in expected.items() if amount > 0
            },
            # 每箱产出的相对标准差，用来把一趟的产出方差拼起来。
            "cv": {
                item: float(value)
                for item, value in (row.value.get("per_trigger_cv") or {}).items()
            },
        }

    # 先登记（即使下面因为缺箱数而造不出动作也先占位）：占位比“重复计价”安全，
    # 真的造不出时缺口会进 `uncovered`。
    result.batched_structures |= set(groups)

    made: set[str] = set()
    for structure, tables in sorted(groups.items()):
        dimension = ctx.structure_dimension(structure)
        chests = ctx.containers_per_site(structure, dimension=dimension)
        if chests is None:
            result.uncovered[f"缺每处箱数:{structure}"] += 1
            continue
        cap = _ensure_capability(ctx, result, once_caps, structure, route="")
        if cap is None:
            continue
        access = access_capability(dimension, structure)
        access_cap: str | None = None
        if access is not None:
            access_cap = _ensure_access(ctx, result, once_caps, dimension, access)
        requires: list[Requirement] = []
        if access_cap:
            # 开门是一次性的：门建好就一直能用。
            requires.append(Requirement(access_cap, 1.0, MODE_ONCE))
        # 靠地图定位的结构：行程按「知道位置」算，而这件事只有地图能提供。
        # 一张图只标一处，去下一处要新的，所以是 `per_use`。
        # 图本身有来源（沉船、海底废墟、废弃营地），走 `visit` 递归定价。
        map_item = _structure_maps().map_for(structure)
        if map_item is not None and ctx.item_registered(map_item) is False:
            result.bundle.note(
                f"{_short(map_item)} does not exist in this version; "
                f"{_short(structure)} location treated as known (optimistic)"
            )
            map_item = None
        if map_item is not None:
            if result.visit is not None:
                result.visit(map_item, 0)
            requires.append(Requirement(map_item, 1.0, MODE_PER_USE))

        yields: dict[str, float] = {}
        # 一趟里各表各自的箱子是**独立抽样**，所以方差与均值一样线性相加：
        # `Var_趟 = Σ count·cvᵢ²·μᵢ²`。不能用混合分布那套 `E[X²]-E[X]²`
        # ——那个是「随机挑一张表开一箱」，箱子一多就会把方差项减成负数。
        spread: dict[str, float] = {}
        for table, data in tables.items():
            per_site = ctx.loot_tables_per_site(structure, table, dimension=dimension)
            # 分表箱数拿不到时退回容器总数（偏乐观，明细在 label 里标出）。
            count = per_site.value if per_site is not None else chests.value
            for item, amount in data["expected"].items():
                yields[item] = yields.get(item, 0.0) + amount * count
                cv = data["cv"].get(item, 0.0)
                if cv:
                    spread[item] = spread.get(item, 0.0) + count * (cv * amount) ** 2
        # 代码塞进结构的物品（末地船的鞘翅）：按「每处几个载体」并进这一趟。
        gadgets: list[str] = []
        for _marker, (gset, carrier, per_carrier, gitem) in GADGET_SOURCES.items():
            if gset != structure:
                continue
            got = ctx.structure_entity_per_site(gset, carrier, dimension=dimension)
            if got is None or got.value <= 0:
                continue
            yields[gitem] = yields.get(gitem, 0.0) + got.value * per_carrier
            gadgets.append(f"{gitem.split(':')[-1]} {got.value * per_carrier:.2f}/处")
        if not yields:
            continue

        travel = result.bundle.provenance.get(f"travel:{structure}")
        travel_cost = travel.value if travel is not None else 0.0
        opened = chests.value * gamedata.CHEST_SECONDS
        primary = max(yields, key=lambda name: yields[name])
        # 一趟的产出方差 → 成本 cv：`cv_趟 · √(每趟件数)`（同 `common.cost_cv`）。
        # 批量主产物也能用：消费者会再除一次单价，两边的 μ 相互抵消。
        mean = yields[primary]
        span = max(0.0, spread.get(primary, 0.0))
        site_cv = (
            math.sqrt(span) / mean * math.sqrt(mean) if mean > 0 and span > 0 else 0.0
        )
        action = Action(
            action_id=f"loot:{_short(structure)}",
            item=primary,
            cost=travel_cost + seconds(opened),
            yields=yields,
            requires=tuple(requires),
            label=(
                f"跑一趟 {_short(structure)}：开 {chests.value:.2f} 个箱"
                f"（行程 {travel_cost:.2f} TU + 开箱 {opened:.1f}s），"
                f"拿到 {len(tables)} 类表的全部产出"
                + (f"，另有 {'、'.join(gadgets)}" if gadgets else "")
            ),
            source=f"loot_expectation+structure_content:{structure}",
            # 只描述**主产物**的波动：`Segment.cv` 是按主产物定的，
            # 顺带产出的其它物品各走各的来源，不共这一份方差。
            yield_cv=site_cv,
        )
        result.emit(action)
        made.add(structure)
    return made

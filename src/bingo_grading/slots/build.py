"""来源路径索引 + 合成表 → 槽位候选。

槽位粒度是「某物品在某路线 × 某获取方式下存在来源」，不是一个完整路线。
一个槽位下可以挂多条来源，它们在该粒度上等价，进入 DAG 后各自展开。

物品的槽位有两个来源：它自己的非合成来源，以及沿配方链展开得到的叶子来源。
后者让「铁锭 × 地下 × 挖掘」这样的格子出现——铁锭本身没有挖掘来源，
它的配方链末端是铁矿石。

`farm` 不由来源条目给出：它需要「作物方块可达 + 种子来源可达」两条同时成立，
由展开器推导，第一版不生成，覆盖率报告里单列为待覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..recipes.expand import (
    MAX_DEPTH,
    CraftIndex,
    ExpandStats,
    Leaf,
    direct_sources,
    expand,
)
from ..sources.constants import ROUTES
from ..sources.index import SourceRecord

# `source_type` → 获取方式。
OBTAIN_METHOD = {
    "block_drop": "mine",
    "mob_drop": "kill",
    "trade": "trade",
    "loot_table": "loot",
    "suspicious_block": "suspicious",
    "shear": "shear",
    "convert": "convert",
    "fill": "fill",
}

METHODS = (*OBTAIN_METHOD.values(), "silk", "farm")

# 精准采集不是独立来源，是挖掘方式的限定。
SILK_TOUCH = "enchant:minecraft:silk_touch"

SLOT_SCHEMA = 1

# 槽位上记录的叶子物品数量上限。槽位只回答「有哪些格子」，不承担清单职责。
LEAF_ITEM_LIMIT = 12


@dataclass(frozen=True)
class Slot:
    item: str
    route: str
    obtain_method: str
    sources: tuple[str, ...]
    subjects: tuple[str, ...]
    probabilistic: int
    min_craft_depth: int = 0
    leaf_items: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.item, self.route, self.obtain_method)

    def as_dict(self) -> dict:
        return {
            "slot_schema": SLOT_SCHEMA,
            "item": self.item,
            "route": self.route,
            "obtain_method": self.obtain_method,
            "sources": list(self.sources),
            "subjects": list(self.subjects),
            "probabilistic": self.probabilistic,
            "min_craft_depth": self.min_craft_depth,
            "leaf_items": list(self.leaf_items),
        }


@dataclass
class SlotBuild:
    slots: list[Slot] = field(default_factory=list)
    stats: ExpandStats = field(default_factory=ExpandStats)
    unexpanded: list[str] = field(default_factory=list)

    def merge_stats(self, other: ExpandStats) -> None:
        self.stats.cycles |= other.cycles
        self.stats.truncated += other.truncated
        self.stats.missing |= other.missing
        self.stats.tag_limited |= other.tag_limited


def method_of(record: SourceRecord) -> str:
    if SILK_TOUCH in record.prereq:
        return "silk"
    try:
        return OBTAIN_METHOD[record.source_type]
    except KeyError:
        raise ValueError(
            f"来源类别 {record.source_type!r} 没有对应的获取方式（{record.rule_ref}）"
        ) from None


def build(
    records: list[SourceRecord],
    *,
    craft: CraftIndex | None = None,
    max_depth: int = MAX_DEPTH,
) -> SlotBuild:
    """按 `(item, route, obtain_method)` 聚合来源。"""
    direct = direct_sources(records)
    roots: set[str] = set(direct)
    if craft is not None:
        roots |= set(craft.recipes)

    result = SlotBuild()
    for root in sorted(roots):
        if craft is None:
            leaves: list[Leaf] = [
                Leaf(item=root, source=source, depth=0, chain=())
                for source in direct.get(root, ())
            ]
            stats = ExpandStats()
        else:
            leaves, stats = expand(craft, direct, root, max_depth=max_depth)
            result.merge_stats(stats)

        if not leaves:
            result.unexpanded.append(root)
            continue

        buckets: dict[tuple[str, str, str], dict] = {}
        for leaf in leaves:
            method = method_of(leaf.source)
            key = (root, leaf.source.route, method)
            bucket = buckets.setdefault(
                key,
                {
                    "sources": set(),
                    "subjects": set(),
                    "leaf_items": set(),
                    "probabilistic": 0,
                    "min_craft_depth": leaf.depth,
                },
            )
            bucket["sources"].add(leaf.source.rule_ref)
            bucket["subjects"].add(leaf.source.subject)
            bucket["leaf_items"].add(leaf.item)
            if leaf.source.probabilistic:
                bucket["probabilistic"] += 1
            bucket["min_craft_depth"] = min(bucket["min_craft_depth"], leaf.depth)

        for (item, route, method), data in sorted(buckets.items()):
            result.slots.append(
                Slot(
                    item=item,
                    route=route,
                    obtain_method=method,
                    sources=tuple(sorted(data["sources"])),
                    subjects=tuple(sorted(data["subjects"])),
                    probabilistic=data["probabilistic"],
                    min_craft_depth=data["min_craft_depth"],
                    leaf_items=tuple(sorted(data["leaf_items"])[:LEAF_ITEM_LIMIT]),
                )
            )
    return result


def write(slots: list[Slot], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = json.dumps({"slot_schema": SLOT_SCHEMA, "count": len(slots)}, sort_keys=True)
    lines = [header, *(json.dumps(slot.as_dict(), ensure_ascii=False, sort_keys=True) for slot in slots)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

"""来源类别、前置闭包、路线判定。"""

from __future__ import annotations

from bingo_grading.sources import index as source_index
from bingo_grading.sources import routes
from bingo_grading.sources.classify import classify, subject_of
from bingo_grading.sources.closure import expand
from bingo_grading.sources.knowledge import RouteRules

from .support import redstone_graph, rule


def test_classify_loot_by_table_prefix() -> None:
    graph = redstone_graph()
    types = {
        rule.family + ":" + str(rule.meta.get("table", "")): classify(rule)
        for rule in graph.rules
        if rule.head == "item:minecraft:redstone"
    }
    assert types["loot:minecraft:blocks/redstone_ore"] == "block_drop"
    assert types["loot:minecraft:chests/simple_dungeon"] == "loot_table"
    assert types["trades:"] == "trade"
    assert types["recipes:"] is None


def test_classify_unmapped_family_returns_none() -> None:
    assert classify(rule("x", "item:minecraft:x", family="code/unknown")) is None


def test_subject_prefers_block_node() -> None:
    loot = rule(
        "loot:x",
        "item:minecraft:redstone",
        ("table:minecraft:blocks/redstone_ore", "block:minecraft:redstone_ore"),
        family="loot",
        meta={"table": "minecraft:blocks/redstone_ore"},
    )
    assert subject_of(loot, "block_drop") == "minecraft:redstone_ore"


def test_subject_from_table_when_no_block_node() -> None:
    loot = rule(
        "loot:x",
        "item:minecraft:redstone",
        ("table:minecraft:blocks/deepslate_redstone_ore",),
        family="loot",
        meta={"table": "minecraft:blocks/deepslate_redstone_ore"},
    )
    assert subject_of(loot, "block_drop") == "minecraft:deepslate_redstone_ore"


def test_subject_for_trade_uses_profession() -> None:
    trade = rule(
        "trade:x",
        "item:minecraft:redstone",
        ("assume:villager_trade",),
        family="trades",
        meta={"profession": "cleric", "level": 2},
    )
    assert subject_of(trade, "trade") == "cleric/2"


def test_subject_for_shared_trade_group_falls_back_to_trade_id() -> None:
    """一份报价文件被多个职业共用时 `profession` 是 null。

    上游用 `applies_to` 列出适用职业、`trade` 给出组名与等级；不能退回
    `rule.id`（那是 `data/.../smith/1/coal_emerald.json` 路径，下游按
    `trade_offer` 的 subject 精确匹配永远查不到，动作会整条丢失）。
    """
    trade = rule(
        "trade:data/minecraft/villager_trade/smith/1/coal_emerald.json",
        "item:minecraft:emerald",
        ("assume:villager_trade",),
        family="trades",
        meta={
            "applies_to": [
                {"level": 1, "profession": "armorer"},
                {"level": 1, "profession": "toolsmith"},
            ],
            "level": 1,
            "profession": None,
            "shared_group": "common_smith",
            "trade": "minecraft:smith/1/coal_emerald",
        },
    )
    assert subject_of(trade, "trade") == "smith/1"


def test_closure_expands_intermediate_nodes() -> None:
    graph = redstone_graph()
    loot = next(rule for rule in graph.rules if rule.id == "loot:redstone_ore")
    closure = expand(graph, loot.body)
    assert "tool:minecraft:iron_pickaxe" in closure.prereq
    assert "world:minecraft:overworld" in closure.prereq
    assert not closure.truncated


def test_closure_keeps_items_as_notes() -> None:
    graph = redstone_graph()
    trade = next(rule for rule in graph.rules if rule.id == "trade:cleric_redstone")
    closure = expand(graph, trade.body)
    assert "assume:villager_trade" in closure.prereq
    assert "item:minecraft:emerald" in closure.notes


def test_closure_ignores_unreachable_cycles_but_reports() -> None:
    rules = [
        rule("a", "block:minecraft:a", ("table:minecraft:t",), family="bridge/table", kind="holds"),
        rule("b", "table:minecraft:t", ("block:minecraft:a",), family="bridge/table", kind="holds"),
    ]
    from .support import graph_of

    graph = graph_of(rules)
    closure = expand(graph, ("block:minecraft:a",))
    assert closure.cycles
    assert not closure.truncated


def test_routes_underground_and_other_dimension() -> None:
    rules = RouteRules(
        underground_blocks=frozenset({"minecraft:redstone_ore"}),
        underground_structure_prefixes=("minecraft:chests/simple_dungeon",),
        dimension_keywords=(("the_nether", ("minecraft:chests/bastion",)),),
    )
    assert routes.decide(source_type="block_drop", subject="minecraft:redstone_ore", prereq=(), rules=rules) == "underground"
    assert routes.decide(source_type="loot_table", subject="minecraft:chests/simple_dungeon", prereq=(), rules=rules) == "underground"
    assert routes.decide(source_type="loot_table", subject="minecraft:chests/bastion_other", prereq=(), rules=rules) == "other_dimension"
    assert routes.decide(source_type="block_drop", subject="minecraft:oak_leaves", prereq=(), rules=rules) == "surface"


def test_craft_only_items_are_counted_separately_from_filtered_ones() -> None:
    """图里有节点但没产出来源行：纯配方属正常，其它家族才是缺口。"""
    from .support import graph_of
    from .test_recipes import probabilities, routes as route_rules

    graph = graph_of(
        [
            rule("craft:crafted", "item:minecraft:crafted", family="recipes"),
            rule("mystery", "item:minecraft:mystery", family="code/unknown"),
        ]
    )
    result = source_index.build(graph, probabilities(), route_rules())
    assert result.graph_items == 2
    assert result.no_source_items == ["minecraft:crafted", "minecraft:mystery"]
    assert result.craft_only_items == ["minecraft:crafted"]
    assert result.filtered_items == ["minecraft:mystery"]


def test_index_header_exposes_coverage_gap(tmp_path) -> None:
    from .support import graph_of
    from .test_recipes import probabilities, routes as route_rules

    graph = graph_of(
        [
            rule("craft:crafted", "item:minecraft:crafted", family="recipes"),
            rule("mystery", "item:minecraft:mystery", family="code/unknown"),
        ]
    )
    path = tmp_path / "source_index.jsonl"
    source_index.build(graph, probabilities(), route_rules()).write(path)
    header, _ = source_index.read(path)
    assert header["graph_items"] == 2
    assert header["craft_only_items"] == 1
    assert header["filtered_items"] == 1
    assert header["filtered_samples"] == ["minecraft:mystery"]

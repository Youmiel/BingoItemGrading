"""槽位聚合与覆盖率报告。"""

from __future__ import annotations

import pytest

from bingo_grading.recipes.expand import build_index
from bingo_grading.slots.build import METHODS, OBTAIN_METHOD, build as build_slots
from bingo_grading.slots.report import item_slots, report
from bingo_grading.sources.constants import SOURCE_TYPES
from bingo_grading.sources.index import build as build_sources

from .support import redstone_graph
from .test_recipes import probabilities, routes


def test_every_source_type_has_a_reported_obtain_method() -> None:
    assert set(OBTAIN_METHOD) == set(SOURCE_TYPES)
    assert set(OBTAIN_METHOD.values()) <= set(METHODS)


@pytest.fixture()
def built():
    graph = redstone_graph()
    sources = build_sources(graph, probabilities(), routes())
    result = build_slots(sources.records, craft=build_index(graph))
    return sources.records, result


def test_slots_aggregate_by_route_and_method(built) -> None:
    _, result = built
    keys = {(slot.item, slot.route, slot.obtain_method) for slot in result.slots}
    assert ("minecraft:redstone", "underground", "mine") in keys
    assert ("minecraft:redstone", "underground", "loot") in keys
    assert ("minecraft:redstone", "other_dimension", "loot") in keys
    assert ("minecraft:redstone", "surface", "trade") in keys


def test_craft_chain_gives_iron_ingot_mining_slot(built) -> None:
    _, result = built
    slots = item_slots(result.slots, "minecraft:iron_ingot")
    mining = [slot for slot in slots if slot.obtain_method == "mine"]
    assert mining, "铁锭应当经由配方链拿到挖掘槽位"
    assert mining[0].route == "underground"
    assert mining[0].min_craft_depth == 1
    assert "minecraft:iron_ore" in mining[0].leaf_items


def test_slot_collects_multiple_sources(built) -> None:
    _, result = built
    slots = {(slot.route, slot.obtain_method): slot for slot in item_slots(result.slots, "minecraft:redstone")}
    assert len(slots[("underground", "loot")].sources) == 1
    assert slots[("underground", "mine")].subjects == ("minecraft:redstone_ore",)


def test_report_marks_design_empty_cells(built) -> None:
    records, result = built
    full = report(result, records)
    empty = {(cell["route"], cell["obtain_method"]): cell for cell in full["empty_cells"]}
    assert empty[("branch_mine", "mine")]["by_design"]
    assert empty[("surface", "farm")]["by_design"]
    assert not empty[("underground", "kill")]["by_design"]


def test_report_counts_cells(built) -> None:
    records, result = built
    full = report(result, records)
    cells = {(cell["route"], cell["obtain_method"]): cell["items"] for cell in full["cells"]}
    assert cells[("underground", "mine")] >= 1
    assert full["items_without_slots"] == []

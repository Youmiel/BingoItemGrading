"""A loot source without structure density is reported as a density gap."""

from __future__ import annotations

from bingo_grading.case.context import CaseContext
from bingo_grading.case.sources import ActionBundle
from bingo_grading.sources import index as source_index
from bingo_grading.sources.actions.graph import GraphActions
from bingo_grading.sources.actions.loot import _loot
from bingo_grading.stats.model import WorldStat, WorldStatStore

SET = "minecraft:buried_treasures"
TABLE = "minecraft:chests/buried_treasure"


def _row(stat_type: str, subject: str, value: dict, condition: str = "") -> WorldStat:
    return WorldStat(
        pack_version="test",
        scope_level="dimension",
        dimension="minecraft:overworld",
        biome="",
        stat_type=stat_type,
        subject=subject,
        condition=condition,
        value=value,
        sample_size=10,
        confidence="high",
        derived_from=(),
        provenance="",
        note="",
    )


def test_missing_density_is_reported_as_density_gap() -> None:
    store = WorldStatStore()
    # Chest counts and per-chest yields exist, structure spacing does not.
    store.add(
        _row(
            "structure_content",
            "minecraft:buried_treasure",
            {"containers_per_start": 1.0, "loot_tables_per_start": {TABLE: 1.0}},
            condition="source=measured",
        )
    )
    store.add(
        _row(
            "loot_expectation",
            TABLE,
            {"per_trigger_expected": {"minecraft:heart_of_the_sea": 1.0}, "table": TABLE},
            condition="tools=present",
        )
    )
    ctx = CaseContext(store=store, dimension="minecraft:overworld")
    record = source_index.SourceRecord(
        item="minecraft:heart_of_the_sea",
        source_type="loot_table",
        subject=TABLE,
        route="surface",
        prereq=(),
        biomes=(),
        notes=(),
        cycles=(),
        probabilistic=False,
        probability=None,
        is_reverse=False,
        primary_source_hint=None,
        rule_ref="test",
        family="loot",
        truncated=False,
        structure_set=SET,
    )
    result = GraphActions(label="test", bundle=ActionBundle("test"))

    assert _loot(record, ctx, result, "test", set()) is None
    assert result.uncovered[f"缺结构密度:{SET}"] == 1
    assert not any(key.startswith("缺每箱期望") for key in result.uncovered)

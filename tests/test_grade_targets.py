"""Whole-library graph targets include items that can only be crafted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bingo_grading.case.context import CaseContext
from bingo_grading.cli.grade_table import _whole_items
from bingo_grading.sources import index as source_index

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "stats" / "26.3"


def _index(tmp_path: Path, items: list[str]) -> Path:
    path = tmp_path / "source_index.jsonl"
    header = {"index_schema": source_index.INDEX_SCHEMA, "label": "test"}
    rows = [header]
    for item in items:
        rows.append(
            {
                "index_schema": source_index.INDEX_SCHEMA,
                "item": item,
                "source_type": "block_drop",
                "subject": item,
                "route": "surface",
                "hard_prereq": [],
                "biomes": [],
                "notes": [],
                "cycles": [],
                "probabilistic": False,
                "probability": None,
                "is_reverse": False,
                "primary_source_hint": None,
                "rule_ref": "test",
                "family": "loot",
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_extra_items_join_targets_with_zero_sources(tmp_path: Path) -> None:
    path = _index(tmp_path, ["minecraft:stone", "minecraft:stone"])
    _, items, counts = _whole_items(path, ("minecraft:ender_eye", "minecraft:stone"))
    assert items == ["minecraft:ender_eye", "minecraft:stone"]
    assert counts == {"minecraft:ender_eye": 0, "minecraft:stone": 2}


@pytest.mark.skipif(not SNAPSHOT.exists(), reason=f"missing snapshot {SNAPSHOT}")
def test_recipe_outputs_cover_craft_only_items() -> None:
    outputs = set(CaseContext.load(SNAPSHOT).recipe_outputs(pack="26.3"))
    assert {"minecraft:ender_eye", "minecraft:blaze_powder", "minecraft:oak_boat"} <= outputs

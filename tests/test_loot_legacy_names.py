"""Legacy loot table names and keys with the same meaning as current ones."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bingo_grading.loot.expect import expect
from bingo_grading.loot.tables import load


def _pack(root: Path, table: dict) -> Path:
    # Legacy plural directory name.
    path = root / "data" / "minecraft" / "loot_tables" / "entities" / "t.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(table), encoding="utf-8")
    return root


def _expect(tmp_path: Path, table: dict):
    return expect("minecraft:entities/t", load([_pack(tmp_path, table)]))


def test_random_chance_with_looting(tmp_path: Path) -> None:
    result = _expect(tmp_path, {"pools": [{
        "rolls": 1,
        "entries": [{"type": "minecraft:item", "name": "minecraft:wither_skeleton_skull"}],
        "conditions": [
            {"condition": "minecraft:killed_by_player"},
            {"condition": "minecraft:random_chance_with_looting", "chance": 0.025, "looting_multiplier": 0.01},
        ],
    }]})
    assert result.per_item["minecraft:wither_skeleton_skull"] == pytest.approx(0.025)
    assert not result.unsupported


def test_block_state_property_and_alternative(tmp_path: Path) -> None:
    result = _expect(tmp_path, {"pools": [{
        "rolls": 1,
        "entries": [{"type": "minecraft:alternatives", "children": [
            {"type": "minecraft:item", "name": "minecraft:wheat", "conditions": [
                {"condition": "minecraft:alternative", "terms": [
                    {"condition": "minecraft:block_state_property", "block": "minecraft:wheat",
                     "properties": {"age": "7"}},
                ]},
            ]},
            {"type": "minecraft:item", "name": "minecraft:wheat_seeds"},
        ]}],
    }]})
    assert result.per_item["minecraft:wheat"] == pytest.approx(1.0)
    assert not result.unsupported


def test_legacy_functions_do_not_change_count(tmp_path: Path) -> None:
    result = _expect(tmp_path, {"pools": [{
        "rolls": 1,
        "entries": [{"type": "minecraft:item", "name": "minecraft:bone", "functions": [
            {"function": "minecraft:set_count", "count": 2},
            {"function": "minecraft:looting_enchant", "count": {"min": 0, "max": 1}},
            {"function": "minecraft:copy_name", "source": "block_entity"},
            {"function": "minecraft:copy_nbt", "source": "block_entity", "ops": []},
        ]}],
    }]})
    assert result.per_item["minecraft:bone"] == pytest.approx(2.0)
    assert not result.unsupported

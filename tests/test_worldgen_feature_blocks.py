"""Blocks placed by biome features are not treated as compressed blocks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bingo_grading import paths
from bingo_grading.case.context import CaseContext
from bingo_grading.case.context.model import _biome_feature_blocks
from bingo_grading.stats.model import WorldStatStore


def _node(kind: str, name: str, *refs: tuple[str, str]) -> dict:
    return {
        "kind": "worldgen",
        "payload": {
            "worldgen_kind": kind,
            "id": f"data/minecraft/worldgen/{kind}/{name}",
            "references": [{"id": ref_id, "slot": slot} for slot, ref_id in refs],
        },
    }


@pytest.fixture()
def layout(tmp_path: Path) -> tuple[Path, Path]:
    rows = [
        # Biome -> placed feature -> feature -> block; the predicate block is not placed.
        _node("placed_feature", "patch_melon",
              ("feature", "minecraft:melon"), ("block", "minecraft:grass_block")),
        _node("feature", "melon", ("block", "minecraft:melon")),
        # Nested: a selector pointing at another placed feature.
        _node("placed_feature", "ice_patch", ("feature", "minecraft:ice_selector")),
        _node("feature", "ice_selector", ("feature", "minecraft:inner_ice")),
        _node("placed_feature", "inner_ice", ("feature", "minecraft:packed_ice_blob")),
        _node("feature", "packed_ice_blob", ("block", "minecraft:packed_ice")),
        # A feature no biome references.
        _node("feature", "pile_hay", ("block", "minecraft:hay_block")),
    ]
    worldgen = tmp_path / "worldgen.jsonl"
    worldgen.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    biomes = tmp_path / "biome"
    biomes.mkdir()
    (biomes / "jungle.json").write_text(
        json.dumps({"features": [[], ["minecraft:patch_melon"]]}), encoding="utf-8"
    )
    (biomes / "ice_spikes.json").write_text(
        json.dumps({"features": [["minecraft:ice_patch"]]}), encoding="utf-8"
    )
    _biome_feature_blocks.cache_clear()
    return worldgen, biomes


def test_blocks_placed_by_biome_features(layout: tuple[Path, Path]) -> None:
    worldgen, biomes = layout
    blocks = _biome_feature_blocks(str(worldgen), str(biomes))
    assert "minecraft:melon" in blocks
    assert "minecraft:packed_ice" in blocks, "nested feature was not expanded"


def test_placement_predicates_and_unreferenced_features_are_excluded(
    layout: tuple[Path, Path],
) -> None:
    worldgen, biomes = layout
    blocks = _biome_feature_blocks(str(worldgen), str(biomes))
    assert "minecraft:grass_block" not in blocks, "placement predicate block counted as placed"
    assert "minecraft:hay_block" not in blocks, "unreferenced feature counted as natural"


def test_pre_26_3_configured_feature_directory(tmp_path: Path) -> None:
    """The legacy `configured_feature` directory name is accepted."""
    rows = [
        _node("placed_feature", "patch_melon", ("feature", "minecraft:patch_melon")),
        _node("configured_feature", "patch_melon", ("block", "minecraft:melon")),
    ]
    worldgen = tmp_path / "worldgen.jsonl"
    worldgen.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    biomes = tmp_path / "biome"
    biomes.mkdir()
    (biomes / "jungle.json").write_text(
        json.dumps({"features": [["minecraft:patch_melon"]]}), encoding="utf-8"
    )
    _biome_feature_blocks.cache_clear()
    assert "minecraft:melon" in _biome_feature_blocks(str(worldgen), str(biomes))


def test_missing_worldgen_facts_means_no_evidence(tmp_path: Path) -> None:
    _biome_feature_blocks.cache_clear()
    assert _biome_feature_blocks(str(tmp_path / "absent.jsonl"), str(tmp_path)) == frozenset()


def test_context_reads_pack_paths(
    layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worldgen, biomes = layout
    datapack = tmp_path / "datapack"
    (datapack / "minecraft" / "worldgen").mkdir(parents=True)
    biomes.rename(datapack / "minecraft" / "worldgen" / "biome")
    monkeypatch.setattr(paths, "worldgen_facts", lambda pack: paths.Resolved(worldgen, paths.LOCAL))
    monkeypatch.setattr(paths, "datapack", lambda pack: paths.Resolved(datapack, paths.LOCAL))
    _biome_feature_blocks.cache_clear()

    context = CaseContext(store=WorldStatStore())
    assert context.placed_by_biome_feature("minecraft:melon", pack="test")
    assert not context.placed_by_biome_feature("minecraft:hay_block", pack="test")

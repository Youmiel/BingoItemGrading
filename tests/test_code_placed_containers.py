"""Chest counts from structure code when neither measured nor template data exists."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bingo_grading import paths
from bingo_grading.case.context import CaseContext
from bingo_grading.case.context.model import _code_container_rows
from bingo_grading.stats.model import WorldStat, WorldStatStore

BURIED = "minecraft:buried_treasures"
BURIED_TABLE = "minecraft:chests/buried_treasure"
MINESHAFTS = "minecraft:mineshafts"
MINESHAFT_TABLE = "minecraft:chests/abandoned_mineshaft"


@pytest.fixture()
def export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    rows = [
        {
            "container": "chest",
            "sites": 1,
            "sources": ["net/minecraft/.../BuriedTreasurePieces.java"],
            "structure_type": "minecraft:buried_treasure",
            "structures": ["minecraft:buried_treasure"],
            "table": BURIED_TABLE,
        },
        {
            "container": "chest",
            "sites": 2,
            "sources": ["net/minecraft/.../MineshaftPieces.java"],
            "structure_type": "minecraft:mineshaft",
            "structures": ["minecraft:mineshaft", "minecraft:mineshaft_mesa"],
            "table": MINESHAFT_TABLE,
        },
    ]
    (tmp_path / "structure_containers.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(paths, "recipe_export", lambda pack: paths.Resolved(tmp_path, paths.LOCAL))
    _code_container_rows.cache_clear()
    return tmp_path


def _measured(subject: str) -> WorldStat:
    return WorldStat(
        pack_version="test",
        scope_level="dimension",
        dimension="minecraft:overworld",
        biome="",
        stat_type="structure_content",
        subject=subject,
        condition="source=measured",
        value={"containers_per_start": 3.0},
        sample_size=10,
        confidence="high",
        derived_from=(),
        provenance="",
        note="",
    )


def _context(*rows: WorldStat) -> CaseContext:
    store = WorldStatStore()
    for row in rows:
        store.add(row)
    return CaseContext(store=store)


def test_code_placed_chest_counts_when_nothing_else_exists(export: Path) -> None:
    found = _context().code_placed_tables_per_site(BURIED, BURIED_TABLE, pack="test")
    assert found is not None
    assert found.value == 1.0
    assert found.source == "recipe_export:structure_containers"
    assert "BuriedTreasurePieces.java" in found.detail


def test_measured_structure_is_not_overridden(export: Path) -> None:
    context = _context(_measured("minecraft:mineshaft"))
    assert context.code_placed_tables_per_site(MINESHAFTS, MINESHAFT_TABLE, pack="test") is None


def test_other_tables_do_not_match(export: Path) -> None:
    context = _context()
    assert context.code_placed_tables_per_site(BURIED, MINESHAFT_TABLE, pack="test") is None
    assert context.code_placed_tables_per_site("minecraft:not_a_set", BURIED_TABLE, pack="test") is None


def test_missing_export_gives_no_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        paths, "recipe_export", lambda pack: paths.Resolved(tmp_path / "absent", paths.MISSING)
    )
    assert _context().code_placed_tables_per_site(BURIED, BURIED_TABLE, pack="test") is None
